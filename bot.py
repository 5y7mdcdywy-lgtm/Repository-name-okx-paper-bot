
import os
import json
import time
import hmac
import base64
import hashlib
import urllib.parse
import urllib.request
import urllib.error
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP

# ===== OKX 双向持仓策略 =====
BASE = "https://openapi.okx.com"
SYMBOLS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]

LEVERAGE = Decimal("10")
MARGIN_USDT = Decimal("5")
TAKE_PROFIT = Decimal("0.06")
STOP_LOSS = Decimal("0.03")

STATE_FILE = "swap_state.json"
LIVE = os.getenv("LIVE_TRADING", "false").strip().lower() == "true"
CONFIRM = os.getenv("CONFIRM_10X_LIVE", "").strip()

KEY = os.getenv("OKX_API_KEY", "").strip()
SECRET = os.getenv("OKX_API_SECRET", "").strip()
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "").strip()

HTTP_TIMEOUT = 20
WAIT_TRIES = 10
WAIT_SECONDS = 2


def dec(value):
    return Decimal(str(value))


def timestamp():
    return datetime.now(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def request(method, path, params=None, body=None, private=False):
    method = method.upper()
    query = "?" + urllib.parse.urlencode(params) if params else ""
    request_path = path + query
    body_text = (
        json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        if body is not None else ""
    )

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "OKXTradingBot/2.0"
    }

    if private:
        now = timestamp()
        message = now + method + request_path + body_text
        signature = base64.b64encode(
            hmac.new(
                SECRET.encode(),
                message.encode(),
                hashlib.sha256
            ).digest()
        ).decode()

        headers.update({
            "OK-ACCESS-KEY": KEY,
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": now,
            "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        })

    req = urllib.request.Request(
        BASE + request_path,
        data=body_text.encode() if body is not None else None,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
            payload = response.read().decode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise RuntimeError(f"OKX HTTP错误 {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"连接 OKX 失败: {exc.reason}") from None

    try:
        result = json.loads(payload)
    except json.JSONDecodeError:
        raise RuntimeError("OKX 返回数据无法解析") from None

    if result.get("code") != "0":
        raise RuntimeError(
            f"OKX API错误: {result.get('code')} {result.get('msg')}"
        )

    return result.get("data", [])


def private_get(path, params=None):
    return request("GET", path, params=params, private=True)


def private_post(path, body):
    return request("POST", path, body=body, private=True)


def public_get(path, params=None):
    return request("GET", path, params=params)


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"positions": {}, "pending": None}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception as exc:
        raise RuntimeError(f"状态文件读取失败，停止交易: {exc}") from None

    if not isinstance(state, dict):
        raise RuntimeError("状态文件格式错误")

    positions = state.get("positions", {})
    if not isinstance(positions, dict):
        raise RuntimeError("positions 状态格式错误")

    # 旧版按 instId 保存，不能直接用于双向模式
    for key, value in positions.items():
        if ":" not in key:
            raise RuntimeError(
                "发现旧版单向持仓状态记录。请先核对 OKX 所有仓位及订单，"
                "不能自动迁移或删除旧记录。"
            )

    state.setdefault("positions", {})
    state.setdefault("pending", None)
    return state


def save_state(state):
    temp = STATE_FILE + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(temp, STATE_FILE)


def position_key(inst_id, pos_side):
    return inst_id + ":" + pos_side


def check_account():
    rows = private_get("/api/v5/account/config")
    if not rows or rows[0].get("posMode") != "long_short_mode":
        raise RuntimeError(
            "OKX账户必须为双向持仓模式(long_short_mode)，"
            "当前模式不符合要求，停止交易。"
        )


def get_positions(inst_id):
    rows = private_get(
        "/api/v5/account/positions",
        {"instId": inst_id}
    )
    result = {}

    for row in rows:
        size = dec(row.get("pos", "0"))
        side = row.get("posSide", "")

        if size == 0:
            continue

        if side not in ("long", "short"):
            raise RuntimeError(
                f"{inst_id} 返回了无法识别的持仓方向，停止操作"
            )

        result[side] = row

    return result


def set_leverage(inst_id):
    for side in ("long", "short"):
        private_post("/api/v5/account/set-leverage", {
            "instId": inst_id,
            "lever": str(LEVERAGE),
            "mgnMode": "isolated",
            "posSide": side,
        })


def get_instrument(inst_id):
    rows = public_get("/api/v5/public/instruments", {
        "instType": "SWAP",
        "instId": inst_id,
    })

    if not rows:
        raise RuntimeError(f"找不到合约: {inst_id}")

    info = rows[0]

    if info.get("state") != "live":
        raise RuntimeError(f"{inst_id} 当前不可交易")

    if (
        info.get("ctType") != "linear"
        or info.get("settleCcy") != "USDT"
    ):
        raise RuntimeError(f"{inst_id} 不是 USDT 线性永续合约")

    if info.get("ctValCcy") != inst_id.split("-")[0]:
        raise RuntimeError(f"{inst_id} 合约面值币种异常")

    return info


def get_price(inst_id):
    rows = public_get("/api/v5/market/ticker", {
        "instId": inst_id
    })

    if not rows or not rows[0].get("last"):
        raise RuntimeError(f"无法获取 {inst_id} 最新价格")

    return dec(rows[0]["last"])


def round_step(value, step, rounding=ROUND_DOWN):
    return (
        (value / step).to_integral_value(rounding=rounding)
        * step
    )


def contract_size(info, price):
    ct_val = dec(info["ctVal"])
    ct_mult = dec(info.get("ctMult") or "1")
    lot = dec(info["lotSz"])
    minimum = dec(info["minSz"])

    if price <= 0 or ct_val <= 0 or ct_mult <= 0 or lot <= 0:
        raise RuntimeError("合约参数异常，拒绝下单")

    one_contract = ct_val * ct_mult * price
    target_notional = MARGIN_USDT * LEVERAGE
    size = round_step(target_notional / one_contract, lot)

    if size < minimum:
        raise RuntimeError(
            "5 USDT保证金预算不足以满足最小下单张数，拒绝放大仓位"
        )

    max_size = info.get("maxMktSz")
    if max_size and size > dec(max_size):
        raise RuntimeError("计算张数超过交易所市价单上限")

    return size


def ema(values, period):
    if len(values) < period:
        raise RuntimeError("EMA数据不足")

    alpha = Decimal("2") / Decimal(period + 1)
    result = sum(values[:period]) / Decimal(period)

    for value in values[period:]:
        result = value * alpha + result * (Decimal("1") - alpha)

    return result


def rsi(values, period=14):
    if len(values) < period + 1:
        raise RuntimeError("RSI数据不足")

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, Decimal("0")))
        losses.append(max(-change, Decimal("0")))

    avg_gain = sum(gains[:period]) / Decimal(period)
    avg_loss = sum(losses[:period]) / Decimal(period)

    for i in range(period, len(gains)):
        avg_gain = (
            avg_gain * Decimal(period - 1) + gains[i]
        ) / Decimal(period)
        avg_loss = (
            avg_loss * Decimal(period - 1) + losses[i]
        ) / Decimal(period)

    if avg_loss == 0:
        return Decimal("100")

    rs = avg_gain / avg_loss
    return Decimal("100") - (
        Decimal("100") / (Decimal("1") + rs)
    )


def closed_closes(inst_id, bar, limit):
    rows = public_get("/api/v5/market/candles", {
        "instId": inst_id,
        "bar": bar,
        "limit": str(limit),
    })

    # OKX confirm=1 表示K线已收盘
    rows = [row for row in rows if row[8] == "1"]
    rows.reverse()

    return [dec(row[4]) for row in rows]


def get_signal(inst_id):
    five = closed_closes(inst_id, "5m", 100)
    fifteen = closed_closes(inst_id, "15m", 60)

    if len(five) < 40 or len(fifteen) < 25:
        print(f"{inst_id} K线数据不足，暂不交易")
        return None

    fast = ema(five, 5)
    slow = ema(five, 13)
    momentum = rsi(five, 14)

    trend = ema(fifteen, 20)
    trend_price = fifteen[-1]
    current_price = five[-1]

    print(
        f"{inst_id} 5m EMA5={fast:.6f} "
        f"EMA13={slow:.6f} RSI={momentum:.2f}"
    )

    # 放宽入场条件，提高信号出现机会；不强制开仓
    if (
        fast > slow
        and current_price > slow
        and trend_price > trend
        and momentum >= Decimal("52")
    ):
        return "long"

    if (
        fast < slow
        and current_price < slow
        and trend_price < trend
        and momentum <= Decimal("48")
    ):
        return "short"

    return None


def confirm_order(result, name):
    if (
        not result
        or result[0].get("sCode") != "0"
        or not result[0].get("ordId")
    ):
        raise RuntimeError(f"{name}未获交易所确认: {result}")


def wait_for_position(inst_id, side):
    for _ in range(WAIT_TRIES):
        time.sleep(WAIT_SECONDS)
        positions = get_positions(inst_id)
        if side in positions:
            return positions[side]

    return None


def place_entry(inst_id, direction, info):
    positions = get_positions(inst_id)

    if direction in positions:
        raise RuntimeError(
            f"{inst_id} 已有 {direction} 仓位，拒绝重复开仓"
        )

    price = get_price(inst_id)
    size = contract_size(info, price)
    tick = dec(info["tickSz"])

    if direction == "long":
        order_side = "buy"
        tp = round_step(
            price * (Decimal("1") + TAKE_PROFIT),
            tick,
            ROUND_UP
        )
        sl = round_step(
            price * (Decimal("1") - STOP_LOSS),
            tick,
            ROUND_DOWN
        )
    else:
        order_side = "sell"
        tp = round_step(
            price * (Decimal("1") - TAKE_PROFIT),
            tick,
            ROUND_DOWN
        )
        sl = round_step(
            price * (Decimal("1") + STOP_LOSS),
            tick,
            ROUND_UP
        )

    state = load_state()
    if state.get("pending"):
        raise RuntimeError("存在未处理订单，停止开仓")

    client_id = uuid.uuid4().hex[:24]

    state["pending"] = {
        "instId": inst_id,
        "posSide": direction,
        "action": "entry",
        "clOrdId": client_id,
        "createdAt": timestamp(),
    }
    save_state(state)

    result = private_post("/api/v5/trade/order", {
        "instId": inst_id,
        "tdMode": "isolated",
        "posSide": direction,
        "clOrdId": client_id,
        "side": order_side,
        "ordType": "market",
        "sz": str(size),
        "attachAlgoOrds": [{
            "tpTriggerPx": str(tp),
            "tpOrdPx": "-1",
            "tpTriggerPxType": "mark",
            "slTriggerPx": str(sl),
            "slOrdPx": "-1",
            "slTriggerPxType": "mark",
        }],
    })

    confirm_order(result, "开仓订单")

    position = wait_for_position(inst_id, direction)

    if not position:
        raise RuntimeError(
            "开仓请求已接受，但尚未检测到持仓。"
            "pending 已保留，请检查 OKX 订单和持仓，勿直接清除状态。"
        )

    state = load_state()
    state["pending"] = None
    state["positions"][position_key(inst_id, direction)] = {
        "size": str(abs(dec(position["pos"]))),
        "clOrdId": client_id,
        "recordedAt": timestamp(),
    }
    save_state(state)

    print(f"开仓确认: {inst_id} {direction}")
    print(f"实际仓位: {position['pos']} 张")
    print(f"止盈触发价估算: {tp}")
    print(f"止损触发价估算: {sl}")
    print("保护单触发价按下单前行情估算，并非实际成交均价。")


def close_position(inst_id, direction, position):
    size = abs(dec(position["pos"]))

    if direction == "long":
        order_side = "sell"
    else:
        order_side = "buy"

    state = load_state()
    if state.get("pending"):
        raise RuntimeError("存在未处理订单，停止平仓")

    client_id = uuid.uuid4().hex[:24]

    state["pending"] = {
        "instId": inst_id,
        "posSide": direction,
        "action": "close",
        "clOrdId": client_id,
        "createdAt": timestamp(),
    }
    save_state(state)

    result = private_post("/api/v5/trade/order", {
        "instId": inst_id,
        "tdMode": "isolated",
        "posSide": direction,
        "clOrdId": client_id,
        "side": order_side,
        "ordType": "market",
        "sz": str(size),
    })

    confirm_order(result, "平仓订单")

    for _ in range(WAIT_TRIES):
        time.sleep(WAIT_SECONDS)
        positions = get_positions(inst_id)

        if direction not in positions:
            state = load_state()
            state["positions"].pop(
                position_key(inst_id, direction),
                None
            )
            state["pending"] = None
            save_state(state)
            print(f"平仓确认: {inst_id} {direction}")
            return

    raise RuntimeError(
        "平仓请求已接受但仓位仍存在，pending 已保留，请检查 OKX。"
    )


def run_once():
    state = load_state()

    if state.get("pending"):
        raise RuntimeError(
            "发现上次未确认订单，交易锁定。"
            "请先核对 OKX 订单和持仓，不要直接删除 pending。"
        )

    check_account()

    # 先检查全部仓位。任何未登记仓位都停止，不自动接管。
    for inst_id in SYMBOLS:
        actual = get_positions(inst_id)

        for side in actual:
            key = position_key(inst_id, side)
            if key not in state["positions"]:
                raise RuntimeError(
                    f"{inst_id} 存在未登记的 {side} 仓位，停止交易"
                )

        for side in list(state["positions"]):
            if side.startswith(inst_id + ":"):
                pos_side = side.split(":")[-1]
                if pos_side not in actual:
                    state["positions"].pop(side, None)
                    save_state(state)
                    print(f"{inst_id} {pos_side} 仓位已不存在，清理本地记录")

    for inst_id in SYMBOLS:
        info = get_instrument(inst_id)
        positions = get_positions(inst_id)
        signal = get_signal(inst_id)

        # 反向信号先平仓，本轮不立即反手
        opposite = (
            "short" if signal == "long"
            else "long" if signal == "short"
            else None
        )

        if opposite and opposite in positions:
            close_position(inst_id, opposite, positions[opposite])
            continue

        if signal and signal in positions:
            print(f"{inst_id} 已有 {signal} 仓位，继续持有")
            continue

        if signal:
            place_entry(inst_id, signal, info)
        else:
            print(f"{inst_id} 暂无开仓信号")


def main():
    if not KEY or not SECRET or not PASSPHRASE:
        raise RuntimeError("缺少 OKX API 密钥配置")

    if not LIVE or CONFIRM != "YES":
        raise RuntimeError(
            "实盘开关未确认：需要 LIVE_TRADING=true "
            "且 CONFIRM_10X_LIVE=YES"
        )

    print("OKX 双向持仓策略：本次执行一轮")
    print("周期：5分钟信号 + 15分钟趋势过滤")
    print("信号：EMA5/EMA13 + RSI")
    print(f"杠杆：{LEVERAGE}x，逐仓")
    print(f"每币保证金预算：{MARGIN_USDT} USDT")
    print(f"止盈：{TAKE_PROFIT * 100}%")
    print(f"止损：{STOP_LOSS * 100}%")
    print(f"交易标的：{', '.join(SYMBOLS)}")

    check_account()

    state = load_state()
    if state.get("pending"):
        raise RuntimeError("发现未处理订单，停止运行")

    # 未登记仓位检查通过后才设置杠杆
    for inst_id in SYMBOLS:
        positions = get_positions(inst_id)
        for side in positions:
            key = position_key(inst_id, side)
            if key not in state["positions"]:
                raise RuntimeError(
                    f"{inst_id} 有未登记的 {side} 仓位，停止操作"
                )

    for inst_id in SYMBOLS:
        set_leverage(inst_id)

    run_once()
    print("本轮执行结束")


if __name__ == "__main__":
    main()

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

# ===== 实盘配置 =====
BASE = "https://www.okx.com"
SYMBOLS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
LEVERAGE = Decimal("10")
MARGIN_USDT = Decimal("5")       # 每个币种的保证金预算，不是下单名义价值
TAKE_PROFIT = Decimal("0.06")  # 按下单前参考价格估算的 6% 止盈
STOP_LOSS = Decimal("0.03")     # 按下单前参考价格估算的 3% 止损
INTERVAL = int(os.getenv("INTERVAL_SECONDS", "900"))  # 每15分钟检查一次
STATE_FILE = os.getenv("STATE_FILE", "swap_state.json")

LIVE = os.getenv("LIVE_TRADING", "false").strip().lower() == "true"
CONFIRM = os.getenv("CONFIRM_10X_LIVE", "").strip()
KEY = os.getenv("OKX_API_KEY", "").strip()
SECRET = os.getenv("OKX_API_SECRET", "").strip()
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "").strip()

HTTP_TIMEOUT = 20
POSITION_WAIT_TRIES = 10
POSITION_WAIT_SECONDS = 2


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
    body_text = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body is not None else ""
    headers = {"Content-Type": "application/json"}

    if private:
        message_time = timestamp()
        message = message_time + method + request_path + body_text
        signature = base64.b64encode(
            hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).digest()
        ).decode()
        headers.update({
            "OK-ACCESS-KEY": KEY,
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": message_time,
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
        raise RuntimeError("OKX 返回了无法识别的数据") from None

    if result.get("code") != "0":
        raise RuntimeError(f"OKX API错误: {result.get('code')} {result.get('msg')}")
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
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{STATE_FILE} 无法读取，停止交易: {exc}") from None

    if not isinstance(state, dict) or not isinstance(state.get("positions", {}), dict):
        raise RuntimeError("本地状态文件格式不正确，停止交易")
    state.setdefault("positions", {})
    state.setdefault("pending", None)
    return state


def save_state(state):
    temp_file = STATE_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, ensure_ascii=False)
        file.write("\n")
    os.replace(temp_file, STATE_FILE)


def check_account():
    config = private_get("/api/v5/account/config")
    if not config or config[0].get("posMode") != "net_mode":
        raise RuntimeError("账户必须是单向持仓模式(net_mode)，程序停止，不会下单")


def get_position(inst_id):
    rows = private_get("/api/v5/account/positions", {"instId": inst_id})
    for row in rows:
        if dec(row.get("pos", "0")) != 0:
            return row
    return None


def set_leverage(inst_id):
    private_post("/api/v5/account/set-leverage", {
        "instId": inst_id,
        "lever": str(LEVERAGE),
        "mgnMode": "isolated",
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
        raise RuntimeError(f"合约当前不可交易: {inst_id}")
    if info.get("ctType") != "linear" or info.get("settleCcy") != "USDT":
        raise RuntimeError(f"{inst_id} 不是 USDT 正向线性永续合约")
    if info.get("ctValCcy") != info.get("baseCcy"):
        raise RuntimeError(f"{inst_id} 合约面值币种与基础币种不一致，拒绝自动计算张数")
    return info


def get_price(inst_id):
    rows = public_get("/api/v5/market/ticker", {"instId": inst_id})
    if not rows or not rows[0].get("last"):
        raise RuntimeError(f"无法获取价格: {inst_id}")
    return dec(rows[0]["last"])


def round_step(value, step, rounding=ROUND_DOWN):
    return (value / step).to_integral_value(rounding=rounding) * step


def contract_size(info, price):
    ct_val = dec(info["ctVal"])
    ct_mult = dec(info.get("ctMult") or "1")
    lot = dec(info["lotSz"])
    minimum = dec(info["minSz"])
    if price <= 0 or ct_val <= 0 or ct_mult <= 0 or lot <= 0:
        raise RuntimeError("合约面值/价格/下单单位异常，拒绝下单")

    # 线性 USDT 合约：每张名义价值 = ctVal * ctMult * 标记价格
    one_contract_notional = ct_val * ct_mult * price
    target_notional = MARGIN_USDT * LEVERAGE
    size = round_step(target_notional / one_contract_notional, lot)

    if size < minimum:
        raise RuntimeError("5 USDT 保证金预算不足以达到该合约最小下单张数，拒绝放大仓位")
    max_size = info.get("maxMktSz")
    if max_size and size > dec(max_size):
        raise RuntimeError("计算出的张数超过交易所市价单上限")
    return size


def get_signal(inst_id):
    rows = public_get("/api/v5/market/candles", {
        "instId": inst_id,
        "bar": "15m",
        "limit": "40",
    })
    # OKX 第 9 列 confirm=1 表示K线已收盘
    closed = [row for row in rows if row[8] == "1"]
    closed.reverse()
    if len(closed) < 23:
        return None

    closes = [dec(row[4]) for row in closed]
    fast_prev = sum(closes[-10:-1]) / Decimal("9")
    slow_prev = sum(closes[-22:-1]) / Decimal("21")
    fast_now = sum(closes[-9:]) / Decimal("9")
    slow_now = sum(closes[-21:]) / Decimal("21")

    if fast_prev <= slow_prev and fast_now > slow_now:
        return "long"
    if fast_prev >= slow_prev and fast_now < slow_now:
        return "short"
    return None


def confirm_order_response(result, action_name):
    if not result or result[0].get("sCode") != "0" or not result[0].get("ordId"):
        raise RuntimeError(f"{action_name}未获交易所确认: {result}")


def wait_for_position(inst_id, expected_direction=None):
    for _ in range(POSITION_WAIT_TRIES):
        time.sleep(POSITION_WAIT_SECONDS)
        position = get_position(inst_id)
        if position:
            if expected_direction:
                actual = "long" if dec(position["pos"]) > 0 else "short"
                if actual != expected_direction:
                    raise RuntimeError(f"{inst_id} 检测到仓位方向与预期不符，停止自动操作")
            return position
    return None


def place_entry(inst_id, direction, info):
    # 下单前再次确认没有仓位，防止重复开仓
    if get_position(inst_id):
        raise RuntimeError(f"{inst_id} 下单前发现已有仓位，拒绝重复开仓")

    price = get_price(inst_id)
    size = contract_size(info, price)
    tick = dec(info["tickSz"])

    if direction == "long":
        side = "buy"
        take_profit_price = round_step(price * (Decimal("1") + TAKE_PROFIT), tick, ROUND_UP)
        stop_price = round_step(price * (Decimal("1") - STOP_LOSS), tick, ROUND_DOWN)
    else:
        side = "sell"
        take_profit_price = round_step(price * (Decimal("1") - TAKE_PROFIT), tick, ROUND_DOWN)
        stop_price = round_step(price * (Decimal("1") + STOP_LOSS), tick, ROUND_UP)

    client_id = uuid.uuid4().hex[:24]
    state = load_state()
    if state.get("pending"):
        raise RuntimeError("发现未处理订单状态，停止下单")
    state["pending"] = {
        "instId": inst_id,
        "action": "entry",
        "clOrdId": client_id,
        "createdAt": timestamp(),
    }
    save_state(state)

    result = private_post("/api/v5/trade/order", {
        "instId": inst_id,
        "tdMode": "isolated",
        "clOrdId": client_id,
        "side": side,
        "ordType": "market",
        "sz": str(size),
        "attachAlgoOrds": [{
            "tpTriggerPx": str(take_profit_price),
            "tpOrdPx": "-1",
            "tpTriggerPxType": "mark",
            "slTriggerPx": str(stop_price),
            "slOrdPx": "-1",
            "slTriggerPxType": "mark",
        }],
    })
    confirm_order_response(result, "开仓订单")

    position = wait_for_position(inst_id, direction)
    if not position:
        raise RuntimeError("开仓请求已被接受，但暂未检测到仓位。pending 已保留；请先检查 OKX 订单/持仓，勿手动清除状态")

    state = load_state()
    state["pending"] = None
    state["positions"][inst_id] = {
        "direction": direction,
        "size": str(abs(dec(position["pos"]))),
        "clOrdId": client_id,
        "recordedAt": timestamp(),
    }
    save_state(state)
    print(f"开仓确认: {inst_id} {direction}，实际仓位 {position['pos']} 张")
    print(f"止盈触发价(按下单前行情估算): {take_profit_price}")
    print(f"止损触发价(按下单前行情估算): {stop_price}")
    print("止盈止损已随开仓单提交；只有开仓成交后，交易所才会激活附带保护单")


def close_position(inst_id, position):
    current_size = abs(dec(position["pos"]))
    pos_value = dec(position["pos"])
    side = "sell" if pos_value > 0 else "buy"
    client_id = uuid.uuid4().hex[:24]

    state = load_state()
    if state.get("pending"):
        raise RuntimeError("发现未处理订单状态，停止平仓操作")
    state["pending"] = {
        "instId": inst_id,
        "action": "close",
        "clOrdId": client_id,
        "createdAt": timestamp(),
    }
    save_state(state)

    result = private_post("/api/v5/trade/order", {
        "instId": inst_id,
        "tdMode": "isolated",
        "clOrdId": client_id,
        "side": side,
        "ordType": "market",
        "sz": str(current_size),
        "reduceOnly": True,
    })
    confirm_order_response(result, "平仓订单")

    for _ in range(POSITION_WAIT_TRIES):
        time.sleep(POSITION_WAIT_SECONDS)
        remaining = get_position(inst_id)
        if remaining is None:
            state = load_state()
            state["positions"].pop(inst_id, None)
            state["pending"] = None
            save_state(state)
            print(f"平仓确认: {inst_id}")
            return
    raise RuntimeError("平仓请求已接受但仓位仍存在。pending 已保留，请检查 OKX 持仓")


def run_once():
    state = load_state()
    if state.get("pending"):
        raise RuntimeError(
            "发现上次未确认订单，自动交易已锁定。请先在 OKX 检查订单和持仓，"
            "确认结果后再处理 swap_state.json，不能直接删除 pending。"
        )

    check_account()

    # 先核对交易所现有仓位，任何未登记仓位都停止，不擅自接管
    for inst_id in SYMBOLS:
        position = get_position(inst_id)
        saved = state["positions"].get(inst_id)
        if position and not saved:
            raise RuntimeError(f"{inst_id} 有未登记的交易所仓位，停止交易，不会擅自接管")
        if saved and not position:
            state["positions"].pop(inst_id, None)
            save_state(state)
            print(f"{inst_id} 交易所仓位已不存在，已清除本地记录")

    for inst_id in SYMBOLS:
        info = get_instrument(inst_id)
        position = get_position(inst_id)
        saved = state["positions"].get(inst_id)
        signal = get_signal(inst_id)

        if position and saved:
            actual_direction = "long" if dec(position["pos"]) > 0 else "short"
            if saved.get("direction") != actual_direction:
                raise RuntimeError(f"{inst_id} 本地方向与交易所方向不一致，停止操作")
            if signal and signal != actual_direction:
                close_position(inst_id, position)
                state = load_state()
            else:
                print(f"{inst_id} 已有 {actual_direction} 仓位，继续持有")
            continue

        if not position and signal:
            place_entry(inst_id, signal, info)
            state = load_state()
        else:
            print(f"{inst_id} 暂无开仓信号")


def main():
    if not KEY or not SECRET or not PASSPHRASE:
        raise RuntimeError("缺少 OKX_API_KEY / OKX_API_SECRET / OKX_PASSPHRASE")
    if not LIVE or CONFIRM != "YES":
        raise RuntimeError("实盘开关未确认：需要 LIVE_TRADING=true 且 CONFIRM_10X_LIVE=YES")

    print("OKX USDT 永续策略：常驻无限循环")
    print(f"杠杆设置目标：{LEVERAGE}x，逐仓")
    print(f"每个币种保证金预算：{MARGIN_USDT} USDT；标的：{', '.join(SYMBOLS)}")
    print(f"止盈：{TAKE_PROFIT * 100}% | 止损：{STOP_LOSS * 100}% | 检查间隔：{INTERVAL}秒")
    print("注意：止盈止损触发价按开仓前行情估算；止损不保证成交价，可能滑点或强平。")

    check_account()
    state = load_state()
    if state.get("pending"):
        raise RuntimeError("发现未处理订单，停止；先检查交易所订单和持仓")

    for inst_id in SYMBOLS:
        set_leverage(inst_id)

    while True:
        started = time.time()
        try:
            run_once()
        except Exception as exc:
            # 订单状态不确定时，pending 锁会阻止下一轮重复开仓
            print(f"本轮运行异常：{type(exc).__name__}: {exc}", flush=True)
        elapsed = time.time() - started
        sleep_for = max(1, INTERVAL - int(elapsed))
        print(f"本轮结束，{sleep_for}秒后进行下一轮", flush=True)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()

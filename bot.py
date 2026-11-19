
import os
import json
import time
import hmac
import base64
import hashlib
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP

BASE = "https://www.okx.com"

SYMBOLS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
LEVERAGE = Decimal("10")
MARGIN_USDT = Decimal("5")
STOP_LOSS = Decimal("0.03")
INTERVAL = 900
STATE_FILE = "swap_state.json"

LIVE = os.getenv("LIVE_TRADING", "false").lower() == "true"
CONFIRM = os.getenv("CONFIRM_10X_LIVE", "")

KEY = os.getenv("OKX_API_KEY", "")
SECRET = os.getenv("OKX_API_SECRET", "")
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")


def dec(x):
    return Decimal(str(x))


def timestamp():
    return datetime.now(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def request(method, path, params=None, body=None, private=False):
    method = method.upper()
    query = ""
    if params:
        query = "?" + urllib.parse.urlencode(params)

    request_path = path + query
    body_text = json.dumps(body, separators=(",", ":")) if body else ""

    headers = {"Content-Type": "application/json"}

    if private:
        ts = timestamp()
        message = ts + method + request_path + body_text
        sign = base64.b64encode(
            hmac.new(
                SECRET.encode(),
                message.encode(),
                hashlib.sha256
            ).digest()
        ).decode()

        headers.update({
            "OK-ACCESS-KEY": KEY,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        })

    req = urllib.request.Request(
        BASE + request_path,
        data=body_text.encode() if body else None,
        headers=headers,
        method=method
    )

    with urllib.request.urlopen(req, timeout=20) as response:
        result = json.loads(response.read().decode())

    if result.get("code") != "0":
        raise RuntimeError("OKX API错误：" + str(result))

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
    with open(STATE_FILE, "r") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def check_account():
    config = private_get("/api/v5/account/config")
    if not config or config[0].get("posMode") != "net_mode":
        raise RuntimeError(
            "账户不是单向持仓模式。程序停止，不会下单。"
        )


def get_position(inst_id):
    rows = private_get(
        "/api/v5/account/positions",
        {"instId": inst_id}
    )
    for row in rows:
        if dec(row.get("pos", "0")) != 0:
            return row
    return None


def set_leverage(inst_id):
    private_post(
        "/api/v5/account/set-leverage",
        {
            "instId": inst_id,
            "lever": str(LEVERAGE),
            "mgnMode": "isolated"
        }
    )


def get_instrument(inst_id):
    rows = public_get(
        "/api/v5/public/instruments",
        {"instType": "SWAP", "instId": inst_id}
    )
    if not rows:
        raise RuntimeError("找不到合约：" + inst_id)

    info = rows[0]
    if info.get("state") != "live":
        raise RuntimeError("合约当前不可交易：" + inst_id)

    if info.get("ctType") != "linear":
        raise RuntimeError("不是正向线性合约：" + inst_id)

    if info.get("settleCcy") != "USDT":
        raise RuntimeError("结算币种不是USDT：" + inst_id)

    return info


def get_price(inst_id):
    rows = public_get(
        "/api/v5/market/ticker",
        {"instId": inst_id}
    )
    if not rows:
        raise RuntimeError("无法获取价格：" + inst_id)
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

    contract_value = ct_val * ct_mult
    notional = MARGIN_USDT * LEVERAGE
    raw = notional / (price * contract_value)
    size = round_step(raw, lot)

    if size < minimum:
        raise RuntimeError(
            "保证金预算不足以达到该合约最小下单张数。"
            "程序不会自动加大仓位。"
        )

    max_size = info.get("maxMktSz")
    if max_size and size > dec(max_size):
        raise RuntimeError("计算出的张数超过交易所市价单上限")

    return size


def get_signal(inst_id):
    rows = public_get(
        "/api/v5/market/candles",
        {"instId": inst_id, "bar": "15m", "limit": "40"}
    )

    # 只使用已经收盘的K线
    closed = [r for r in rows if r[8] == "1"]
    closed.reverse()

    if len(closed) < 23:
        return None

    prices = [dec(r[4]) for r in closed]

    fast_prev = sum(prices[-10:-1]) / Decimal("9")
    slow_prev = sum(prices[-22:-1]) / Decimal("21")

    fast_now = sum(prices[-9:]) / Decimal("9")
    slow_now = sum(prices[-21:]) / Decimal("21")

    if fast_prev <= slow_prev and fast_now > slow_now:
        return "long"

    if fast_prev >= slow_prev and fast_now < slow_now:
        return "short"

    return None


def place_entry(inst_id, direction, info):
    price = get_price(inst_id)
    size = contract_size(info, price)
    tick = dec(info["tickSz"])

    if direction == "long":
        side = "buy"
        stop = round_step(
            price * (Decimal("1") - STOP_LOSS),
            tick,
            ROUND_DOWN
        )
    else:
        side = "sell"
        stop = round_step(
            price * (Decimal("1") + STOP_LOSS),
            tick,
            ROUND_UP
        )

    client_id = uuid.uuid4().hex[:24]

    state = load_state()
    state["pending"] = {
        "instId": inst_id,
        "action": "entry",
        "clOrdId": client_id
    }
    save_state(state)

    result = private_post(
        "/api/v5/trade/order",
        {
            "instId": inst_id,
            "tdMode": "isolated",
            "clOrdId": client_id,
            "side": side,
            "ordType": "market",
            "sz": str(size),
            "attachAlgoOrds": [{
                "slTriggerPx": str(stop),
                "slOrdPx": "-1",
                "slTriggerPxType": "mark"
            }]
        }
    )

    if not result or result[0].get("sCode") != "0":
        raise RuntimeError("开仓订单未获交易所确认：" + str(result))

    # 等待交易所仓位出现
    position = None
    for _ in range(10):
        time.sleep(2)
        position = get_position(inst_id)
        if position:
            break

    if not position:
        raise RuntimeError(
            "订单已提交但未确认仓位。程序停止，请检查OKX订单记录。"
        )

    state = load_state()
    state["pending"] = None
    state["positions"][inst_id] = {
        "direction": direction,
        "size": str(size),
        "clOrdId": client_id
    }
    save_state(state)

    print("开仓确认：", inst_id, direction, size, "张")
    print("交易所止损触发价：", stop)


def close_position(inst_id, position):
    size = abs(dec(position["pos"]))
    pos_value = dec(position["pos"])
    side = "sell" if pos_value > 0 else "buy"

    state = load_state()
    state["pending"] = {
        "instId": inst_id,
        "action": "close"
    }
    save_state(state)

    result = private_post(
        "/api/v5/trade/order",
        {
            "instId": inst_id,
            "tdMode": "isolated",
            "clOrdId": uuid.uuid4().hex[:24],
            "side": side,
            "ordType": "market",
            "sz": str(size),
            "reduceOnly": True
        }
    )

    if not result or result[0].get("sCode") != "0":
        raise RuntimeError("平仓订单未获交易所确认：" + str(result))

    for _ in range(10):
        time.sleep(2)
        if get_position(inst_id) is None:
            state = load_state()
            state["positions"].pop(inst_id, None)
            state["pending"] = None
            save_state(state)
            print("平仓确认：", inst_id)
            return

    raise RuntimeError("平仓后仓位仍存在，请检查交易所持仓")


def run_once():
    state = load_state()

    if state.get("pending"):
        raise RuntimeError(
            "发现上次未确认订单，停止自动交易。"
            "请先检查OKX订单和持仓，再处理状态文件。"
        )

    check_account()

    for inst_id in SYMBOLS:
        info = get_instrument(inst_id)
        position = get_position(inst_id)
        saved = state["positions"].get(inst_id)
        signal = get_signal(inst_id)

        if position and not saved:
            raise RuntimeError(
                inst_id + "存在未登记仓位，程序停止，不会擅自接管。"
            )

        if saved and not position:
            state["positions"].pop(inst_id, None)
            save_state(state)
            print(inst_id, "仓位已不存在，清除本地记录")
            continue

        if position and saved:
            current_direction = (
                "long" if dec(position["pos"]) > 0 else "short"
            )
            if signal and signal != current_direction:
                close_position(inst_id, position)
            else:
                print(inst_id, "已有仓位，继续持有")
            continue

        if not position and signal:
            place_entry(inst_id, signal, info)
        else:
            print(inst_id, "暂无开仓信号")


def main():
    if not KEY or not SECRET or not PASSPHRASE:
        raise RuntimeError("缺少OKX API环境变量")

    if not LIVE or CONFIRM != "YES":
        raise RuntimeError(
            "实盘开关未同时确认。需要 LIVE_TRADING=true "
            "和 CONFIRM_10X_LIVE=YES"
        )

    print("OKX USDT永续策略启动")
    print("逐仓杠杆：10倍")
    print("每个币保证金预算：", MARGIN_USDT, "USDT")

    for inst_id in SYMBOLS:
        set_leverage(inst_id)

    while True:
        run_once()
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()

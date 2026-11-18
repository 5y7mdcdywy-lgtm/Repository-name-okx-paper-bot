
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
from decimal import Decimal, ROUND_DOWN

# ===== 实盘参数 =====

BASE = os.getenv("OKX_BASE_URL", "https://www.okx.com").rstrip("/")
SYMBOLS = ["BTC-USDT", "ETH-USDT"]

TRADE_USDT = Decimal("5")
STOP_LOSS = Decimal("0.03")
INTERVAL = 900
SLIPPAGE = "0.01"

STATE_FILE = "live_state.json"

LIVE = os.getenv("LIVE_TRADING", "false").lower() == "true"

KEY = os.getenv("OKX_API_KEY", "")
SECRET = os.getenv("OKX_API_SECRET", "")
PASS = os.getenv("OKX_PASSPHRASE", "")

if LIVE and not all([KEY, SECRET, PASS]):
    raise RuntimeError("缺少 OKX API 环境变量")

# ===== 状态文件 =====

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"positions": {}, "last_candle": {}, "pending": None}

    with open(STATE_FILE, "r") as f:
        state = json.load(f)

    if state.get("pending"):
        raise RuntimeError(
            "发现未确认订单，程序停止，禁止重复下单。"
            "请先检查 OKX 订单记录。"
        )

    return state


def save_state(state):
    temp = STATE_FILE + ".tmp"
    with open(temp, "w") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, STATE_FILE)


# ===== OKX API =====

def api(method, path, body=None, private=False):
    timestamp = datetime.now(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")

    payload = json.dumps(
        body, separators=(",", ":")
    ) if body is not None else ""

    headers = {"Content-Type": "application/json"}

    if private:
        message = timestamp + method + path + payload
        digest = hmac.new(
            SECRET.encode(),
            message.encode(),
            hashlib.sha256
        ).digest()

        headers.update({
            "OK-ACCESS-KEY": KEY,
            "OK-ACCESS-SIGN": base64.b64encode(digest).decode(),
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": PASS
        })

    req = urllib.request.Request(
        BASE + path,
        data=payload.encode() if body is not None else None,
        headers=headers,
        method=method
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError("OKX HTTP错误：" + e.read().decode())
    except Exception as e:
        raise RuntimeError("API连接失败：" + str(e))

    if result.get("code") != "0":
        raise RuntimeError("OKX返回错误：" + str(result))

    return result.get("data", [])


# ===== 行情 =====

def ticker(symbol):
    q = urllib.parse.urlencode({"instId": symbol})
    data = api("GET", "/api/v5/market/ticker?" + q)

    if not data:
        raise RuntimeError("无法取得行情：" + symbol)

    return Decimal(data[0]["last"])


def candles(symbol):
    q = urllib.parse.urlencode({
        "instId": symbol,
        "bar": "15m",
        "limit": "40"
    })

    rows = api("GET", "/api/v5/market/candles?" + q)

    # 只使用已经收盘的K线
    rows = [r for r in rows if r[8] == "1"]
    rows.sort(key=lambda r: int(r[0]))

    if len(rows) < 22:
        raise RuntimeError("K线数量不足：" + symbol)

    return rows


def get_signal(symbol):
    rows = candles(symbol)
    closes = [Decimal(r[4]) for r in rows]

    ma5 = sum(closes[-5:]) / Decimal("5")
    ma20 = sum(closes[-20:]) / Decimal("20")

    old5 = sum(closes[-6:-1]) / Decimal("5")
    old20 = sum(closes[-21:-1]) / Decimal("20")

    if old5 <= old20 and ma5 > ma20:
        signal = "BUY"
    elif old5 >= old20 and ma5 < ma20:
        signal = "SELL"
    else:
        signal = "HOLD"

    return signal, rows[-1][0]


# ===== 账户 =====

def balance(currency):
    q = urllib.parse.urlencode({"ccy": currency})
    data = api(
        "GET",
        "/api/v5/account/balance?" + q,
        private=True
    )

    for item in data[0].get("details", []):
        if item["ccy"] == currency:
            return Decimal(item["availBal"])

    return Decimal("0")


def instrument(symbol):
    q = urllib.parse.urlencode({
        "instType": "SPOT",
        "instId": symbol
    })

    data = api("GET", "/api/v5/public/instruments?" + q)

    if not data or data[0]["state"] != "live":
        raise RuntimeError("交易品种不可交易：" + symbol)

    return data[0]


def round_down(value, step):
    value = Decimal(value)
    step = Decimal(step)
    return (
        (value / step).to_integral_value(rounding=ROUND_DOWN)
        * step
    )


def fmt(value):
    return format(value, "f")


# ===== 订单查询 =====

def query_order(symbol, order_id):
    q = urllib.parse.urlencode({
        "instId": symbol,
        "ordId": order_id
    })

    data = api(
        "GET",
        "/api/v5/trade/order?" + q,
        private=True
    )

    if not data:
        raise RuntimeError("订单查询失败")

    return data[0]


# ===== 下单与成交确认 =====

def place_order(state, symbol, side, size, target):
    client_id = uuid.uuid4().hex[:24]

    order = {
        "instId": symbol,
        "tdMode": "cash",
        "clOrdId": client_id,
        "side": side,
        "ordType": "market",
        "sz": fmt(size),
        "tgtCcy": target,
        "slippagePct": SLIPPAGE
    }

    # 下单前记录，避免异常后重复下单
    state["pending"] = {
        "symbol": symbol,
        "side": side,
        "client_id": client_id
    }
    save_state(state)

    # 网络异常时不自动重试
    result = api(
        "POST",
        "/api/v5/trade/order",
        order,
        private=True
    )

    if not result or result[0].get("sCode") != "0":
        raise RuntimeError("交易所拒绝订单：" + str(result))

    order_id = result[0]["ordId"]

    for _ in range(15):
        time.sleep(2)
        info = query_order(symbol, order_id)

        if info["state"] in ("filled", "canceled", "mmp_canceled"):
            filled = Decimal(info["accFillSz"])
            avg = Decimal(info["avgPx"] or "0")

            if filled > 0:
                pos = state["positions"].get(symbol)

                if side == "buy":
                    old_qty = Decimal(pos["qty"]) if pos else Decimal("0")
                    old_price = Decimal(pos["entry"]) if pos else Decimal("0")

                    total = old_qty + filled
                    entry = (
                        (old_qty * old_price + filled * avg) / total
                    )

                    state["positions"][symbol] = {
                        "qty": fmt(total),
                        "entry": fmt(entry)
                    }

                elif side == "sell" and pos:
                    remaining = max(
                        Decimal("0"),
                        Decimal(pos["qty"]) - filled
                    )

                    if remaining == 0:
                        state["positions"].pop(symbol, None)
                    else:
                        pos["qty"] = fmt(remaining)

            state["pending"] = None
            save_state(state)

            print(
                "订单结果：", info["state"],
                "成交数量：", filled,
                "均价：", avg
            )
            return

    raise RuntimeError(
        "订单未能确认，程序停止，请检查 OKX 订单记录"
    )


# ===== 买入 =====

def buy(state, symbol):
    if symbol in state["positions"]:
        print(symbol, "已有持仓，不重复买入")
        return

    if balance("USDT") < TRADE_USDT + Decimal("0.10"):
        print("USDT余额不足")
        return

    info = instrument(symbol)
    price = ticker(symbol)

    minimum = Decimal(info["minSz"])
    min_cost = minimum * price

    if min_cost > TRADE_USDT:
        print(symbol, "交易所最小下单金额超过5 USDT，跳过")
        return

    print("真实买入：", symbol, TRADE_USDT, "USDT")

    place_order(
        state, symbol, "buy", TRADE_USDT, "quote_ccy"
    )


# ===== 卖出 =====

def sell(state, symbol):
    pos = state["positions"].get(symbol)

    if not pos:
        return

    info = instrument(symbol)
    coin = symbol.split("-")[0]

    available = balance(coin)
    tracked = Decimal(pos["qty"])

    amount = round_down(
        min(available, tracked),
        Decimal(info["lotSz"])
    )

    if amount < Decimal(info["minSz"]):
        print(symbol, "可卖数量不足最小交易量")
        return

    print("真实卖出：", symbol, amount)

    place_order(
        state, symbol, "sell", amount, "base_ccy"
    )


# ===== 每轮策略 =====

def run_once(state):
    for symbol in SYMBOLS:
        signal, candle_id = get_signal(symbol)

        # 同一根K线只处理一次
        if state["last_candle"].get(symbol) == candle_id:
            continue

        state["last_candle"][symbol] = candle_id
        save_state(state)

        price = ticker(symbol)
        pos = state["positions"].get(symbol)

        print(
            datetime.now().isoformat(),
            symbol,
            "价格", price,
            "信号", signal
        )

        if pos:
            entry = Decimal(pos["entry"])

            # 3%止损
            if price <= entry * (Decimal("1") - STOP_LOSS):
                print(symbol, "触发3%止损")
                sell(state, symbol)

            elif signal == "SELL":
                sell(state, symbol)

        elif signal == "BUY":
            buy(state, symbol)


# ===== 主程序 =====

def main():
    print("OKX现货机器人启动")
    print("实盘开关：", LIVE)
    print("币种：", SYMBOLS)
    print("每笔上限：", TRADE_USDT)

    if not LIVE:
        print("当前未开启实盘，下单功能关闭")
        return

    state = load_state()

    while True:
        try:
            run_once(state)
        except Exception as e:
            print("发生错误，停止交易：", str(e))
            raise

        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()

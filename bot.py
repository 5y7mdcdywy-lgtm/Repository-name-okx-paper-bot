
import json
import os
import urllib.request
import urllib.parse
from datetime import datetime, timezone

BASE = "https://www.okx.com"
SYMBOLS = ["BTC-USDT", "ETH-USDT"]
TRADE_LIMIT = 5.0
INITIAL_CASH = 20.0
FEE_RATE = 0.001
STATE_FILE = "paper_state.json"


def get_candles(symbol):
    params = urllib.parse.urlencode({
        "instId": symbol,
        "bar": "15m",
        "limit": "100"
    })
    url = BASE + "/api/v5/market/candles?" + params
    req = urllib.request.Request(
        url, headers={"User-Agent": "PaperBot"}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode())

    if data.get("code") != "0":
        raise Exception(data.get("msg", "API error"))

    rows = list(reversed(data["data"]))
    rows = [x for x in rows if x[8] == "1"]

    return [(x[0], float(x[4])) for x in rows]


def ema_series(values, period):
    k = 2 / (period + 1)
    result = [values[0]]

    for value in values[1:]:
        result.append(
            value * k + result[-1] * (1 - k)
        )

    return result


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)

    return {
        "cash": INITIAL_CASH,
        "positions": {},
        "last_candles": {},
        "realized_pnl": 0.0,
        "trades": []
    }


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def analyze(symbol, state):
    candles = get_candles(symbol)

    if len(candles) < 51:
        print(symbol, "数据不足")
        return

    timestamps = [x[0] for x in candles]
    prices = [x[1] for x in candles]

    fast = ema_series(prices, 20)
    slow = ema_series(prices, 50)

    timestamp = timestamps[-1]
    price = prices[-1]

    if state["last_candles"].get(symbol) == timestamp:
        print(symbol, "本根K线已经分析过，跳过")
        return

    bullish = (
        fast[-2] <= slow[-2]
        and fast[-1] > slow[-1]
    )
    bearish = (
        fast[-2] >= slow[-2]
        and fast[-1] < slow[-1]
    )

    position = state["positions"].get(symbol)

    print("\n", symbol, "价格:", price)

    if bullish and not position:
        amount = min(TRADE_LIMIT, state["cash"] / (1 + FEE_RATE))

        if amount > 0:
            fee = amount * FEE_RATE
            quantity = amount / price
            total_cost = amount + fee

            state["cash"] -= total_cost
            state["positions"][symbol] = {
                "quantity": quantity,
                "cost": total_cost,
                "entry_price": price
            }

            state["trades"].append({
                "symbol": symbol,
                "action": "模拟买入",
                "price": price,
                "amount": amount,
                "fee": fee,
                "time": datetime.now(timezone.utc).isoformat()
            })

            print("模拟买入成功，金额:", round(amount, 4), "USDT")

    elif bearish and position:
        quantity = position["quantity"]
        gross = quantity * price
        fee = gross * FEE_RATE
        net = gross - fee
        pnl = net - position["cost"]

        state["cash"] += net
        state["realized_pnl"] += pnl
        del state["positions"][symbol]

        state["trades"].append({
            "symbol": symbol,
            "action": "模拟卖出",
            "price": price,
            "pnl": pnl,
            "fee": fee,
            "time": datetime.now(timezone.utc).isoformat()
        })

        print("模拟卖出成功，盈亏:", round(pnl, 4), "USDT")

    else:
        print("当前状态:", "持仓中" if position else "空仓")
        print("信号:", "金叉" if bullish else "死叉" if bearish else "无交叉")

    state["last_candles"][symbol] = timestamp


def main():
    print("OKX BTC + ETH 模拟交易启动")
    print(datetime.now(timezone.utc).isoformat())

    state = load_state()

    for symbol in SYMBOLS:
        try:
            analyze(symbol, state)
        except Exception as e:
            print(symbol, "分析失败:", str(e))

    equity = state["cash"]

    for symbol, position in state["positions"].items():
        try:
            candles = get_candles(symbol)
            if candles:
                equity += position["quantity"] * candles[-1][1]
        except Exception:
            equity += position["cost"]

    print("\n========== 模拟账户 ==========")
    print("可用余额:", round(state["cash"], 4), "USDT")
    print("持仓数量:", len(state["positions"]))
    print("已实现盈亏:", round(state["realized_pnl"], 4), "USDT")
    print("账户估值:", round(equity, 4), "USDT")
    print("模拟交易次数:", len(state["trades"]))

    state["trades"] = state["trades"][-100:]
    save_state(state)
    print("模拟账户记录已保存")


if __name__ == "__main__":
    main()

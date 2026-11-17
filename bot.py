
import json
import urllib.request
import urllib.parse
from datetime import datetime, timezone

BASE = "https://www.okx.com"
SYMBOLS = ["BTC-USDT", "ETH-USDT"]
TRADE_LIMIT = 5.0

def get_candles(symbol):
    params = urllib.parse.urlencode({
        "instId": symbol,
        "bar": "15m",
        "limit": "60"
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
    return [float(x[4]) for x in rows]

def ema(values, period):
    result = values[0]
    k = 2 / (period + 1)
    for v in values[1:]:
        result = v * k + result * (1 - k)
    return result

def analyze(symbol):
    prices = get_candles(symbol)
    if len(prices) < 50:
        print(symbol, "数据不足")
        return

    fast = ema(prices[-20:], 20)
    slow = ema(prices[-50:], 50)
    price = prices[-1]

    if fast > slow:
        signal = "观察买入信号"
    elif fast < slow:
        signal = "观察卖出信号"
    else:
        signal = "观望"

    print(symbol, signal, "价格:", price)
    print("单笔预算上限:", TRADE_LIMIT, "USDT")

if __name__ == "__main__":
    print("OKX 行情分析启动")
    print(datetime.now(timezone.utc).isoformat())

    for symbol in SYMBOLS:
        try:
            analyze(symbol)
        except Exception as e:
            print(symbol, "分析失败:", e)

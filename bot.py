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

# ================= 配置 =================
BASE = os.getenv("OKX_BASE_URL", "https://openapi.okx.com").rstrip("/")
SYMBOLS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]

LEVERAGE = Decimal("10")
MARGIN_USDT = Decimal("5")
TAKE_PROFIT = Decimal("0.06")
STOP_LOSS = Decimal("0.03")

STATE_FILE = "swap_state.json"
HTTP_TIMEOUT = 20
POLL_TRIES = 15
POLL_SECONDS = 2

LIVE = os.getenv("LIVE_TRADING", "false").lower() == "true"
CONFIRM = os.getenv("CONFIRM_10X_LIVE", "").strip()

KEY = os.getenv("OKX_API_KEY", "").strip()
SECRET = os.getenv("OKX_API_SECRET", "").strip()
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "").strip()


# ================= 基础工具 =================
def D(x):
    return Decimal(str(x))


def now():
    return datetime.now(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def api(method, path, params=None, body=None, private=False):
    method = method.upper()
    query = urllib.parse.urlencode(params or {})
    request_path = path + (("?" + query) if query else "")

    body_text = (
        json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        if body is not None else ""
    )

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "OKX-Swap-Bot/3.0",
    }

    if private:
        if not KEY or not SECRET or not PASSPHRASE:
            raise RuntimeError("API密钥不完整")

        ts = now()
        sign_text = ts + method + request_path + body_text
        sign = base64.b64encode(
            hmac.new(
                SECRET.encode(),
                sign_text.encode(),
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
        data=body_text.encode() if body is not None else None,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as res:
            raw = res.read().decode()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:600]
        raise RuntimeError(f"HTTP {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络连接失败: {e.reason}") from None

    try:
        result = json.loads(raw)
    except Exception:
        raise RuntimeError("OKX返回内容不是有效JSON") from None

    if result.get("code") != "0":
        raise RuntimeError(
            f"OKX错误 {result.get('code')}: {result.get('msg')}"
        )

    return result.get("data", [])


def get(path, params=None, private=True):
    return api("GET", path, params=params, private=private)


def post(path, body):
    return api("POST", path, body=body, private=True)


# ================= 状态文件 =================
def load_state():
    if not os.path.exists(STATE_FILE):
        return {"positions": {}, "pending": None, "halted": False}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
    except Exception as e:
        raise RuntimeError(f"状态文件损坏，停止交易: {e}") from None

    if not isinstance(s, dict):
        raise RuntimeError("状态文件格式错误")

    if s.get("halted"):
        raise RuntimeError(
            "机器人处于故障锁定状态。必须先人工核对OKX持仓和委托。"
        )

    positions = s.get("positions", {})
    if not isinstance(positions, dict):
        raise RuntimeError("持仓状态格式错误")

    for key in positions:
        if ":" not in key:
            raise RuntimeError("发现旧版持仓记录，拒绝自动迁移")

    s.setdefault("positions", {})
    s.setdefault("pending", None)
    s.setdefault("halted", False)
    return s


def save_state(s):
    temp = STATE_FILE + ".tmp"
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(temp, STATE_FILE)


def halt(reason):
    s = {
        "positions": {},
        "pending": {"reason": reason, "time": now()},
        "halted": True,
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                old = json.load(f)
            if isinstance(old, dict):
                s["positions"] = old.get("positions", {})
        except Exception:
            pass
    save_state(s)
    raise RuntimeError("交易锁定: " + reason)


def key_for(inst, side):
    return inst + ":" + side


# ================= 账户及持仓 =================
def check_account():
    rows = get("/api/v5/account/config")
    if not rows or rows[0].get("posMode") != "long_short_mode":
        raise RuntimeError("账户必须为双向持仓模式 long_short_mode")


def positions(inst):
    rows = get(
        "/api/v5/account/positions",
        {"instId": inst}
    )
    result = {}
    for p in rows:
        size = D(p.get("pos", "0"))
        side = p.get("posSide")
        if size == 0:
            continue
        if side not in ("long", "short"):
            raise RuntimeError(f"{inst}持仓方向无法识别")
        result[side] = p
    return result


def set_leverage(inst):
    for side in ("long", "short"):
        rows = post("/api/v5/account/set-leverage", {
            "instId": inst,
            "lever": str(LEVERAGE),
            "mgnMode": "isolated",
            "posSide": side,
        })
        if not rows:
            raise RuntimeError(f"{inst}设置杠杆未返回结果")


# ================= 合约信息 =================
def instrument(inst):
    rows = get(
        "/api/v5/public/instruments",
        {"instType": "SWAP", "instId": inst},
        private=False,
    )
    if not rows:
        raise RuntimeError(f"找不到合约 {inst}")

    x = rows[0]
    if x.get("state") != "live":
        raise RuntimeError(f"{inst}当前不可交易")
    if x.get("settleCcy") != "USDT":
        raise RuntimeError(f"{inst}不是USDT结算合约")

    return x


def price(inst):
    rows = get(
        "/api/v5/market/ticker",
        {"instId": inst},
        private=False,
    )
    if not rows or not rows[0].get("last"):
        raise RuntimeError(f"{inst}行情获取失败")
    return D(rows[0]["last"])


def round_step(v, step, mode=ROUND_DOWN):
    return (v / step).to_integral_value(rounding=mode) * step


def size_for(info, px):
    ct_val = D(info["ctVal"])
    ct_mult = D(info.get("ctMult") or "1")
    lot = D(info["lotSz"])
    minimum = D(info["minSz"])

    if min(ct_val, ct_mult, lot, px) <= 0:
        raise RuntimeError("合约参数异常")

    notional = MARGIN_USDT * LEVERAGE
    size = round_step(notional / (ct_val * ct_mult * px), lot)

    if size < minimum:
        raise RuntimeError(
            "每币5 USDT预算不足以满足最小下单量，拒绝扩大仓位"
        )

    max_size = info.get("maxMktSz")
    if max_size and size > D(max_size):
        raise RuntimeError("下单数量超过交易所市价单上限")

    return size


# ================= 信号 =================
def candles(inst, bar, limit):
    rows = get(
        "/api/v5/market/candles",
        {"instId": inst, "bar": bar, "limit": str(limit)},
        private=False,
    )
    rows = [r for r in rows if len(r) > 8 and r[8] == "1"]
    rows.reverse()
    return [D(r[4]) for r in rows]


def ema(values, period):
    if len(values) < period:
        raise RuntimeError("EMA数据不足")
    alpha = D(2) / D(period + 1)
    result = sum(values[:period]) / D(period)
    for v in values[period:]:
        result = v * alpha + result * (D(1) - alpha)
    return result


def rsi(values, period=14):
    if len(values) < period + 1:
        raise RuntimeError("RSI数据不足")

    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(x, D(0)) for x in changes]
    losses = [max(-x, D(0)) for x in changes]

    ag = sum(gains[:period]) / D(period)
    al = sum(losses[:period]) / D(period)

    for i in range(period, len(changes)):
        ag = (ag * D(period - 1) + gains[i]) / D(period)
        al = (al * D(period - 1) + losses[i]) / D(period)

    if al == 0:
        return D(100)
    return D(100) - D(100) / (D(1) + ag / al)


def signal(inst):
    five = candles(inst, "5m", 100)
    fifteen = candles(inst, "15m", 60)

    if len(five) < 40 or len(fifteen) < 25:
        print(inst, "K线不足")
        return None

    e5 = ema(five, 5)
    e13 = ema(five, 13)
    r = rsi(five, 14)
    e20 = ema(fifteen, 20)

    print(
        inst,
        "EMA5", e5,
        "EMA13", e13,
        "RSI", r,
        "15m EMA20", e20,
    )

    if e5 > e13 and five[-1] > e13 and fifteen[-1] > e20 and r >= 52:
        return "long"

    if e5 < e13 and five[-1] < e13 and fifteen[-1] < e20 and r <= 48:
        return "short"

    return None


# ================= 订单查询 =================
def order_detail(inst, ord_id=None, cl_ord_id=None):
    params = {"instId": inst}
    if ord_id:
        params["ordId"] = ord_id
    elif cl_ord_id:
        params["clOrdId"] = cl_ord_id
    else:
        raise RuntimeError("查询订单缺少ordId/clOrdId")
    rows = get("/api/v5/trade/order", params)
    if not rows:
        raise RuntimeError("无法查询开仓订单详情")
    return rows[0]


def algo_detail(algo_cl_ord_id):
    # 附带策略单成交后，客户自定义ID对应 algoClOrdId，
    # attachAlgoId 不能直接当普通 algoId 查询。
    rows = get(
        "/api/v5/trade/order-algo",
        {"algoClOrdId": algo_cl_ord_id},
    )
    if not rows:
        raise RuntimeError("无法查询止盈止损策略委托详情")
    return rows[0]


def wait_order(inst, ord_id):
    for _ in range(POLL_TRIES):
        x = order_detail(inst, ord_id)
        if x.get("state") == "filled":
            return x
        if x.get("state") in ("canceled", "mmp_canceled"):
            raise RuntimeError("开仓订单已取消")
        time.sleep(POLL_SECONDS)
    raise RuntimeError("开仓订单未能确认完全成交")


def wait_position(inst, side):
    for _ in range(POLL_TRIES):
        p = positions(inst)
        if side in p:
            return p[side]
        time.sleep(POLL_SECONDS)
    return None


# ================= 保护单核实及修正 =================
def verify_and_fix_protection(inst, side, algo_cl_ord_id, avg_px, tick):
    expected_tp = (
        avg_px * (D(1) + TAKE_PROFIT)
        if side == "long"
        else avg_px * (D(1) - TAKE_PROFIT)
    )
    expected_sl = (
        avg_px * (D(1) - STOP_LOSS)
        if side == "long"
        else avg_px * (D(1) + STOP_LOSS)
    )

    expected_tp = round_step(
        expected_tp, tick,
        ROUND_UP if side == "long" else ROUND_DOWN
    )
    expected_sl = round_step(
        expected_sl, tick,
        ROUND_DOWN if side == "long" else ROUND_UP
    )

    last_error = None
    for attempt in range(POLL_TRIES):
        try:
            a = algo_detail(algo_cl_ord_id)
            if a.get("state") != "live":
                raise RuntimeError(
                    "止盈止损策略单不是live状态: " + str(a.get("state"))
                )

            tp = D(a.get("tpTriggerPx") or "0")
            sl = D(a.get("slTriggerPx") or "0")
            tp_ord_px = str(a.get("tpOrdPx") or "")
            sl_ord_px = str(a.get("slOrdPx") or "")
            tp_type = str(a.get("tpTriggerPxType") or "")
            sl_type = str(a.get("slTriggerPxType") or "")

            correct = (
                tp == expected_tp and sl == expected_sl
                and tp_ord_px == "-1" and sl_ord_px == "-1"
                and tp_type == "mark" and sl_type == "mark"
            )

            if not correct:
                changed = post("/api/v5/trade/amend-algos", {
                    "instId": inst,
                    "algoClOrdId": algo_cl_ord_id,
                    "newTpTriggerPx": str(expected_tp),
                    "newTpOrdPx": "-1",
                    "newTpTriggerPxType": "mark",
                    "newSlTriggerPx": str(expected_sl),
                    "newSlOrdPx": "-1",
                    "newSlTriggerPxType": "mark",
                })
                if not changed or changed[0].get("sCode") != "0":
                    raise RuntimeError(
                        "修正止盈止损失败: " +
                        str(changed[0] if changed else "空响应")
                    )
                time.sleep(1)
                a = algo_detail(algo_cl_ord_id)

            if (
                a.get("state") == "live"
                and D(a.get("tpTriggerPx") or "0") == expected_tp
                and D(a.get("slTriggerPx") or "0") == expected_sl
                and str(a.get("tpOrdPx") or "") == "-1"
                and str(a.get("slOrdPx") or "") == "-1"
                and str(a.get("tpTriggerPxType") or "") == "mark"
                and str(a.get("slTriggerPxType") or "") == "mark"
            ):
                print("止盈止损双重核验通过:", algo_cl_ord_id)
                print("实际均价:", avg_px)
                print("止盈价:", expected_tp, "止损价:", expected_sl)
                return {
                    "algoClOrdId": algo_cl_ord_id,
                    "avgPx": str(avg_px),
                    "tp": str(expected_tp),
                    "sl": str(expected_sl),
                    "triggerType": "mark",
                    "orderType": "market",
                }
            raise RuntimeError("止盈止损回读值与设定值不一致")
        except Exception as e:
            last_error = e
            if attempt < POLL_TRIES - 1:
                time.sleep(POLL_SECONDS)

    raise RuntimeError("止盈止损无法确认: " + str(last_error))


# ================= 故障时尝试平仓 =================
def emergency_close(inst, side, entry_ord_id=None, entry_cl_ord_id=None,
                    algo_cl_ord_id=None):
    # 先撤销尚未完成的开仓主单，防止剩余数量在平仓后再次成交。
    if entry_ord_id or entry_cl_ord_id:
        try:
            od = order_detail(inst, entry_ord_id, entry_cl_ord_id)
            if od.get("state") in ("live", "partially_filled"):
                cancel_params = {"instId": inst}
                if od.get("ordId"):
                    cancel_params["ordId"] = od["ordId"]
                else:
                    cancel_params["clOrdId"] = entry_cl_ord_id
                canceled = post("/api/v5/trade/cancel-order", cancel_params)
                if not canceled or canceled[0].get("sCode") != "0":
                    raise RuntimeError("撤销未完成开仓主单失败")

                for _ in range(POLL_TRIES):
                    time.sleep(1)
                    od = order_detail(inst, entry_ord_id, entry_cl_ord_id)
                    if od.get("state") in ("canceled", "filled", "mmp_canceled"):
                        break

                if od.get("state") not in ("canceled", "filled", "mmp_canceled"):
                    raise RuntimeError("开仓主单撤销状态无法确认")
        except Exception as e:
            raise RuntimeError("无法确认开仓主单已停止: " + str(e))

    # 主单停止后，才处理已成交仓位。
    for _ in range(POLL_TRIES):
        p = positions(inst)
        if side not in p:
            print("故障处理：交易所当前没有该方向持仓")
            break

        size = abs(D(p[side]["pos"]))
        if size <= 0:
            break

        close_side = "sell" if side == "long" else "buy"
        result = post("/api/v5/trade/order", {
            "instId": inst,
            "tdMode": "isolated",
            "posSide": side,
            "side": close_side,
            "ordType": "market",
            "sz": str(size),
            "clOrdId": uuid.uuid4().hex[:24],
        })
        if not result or result[0].get("sCode") != "0":
            raise RuntimeError("紧急市价平仓请求失败，请立即检查OKX持仓")

        flat = False
        for _ in range(POLL_TRIES):
            time.sleep(POLL_SECONDS)
            if side not in positions(inst):
                flat = True
                break
        if flat:
            break

    if side in positions(inst):
        raise RuntimeError("紧急平仓后仍检测到持仓，必须立即人工处理")

    # 确认仓位归零后，再撤销仍处于live状态的附带保护策略。
    if algo_cl_ord_id:
        try:
            a = algo_detail(algo_cl_ord_id)
            if a.get("state") == "live" and a.get("algoId"):
                canceled = post("/api/v5/trade/cancel-algos", [{
                    "instId": inst,
                    "algoId": a["algoId"],
                }])
                if not canceled or canceled[0].get("sCode") != "0":
                    raise RuntimeError("撤销孤立止盈止损策略失败")
        except Exception as e:
            print("保护策略撤销未能确认，请人工检查OKX策略委托:", e)

    print("紧急平仓已由交易所持仓回读确认归零")


# ================= 开仓 =================
def enter(inst, side, info):
    s = load_state()
    if s.get("pending"):
        raise RuntimeError("存在未处理订单，禁止开仓")

    current = positions(inst)
    if side in current:
        print(inst, side, "已有仓位，跳过")
        return

    px = price(inst)
    size = size_for(info, px)
    tick = D(info["tickSz"])

    client_id = uuid.uuid4().hex[:24]
    algo_client_id = uuid.uuid4().hex[:24]

    s["pending"] = {
        "instId": inst,
        "side": side,
        "clOrdId": client_id,
        "algoClOrdId": algo_client_id,
        "time": now(),
    }
    save_state(s)

    close_side = "buy" if side == "long" else "sell"

    if side == "long":
        tp = round_step(px * (D(1) + TAKE_PROFIT), tick, ROUND_UP)
        sl = round_step(px * (D(1) - STOP_LOSS), tick, ROUND_DOWN)
    else:
        tp = round_step(px * (D(1) - TAKE_PROFIT), tick, ROUND_DOWN)
        sl = round_step(px * (D(1) + STOP_LOSS), tick, ROUND_UP)

    ord_id = None
    try:
        result = post("/api/v5/trade/order", {
            "instId": inst,
            "tdMode": "isolated",
            "posSide": side,
            "side": close_side,
            "ordType": "market",
            "sz": str(size),
            "clOrdId": client_id,
            "attachAlgoOrds": [{
                "attachAlgoClOrdId": algo_client_id,
                "tpTriggerPx": str(tp),
                "tpOrdPx": "-1",
                "tpTriggerPxType": "mark",
                "slTriggerPx": str(sl),
                "slOrdPx": "-1",
                "slTriggerPxType": "mark",
            }],
        })

        if not result or result[0].get("sCode") != "0":
            raise RuntimeError("开仓订单未获交易所确认")

        ord_id = result[0].get("ordId")
        if not ord_id:
            raise RuntimeError("开仓订单缺少ordId")

        filled = wait_order(inst, ord_id)
        avg_px = D(filled.get("avgPx") or "0")
        filled_size = D(filled.get("accFillSz") or "0")

        if avg_px <= 0 or filled_size <= 0:
            raise RuntimeError("成交均价或成交数量无效")

        pos = wait_position(inst, side)
        if not pos:
            raise RuntimeError("订单成交后未检测到对应持仓")

        protection = verify_and_fix_protection(
            inst, side, algo_client_id, avg_px, tick
        )

        s = load_state()
        s["positions"][key_for(inst, side)] = {
            "ordId": ord_id,
            "size": str(abs(D(pos["pos"]))),
            "avgPx": str(avg_px),
            "protection": protection,
            "time": now(),
        }
        s["pending"] = None
        save_state(s)

        print("开仓及保护单核验完成:", inst, side)

    except Exception as e:
        print("开仓流程异常:", str(e))
        try:
            emergency_close(
                inst, side, ord_id, client_id, algo_client_id
            )
        except Exception as close_error:
            print("紧急平仓也未能确认:", close_error)

        halt(
            f"{inst} {side} 开仓或保护单验证失败。"
            "已停止后续交易，必须人工核对OKX。"
        )


# ================= 平仓 =================
def close_position(inst, side, pos):
    s = load_state()
    if s.get("pending"):
        raise RuntimeError("存在未处理订单，禁止平仓操作")

    size = abs(D(pos["pos"]))
    close_side = "sell" if side == "long" else "buy"
    client_id = uuid.uuid4().hex[:24]

    s["pending"] = {
        "instId": inst,
        "side": side,
        "action": "close",
        "clOrdId": client_id,
        "time": now(),
    }
    save_state(s)

    result = post("/api/v5/trade/order", {
        "instId": inst,
        "tdMode": "isolated",
        "posSide": side,
        "side": close_side,
        "ordType": "market",
        "sz": str(size),
        "clOrdId": client_id,
    })

    if not result or result[0].get("sCode") != "0":
        halt(f"{inst} {side} 平仓订单未获确认")

    for _ in range(POLL_TRIES):
        time.sleep(POLL_SECONDS)
        if side not in positions(inst):
            s = load_state()
            s["positions"].pop(key_for(inst, side), None)
            s["pending"] = None
            save_state(s)
            print("平仓确认:", inst, side)
            return

    halt(f"{inst} {side} 平仓后仍有仓位，需人工检查")


# ================= 主循环 =================
def run_once():
    s = load_state()
    if s.get("pending"):
        raise RuntimeError("上次执行留下未处理订单，停止运行")

    check_account()

    # 先核对所有实际仓位，禁止自动接管陌生仓位
    for inst in SYMBOLS:
        actual = positions(inst)
        for side in actual:
            if key_for(inst, side) not in s["positions"]:
                raise RuntimeError(
                    f"{inst}存在未登记的{side}仓位，停止交易"
                )

    for inst in SYMBOLS:
        info = instrument(inst)
        actual = positions(inst)
        sig = signal(inst)

        opposite = (
            "short" if sig == "long"
            else "long" if sig == "short"
            else None
        )

        if opposite and opposite in actual:
            close_position(inst, opposite, actual[opposite])
            continue

        if sig and sig in actual:
            print(inst, sig, "已有仓位，不重复开仓")
            continue

        if sig:
            enter(inst, sig, info)
        else:
            print(inst, "暂无信号")


def main():
    if not KEY or not SECRET or not PASSPHRASE:
        raise RuntimeError("缺少OKX API凭据")

    if not LIVE or CONFIRM != "YES":
        raise RuntimeError(
            "实盘开关未确认；当前程序不会下单。"
        )

    print("OKX双向永续策略启动")
    print("标的:", SYMBOLS)
    print("逐仓杠杆:", LEVERAGE)
    print("每币保证金预算:", MARGIN_USDT)
    print("止盈:", TAKE_PROFIT)
    print("止损:", STOP_LOSS)

    check_account()

    s = load_state()
    if s.get("pending"):
        raise RuntimeError("存在未完成订单，停止运行")

    for inst in SYMBOLS:
        actual = positions(inst)
        for side in actual:
            if key_for(inst, side) not in s["positions"]:
                raise RuntimeError(
                    f"{inst}有未登记仓位，拒绝运行"
                )

    for inst in SYMBOLS:
        set_leverage(inst)

    run_once()
    print("本轮执行完成")


if __name__ == "__main__":
    main()

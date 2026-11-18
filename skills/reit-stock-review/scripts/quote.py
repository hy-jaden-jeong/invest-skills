#!/usr/bin/env python3
"""reit-stock-review: 주가를 받아 facts의 price 칸에 넣는다.

명령
  price T... [--period P]     key 없는 Yahoo chart endpoint에서 마지막 거래일 종가를 받는다.
                              실패(HTTP 오류, 형식 변화, 빈 값)하면 missing(웹 채우기로 넘긴다)
  web T --period P (--value V --as-of D --url U --quote Q [--secondary] | --missing 이유)
                              웹에서 찾은 가격을 넣는다. URL과 원문 문장이 없으면 거절한다
  --check                     network 없이 확인한다

--period를 주면 $INVEST_DATA_DIR/tickers/<T>/facts/<P>.json의 price 칸을 바꾼다(파일이 있어야 한다).
exit: 0 = 다 채움, 2 = 일부 missing, 1 = 입력·network 오류.
Python 3 stdlib만 쓴다(3.9 이상).
"""
import argparse
import datetime as dt
import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reit_calc  # noqa: E402  같은 skill 폴더 안의 script

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=5d&interval=1d"
# 문서에 없는 endpoint다. 일반 브라우저 User-Agent가 아니면 거절하는 일이 있어 이 값을 쓴다(사람·레포 정보 없음).
YAHOO_UA = "Mozilla/5.0"


def log(msg):
    print(msg, file=sys.stderr)


def facts_path(t, period):
    return os.path.join(reit_calc.data_dir(), "tickers", t.upper(), "facts", "%s.json" % period)


def age_status(as_of, today, max_days):
    age = (today - as_of).days
    if age > max_days:
        return "stale", "가격 기준일이 %d일 전(허용 %d일)" % (age, max_days)
    return None, ""


def yahoo_symbol(t):
    """Yahoo는 class 주식의 점을 줄표로 쓴다(BRK.B → BRK-B)."""
    return t.upper().replace(".", "-")


def yahoo_url(symbol):
    return YAHOO_CHART % urllib.parse.quote(yahoo_symbol(symbol), safe="-^")


def yahoo_get(symbol):
    url = yahoo_url(symbol)
    req = urllib.request.Request(url, headers={"User-Agent": YAHOO_UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise IOError("Yahoo chart HTTP %d" % e.code)
    except (urllib.error.URLError, OSError) as e:
        raise IOError("Yahoo chart network 오류 %s" % getattr(e, "reason", e))


def from_yahoo(symbol, body, today, max_days, now=None):
    """Yahoo chart JSON → 값 envelope. 마지막으로 끝난 거래일의 종가를 쓴다.
    장 중이면 오늘 bar의 close는 종가가 아니라 그때 가격이라 건너뛴다.
    형식이 다르거나 값이 비면 missing(0이나 추측으로 채우지 않는다)."""
    now = now if now is not None else dt.datetime.now(dt.timezone.utc).timestamp()
    url = yahoo_url(symbol)
    try:
        doc = json.loads(body)
        chart = doc["chart"]
        if chart.get("error") or not chart.get("result"):
            err = chart.get("error") or {}
            return reit_calc.missing("Yahoo chart가 %s 값을 주지 않음(%s)" % (symbol, err.get("description") or err.get("code") or "빈 result"))
        r = chart["result"][0]
        meta = r["meta"]
        ts = r["timestamp"]
        closes = r["indicators"]["quote"][0]["close"]
        if not isinstance(ts, list) or not isinstance(closes, list) or len(ts) != len(closes):
            raise ValueError("timestamp와 close 길이가 다름")
        if str(meta.get("symbol", "")).upper() != yahoo_symbol(symbol):
            raise ValueError("다른 symbol이 옴(%r)" % meta.get("symbol"))
        if meta.get("currency") != "USD":
            return reit_calc.missing("Yahoo chart %s 통화가 USD가 아님(%r)" % (symbol, meta.get("currency")))
        off = int(meta.get("gmtoffset") or 0)
        reg = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
        rstart, rend = reg.get("start"), reg.get("end")
        rmt = meta.get("regularMarketTime")
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as e:
        return reit_calc.missing("Yahoo chart 형식이 바뀜(%s): 웹 채우기(quote.py web)로 넣는다" % e)
    today_local = dt.datetime.fromtimestamp(now + off, dt.timezone.utc).date()
    pick = None
    for t, c in zip(ts, closes):
        if not reit_calc.is_finite(t) or not reit_calc.is_finite(c) or c <= 0:
            continue
        day = dt.datetime.fromtimestamp(t + off, dt.timezone.utc).date()
        if reit_calc.is_finite(rstart) and reit_calc.is_finite(rend):
            closed = t < rstart or now >= rend   # 지금 세션 bar는 장이 끝난 뒤에만 종가다
        else:
            closed = day < today_local           # 세션 정보가 없으면 오늘 bar는 쓰지 않는다
        if closed:
            pick = (t, c, day)
    if pick is None:
        return reit_calc.missing("Yahoo chart에 %s의 끝난 거래일 종가가 없음: 웹 채우기(quote.py web)로 넣는다" % symbol)
    t, c, as_of = pick
    if as_of > today:
        return reit_calc.missing("Yahoo chart 기준일(%s)이 오늘(%s)보다 늦음" % (as_of.isoformat(), today.isoformat()))
    status, note = age_status(as_of, today, max_days)
    c = round(float(c), 4)
    return {"value": c, "unit": "USD/share", "status": status or "reported", "as_of": as_of.isoformat(),
            "source": "yahoo:chart",
            "locator": "%s as_of=%s close=%s timestamp=%s regularMarketTime=%s" % (url, as_of.isoformat(), c, t, rmt),
            "note": note or "Yahoo chart 마지막 거래일 종가(거래소 종가를 옮긴 시세 feed, key 없음)"}


def get_price(symbol, today, max_days, fetch=yahoo_get, now=None):
    try:
        return from_yahoo(symbol, fetch(symbol), today, max_days, now)
    except (IOError, ValueError) as e:
        return reit_calc.missing("Yahoo chart 받기 실패(%s): 웹 채우기(quote.py web)로 넣는다" % e)


def web_price(today, max_days, value=None, as_of=None, url=None, quote=None, secondary=False, missing=None):
    """웹 채우기 규칙: URL·원문 문장이 없으면 거절(None, 이유). 2차 자료는 estimated, 오래되면 stale.
    둘 다면 더 나쁜 estimated를 지키고 나이는 note에 쓴다(나쁜 순서 reported < derived < stale < estimated < missing)."""
    if missing is not None:
        if not missing.strip():
            return None, "--missing에는 이유를 쓴다"
        return reit_calc.missing(missing.strip()), ""
    if not url or not url.startswith(("http://", "https://")):
        return None, "--url이 없거나 http(s)로 시작하지 않음. 출처 URL이 없으면 --missing으로 둔다"
    if not quote or not quote.strip():
        return None, "--quote(원문 문장)가 없음. 원문 문장이 없으면 --missing으로 둔다"
    try:
        v = float(value)
        d = reit_calc.parse_date(as_of)
    except (TypeError, ValueError):
        return None, "--value는 숫자, --as-of는 YYYY-MM-DD"
    if not reit_calc.is_finite(v) or v <= 0:
        return None, "--value는 0보다 큰 유한한 숫자여야 함"
    if d > today:
        return None, "--as-of 날짜(%s)가 오늘(%s)보다 늦음" % (d.isoformat(), today.isoformat())
    status, note = age_status(d, today, max_days)
    kind = "2차 자료" if secondary else "1차 자료"
    base = "estimated" if secondary else "reported"
    return {"value": v, "unit": "USD/share", "status": reit_calc.worst([base, status or base]),
            "as_of": d.isoformat(), "source": "web:" + url, "locator": quote.strip(),
            "note": "; ".join(x for x in (kind, note) if x)}, ""


def put_price(path, env):
    """facts의 price 칸에 넣는다. 새 값이 missing이면 앞서 넣은 값을 덮지 않는다."""
    if not os.path.exists(path):
        log("facts 파일이 없음: %s (먼저 edgar.py xbrl을 돌린다)" % path)
        return False
    doc = reit_calc.read_json(path)
    cur = doc.setdefault("facts", {}).get("price") or {}
    if env["status"] == "missing" and cur.get("value") is not None:
        log("새 가격이 missing이라 앞서 넣은 가격(%s, %s)을 남김" % (cur.get("value"), cur.get("as_of")))
        return True
    doc["facts"]["price"] = env
    reit_calc.write_json(doc, path)
    return True


def cmd_price(args, today=None, fetch=yahoo_get):
    today = today or dt.date.today()
    th = reit_calc.load_thresholds()
    out, code = {}, 0
    for t in args.tickers:
        env = get_price(t.upper(), today, th["price_max_age_days"], fetch)
        out[t.upper()] = env
        if env["status"] == "missing":
            code = 2
        if args.period and not put_price(facts_path(t, args.period), env):
            return 1
    print(json.dumps(out, ensure_ascii=False, indent=2, allow_nan=False))
    return code


def cmd_web(args, today=None):
    today = today or dt.date.today()
    th = reit_calc.load_thresholds()
    env, why = web_price(today, th["price_max_age_days"], value=args.value, as_of=args.as_of, url=args.url,
                         quote=args.quote, secondary=args.secondary, missing=args.missing)
    if env is None:
        log(why)
        return 1
    if not put_price(args.facts or facts_path(args.ticker, args.period), env):
        return 1
    log("%s price: %s %s (%s)" % (args.ticker.upper(), env["status"], env["value"], env.get("as_of")))
    return 0 if env["status"] != "missing" else 2


def main(argv):
    p = argparse.ArgumentParser(prog="quote.py", description="주가 → facts price")
    p.add_argument("--check", action="store_true", help="network 없이 확인")
    sub = p.add_subparsers(dest="cmd")
    pr = sub.add_parser("price")
    pr.add_argument("tickers", nargs="+")
    pr.add_argument("--period")
    w = sub.add_parser("web")
    w.add_argument("ticker")
    w.add_argument("--period")
    w.add_argument("--facts", help="기본: $INVEST_DATA_DIR/tickers/<T>/facts/<P>.json")
    w.add_argument("--value")
    w.add_argument("--as-of")
    w.add_argument("--url")
    w.add_argument("--quote")
    w.add_argument("--secondary", action="store_true", help="기사·시세 사이트 같은 2차 자료")
    w.add_argument("--missing", help="못 찾았을 때 이유")
    try:
        args = p.parse_args(argv)
    except SystemExit as e:
        return 0 if e.code == 0 else 1
    if args.check:
        return run_check()
    if args.cmd == "price":
        return cmd_price(args)
    if args.cmd == "web":
        if not args.facts and not args.period:
            log("--period나 --facts가 필요함")
            return 1
        return cmd_web(args)
    p.print_help(sys.stderr)
    return 1


# ---------------------------------------------------------------- --check

def run_check():
    fails = []

    def ok(cond, label):
        if not cond:
            fails.append(label)
        log(("PASS " if cond else "FAIL ") + label)

    today = dt.date(2026, 11, 18)
    th = reit_calc.load_thresholds()
    md = th["price_max_age_days"]
    fx = os.path.join(reit_calc.skill_root(), "tests", "fixtures", "yahoo")
    with open(os.path.join(fx, "chart_TEST.json"), encoding="utf-8") as f:
        body = f.read()
    with open(os.path.join(fx, "chart_NOPE.json"), encoding="utf-8") as f:
        nope = f.read()
    j = json.loads(body)
    reg = j["chart"]["result"][0]["meta"]["currentTradingPeriod"]["regular"]
    before_open = reg["start"] - 3600          # 2026-11-18 08:30 ET: 어제 장은 끝났고 오늘 장은 아직
    e = from_yahoo("TEST", body, today, md, now=before_open)
    ok(e["value"] == 55.12 and e["as_of"] == "2026-11-17" and e["status"] == "reported"
       and e["source"] == "yahoo:chart" and "close=55.12" in e["locator"] and "as_of=2026-11-17" in e["locator"]
       and e["locator"].startswith("https://query1.finance.yahoo.com/v8/finance/chart/TEST?range=5d&interval=1d"),
       "Yahoo chart: 마지막 거래일 종가, 기준일, locator에 URL·기준일")
    ok(from_yahoo("TEST", body, dt.date(2026, 11, 30), md, now=before_open)["status"] == "stale",
       "오래된 Yahoo 종가는 stale(%d일 규칙)" % md)
    # 장 중: 오늘(11-18) bar가 생겼지만 close는 그때 가격이라 쓰지 않는다
    live = json.loads(body)
    rr = live["chart"]["result"][0]
    rr["timestamp"].append(reg["start"])
    rr["indicators"]["quote"][0]["close"].append(99.0)
    in_session = reg["start"] + 3600
    e2 = from_yahoo("TEST", json.dumps(live), today, md, now=in_session)
    ok(e2["value"] == 55.12 and e2["as_of"] == "2026-11-17", "장 중이면 오늘 bar를 건너뛰고 어제 종가")
    e3 = from_yahoo("TEST", json.dumps(live), today, md, now=reg["end"] + 60)
    ok(e3["value"] == 99.0 and e3["as_of"] == "2026-11-18", "장이 끝난 뒤에는 오늘 종가")
    ok(from_yahoo("NOPE", nope, today, md, now=before_open)["status"] == "missing", "모르는 symbol(error 응답)은 missing")
    bad = json.loads(body)
    del bad["chart"]["result"][0]["indicators"]
    ok(from_yahoo("TEST", json.dumps(bad), today, md, now=before_open)["status"] == "missing"
       and "형식" in from_yahoo("TEST", json.dumps(bad), today, md, now=before_open)["note"], "형식이 바뀌면 missing")
    ok(from_yahoo("TEST", "<html>blocked</html>", today, md, now=before_open)["status"] == "missing", "JSON이 아니면 missing")
    empty = json.loads(body)
    empty["chart"]["result"][0]["indicators"]["quote"][0]["close"] = [None] * 5
    ok(from_yahoo("TEST", json.dumps(empty), today, md, now=before_open)["status"] == "missing", "close가 비면 missing")
    nan = json.loads(body)
    nan["chart"]["result"][0]["indicators"]["quote"][0]["close"] = [float("nan")] * 5
    ok(from_yahoo("TEST", json.dumps(nan), today, md, now=before_open)["status"] == "missing", "close가 NaN이면 missing")
    other = json.loads(body)
    other["chart"]["result"][0]["meta"]["symbol"] = "XYZ"
    ok(from_yahoo("TEST", json.dumps(other), today, md, now=before_open)["status"] == "missing", "다른 symbol이 오면 missing")
    krw = json.loads(body)
    krw["chart"]["result"][0]["meta"]["currency"] = "KRW"
    ok(from_yahoo("TEST", json.dumps(krw), today, md, now=before_open)["status"] == "missing", "USD가 아니면 missing")
    ok(yahoo_url("BRK.B").endswith("/BRK-B?range=5d&interval=1d"), "class 주식 점은 줄표로(BRK.B → BRK-B)")

    def down(sym):
        raise IOError("Yahoo chart HTTP 429 (가상)")
    d = get_price("TEST", today, md, down)
    ok(d["status"] == "missing" and "웹 채우기" in d["note"], "Yahoo 실패는 missing(0으로 채우지 않음), 웹 채우기로 넘김")

    class A:
        tickers, period = ["TEST", "NOPE"], None
    got = {}

    def fake(sym):
        got[sym] = True
        return body if sym == "TEST" else nope
    import contextlib
    import io as _io
    with contextlib.redirect_stdout(_io.StringIO()):
        ok(cmd_price(A, today=today, fetch=fake) == 2 and set(got) == {"TEST", "NOPE"},
           "price: 한 종목이라도 missing이면 exit 2")

    a = dict(value="55.1", as_of="2026-11-17", url="https://www.nyse.com/quote/XNYS:TEST (가상)", quote="TEST 55.10 (가상)")
    ok(web_price(today, md, **dict(a, url=""))[0] is None and web_price(today, md, **dict(a, quote=" "))[0] is None,
       "웹 가격은 URL·원문 문장이 없으면 거절")
    ok(web_price(today, md, **dict(a, value="0"))[0] is None, "0 이하 가격은 거절")
    e, _ = web_price(today, md, **a)
    ok(e["status"] == "reported" and e["source"].startswith("web:https://") and e["locator"] == "TEST 55.10 (가상)",
       "1차 자료 웹 가격은 reported, source=web:URL, locator=원문")
    ok(web_price(today, md, secondary=True, **a)[0]["status"] == "estimated", "2차 자료 웹 가격은 estimated")
    ok(web_price(today, md, **dict(a, as_of="2026-11-01"))[0]["status"] == "stale", "오래된 웹 가격은 stale")
    ok(web_price(today, md, missing="찾지 못함")[0]["status"] == "missing", "--missing")
    ok(all(web_price(today, md, **dict(a, value=v))[0] is None for v in ("nan", "inf", "-inf")),
       "NaN·무한대 가격은 거절")
    old2 = web_price(today, md, secondary=True, **dict(a, as_of="2026-11-01"))[0]
    ok(old2["status"] == "estimated" and "허용 %d일" % md in old2["note"],
       "오래된 2차 자료는 estimated를 지킨다(estimated가 stale보다 나쁨), note에 나이")
    ok(web_price(today, md, **dict(a, as_of="2026-11-19"))[0] is None, "기준일이 오늘보다 늦으면 거절")

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "f.json")
        reit_calc.write_json({"schema": reit_calc.FACTS_SCHEMA, "ticker": "TEST", "period": "2026Q3", "facts": {}}, path)
        put_price(path, web_price(today, md, **a)[0])
        put_price(path, reit_calc.missing("Yahoo chart 실패(가상)"))
        ok(reit_calc.read_json(path)["facts"]["price"]["value"] == 55.1, "missing이 앞서 넣은 가격을 덮지 않음")
        ok(main(["web", "TEST", "--facts", path, "--value", "55", "--as-of", "2026-11-17", "--url", "https://x (가상)"]) == 1,
           "CLI: --quote 없으면 exit 1")
        ok(not put_price(os.path.join(tmp, "none.json"), e), "facts 파일이 없으면 넣지 않음")

    log("check: %d개 실패" % len(fails) if fails else "check: 모두 통과")
    return 2 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

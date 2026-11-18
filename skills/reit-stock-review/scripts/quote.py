#!/usr/bin/env python3
"""reit-stock-review: 주가를 받아 facts의 price 칸에 넣는다.

명령
  price T... [--period P]     FINNHUB_API_KEY가 있으면 Finnhub /quote, 없으면 missing(웹 채우기로 넘긴다)
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

FINNHUB = "https://finnhub.io/api/v1/quote"


def log(msg):
    print(msg, file=sys.stderr)


def facts_path(t, period):
    return os.path.join(reit_calc.data_dir(), "tickers", t.upper(), "facts", "%s.json" % period)


def age_status(as_of, today, max_days):
    age = (today - as_of).days
    if age > max_days:
        return "stale", "가격 기준일이 %d일 전(허용 %d일)" % (age, max_days)
    return None, ""


def finnhub_get(symbol, key):
    url = FINNHUB + "?" + urllib.parse.urlencode({"symbol": symbol, "token": key})
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise IOError("Finnhub HTTP %d" % e.code)
    except (urllib.error.URLError, OSError) as e:
        raise IOError("Finnhub network 오류 %s" % getattr(e, "reason", e))


def from_finnhub(symbol, body, today, max_days):
    """Finnhub /quote JSON → 값 envelope. c는 현재가(장 마감 뒤에는 종가), t는 그 시각(unix)."""
    q = json.loads(body)
    c, t = q.get("c"), q.get("t")
    if not c or not t:
        return reit_calc.missing("Finnhub가 %s 값을 주지 않음(c=%r, t=%r)" % (symbol, c, t))
    as_of = dt.datetime.fromtimestamp(t, dt.timezone.utc).date()
    status, note = age_status(as_of, today, max_days)
    return {"value": float(c), "unit": "USD/share", "status": status or "reported", "as_of": as_of.isoformat(),
            "source": "finnhub:/quote", "locator": "%s?symbol=%s c=%s t=%s" % (FINNHUB, symbol, c, t),
            "note": note or "Finnhub 현재가(장 마감 뒤에는 종가)"}


def get_price(symbol, key, today, max_days, fetch=finnhub_get):
    if not key:
        return reit_calc.missing("FINNHUB_API_KEY 없음: 웹 채우기(quote.py web)로 넣는다")
    try:
        return from_finnhub(symbol, fetch(symbol, key), today, max_days)
    except (IOError, ValueError) as e:
        return reit_calc.missing("Finnhub 받기 실패: %s" % e)


def web_price(today, max_days, value=None, as_of=None, url=None, quote=None, secondary=False, missing=None):
    """웹 채우기 규칙: URL·원문 문장이 없으면 거절(None, 이유). 2차 자료는 estimated, 오래되면 stale."""
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
    if v <= 0:
        return None, "--value는 0보다 커야 함"
    status, note = age_status(d, today, max_days)
    kind = "2차 자료" if secondary else "1차 자료"
    return {"value": v, "unit": "USD/share", "status": status or ("estimated" if secondary else "reported"),
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


def cmd_price(args, today=None, fetch=finnhub_get):
    today = today or dt.date.today()
    th = reit_calc.load_thresholds()
    key = os.environ.get("FINNHUB_API_KEY", "").strip()
    out, code = {}, 0
    for t in args.tickers:
        env = get_price(t.upper(), key, today, th["price_max_age_days"], fetch)
        out[t.upper()] = env
        if env["status"] == "missing":
            code = 2
        if args.period and not put_price(facts_path(t, args.period), env):
            return 1
    print(json.dumps(out, ensure_ascii=False, indent=2))
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
    # 2026-11-17 21:00 UTC (가상 응답)
    body = json.dumps({"c": 55.12, "d": 0.3, "dp": 0.5, "h": 55.5, "l": 54.9, "o": 55.0, "pc": 54.82, "t": 1794949200})
    e = from_finnhub("TEST", body, today, md)
    ok(e["value"] == 55.12 and e["as_of"] == "2026-11-17" and e["status"] == "reported"
       and e["source"] == "finnhub:/quote" and "c=55.12" in e["locator"], "Finnhub /quote: c와 t로 값·기준일")
    ok(from_finnhub("TEST", body, dt.date(2026, 11, 30), md)["status"] == "stale", "오래된 Finnhub 값은 stale")
    ok(from_finnhub("NOPE", json.dumps({"c": 0, "t": 0}), today, md)["status"] == "missing", "모르는 symbol(c=0)은 missing")
    ok(get_price("TEST", "", today, md)["status"] == "missing" and "웹 채우기" in get_price("TEST", "", today, md)["note"],
       "FINNHUB_API_KEY가 없으면 missing, 웹 채우기로 넘김")

    def down(sym, key):
        raise IOError("Finnhub HTTP 429 (가상)")
    ok(get_price("TEST", "k", today, md, down)["status"] == "missing", "Finnhub 실패는 missing(0으로 채우지 않음)")

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

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "f.json")
        reit_calc.write_json({"schema": reit_calc.FACTS_SCHEMA, "ticker": "TEST", "period": "2026Q3", "facts": {}}, path)
        put_price(path, web_price(today, md, **a)[0])
        put_price(path, reit_calc.missing("Finnhub 실패(가상)"))
        ok(reit_calc.read_json(path)["facts"]["price"]["value"] == 55.1, "missing이 앞서 넣은 가격을 덮지 않음")
        ok(main(["web", "TEST", "--facts", path, "--value", "55", "--as-of", "2026-11-17", "--url", "https://x (가상)"]) == 1,
           "CLI: --quote 없으면 exit 1")
        ok(not put_price(os.path.join(tmp, "none.json"), e), "facts 파일이 없으면 넣지 않음")

    log("check: %d개 실패" % len(fails) if fails else "check: 모두 통과")
    return 2 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

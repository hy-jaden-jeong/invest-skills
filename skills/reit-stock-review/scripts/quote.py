#!/usr/bin/env python3
"""reit-stock-review: 주가를 받아 facts의 price 칸에 넣는다.

명령
  price T... [--period P]     key 없는 Yahoo chart endpoint에서 마지막 거래일 종가를 받는다.
                              실패(HTTP 오류, 형식 변화, 빈 값)하면 missing(웹 채우기로 넘긴다)
  web T --period P (--value V --as-of D --url U --quote Q [--secondary] | --missing 이유)
                              웹에서 찾은 가격을 넣는다. URL과 원문 문장이 없으면 거절한다
  dividends T [--facts F]     key 없는 Yahoo chart events=div,split에서 split 조정된 지급 내역을 받아
                              정기·특별을 가르고 연간 정기 주당 배당을 낸다. --facts면 series에 넣는다.
                              $INVEST_DATA_DIR/tickers/<T>/dividends.json도 쓴다. 실패하면 exit 1(edgar.py dividends로 대신)
  --check                     network 없이 확인한다

--period를 주면 $INVEST_DATA_DIR/tickers/<T>/facts/<P>.json의 price 칸을 바꾼다(파일이 있어야 한다).
exit: 0 = 다 채움, 2 = 일부 missing, 1 = 입력·network 오류.
정기·특별을 가르는 규칙은 references/facts-schema.md "series"의 dividend_per_share_annual에 있다.
Python 3 stdlib만 쓴다(3.9 이상).
"""
import argparse
import collections
import datetime as dt
import json
import math
import os
import statistics
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
# 배당 지급 내역. interval=3mo는 월 배당 회사의 지급을 빠뜨린다(2026-11-19 O: 10년 119건 중 42건만 옴). 1mo는 다 온다.
# range=max는 상장 뒤 전부다(10y는 GLPI 2014 특별배당이 빠진다).
YAHOO_DIV = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=max&interval=1mo&events=div%%2Csplit"
DIV_SCHEMA = "reit-dividends/1"
PAY_LAG_DAYS = 15        # 해와 자리는 ex-date + 15일(지급일에 가깝게)로 정한다. 연말 ex-date와 ex-date 관행 변화를 흡수한다
OUTLIER = 1.2            # 앞뒤 지급보다 이 배수 넘게 크거나 작으면 주기·금액 규칙에서 벗어난 지급
SIMPLE_SPLIT_MAX = 10    # 기약분수의 한쪽이 1이거나 둘 다 이 값 이하면 진짜 split(4:10 → 2:5, 1:25). 1032:1000(→ 129:125)은 가격 조정
SERIES_YEARS = 15        # facts series에는 최근 끝난 15년만 넣는다(오래된 Yahoo 기록은 빈틈이 있어 컷으로 읽힌다. dividends.json에는 전부)
FREQS = ((45, 12, "monthly"), (135, 4, "quarterly"), (270, 2, "semiannual"), (None, 1, "annual"))
DIV_FALLBACK = "edgar.py dividends T --facts F로 대신한다(XBRL 연간 값이라 split·특별배당을 가르지 못하니 note에 적는다)"


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
    return yahoo_fetch(yahoo_url(symbol))


def yahoo_fetch(url):
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


# ---------------------------------------------------------------- 배당 지급 내역

def yahoo_div_url(symbol):
    return YAHOO_DIV % urllib.parse.quote(yahoo_symbol(symbol), safe="-^")


def simple_ratio(n, d):
    """진짜 split 비율인가(4:10, 2:1, 1:2). 1032:1000 같은 값은 spin-off 등의 가격 조정이다."""
    if not (reit_calc.is_finite(n) and reit_calc.is_finite(d)) or n <= 0 or d <= 0:
        return False
    if abs(n - round(n)) > 1e-9 or abs(d - round(d)) > 1e-9:
        return False
    n, d = int(round(n)), int(round(d))
    g = math.gcd(n, d)
    n, d = n // g, d // g
    return min(n, d) == 1 or max(n, d) <= SIMPLE_SPLIT_MAX


def parse_div_chart(symbol, body):
    """Yahoo chart(events=div,split) → (첫 거래일, 지급 [(ex-date, Yahoo 금액)], split [(날짜, 분자, 분모)]).
    형식이 다르면 ValueError."""
    try:
        doc = json.loads(body)
        chart = doc["chart"]
        if chart.get("error") or not chart.get("result"):
            err = chart.get("error") or {}
            raise ValueError("Yahoo chart가 %s 값을 주지 않음(%s)" % (symbol, err.get("description") or err.get("code") or "빈 result"))
        r = chart["result"][0]
        meta = r["meta"]
        if str(meta.get("symbol", "")).upper() != yahoo_symbol(symbol):
            raise ValueError("다른 symbol이 옴(%r)" % meta.get("symbol"))
        if meta.get("currency") != "USD":
            raise ValueError("통화가 USD가 아님(%r)" % meta.get("currency"))
        off = int(meta.get("gmtoffset") or 0)
        ev = r.get("events") or {}
        day = lambda t: dt.datetime.fromtimestamp(t + off, dt.timezone.utc).date()  # noqa: E731
        pays = []
        for v in (ev.get("dividends") or {}).values():
            if not reit_calc.is_finite(v.get("date")) or not reit_calc.is_finite(v.get("amount")) or v["amount"] <= 0:
                raise ValueError("지급 항목 형식이 다름(%r)" % (v,))
            pays.append((day(v["date"]), float(v["amount"])))
        splits = []
        for v in (ev.get("splits") or {}).values():
            n, m = v.get("numerator"), v.get("denominator")
            if not reit_calc.is_finite(v.get("date")) or not reit_calc.is_finite(n) or not reit_calc.is_finite(m) or n <= 0 or m <= 0:
                raise ValueError("split 항목 형식이 다름(%r)" % (v,))
            splits.append((day(v["date"]), n, m))
        first = meta.get("firstTradeDate")
        ts = r.get("timestamp") or []
        start = day(first) if reit_calc.is_finite(first) else (day(ts[0]) if ts and reit_calc.is_finite(ts[0]) else None)
    except (KeyError, IndexError, TypeError, AttributeError) as e:
        raise ValueError("Yahoo chart 형식이 바뀜(%s)" % e)
    except json.JSONDecodeError as e:
        raise ValueError("Yahoo chart 응답이 JSON이 아님(%s)" % e)
    return start, sorted(pays), sorted(splits, key=lambda x: x[0])


def frequency(dates):
    """최근 지급 간격(최대 12개)의 중앙값 → (연 횟수, 이름, 중앙값 일수)."""
    gaps = [(b - a).days for a, b in zip(dates, dates[1:])][-12:]
    med = statistics.median(gaps)
    for top, per_year, name in FREQS:
        if top is None or med <= top:
            return per_year, name, med


def slot(d, per_year):
    """지급 자리: ex-date + 15일이 든 (해, 월·분기·반기). 한 자리에 두 건이면 하나는 주기 밖 지급일 수 있다."""
    p = d + dt.timedelta(days=PAY_LAG_DAYS)
    return p.year, (p.month - 1) * per_year // 12


def classify(rows, per_year):
    """지급마다 kind(정기·특별)를 정한다. 규칙(references/facts-schema.md):
    1. 앞뒤 지급보다 모두 20% 넘게 크거나 작은 지급이 다른 지급과 한 자리에 있으면 통째로 특별
    2. 혼자 있고 앞뒤보다 20% 넘게 작으면 정기로 두되 불확실(주식으로 일부 준 배당이면 현금 부분만 보인다)
    3. 혼자 있고 1·2를 건너뛴 앞뒤 정기보다 20% 넘게 크면 정기(앞뒤 중 큰 값) + 특별(나머지)로 나눈다
    끝 지급(이웃이 하나)은 1만 본다(혼자면 큰 인상인지 특별인지 모른다)."""
    n = len(rows)
    slots = [slot(r["ex_date"], per_year) for r in rows]
    count = collections.Counter(slots)
    amt = [r["amount"] for r in rows]

    def nbrs(i, skip=()):
        out = []
        for step in (-1, 1):
            j = i + step
            while 0 <= j < n and j in skip:
                j += step
            if 0 <= j < n:
                out.append(amt[j])
        return out

    def hi(a, nb):
        return bool(nb) and all(a > OUTLIER * b for b in nb)

    def lo(a, nb):
        return bool(nb) and all(a * OUTLIER < b for b in nb)

    extra = {i for i in range(n) if count[slots[i]] > 1 and (hi(amt[i], nbrs(i)) or lo(amt[i], nbrs(i)))}
    low = {i for i in range(n) if i not in extra and len(nbrs(i)) == 2 and lo(amt[i], nbrs(i))}
    out = []
    for i, r in enumerate(rows):
        base = dict(r, year=str(slots[i][0]), uncertain=False, note="")
        nb = nbrs(i, extra | low)
        if i in extra:
            raw = nbrs(i)
            out.append(dict(base, kind="special", note="같은 자리에 다른 지급이 있고 금액이 앞뒤 지급(%s)과 20%% 넘게 다름" % (
                ", ".join(map(str, raw)))))
        elif i in low:
            raw = nbrs(i)
            out.append(dict(base, kind="regular", uncertain=True,
                            note="앞뒤 지급(%s, %s)보다 20%% 넘게 작은데 제 자리에 혼자 있음: 일부를 주식으로 준 배당이면 현금 부분만 보인다" % (
                                raw[0], raw[1])))
        elif len(nb) == 2 and hi(amt[i], nb):
            reg = max(nb)
            why = "Yahoo가 한 건으로 준 %s를 정기 %s(앞뒤 정기 중 큰 값) + 특별 %s로 나눔" % (
                r["yahoo_amount"], reg, round(amt[i] - reg, 6))
            out.append(dict(base, kind="regular", amount=reg, split_from_spike=True, note=why))
            out.append(dict(base, kind="special", amount=round(amt[i] - reg, 6), split_from_spike=True, note=why))
        else:
            out.append(dict(base, kind="regular"))
    return out


def rebalance_years(pays, per_year):
    """ex-date + 15일로 정한 해가 한 해 f−1회, 다음 해 f+1회(또는 반대)면 경계의 정기 지급 하나를 옮긴다.
    옮기는 지급의 ex-date가 옮겨 갈 해 안(또는 그 해 바로 앞 12월)에 있을 때만 옮긴다."""
    def regs(y):
        return [p for p in pays if p["kind"] == "regular" and p["year"] == str(y)]
    for y in sorted({int(p["year"]) for p in pays}):
        a, b = regs(y), regs(y + 1)
        pick, to = None, None
        if len(a) == per_year - 1 and len(b) == per_year + 1:
            pick = min(b, key=lambda p: p["ex_date"])
            to = y if pick["ex_date"].year == y else None
        elif len(a) == per_year + 1 and len(b) == per_year - 1:
            pick = max(a, key=lambda p: p["ex_date"])
            to = y + 1 if pick["ex_date"].month == 12 else None
        if to is None:
            continue
        for p in pays:
            if p["ex_date"] == pick["ex_date"]:
                p["year"] = str(to)
                p["note"] = "; ".join(x for x in (p["note"], "해 경계: %d·%d년이 %d·%d회라 이 지급을 %d년으로 옮김" % (
                    y, y + 1, len(a), len(b), to)) if x)


def dividends_doc(symbol, body, today):
    """Yahoo 지급 내역 → 정기·특별, 해마다 정기 합, facts series. 형식이 다르거나 지급이 2건보다 적으면 ValueError."""
    start, raw, splits = parse_div_chart(symbol, body)
    if len(raw) < 2:
        raise ValueError("Yahoo chart에 %s 지급 내역이 %d건뿐(배당이 없거나 형식이 바뀜)" % (symbol, len(raw)))
    split_rows = []
    for d, n, m in splits:
        real = simple_ratio(n, m)
        split_rows.append({"date": d.isoformat(), "numerator": n, "denominator": m, "ratio": "%g:%g" % (n, m),
                           "kind": "split" if real else "adjustment",
                           "note": "Yahoo 금액이 이미 split 조정돼 있어 그대로 쓴다" if real else
                           "split이 아닌 가격 조정(spin-off 등)으로 보여, 이 날 전 지급 금액의 Yahoo 조정을 되돌린다(회사 공시 기준)"})
    rows = []
    for d, a in raw:
        amt = a
        for sd, n, m in splits:
            if not simple_ratio(n, m) and d < sd:
                amt *= n / m   # Yahoo 조정값 = 원래 값 × 분모/분자
        rows.append({"ex_date": d, "amount": round(amt, 6), "yahoo_amount": a})
    per_year, fname, med = frequency([r["ex_date"] for r in rows])
    pays = classify(rows, per_year)
    rebalance_years(pays, per_year)
    first_year = int(pays[0]["year"])
    if start and start > dt.date(start.year, 1, 15):
        first_year = max(first_year, start.year + 1)   # 상장한 해는 덜 찬 해다
    url = yahoo_div_url(symbol)
    annual, series, specials = [], [], []
    for y in range(first_year, today.year + 1):
        ys = str(y)
        reg = [p for p in pays if p["year"] == ys and p["kind"] == "regular"]
        spc = [p for p in pays if p["year"] == ys and p["kind"] == "special"]
        complete = y < today.year
        notes = []
        if any(p["uncertain"] for p in reg):
            notes.append("불확실한 지급이 있음(앞뒤보다 20% 넘게 작음): 공시 연간 값과 맞춰 본다")
        if any(p.get("split_from_spike") for p in reg):
            notes.append("한 건을 정기 + 특별로 나눈 지급이 있음(정기 부분은 앞뒤 정기에서 추정)")
        if complete and len(reg) != per_year:
            notes.append("정기 지급 %d회(주기상 %d회)" % (len(reg), per_year))
        if not complete:
            notes.append("진행 중인 해라 부분 합(CAGR·컷에 쓰지 않음)")
        row = {"period": ys, "regular": round(sum(p["amount"] for p in reg), 6),
               "special": round(sum(p["amount"] for p in spc), 6), "regular_count": len(reg),
               "complete": complete, "uncertain": any(p["uncertain"] for p in reg),
               "status": "estimated" if notes else "reported", "note": "; ".join(notes)}
        annual.append(row)
        if complete and y >= today.year - SERIES_YEARS:
            loc = "%s ex-date %s (정기 %d회)" % (url, ", ".join("%s %s" % (p["ex_date"].isoformat(), p["amount"]) for p in reg)
                                                 or "없음", len(reg))
            series.append({"period": ys, "value": row["regular"], "unit": "USD/share", "status": row["status"],
                           "as_of": "%d-12-31" % y, "source": "yahoo:dividends", "locator": loc,
                           "note": row["note"] or "Yahoo 지급 내역의 정기 주당 배당 합(split 조정, ex-date + %d일의 해)" % PAY_LAG_DAYS})
        for p in (spc if y >= today.year - SERIES_YEARS else []):
            specials.append({"period": ys, "ex_date": p["ex_date"].isoformat(), "value": p["amount"], "unit": "USD/share",
                             "status": "estimated" if p.get("split_from_spike") else "reported",
                             "as_of": p["ex_date"].isoformat(), "source": "yahoo:dividends",
                             "locator": "%s ex-date %s %s" % (url, p["ex_date"].isoformat(), p["yahoo_amount"]), "note": p["note"]})
    for p in pays:
        p["ex_date"] = p["ex_date"].isoformat()
        p.pop("split_from_spike", None)
    return {"schema": DIV_SCHEMA, "ticker": symbol.upper(), "as_of": today.isoformat(), "source": "yahoo:dividends",
            "url": url, "first_trade_date": start.isoformat() if start else None,
            "frequency": {"name": fname, "per_year": per_year, "median_gap_days": med,
                          "basis": "최근 지급 간격(최대 12개) 중앙값"},
            "splits": split_rows, "payments": pays, "annual": annual,
            "series": {"dividend_per_share_annual": series, "special_dividends": specials},
            "note": "해는 ex-date + %d일의 달력 연도(지급일에 가깝다). 정기·특별 규칙은 references/facts-schema.md" % PAY_LAG_DAYS}


def _filled(e):
    return bool(e) and e.get("value") is not None and e.get("status") != "missing" and str(e.get("locator", "")).strip()


def merge_dividend_series(have, doc):
    """facts의 연간 series에 Yahoo 정기 합을 넣는다. (합친 series, 한 일 {filled, replaced, updated, kept, kept_uncertain}).
    - 비었거나 없는 해: 채운다
    - XBRL(sec:xbrl) 값: Yahoo 값으로 바꾸고 다르면 note에 XBRL 값을 적는다. Yahoo 값이 불확실한 해면 XBRL 값을 남기고 note에 Yahoo 값
    - 전에 넣은 Yahoo 값: 바꾼다
    - 그 밖의 출처(agent가 공시에서 고친 값): 덮지 않는다
    Yahoo 밖의 해(상장 전, XBRL만 있는 해)는 그대로 둔다."""
    by = {str(e.get("period")): e for e in have}
    unc = {a["period"]: a["uncertain"] for a in doc["annual"]}
    acts = {"filled": [], "replaced": [], "updated": [], "kept": [], "kept_uncertain": []}
    for e in doc["series"]["dividend_per_share_annual"]:
        y = e["period"]
        cur = by.get(y)
        src = str((cur or {}).get("source", ""))
        if not _filled(cur):
            by[y] = e
            acts["filled"].append(y)
        elif src.startswith("yahoo:"):
            by[y] = e
            acts["updated"].append(y)
        elif src.startswith("sec:xbrl"):
            if unc.get(y):
                add = "Yahoo 정기 합 %s는 불확실한 지급이 있어 쓰지 않고 공시 값을 남김" % e["value"]
                if add not in str(cur.get("note", "")):
                    cur = dict(cur, note="; ".join(x for x in (cur.get("note"), add) if x))
                    by[y] = cur
                acts["kept_uncertain"].append(y)
            else:
                ne = dict(e)
                if abs(float(cur["value"]) - e["value"]) > 1e-9:
                    ne["note"] = "; ".join(x for x in (
                        "XBRL 값 %s(%s)를 Yahoo 정기 합으로 바꿈: XBRL 연간 값은 split 전 기준이거나 특별배당이 섞일 수 있다" % (
                            cur["value"], cur.get("locator", "")), e.get("note")) if x)
                by[y] = ne
                acts["replaced"].append(y)
        else:
            acts["kept"].append(y)
    return [by[k] for k in sorted(by)], acts


def div_out_path(t):
    return os.path.join(reit_calc.data_dir(), "tickers", t.upper(), "dividends.json")


def cmd_dividends(args, today=None, fetch=yahoo_fetch):
    today = today or dt.date.today()
    t = args.ticker.upper()
    try:
        doc = dividends_doc(t, fetch(yahoo_div_url(t)), today)
    except (IOError, ValueError) as e:
        log("Yahoo 배당 지급 내역 받기 실패(%s): %s" % (e, DIV_FALLBACK))
        return 1
    reit_calc.write_json(doc, div_out_path(t))
    if args.facts:
        if not os.path.exists(args.facts):
            log("facts 파일이 없음: %s (먼저 edgar.py xbrl을 돌린다)" % args.facts)
            return 1
        fd = reit_calc.read_json(args.facts)
        ser = fd.setdefault("series", {})
        ser["dividend_per_share_annual"], acts = merge_dividend_series(ser.get("dividend_per_share_annual") or [], doc)
        ser["special_dividends"] = doc["series"]["special_dividends"]
        reit_calc.write_json(fd, args.facts)
        log("facts 연간 배당: 채움 %s, XBRL에서 바꿈 %s, 다시 넣음 %s, 불확실해 공시 값을 남김 %s, agent 값이라 남김 %s; 특별배당 %d건: %s" % (
            acts["filled"] or "-", acts["replaced"] or "-", acts["updated"] or "-", acts["kept_uncertain"] or "-",
            acts["kept"] or "-", len(ser["special_dividends"]), args.facts))
    log("%s 배당: %s(연 %d회), 끝난 해 %d개, 특별 %d건, split·조정 %d건 → %s" % (
        t, doc["frequency"]["name"], doc["frequency"]["per_year"], len(doc["series"]["dividend_per_share_annual"]),
        len(doc["series"]["special_dividends"]), len(doc["splits"]), div_out_path(t)))
    print(json.dumps(doc, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


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
    dv = sub.add_parser("dividends")
    dv.add_argument("ticker")
    dv.add_argument("--facts", help="series.dividend_per_share_annual·special_dividends를 넣을 facts 파일")
    try:
        args = p.parse_args(argv)
    except SystemExit as e:
        return 0 if e.code == 0 else 1
    if args.check:
        return run_check()
    if args.cmd == "price":
        return cmd_price(args)
    if args.cmd == "dividends":
        return cmd_dividends(args)
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

    check_dividends(ok, fx)

    log("check: %d개 실패" % len(fails) if fails else "check: 모두 통과")
    return 2 if fails else 0


def check_dividends(ok, fx):
    """quote.py dividends: 가상 Yahoo 지급 내역(divs_TEST 분기, divs_MON 월). 기대값은 fixture를 만들 때 손으로 셈."""
    today = dt.date(2026, 11, 18)
    with open(os.path.join(fx, "divs_TEST.json"), encoding="utf-8") as f:
        body = f.read()
    d = dividends_doc("TEST", body, today)
    ok(d["frequency"]["per_year"] == 4 and d["frequency"]["name"] == "quarterly", "dividends: 분기 주기(간격 중앙값 약 91일)")
    sp = {s["date"]: s for s in d["splits"]}
    ok(sp["2021-07-01"]["kind"] == "adjustment" and sp["2024-06-03"]["kind"] == "split",
       "1050:1000은 split이 아닌 가격 조정(spin-off 등), 1:2는 split")
    pays = d["payments"]
    by = {}
    for r in pays:
        by.setdefault(r["ex_date"], []).append(r)
    ok(by["2019-03-15"][0]["amount"] == 0.42 and by["2019-03-15"][0]["yahoo_amount"] == 0.40,
       "가격 조정(1050:1000) 전 지급은 Yahoo 조정을 되돌린다(0.40 × 1.05 = 0.42)")
    ok(by["2023-06-15"][0]["amount"] == 0.45, "1:2 reverse split 전 금액은 Yahoo split 조정 값 그대로(0.45)")
    ok([r["kind"] for r in by["2020-12-28"]] == ["special"] and by["2020-12-28"][0]["amount"] == 0.105
       and by["2020-12-28"][0]["year"] == "2021",
       "같은 분기 자리(ex-date + 15일)에 든 둘째 지급이 앞뒤보다 20% 넘게 작으면 특별(0.10 × 1.05 = 0.105, 해는 2021)")
    ok(sorted((r["kind"], r["amount"]) for r in by["2024-09-13"]) == [("regular", 0.5), ("special", 0.55)],
       "앞뒤 정기보다 20% 넘게 큰 지급(1.05)은 정기(앞뒤 중 큰 0.50) + 특별(0.55)로 나눔")
    ok(by["2020-05-15"][0]["kind"] == "regular" and by["2020-05-15"][0]["uncertain"] is True,
       "제 자리에 혼자 있고 앞뒤보다 20% 넘게 작은 지급은 정기이되 불확실(현금 일부만 보인 주식 배당일 수 있음)")
    ok(all(r["kind"] == "regular" for r in by["2023-04-20"] + by["2023-06-15"]),
       "늦어진 지급 두 건이 한 자리에 들어도 금액이 같으면 둘 다 정기")
    ok([r["kind"] for r in by["2025-06-13"]] == ["regular"], "삭감(0.50 → 0.30)은 특별이 아님")
    # 현금 일부만 보인 지급 둘 사이의 제 금액 지급(GLPI 2020: 0.70, 0.12, 0.60, 0.12, 0.65 모양, 가상 날짜)
    seq = [(dt.date(2020, 3, 5), 0.70), (dt.date(2020, 5, 12), 0.12), (dt.date(2020, 8, 14), 0.60),
           (dt.date(2020, 11, 13), 0.12), (dt.date(2021, 3, 8), 0.65), (dt.date(2021, 6, 10), 0.67)]
    cl = classify([{"ex_date": a, "amount": b, "yahoo_amount": b} for a, b in seq], 4)
    ok([(r["kind"], r["amount"], r["uncertain"]) for r in cl] ==
       [("regular", 0.70, False), ("regular", 0.12, True), ("regular", 0.60, False), ("regular", 0.12, True),
        ("regular", 0.65, False), ("regular", 0.67, False)],
       "작은 지급 둘 사이의 지급(0.60)은 큰 지급으로 나누지 않음: 큰 지급은 불확실·특별 지급을 건너뛴 앞뒤 정기와 견준다")
    # 20% 경계(가상): 앞뒤 0.50 사이의 0.65(30% 큼)는 나누고, 0.59(18% 큼)는 정기
    for mid, want in ((0.65, [("regular", 0.5), ("special", 0.15)]), (0.59, [("regular", 0.59)])):
        q4 = [dt.date(2022, 3, 10), dt.date(2022, 6, 9), dt.date(2022, 9, 8), dt.date(2022, 12, 8), dt.date(2023, 3, 9)]
        cl = classify([{"ex_date": a, "amount": b, "yahoo_amount": b} for a, b in zip(q4, (0.5, 0.5, mid, 0.5, 0.5))], 4)
        ok([(r["kind"], r["amount"]) for r in cl if r["ex_date"] == q4[2]] == want,
           "주기 밖 금액 규칙은 앞뒤보다 20%% 넘게 클 때: %s → %s" % (mid, want))
    ann = {a["period"]: a for a in d["annual"]}
    want = {"2019": (1.68, 4, "reported"), "2020": (1.344, 4, "estimated"), "2021": (1.74, 4, "reported"),
            "2022": (1.8, 4, "reported"), "2023": (1.8, 4, "reported"), "2024": (1.9, 4, "estimated"),
            "2025": (1.4, 4, "reported")}
    ok({k: (a["regular"], a["regular_count"], a["status"]) for k, a in ann.items() if a["complete"]} == want,
       "연간 정기 합(해 = ex-date + 15일의 달력 연도): %s" % {k: a["regular"] for k, a in ann.items()})
    ok("2018" not in ann, "첫 거래 해(2018-06 상장)는 덜 찬 해라 뺌")
    ok(ann["2026"]["complete"] is False and ann["2026"]["regular"] == 0.9, "진행 중인 해(2026)는 부분 합, complete=false")
    ok(ann["2021"]["special"] == 0.105 and ann["2024"]["special"] == 0.55 and ann["2020"]["uncertain"] is True,
       "특별 합과 불확실한 해")
    ser = d["series"]["dividend_per_share_annual"]
    ok([e["period"] for e in ser] == ["2019", "2020", "2021", "2022", "2023", "2024", "2025"]
       and all(e["source"] == "yahoo:dividends" and e["unit"] == "USD/share" and e["locator"].startswith(yahoo_div_url("TEST"))
               and "2024-09-13" in [e2 for e2 in ser if e2["period"] == "2024"][0]["locator"] for e in ser),
       "facts series: 끝난 해만, source=yahoo:dividends, locator에 URL과 ex-date")
    ok([(e["period"], e["ex_date"], e["value"]) for e in d["series"]["special_dividends"]]
       == [("2021", "2020-12-28", 0.105), ("2024", "2024-09-13", 0.55)], "special_dividends series")

    with open(os.path.join(fx, "divs_MON.json"), encoding="utf-8") as f:
        mon = dividends_doc("MON", f.read(), today)
    ma = {a["period"]: a for a in mon["annual"]}
    ok(mon["frequency"]["name"] == "monthly" and mon["frequency"]["per_year"] == 12, "월 배당은 monthly(12)")
    ok({k: (a["regular"], a["regular_count"], a["status"]) for k, a in ma.items() if a["complete"]}
       == {"2022": (2.64, 11, "estimated"), "2023": (2.99, 12, "reported"), "2024": (3.11, 12, "reported"),
           "2025": (3.24, 12, "reported")},
       "월 배당: ex-date가 월말에서 월초로 바뀐 해(2024)도 12회, 첫 지급 해는 11회라 estimated: %s" % (
           {k: (a["regular"], a["regular_count"]) for k, a in ma.items()},))
    ok(not any(r["kind"] == "special" for r in mon["payments"]), "월 배당 인상은 특별이 아님")

    # 해 경계: 다른 해는 12월 초인 ex-date가 한 해만 12월 19일이면 +15일로 다음 해에 넘어가 3회·5회가 된다(가상, MPT 2008 모양)
    def fake_chart(sym, first, pays):
        t = lambda s: int(dt.datetime(*map(int, s.split("-")), 14, 30, tzinfo=dt.timezone.utc).timestamp())  # noqa: E731
        return json.dumps({"chart": {"error": None, "result": [{
            "meta": {"symbol": sym, "currency": "USD", "gmtoffset": -18000, "firstTradeDate": t(first)},
            "timestamp": [t(first)], "events": {"dividends": {str(t(a)): {"date": t(a), "amount": b} for a, b in pays}}}]}})
    edge = [("2007-03-27", 0.27), ("2007-06-12", 0.27), ("2007-09-12", 0.27), ("2007-12-11", 0.27),
            ("2008-03-11", 0.27), ("2008-06-11", 0.27), ("2008-09-16", 0.27), ("2008-12-19", 0.20),
            ("2009-03-17", 0.20), ("2009-06-09", 0.20), ("2009-09-15", 0.20), ("2009-12-15", 0.20), ("2010-03-16", 0.20)]
    ea = {a["period"]: a for a in dividends_doc("EDGE", fake_chart("EDGE", "2005-07-06", edge), dt.date(2010, 6, 1))["annual"]}
    ok((ea["2008"]["regular_count"], ea["2008"]["regular"], ea["2009"]["regular_count"], ea["2009"]["regular"])
       == (4, 1.01, 4, 0.8) and ea["2008"]["status"] == "reported",
       "해 경계: 한 해가 3회, 다음 해가 5회면 경계의 지급(2008-12-19)을 ex-date의 해로 되돌림(2008 = 0.27×3 + 0.20, 2009 = 0.20×4)")
    far = dividends_doc("TEST", body, dt.date(2040, 1, 1))
    ok(far["series"]["dividend_per_share_annual"][0]["period"] == "2025" and len(far["series"]["dividend_per_share_annual"]) == 15
       and far["annual"][0]["period"] == "2019",
       "facts series는 최근 끝난 15년만(오래된 Yahoo 기록의 빈틈을 컷으로 읽지 않게), annual은 전부")

    # 지급이 끊긴 해는 0으로 남는다(빠지면 컷이 안 보인다)
    gone = json.loads(body)
    ev = gone["chart"]["result"][0]["events"]["dividends"]
    for k in [k for k, v in ev.items() if dt.datetime.fromtimestamp(v["date"], dt.timezone.utc).year >= 2025]:
        del ev[k]
    g = {a["period"]: a for a in dividends_doc("TEST", json.dumps(gone), today)["annual"]}
    ok(g["2025"]["regular"] == 0 and g["2025"]["regular_count"] == 0 and g["2025"]["complete"],
       "지급이 없는 끝난 해(2025)는 0으로 둔다")
    for bad, why in (("<html>blocked</html>", "JSON이 아님"), (json.dumps({"chart": {"result": None, "error": {"code": "Not Found"}}}), "error 응답")):
        try:
            dividends_doc("TEST", bad, today)
            raised = False
        except ValueError:
            raised = True
        ok(raised, "dividends: %s이면 ValueError" % why)

    # facts에 넣기
    have = [{"period": "2017", "value": 1.5, "unit": "USD/share", "status": "reported", "source": "sec:xbrl 10-K (가상)",
             "locator": "us-gaap:CommonStockDividendsPerShareDeclared FY2017 (가상)", "note": ""},
            {"period": "2019", "value": 1.6, "unit": "USD/share", "status": "reported", "source": "sec:xbrl 10-K (가상)",
             "locator": "us-gaap:CommonStockDividendsPerShareCashPaid FY2019 (가상)", "note": ""},
            {"period": "2020", "value": 1.68, "unit": "USD/share", "status": "reported", "source": "sec:xbrl 10-K (가상)",
             "locator": "us-gaap:CommonStockDividendsPerShareCashPaid FY2020 (가상)", "note": ""},
            {"period": "2021", "value": 1.75, "unit": "USD/share", "status": "reported", "source": "sec:8-K (가상)",
             "locator": "agent가 배당 공지에서 고친 값 (가상)", "note": ""},
            {"period": "2022", "value": None, "status": "missing", "source": "sec:xbrl (가상)", "locator": "x (가상)",
             "note": "split 전 기준이라 비움 (가상)"}]
    merged, acts = merge_dividend_series(have, d)
    mb = {e["period"]: e for e in merged}
    ok(mb["2019"]["value"] == 1.68 and mb["2019"]["source"] == "yahoo:dividends" and "XBRL" in mb["2019"]["note"]
       and "1.6" in mb["2019"]["note"], "XBRL 값(1.6)이 Yahoo 정기 합(1.68)과 다르면 Yahoo로 바꾸고 note에 적음")
    ok(mb["2020"]["value"] == 1.68 and mb["2020"]["source"].startswith("sec:xbrl") and "1.344" in mb["2020"]["note"],
       "Yahoo 값이 불확실한 해(2020)는 XBRL 값을 남기고 Yahoo 값을 note에")
    ok(mb["2021"]["value"] == 1.75 and mb["2021"]["source"] == "sec:8-K (가상)", "agent가 다른 출처로 고친 해는 덮지 않음")
    ok(mb["2022"]["value"] == 1.8 and mb["2017"]["value"] == 1.5 and "2026" not in mb,
       "빈 해는 채우고, Yahoo 밖의 해(2017)는 그대로, 진행 중인 해(2026)는 넣지 않음")
    ok(acts["kept"] == ["2021"] and acts["replaced"] == ["2019"] and "2020" in acts["kept_uncertain"],
       "무엇을 바꿨는지 알림 %s" % acts)
    M = {}
    reit_calc.dividend_history({"dividend_per_share_annual": merged}, M)
    ok(M["dividend_cuts"]["value"] == ["2025"] and M["dividend_cagr_3y"]["value"] is not None
       and M["dividend_cagr_5y"]["value"] is not None,
       "합친 series로 reit_calc: 컷은 실제 삭감(2025)만, 특별·불확실 해 때문의 컷 없음, CAGR 3·5년 계산 (got %s)" %
       M["dividend_cuts"]["value"])
    ok(abs(M["dividend_cagr_3y"]["value"] - (-8.0359)) < 0.001,
       "3년 CAGR = (1.40 / 1.80)^(1/3) − 1 = −8.036%% (got %s)" % M["dividend_cagr_3y"]["value"])

    # CLI: Yahoo가 실패하면 exit 1과 edgar.py dividends 안내, 성공하면 facts에 넣고 파일을 쓴다
    import contextlib
    import io as _io
    old_dir = os.environ.get("INVEST_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["INVEST_DATA_DIR"] = tmp
            fpath = os.path.join(tmp, "f.json")
            reit_calc.write_json({"schema": reit_calc.FACTS_SCHEMA, "ticker": "TEST", "period": "2026Q3", "facts": {},
                                  "series": {"dividend_per_share_annual": have}}, fpath)

            class A:
                ticker, facts = "TEST", fpath

            def down(url):
                raise IOError("Yahoo chart HTTP 429 (가상)")
            err = _io.StringIO()
            with contextlib.redirect_stdout(_io.StringIO()), contextlib.redirect_stderr(err):
                rc = cmd_dividends(A, today=today, fetch=down)
            ok(rc == 1 and "edgar.py dividends" in err.getvalue(), "Yahoo 실패면 exit 1과 edgar.py dividends 안내")
            ok(reit_calc.read_json(fpath)["series"]["dividend_per_share_annual"] == have, "실패하면 facts를 바꾸지 않음")
            out = _io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(_io.StringIO()):
                rc = cmd_dividends(A, today=today, fetch=lambda url: body)
            fd = reit_calc.read_json(fpath)
            ok(rc == 0 and [e["period"] for e in fd["series"]["special_dividends"]] == ["2021", "2024"]
               and {e["period"]: e["value"] for e in fd["series"]["dividend_per_share_annual"]}["2022"] == 1.8
               and json.loads(out.getvalue())["ticker"] == "TEST"
               and reit_calc.read_json(os.path.join(tmp, "tickers", "TEST", "dividends.json"))["schema"] == DIV_SCHEMA,
               "성공하면 exit 0, facts의 연간·특별 series를 채우고 tickers/<T>/dividends.json을 씀")
    finally:
        if old_dir is None:
            os.environ.pop("INVEST_DATA_DIR", None)
        else:
            os.environ["INVEST_DATA_DIR"] = old_dir


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

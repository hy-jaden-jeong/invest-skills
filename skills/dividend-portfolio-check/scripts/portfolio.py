#!/usr/bin/env python3
"""dividend-portfolio-check: holdings CSV와 종목 metrics로 포트폴리오를 점검한다.

명령
  check [--holdings P] [--out P] [--date D] [--filings-dir D] [--reitwatch-dir D]
                         비중, 섹터 쏠림, 예상 연 배당과 월별 일정, 종목별 flag,
                         금리 민감도 노출, 오래된 종목(needs_review)을 snapshot JSON에 쓴다
  paydate T --date D --source S --locator L [--secondary]
                         최근 배당 지급일을 출처와 함께 넣는다(분기·반기·연 배당 일정용)
  --check                network 없이 fixture로 확인한다

숫자는 이 script만 계산한다. 종목 지표는 reit-stock-review의 metrics를 읽기만 하고 다시 계산하지 않는다.
EDGAR는 부르지 않는다. agent가 edgar.py filings 출력을 $INVEST_DATA_DIR/portfolio/filings/<T>.json에 둔다.

exit: 0 = 다 채움, 2 = 계산은 했지만 missing·needs_review·freshness unknown이 있음, 1 = 입력 오류.
Python 3 stdlib만 쓴다(3.9 이상).
"""
import argparse
import copy
import csv
import datetime as dt
import glob
import io
import json
import math
import os
import re
import sys
import tempfile

SCHEMA = "portfolio-snapshot/1"
DATES_SCHEMA = "portfolio-dividend-dates/1"
METRICS_SCHEMA = "reit-metrics/1"
STATUSES = ["reported", "derived", "stale", "estimated", "missing"]
RANK = {s: i for i, s in enumerate(STATUSES)}  # 뒤로 갈수록 나쁘다(reit-stock-review와 같은 순서)
FREQUENCIES = (1, 2, 4, 12)  # 배당 일정을 만들 수 있는 연 지급 횟수
COLUMNS = ["ticker", "shares", "cost_basis", "account", "note"]
GRADES = ["high", "medium", "low", "review"]


def log(msg):
    print(msg, file=sys.stderr)


def data_dir():
    return os.path.expanduser(os.environ.get("INVEST_DATA_DIR") or "~/invest-data")


def skill_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(doc, path):
    text = json.dumps(doc, ensure_ascii=False, indent=2, allow_nan=False)  # NaN·Infinity 토큰을 쓰지 않는다
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    os.replace(tmp, path)


def parse_date(s):
    return dt.date(*[int(x) for x in str(s)[:10].split("-")])


def worst(statuses):
    """가장 나쁜 status. 모르는 status는 missing으로 본다."""
    return max((s if s in RANK else "missing" for s in statuses), key=RANK.get)


def is_finite(v):
    """숫자이고 NaN·무한대가 아니다. bool은 숫자로 치지 않는다."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def positive(text):
    """holdings 숫자 칸: 유한한 양수만 받는다."""
    x = float(text)
    if not math.isfinite(x) or x <= 0:
        raise ValueError
    return x


def r4(x):
    return None if x is None else round(x, 4)


def val(value, unit, status, as_of=None, source="", locator="", note=""):
    return {"value": value, "unit": unit, "status": status, "as_of": as_of,
            "source": source, "locator": locator, "note": note}


def missing(unit, note):
    return val(None, unit, "missing", note=note)


def derive(unit, inputs, fn, note=""):
    """inputs의 가장 나쁜 status를 물려받는다. 하나라도 missing이면 missing."""
    bad = [v for v in inputs if v["status"] == "missing" or v["value"] is None]
    if bad:
        return missing(unit, "; ".join(v.get("note") or "입력 missing" for v in bad))
    st = worst([v["status"] for v in inputs])
    st = "derived" if st == "reported" else st
    notes = [v["note"] for v in inputs if v["status"] in ("stale", "estimated") and v.get("note")]
    if note:
        notes.insert(0, note)
    return val(fn(*[v["value"] for v in inputs]), unit, st, note="; ".join(dict.fromkeys(notes)))


# ---------------------------------------------------------------- holdings

def holdings_path(arg):
    """--holdings > INVEST_HOLDINGS_FILE > $INVEST_DATA_DIR/holdings.csv. 없으면 (None, 찾아본 곳)."""
    tried = []
    for src, p in (("--holdings", arg), ("INVEST_HOLDINGS_FILE", os.environ.get("INVEST_HOLDINGS_FILE")),
                   ("$INVEST_DATA_DIR/holdings.csv", os.path.join(data_dir(), "holdings.csv"))):
        if not p:
            continue
        p = os.path.expanduser(p)
        if os.path.isfile(p):
            return p, src
        tried.append("%s=%s" % (src, p))
        if src == "--holdings":
            break  # 인자로 준 경로가 없으면 다른 곳을 보지 않는다
    return None, tried


def parse_holdings(text):
    """(종목별 묶음 list, 오류 list). 같은 ticker의 여러 줄은 주식 수를 더한다."""
    lines = [(i + 1, ln) for i, ln in enumerate(text.splitlines()) if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        return [], ["holdings에 머리줄과 종목 줄이 없음"]
    reader = csv.reader(io.StringIO("\n".join(ln for _, ln in lines)))
    head = [h.strip().lower() for h in next(reader)]
    errors = []
    for need in ("ticker", "shares"):
        if need not in head:
            errors.append("머리줄(%d번 줄)에 %s 칸이 없음. 칸: %s" % (lines[0][0], need, ", ".join(COLUMNS)))
    extra = [h for h in head if h not in COLUMNS]
    if extra:
        errors.append("모르는 칸: %s. 칸: %s" % (", ".join(extra), ", ".join(COLUMNS)))
    if errors:
        return [], errors
    by = {}
    for (lineno, _), cells in zip(lines[1:], reader):
        row = dict(zip(head, [c.strip() for c in cells]))
        t = row.get("ticker", "").upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", t):
            errors.append("%d번 줄: ticker %r" % (lineno, row.get("ticker")))
            continue
        try:
            shares = positive(row.get("shares", ""))
        except ValueError:
            errors.append("%d번 줄: shares %r는 유한한 양수가 아님" % (lineno, row.get("shares")))
            continue
        cost = None
        if row.get("cost_basis"):
            try:
                cost = positive(row["cost_basis"])
            except ValueError:
                errors.append("%d번 줄: cost_basis %r는 유한한 양수가 아님" % (lineno, row["cost_basis"]))
                continue
        h = by.setdefault(t, {"ticker": t, "shares": 0.0, "lots": []})
        h["shares"] += shares
        h["lots"].append({"line": lineno, "shares": shares, "cost_basis": cost,
                          "account": row.get("account", ""), "note": row.get("note", "")})
    if not by and not errors:
        errors.append("종목 줄이 없음")
    return list(by.values()), errors


# ---------------------------------------------------------------- 읽는 데이터

def quarter_of(d):
    return "%dQ%d" % (d.year, (d.month - 1) // 3 + 1)


def earnings_quarter(report_date):
    """실적 8-K의 report_date보다 앞선 마지막 달력 분기 끝날의 분기. 2026-11-02 8-K는 2026Q3다."""
    d = parse_date(report_date)
    q = (d.month - 1) // 3  # 0..3, 이번 분기
    return "%dQ4" % (d.year - 1) if q == 0 else "%dQ%d" % (d.year, q)


def period_key(p):
    m = re.fullmatch(r"(\d{4})Q([1-4])", p or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def latest_metrics(ddir, t):
    paths = [(period_key(os.path.basename(p)[:-5]), p) for p in glob.glob(os.path.join(ddir, "tickers", t, "metrics", "*.json"))]
    paths = sorted(x for x in paths if x[0])
    for _, p in reversed(paths):
        try:
            doc = read_json(p)
        except (OSError, ValueError):
            continue
        if doc.get("schema") == METRICS_SCHEMA:
            return doc, p
    return None, None


def load_facts(ddir, t, metrics):
    for p in (metrics.get("facts_file"), os.path.join(ddir, "tickers", t, "facts", "%s.json" % metrics["period"])):
        if p and os.path.isfile(p):
            try:
                return read_json(p).get("facts", {}), p
            except (OSError, ValueError):
                pass
    return {}, None


def latest_reitwatch(rw_dir):
    files = sorted(glob.glob(os.path.join(rw_dir, "*.json")))
    for p in reversed(files):
        try:
            doc = read_json(p)
        except (OSError, ValueError):
            continue
        if doc.get("schema") == "reitwatch-parsed/1":
            return doc, p
    return None, None


def reitwatch_age_note(rw, today, max_days):
    """REITWatch 가격 기준일이 today보다 max_days 넘게 앞서면 그 이유 글(reit-stock-review와 같은 규칙)."""
    if not rw or not rw.get("as_of"):
        return ""
    age = (today - parse_date(rw["as_of"])).days
    if age <= max_days:
        return ""
    return "REITWatch %s 호(가격 기준일 %s)는 %s보다 %d일 앞섬(허용 %d일): reit-sector-scan fetch로 갱신" % (
        rw.get("month"), rw["as_of"], today.isoformat(), age, max_days)


def load_optional(path, schema=None):
    if not path or not os.path.isfile(path):
        return None
    try:
        doc = read_json(path)
    except (OSError, ValueError):
        return None
    return doc if schema is None or doc.get("schema") == schema else None


def fact_value(f, unit, today=None, max_age=None):
    """facts의 값 하나를 읽는다. reit-stock-review의 값 규칙과 같다: status는 다섯 가지 중 하나,
    value는 유한한 숫자, status와 상관없이 source·locator가 없는 값은 쓰지 않는다."""
    if not isinstance(f, dict) or f.get("status") not in STATUSES or f.get("status") == "missing" or f.get("value") is None:
        return missing(unit, (f or {}).get("note") or "facts에 값 없음")
    if not is_finite(f["value"]):
        return missing(unit, "value(%r)가 유한한 숫자가 아님" % f["value"])
    if not (str(f.get("source", "")).strip() and str(f.get("locator", "")).strip()):
        return missing(unit, "source나 locator가 없어 쓰지 않음")
    out = val(f["value"], unit, f["status"], f.get("as_of"), f.get("source", ""), f.get("locator", ""), f.get("note", ""))
    if today and max_age is not None and out["status"] in ("reported", "derived") and out["as_of"]:
        age = (today - parse_date(out["as_of"])).days
        if age > max_age:
            out["status"] = "stale"
            out["note"] = "; ".join(x for x in (out["note"], "기준일이 %d일 전(허용 %d일)" % (age, max_age)) if x)
    return out


def metric_value(m, unit):
    if not isinstance(m, dict) or m.get("status") not in STATUSES or m.get("value") is None:
        return missing(unit, (m or {}).get("note") or "metrics에 값 없음")
    if not isinstance(m["value"], str) and not is_finite(m["value"]):
        return missing(unit, "metrics 값(%r)이 유한한 숫자가 아님" % m["value"])
    return val(m["value"], m.get("unit") or unit, m["status"], m.get("as_of"), "metrics", m.get("metric", ""), m.get("note", ""))


# ---------------------------------------------------------------- 종목 하나

def freshness(t, metrics, filings):
    if metrics is None:
        return {"status": "no_metrics", "metrics_period": None, "latest_earnings_quarter": None,
                "note": "metrics 없음: reit-stock-review deep으로 분석"}
    out = {"status": "unknown", "metrics_period": metrics["period"], "latest_earnings_quarter": None, "note": ""}
    e = (filings or {}).get("latest_earnings_8k")
    if filings is None:
        out["note"] = "filings 목록 없음: edgar.py filings %s 출력을 portfolio/filings/%s.json에 둔다" % (t, t)
        return out
    if not e:
        out["note"] = "filings 목록에 실적 8-K(item 2.02)가 없음"
        return out
    q = earnings_quarter(e.get("report_date") or e["filed"])
    out.update(latest_earnings_quarter=q, accession=e.get("accession"), filed=e.get("filed"))
    if period_key(q) > period_key(metrics["period"]):
        out.update(status="stale", note="실적 8-K %s(%s)가 %s를 냈는데 metrics는 %s" % (
            e.get("accession"), e.get("filed"), q, metrics["period"]))
    else:
        out["status"] = "fresh"
    return out


def schedule(fwd, freq, paydate, shares):
    """월별 지급액. 월배당은 날짜 없이 매달, 그 밖은 최근 지급일의 달부터 12/빈도 달 간격.
    빈도는 1·2·4·12만 받는다(4.0은 4). 아니면 그 종목 일정만 missing이고 점검은 계속한다."""
    if fwd["value"] is None or freq["value"] is None:
        return {"months": None, "status": "missing", "note": "; ".join(
            v["note"] for v in (fwd, freq) if v["value"] is None) or "값 없음"}
    n = freq["value"]
    if not is_finite(n) or n not in FREQUENCIES:
        return {"months": None, "status": "missing",
                "note": "배당 빈도 %s는 일정을 만들 수 없음(1·2·4·12만)" % n}
    n = int(n)
    inputs = [fwd, freq]
    if n == 12:
        months = list(range(1, 13))
    else:
        if paydate is None or paydate.get("value") is None:
            return {"months": None, "status": "missing",
                    "note": "최근 배당일 없음: portfolio.py paydate로 넣는다(빈도 %d)" % n}
        m0 = parse_date(paydate["value"]).month
        months = sorted((m0 - 1 + k * 12 // n) % 12 + 1 for k in range(n))
        inputs.append(paydate)
    per = fwd["value"] / n * shares
    st = worst([v["status"] for v in inputs])
    return {"months": {str(m): r4(per) for m in months}, "per_payment": r4(per),
            "status": "derived" if st == "reported" else st, "note": ""}


def ticker_view(h, ddir, rw, rw_path, filings_dir, dates, assets, today):
    t = h["ticker"]
    shares = h["shares"]
    metrics, mpath = latest_metrics(ddir, t)
    row = next((r for r in (rw or {}).get("rows", []) if r.get("ticker") == t), None)
    rw_src = "reitwatch:%s" % (rw or {}).get("month", "")
    rw_loc = "%s rows[ticker=%s]" % (rw_path, t)
    rw_note = "REITWatch %s 호 screening 값(가격 기준일 %s)" % ((rw or {}).get("month"), (rw or {}).get("as_of"))
    # 오래된 REITWatch 값은 estimated(stale보다 나쁨)를 지키고 나이를 note에 붙인다
    rw_note = "; ".join(x for x in (rw_note, reitwatch_age_note(rw, today, assets["reitwatch_max_age_days"])) if x)
    v = {"ticker": t, "shares": shares, "lots": h["lots"], "basis": "none", "metrics_file": mpath, "facts_file": None}
    max_age = assets.get("price_max_age_days", 5)
    if metrics:
        v["basis"] = "metrics"
        v["period"] = metrics["period"]
        facts, fpath = load_facts(ddir, t, metrics)
        v["facts_file"] = fpath
        m = metrics.get("metrics", {})
        price = fact_value(facts.get("price"), "USD/share", today, max_age)
        if price["value"] is None and row and row.get("price") is not None:
            price = val(row["price"], "USD/share", "estimated", (rw or {}).get("as_of"), rw_src, rw_loc + ".price",
                        "facts 가격 없음(%s), %s" % (price["note"], rw_note))
        fwd = metric_value(m.get("forward_dividend"), "USD/share")
        freq = fact_value(facts.get("dividend_frequency"), "per year")
        sub = metric_value(m.get("subsector"), "")
        v["flags"] = [{k: f.get(k) for k in ("flag", "level", "value", "status", "basis", "note")} for f in metrics.get("flags", [])]
        # pass null은 회사가 그 값을 내지 않아 맞춰 볼 것이 없는 check다(reit-stock-review "공시 없음")
        v["checks_failed"] = [c.get("check") for c in metrics.get("checks", []) if c.get("pass") is False]
        v["from_metrics"] = {k: metric_value(m.get(k), "") for k in
                             ("forward_yield", "affo_payout_forward", "net_debt_to_ebitdare", "fixed_rate_debt_pct",
                              "yield_spread_vs_10y", "p_ffo", "p_affo") if k in m}
    else:
        v["flags"] = [{"flag": "metrics", "level": "review", "value": None, "status": "missing", "basis": "",
                       "note": "metrics 없음: reit-stock-review deep으로 분석하기 전까지 screening 값만 있음"}]
        v["checks_failed"] = []
        freq = missing("per year", "metrics 없음: 배당 빈도 모름")
        if row:
            v["basis"] = "reitwatch"
            price = val(row.get("price"), "USD/share", "estimated" if row.get("price") is not None else "missing",
                        (rw or {}).get("as_of"), rw_src, rw_loc + ".price", rw_note)
            dy = val(row.get("div_yield"), "%", "estimated" if row.get("div_yield") is not None else "missing",
                     (rw or {}).get("as_of"), rw_src, rw_loc + ".div_yield", rw_note)
            fwd = derive("USD/share", [dy, price], lambda y, p: r4(y / 100 * p), "REITWatch 배당수익률 × 가격")
            sub = val(row.get("subsector"), "", "estimated", (rw or {}).get("as_of"), rw_src, rw_loc + ".subsector",
                      "Nareit REITWatch 분류")
            v["screening"] = {k: row.get(k) for k in ("name", "subsector", "price", "div_yield", "p_ffo_fy1",
                                                      "mkt_cap", "debt_ratio")}
            v["screening"].update(source=rw_src, as_of=(rw or {}).get("as_of"))
        else:
            why = "metrics 없음, REITWatch 최신 호에도 없음"
            price, fwd, sub = missing("USD/share", why), missing("USD/share", why), missing("", why)
    sub_name = assets.get("aliases", {}).get(sub["value"], sub["value"])
    info = assets["subsectors"].get(sub_name) if sub_name else None
    v["price"], v["forward_dividend"], v["dividend_frequency"], v["subsector"] = price, fwd, freq, sub
    v["sector"] = (info or {}).get("sector")
    v["rate_sensitivity"] = (val(info["rate"], "grade", "estimated", note="%s. %s" % (info["why"], assets["rate_note"]))
                             if info else val("review", "grade", "missing", note="subsector를 몰라 등급 없음"))
    sh = val(shares, "shares", "reported", source="holdings", locator="lines %s" % ",".join(str(x["line"]) for x in h["lots"]))
    v["value"] = derive("USD", [price, sh], lambda p, s: p * s)
    v["annual_dividend"] = derive("USD", [fwd, sh], lambda d, s: d * s)
    lots = h["lots"]
    if all(x["cost_basis"] for x in lots):
        cost = val(sum(x["cost_basis"] * x["shares"] for x in lots) / shares, "USD/share", "reported",
                   source="holdings", locator=sh["locator"])
        v["yield_on_cost"] = derive("%", [fwd, cost], lambda d, c: r4(d / c * 100), "forward dividend / 평균 매입가")
    else:
        v["yield_on_cost"] = missing("%", "cost_basis가 없는 줄이 있음")
    v["value"]["value"] = r4(v["value"]["value"])
    v["annual_dividend"]["value"] = r4(v["annual_dividend"]["value"])
    paydate = (dates or {}).get("tickers", {}).get(t)
    v["schedule"] = schedule(fwd, freq, paydate, shares)
    v["paydate"] = paydate
    v["freshness"] = freshness(t, metrics, load_optional(os.path.join(filings_dir, "%s.json" % t)))
    return v


# ---------------------------------------------------------------- 포트폴리오

def group_pct(views, key, weights):
    out = {}
    for x in views:
        k = key(x) or "none"
        g = out.setdefault(k, {"pct": 0.0, "tickers": []})
        g["tickers"].append(x["ticker"])
        if weights["value"] is not None:
            g["pct"] += x["weight"]["value"]
    for g in out.values():
        g["pct"] = r4(g["pct"]) if weights["value"] is not None else None
        g["status"] = weights["status"]
    return dict(sorted(out.items(), key=lambda kv: (-(kv[1]["pct"] or 0), kv[0])))


def portfolio(views, assets):
    total = derive("USD", [x["value"] for x in views], lambda *a: sum(a))
    for x in views:
        x["weight"] = derive("%", [x["value"], total], lambda a, b: r4(a / b * 100))
    if total["value"] is None:
        for x in views:
            x["weight"] = missing("%", "평가액 합이 missing: " + total["note"])
    weights = {"value": total["value"], "status": total["status"]}
    annual = derive("USD", [x["annual_dividend"] for x in views], lambda *a: sum(a))
    fy = derive("%", [annual, total], lambda a, b: r4(a / b * 100), "연 배당 합 / 평가액 합")
    total["value"], annual["value"] = r4(total["value"]), r4(annual["value"])
    # 월별 일정은 일정을 아는 종목만 더한다(배당일을 모르는 종목 때문에 월배당 종목 일정까지 사라지지 않게)
    known = [x for x in views if x["schedule"]["months"]]
    months = {str(m): r4(sum(x["schedule"]["months"].get(str(m), 0.0) for x in known)) for m in range(1, 13)}
    known_annual = sum(x["annual_dividend"]["value"] for x in known)
    sched = {"months": months, "unit": "USD", "covered": [x["ticker"] for x in known],
             "not_covered": [{"ticker": x["ticker"], "note": x["schedule"]["note"]} for x in views if not x["schedule"]["months"]],
             "coverage_pct": r4(known_annual / annual["value"] * 100) if annual["value"] else None,
             "status": worst([x["schedule"]["status"] for x in known]) if known else "missing",
             "note": "일정을 아는 종목만 더한 부분 합이다. coverage_pct는 연 배당 합 중 그 몫이다"}
    hhi = (val(r4(sum((x["value"]["value"] / total["value"] * 100) ** 2 for x in views)), "%^2", weights["status"],
               note="종목 비중(%)의 제곱 합, 0~10000. 기준값 없음") if total["value"] is not None
           else missing("%^2", "비중이 missing"))
    rate = {}
    for g in GRADES:
        members = [x for x in views if x["rate_sensitivity"]["value"] == g]
        if members:
            st = "missing" if g == "review" or total["value"] is None else worst(["estimated", weights["status"]])
            rate[g] = {"pct": r4(sum(x["weight"]["value"] for x in members)) if st != "missing" else None,
                       "tickers": [x["ticker"] for x in members], "status": st}
    flags = {}
    for x in views:
        for f in x["flags"]:
            flags.setdefault(f["level"], []).append("%s:%s" % (x["ticker"], f["flag"]))
    return {"total_value": total, "annual_dividend": annual, "forward_yield": fy, "monthly_schedule": sched,
            "concentration": {"by_subsector": group_pct(views, lambda x: x["subsector"]["value"], weights),
                              "by_sector": group_pct(views, lambda x: x["sector"], weights),
                              "hhi": hhi,
                              "top": max((x["weight"]["value"], x["ticker"]) for x in views)[1]
                              if all(x["weight"]["value"] is not None for x in views) else None},
            "rate_exposure": {"grades": rate, "note": assets["rate_note"]},
            "flags_by_level": flags}


def build(holdings, ddir, rw_dir, filings_dir, dates_path, assets, today, holdings_file=""):
    rw, rw_path = latest_reitwatch(rw_dir)
    dates = load_optional(dates_path, DATES_SCHEMA)
    views = [ticker_view(h, ddir, rw, rw_path, filings_dir, dates, assets, today) for h in sorted(holdings, key=lambda h: h["ticker"])]
    pf = portfolio(views, assets)
    needs = [{"ticker": x["ticker"], "reason": x["freshness"]["status"], "note": x["freshness"]["note"],
              "screening": x.get("screening")} for x in views if x["freshness"]["status"] in ("no_metrics", "stale")]
    unknown = [x["ticker"] for x in views if x["freshness"]["status"] == "unknown"]
    gaps = []
    rw_age = reitwatch_age_note(rw, today, assets["reitwatch_max_age_days"])
    if rw_age and any(x["basis"] == "reitwatch" or x["price"]["source"].startswith("reitwatch:") for x in views):
        gaps.append({"item": "reitwatch", "status": "stale", "reason": rw_age})
    for x in views:
        for k in ("price", "forward_dividend", "dividend_frequency", "subsector", "value", "annual_dividend"):
            if x[k]["status"] in ("missing", "stale", "estimated"):
                gaps.append({"item": "%s.%s" % (x["ticker"], k), "status": x[k]["status"], "reason": x[k]["note"]})
        if x["schedule"]["status"] == "missing":
            gaps.append({"item": "%s.schedule" % x["ticker"], "status": "missing", "reason": x["schedule"]["note"]})
        for c in x["checks_failed"]:
            gaps.append({"item": "%s.check:%s" % (x["ticker"], c), "status": "failed", "reason": "metrics의 check 실패"})
    for k in ("total_value", "annual_dividend", "forward_yield"):
        if pf[k]["status"] == "missing":
            gaps.append({"item": "portfolio.%s" % k, "status": "missing", "reason": pf[k]["note"]})
    n_failed = sum(len(x["checks_failed"]) for x in views)
    doc = {"schema": SCHEMA, "date": today.isoformat(), "holdings_file": holdings_file,
           "inputs": {"data_dir": ddir, "reitwatch_file": rw_path, "filings_dir": filings_dir,
                      "dividend_dates_file": dates_path if dates else None},
           "tickers": views, "portfolio": pf, "needs_review": needs, "freshness_unknown": unknown,
           "data_gaps": gaps,
           "summary": {"tickers": len(views), "needs_review": len(needs), "freshness_unknown": len(unknown),
                       "missing": sum(1 for g in gaps if g["status"] == "missing"), "checks_failed": n_failed}}
    code = 2 if (needs or unknown or doc["summary"]["missing"] or n_failed) else 0
    return doc, code


# ---------------------------------------------------------------- CLI

def load_assets():
    return read_json(os.path.join(skill_root(), "assets", "sectors.json"))


def cmd_check(args):
    path, tried = holdings_path(args.holdings)
    if not path:
        log("holdings 파일이 없음(찾아본 곳: %s). 사용자에게 경로를 묻고 --holdings로 주거나 "
            "INVEST_HOLDINGS_FILE을 export한다. 형식은 assets/holdings.example.csv" % ", ".join(tried or ["없음"]))
        return 1
    with open(path, encoding="utf-8-sig") as f:
        holdings, errors = parse_holdings(f.read())
    if errors:
        for e in errors:
            log("holdings %s: %s" % (path, e))
        return 1
    try:
        today = parse_date(args.date) if args.date else dt.date.today()
    except (ValueError, TypeError):
        log("--date는 YYYY-MM-DD")
        return 1
    ddir = data_dir()
    doc, code = build(holdings, ddir,
                      args.reitwatch_dir or os.path.join(ddir, "reitwatch", "parsed"),
                      args.filings_dir or os.path.join(ddir, "portfolio", "filings"),
                      os.path.join(ddir, "portfolio", "dividend_dates.json"), load_assets(), today, path)
    out = args.out or os.path.join(ddir, "portfolio", "snapshots", "%s.json" % today.isoformat())
    write_json(doc, out)
    pf = doc["portfolio"]
    log("종목 %d, 평가액 %s(%s), 연 배당 %s(%s), 월별 일정 coverage %s%%" % (
        len(doc["tickers"]), pf["total_value"]["value"], pf["total_value"]["status"],
        pf["annual_dividend"]["value"], pf["annual_dividend"]["status"], pf["monthly_schedule"]["coverage_pct"]))
    for n in doc["needs_review"]:
        log("needs_review %s: %s (%s)" % (n["ticker"], n["reason"], n["note"]))
    for t in doc["freshness_unknown"]:
        log("freshness unknown %s: filings 목록을 저장하고 다시 check" % t)
    log("missing %d, needs_review %d" % (doc["summary"]["missing"], doc["summary"]["needs_review"]))
    print(out)
    return code


def cmd_paydate(args):
    t = args.ticker.upper()
    try:
        d = parse_date(args.date)
    except (ValueError, TypeError):
        log("--date는 YYYY-MM-DD")
        return 1
    if d > dt.date.today():
        log("--date 날짜(%s)가 오늘보다 늦음. 지급일은 이미 지난 최근 지급일을 넣는다" % d.isoformat())
        return 1
    if not (args.source.strip() and args.locator.strip()):
        log("--source와 --locator(원문 문장)가 있어야 넣는다. 출처 없는 값은 쓰지 않는다")
        return 1
    path = os.path.join(data_dir(), "portfolio", "dividend_dates.json")
    doc = load_optional(path, DATES_SCHEMA) or {"schema": DATES_SCHEMA, "tickers": {}}
    doc["tickers"][t] = val(args.date[:10], "date", "estimated" if args.secondary else "reported", args.date[:10],
                            args.source.strip(), args.locator.strip(), "2차 자료" if args.secondary else "")
    doc["tickers"] = dict(sorted(doc["tickers"].items()))
    write_json(doc, path)
    log("%s 최근 배당일 %s(%s)" % (t, args.date[:10], doc["tickers"][t]["status"]))
    print(path)
    return 0


def main(argv):
    p = argparse.ArgumentParser(prog="portfolio.py", description="배당 포트폴리오 점검")
    p.add_argument("--check", action="store_true", help="network 없이 fixture로 확인")
    sub = p.add_subparsers(dest="cmd")
    c = sub.add_parser("check")
    c.add_argument("--holdings")
    c.add_argument("--out")
    c.add_argument("--date", help="기준일 YYYY-MM-DD(기본 오늘). snapshot 이름과 가격 stale 기준")
    c.add_argument("--filings-dir", help="기본: $INVEST_DATA_DIR/portfolio/filings")
    c.add_argument("--reitwatch-dir", help="기본: $INVEST_DATA_DIR/reitwatch/parsed")
    d = sub.add_parser("paydate")
    d.add_argument("ticker")
    d.add_argument("--date", required=True)
    d.add_argument("--source", required=True)
    d.add_argument("--locator", required=True)
    d.add_argument("--secondary", action="store_true")
    try:
        args = p.parse_args(argv)
    except SystemExit as e:
        return 0 if e.code == 0 else 1
    if args.check:
        return run_check()
    if args.cmd == "check":
        return cmd_check(args)
    if args.cmd == "paydate":
        return cmd_paydate(args)
    p.print_help(sys.stderr)
    return 1


# ---------------------------------------------------------------- --check

def run_check():
    root = skill_root()
    fx = os.path.join(root, "tests", "fixtures")
    ddir = os.path.join(fx, "data")
    exp = read_json(os.path.join(root, "tests", "expected.json"))
    assets = load_assets()
    today = parse_date(exp["date"])
    fails = []

    def ok(cond, label):
        if not cond:
            fails.append(label)
        log(("PASS " if cond else "FAIL ") + label)

    def near(a, b):
        return a is not None and b is not None and abs(a - b) < 1e-4

    with open(os.path.join(fx, "holdings.csv"), encoding="utf-8") as f:
        text = f.read()
    holdings, errors = parse_holdings(text)
    ok(not errors and [h["ticker"] for h in holdings] == ["AAA", "BBB", "CCC"], "holdings 3종목, 주석 줄 건너뜀")

    def run(hs=None, dates=os.path.join(ddir, "portfolio", "dividend_dates.json"), day=today):
        return build(copy.deepcopy(hs or holdings), ddir, os.path.join(ddir, "reitwatch", "parsed"),
                     os.path.join(ddir, "portfolio", "filings"), dates, assets, day)

    doc, code = run()
    by = {x["ticker"]: x for x in doc["tickers"]}
    for t, e in exp["tickers"].items():
        x = by[t]
        ok(near(x["shares"], e["shares"]), "%s 주식 수 %s (계좌별 줄을 더함)" % (t, x["shares"]))
        ok(near(x["value"]["value"], e["value"]), "%s 평가액 %s = %s" % (t, x["value"]["value"], e["value"]))
        ok(near(x["weight"]["value"], e["weight_pct"]), "%s 비중 %s = %s" % (t, x["weight"]["value"], e["weight_pct"]))
        ok(near(x["annual_dividend"]["value"], e["annual_dividend"]),
           "%s 연 배당 %s = %s" % (t, x["annual_dividend"]["value"], e["annual_dividend"]))
        ok(x["freshness"]["status"] == e["freshness"], "%s freshness %s" % (t, x["freshness"]["status"]))
        if "period" in e:
            ok(x.get("period") == e["period"], "%s 최신 metrics 분기 %s" % (t, x.get("period")))
    ok(near(by["AAA"]["yield_on_cost"]["value"], exp["tickers"]["AAA"]["yield_on_cost_pct"]), "AAA yield on cost 7.5%")
    ok(by["BBB"]["yield_on_cost"]["status"] == "missing", "cost_basis 없는 종목은 yield on cost missing")
    pf = doc["portfolio"]
    ok(near(pf["total_value"]["value"], exp["total_value"]), "평가액 합 %s" % pf["total_value"]["value"])
    ok(near(pf["annual_dividend"]["value"], exp["annual_dividend"]), "연 배당 합 %s" % pf["annual_dividend"]["value"])
    ok(near(pf["forward_yield"]["value"], exp["forward_yield_pct"]), "포트폴리오 배당수익률 %s" % pf["forward_yield"]["value"])
    ok(by["AAA"]["weight"]["status"] == "estimated" and pf["total_value"]["status"] == "estimated",
       "2차 자료·REITWatch 가격이 섞이면 비중·합은 estimated")
    ok(by["BBB"]["price"]["status"] == "reported" and by["CCC"]["price"]["status"] == "estimated"
       and "reitwatch:2026-10" == by["CCC"]["price"]["source"], "metrics 없는 종목 가격은 REITWatch 최신 호(estimated)")
    ms = pf["monthly_schedule"]
    for m, v in exp["monthly"].items():
        if m != "why" and m != "coverage_pct":
            ok(near(ms["months"][m], v), "%s월 배당 %s = %s" % (m, ms["months"][m], v))
    ok(near(ms["coverage_pct"], exp["monthly"]["coverage_pct"]) and ms["covered"] == ["AAA", "BBB"]
       and [n["ticker"] for n in ms["not_covered"]] == ["CCC"], "월별 일정 coverage 65.7143%, CCC는 not_covered")
    ok(near(pf["concentration"]["hhi"]["value"], exp["hhi"]), "HHI %s" % pf["concentration"]["hhi"]["value"])
    for k, key in (("subsector_pct", "by_subsector"), ("sector_pct", "by_sector")):
        got = {n: g["pct"] for n, g in pf["concentration"][key].items()}
        ok(all(near(got.get(n), v) for n, v in exp[k].items()) and len(got) == len(exp[k]), "%s %s" % (key, got))
    got = {g: x["pct"] for g, x in pf["rate_exposure"]["grades"].items()}
    ok(all(near(got.get(g), v) for g, v in exp["rate_pct"].items())
       and all(x["status"] == "estimated" for x in pf["rate_exposure"]["grades"].values())
       and "엇갈" in pf["rate_exposure"]["note"], "금리 민감도 노출 %s, estimated, 엇갈림 note" % got)
    ok([n["ticker"] for n in doc["needs_review"]] == exp["needs_review"] and code == 2,
       "needs_review BBB(stale)·CCC(no_metrics), exit 2")
    ok(doc["needs_review"][1]["screening"]["p_ffo_fy1"] == 15.0, "metrics 없는 종목은 REITWatch screening 값을 붙임")
    ok(any(f["flag"] == "maturity_concentration" and f["level"] == "watch" for f in by["BBB"]["flags"])
       and by["CCC"]["flags"][0]["level"] == "review", "종목별 flag: metrics flag를 옮기고, metrics 없으면 review")
    for d, q in exp["earnings_quarter"].items():
        ok(earnings_quarter(d) == q, "실적 8-K %s → %s" % (d, earnings_quarter(d)))

    # 지급일이 없으면 분기 배당 종목 일정은 missing, 월배당만 남는다
    d2, _ = run(dates=os.path.join(ddir, "none.json"))
    ms2 = d2["portfolio"]["monthly_schedule"]
    ok(all(near(ms2["months"][m], v) for m, v in exp["monthly_without_paydate"].items() if m not in ("why", "coverage_pct"))
       and near(ms2["coverage_pct"], exp["monthly_without_paydate"]["coverage_pct"])
       and "paydate" in ms2["not_covered"][0]["note"], "지급일이 없으면 BBB 일정 missing, coverage 42.8571%")

    # 가격 기준일이 5일을 넘으면 reported도 stale
    d3, _ = run(day=parse_date("2026-11-30"))
    b3 = next(x for x in d3["tickers"] if x["ticker"] == "BBB")
    ok(b3["price"]["status"] == "stale" and b3["value"]["status"] == "stale", "BBB 가격 13일 전 → stale")

    # 어디에도 없는 종목: 가격 missing → 합과 비중이 missing(보간하지 않음)
    hs, _ = parse_holdings(text + "DDD,10,,,\n")
    d4, c4 = run(hs)
    ok(c4 == 2 and d4["portfolio"]["total_value"]["status"] == "missing"
       and all(x["weight"]["value"] is None for x in d4["tickers"])
       and d4["portfolio"]["annual_dividend"]["value"] is None, "가격 없는 종목이 있으면 합·비중 missing")

    # filings 목록이 없으면 freshness unknown
    d5, c5 = build(copy.deepcopy(holdings), ddir, os.path.join(ddir, "reitwatch", "parsed"), os.path.join(ddir, "none"),
                   None, assets, today)
    a5 = next(x for x in d5["tickers"] if x["ticker"] == "AAA")
    ok(a5["freshness"]["status"] == "unknown" and d5["freshness_unknown"] == ["AAA", "BBB"] and c5 == 2,
       "filings 목록이 없으면 freshness unknown, exit 2")

    # 출처 없는 facts 가격은 쓰지 않고 REITWatch로 내려간다
    ok(fact_value({"value": 1.0, "status": "reported", "source": "x", "locator": ""}, "USD/share")["status"] == "missing",
       "locator 없는 facts 값은 missing")

    # 홀딩스 형식 오류
    for bad, label in (("ticker,shares\nAAA,abc\n", "shares가 숫자가 아님"), ("symbol,qty\nAAA,1\n", "칸 이름이 틀림"),
                       ("ticker,shares\nAAA,-5\n", "shares 음수")):
        ok(parse_holdings(bad)[1], "holdings 오류: %s" % label)

    # 추천 문장이 없다(사라·팔아라·비중 조정·목표가를 쓰지 않는다)
    blob = json.dumps(doc, ensure_ascii=False)
    ok(not re.search(r"사라|팔아|매수하|매도하|목표가|비중을 (늘|줄)|\bbuy\b|\bsell\b", blob, re.I), "snapshot에 추천 문장 없음")

    # CLI: holdings 경로 순서와 snapshot, paydate
    saved = {k: os.environ.get(k) for k in ("INVEST_DATA_DIR", "INVEST_HOLDINGS_FILE")}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["INVEST_DATA_DIR"] = tmp
            os.environ.pop("INVEST_HOLDINGS_FILE", None)
            ok(main(["check", "--date", "2026-11-18"]) == 1, "holdings가 어디에도 없으면 exit 1")
            hp = os.path.join(tmp, "h.csv")
            with open(hp, "w", encoding="utf-8") as f:
                f.write("ticker,shares\nZZZ,1\n")
            os.environ["INVEST_HOLDINGS_FILE"] = hp
            ok(main(["check", "--date", "2026-11-18"]) == 2
               and read_json(os.path.join(tmp, "portfolio", "snapshots", "2026-11-18.json"))["holdings_file"] == hp,
               "INVEST_HOLDINGS_FILE을 읽고 portfolio/snapshots/<날짜>.json에 씀")
            ok(main(["check", "--holdings", os.path.join(tmp, "none.csv")]) == 1, "--holdings 경로가 없으면 exit 1")
            ok(main(["paydate", "zzz", "--date", "2026-10-15", "--source", "sec:8-K (가상)", "--locator", ""]) == 1,
               "locator 없는 paydate는 거절")
            ok(main(["paydate", "zzz", "--date", "2026-10-15", "--source", "web:https://example.com(가상)",
                     "--locator", "paid October 15(가상)", "--secondary"]) == 0
               and read_json(os.path.join(tmp, "portfolio", "dividend_dates.json"))["tickers"]["ZZZ"]["status"] == "estimated",
               "paydate --secondary는 estimated")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # 같은 입력이면 같은 바이트
    ok(json.dumps(run()[0], ensure_ascii=False) == json.dumps(run()[0], ensure_ascii=False), "같은 입력이면 같은 snapshot")

    # 숫자는 유한해야 한다(NaN·무한대는 입력 오류, facts·metrics 값은 missing)
    for bad, label in (("ticker,shares\nAAA,nan\n", "shares nan"), ("ticker,shares\nAAA,inf\n", "shares inf"),
                       ("ticker,shares,cost_basis\nAAA,1,nan\n", "cost_basis nan")):
        ok(parse_holdings(bad)[1], "holdings 오류: %s" % label)
    ok(fact_value({"value": float("nan"), "status": "reported", "source": "x (가상)", "locator": "y (가상)"},
                  "USD/share")["status"] == "missing"
       and metric_value({"value": float("inf"), "status": "derived"}, "%")["status"] == "missing",
       "facts·metrics의 NaN·무한대 값은 missing")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            write_json({"x": float("nan")}, os.path.join(tmp, "n.json"))
        nan_written = True
    except ValueError:
        nan_written = False
    ok(not nan_written, "JSON에 NaN을 쓰려 하면 멈춤(allow_nan=False)")

    # 가격 없는 종목이 있으면 비중 1위(top)도 missing(None을 0으로 보지 않음)
    ok(d4["portfolio"]["concentration"]["top"] is None and pf["concentration"]["top"] == "AAA",
       "비중이 missing인 종목이 있으면 concentration.top은 null, 아니면 비중 1위")

    # 배당 빈도: 4.0은 4로, 1·2·4·12가 아니면 그 종목 일정만 not_covered
    fwd = val(4.0, "USD/share", "derived")
    pd_ = val("2026-10-15", "date", "reported")
    s4 = schedule(fwd, val(4.0, "per year", "reported"), pd_, 10)
    s3 = schedule(fwd, val(3, "per year", "reported"), pd_, 10)
    ok(s4["months"] == {"1": 10.0, "4": 10.0, "7": 10.0, "10": 10.0} and s3["months"] is None
       and "3" in s3["note"], "빈도 4.0은 1·4·7·10월 10.0씩, 빈도 3은 일정 missing")

    with tempfile.TemporaryDirectory() as tmp:
        import shutil
        dd = os.path.join(tmp, "data")
        shutil.copytree(ddir, dd)

        def run_tmp(hs_text, day=today):
            hs2, _ = parse_holdings(hs_text)
            return build(hs2, dd, os.path.join(dd, "reitwatch", "parsed"), os.path.join(dd, "portfolio", "filings"),
                         os.path.join(dd, "portfolio", "dividend_dates.json"), assets, day)
        ok(run_tmp("ticker,shares\nAAA,100\n")[1] == 0, "AAA 하나(다 채움)는 exit 0")
        mp = os.path.join(dd, "tickers", "AAA", "metrics", "2026Q3.json")
        m = read_json(mp)
        m["checks"] = [dict(c, **{"pass": False}) for c in m["checks"]]
        write_json(m, mp)
        d6, c6 = run_tmp("ticker,shares\nAAA,100\n")
        ok(c6 == 2 and d6["summary"]["checks_failed"] == 1, "metrics에 check 실패가 있으면 exit 2")
        m["checks"] = [dict(c, **{"pass": None}) for c in m["checks"]]
        write_json(m, mp)
        d6n, c6n = run_tmp("ticker,shares\nAAA,100\n")
        ok(c6n == 0 and d6n["summary"]["checks_failed"] == 0,
           "check pass가 null(회사가 그 값을 내지 않아 맞춰 볼 것이 없음)이면 실패로 세지 않음")
        fp = os.path.join(dd, "tickers", "BBB", "facts", "2026Q2.json")
        f = read_json(fp)
        f["facts"]["dividend_frequency"]["value"] = 3
        write_json(f, fp)
        d7, c7 = run_tmp("ticker,shares\nAAA,100\nBBB,40\n")
        ok(c7 == 2 and [n["ticker"] for n in d7["portfolio"]["monthly_schedule"]["not_covered"]] == ["BBB"]
           and d7["portfolio"]["monthly_schedule"]["covered"] == ["AAA"], "빈도 3인 종목이 있어도 점검은 끝나고 그 종목만 not_covered")
        rp = os.path.join(dd, "reitwatch", "parsed", "2026-10.json")
        r = read_json(rp)
        for row in r["rows"]:
            if row["ticker"] == "CCC":
                row["subsector"] = "Unknown (가상)"
        write_json(r, rp)
        d8, _ = run_tmp("ticker,shares\nCCC,200\n")
        rv = d8["portfolio"]["rate_exposure"]["grades"]["review"]
        ok(d8["portfolio"]["total_value"]["value"] == 4000.0 and rv["status"] == "missing" and rv["pct"] is None,
           "금리 민감도 review(status missing)의 pct는 null")

    # REITWatch 가격 기준일이 --date보다 허용 일수를 넘게 앞서면 나이를 적는다(estimated는 그대로)
    d9, _ = run(day=parse_date("2027-03-01"))
    c9 = next(x for x in d9["tickers"] if x["ticker"] == "CCC")
    ok(c9["price"]["status"] == "estimated" and "허용 %d일" % assets["reitwatch_max_age_days"] in c9["price"]["note"]
       and any(g["item"] == "reitwatch" and g["status"] == "stale" for g in d9["data_gaps"])
       and not any(g["item"] == "reitwatch" for g in doc["data_gaps"]),
       "REITWatch 2026-09-30이 2027-03-01보다 152일 앞섬: CCC 가격 note와 데이터 구멍 reitwatch stale")

    # paydate 날짜가 오늘보다 늦으면 거절
    with tempfile.TemporaryDirectory() as tmp:
        saved_dir = os.environ.get("INVEST_DATA_DIR")
        os.environ["INVEST_DATA_DIR"] = tmp
        try:
            future = (dt.date.today() + dt.timedelta(days=1)).isoformat()
            ok(main(["paydate", "zzz", "--date", future, "--source", "sec:8-K (가상)", "--locator", "x (가상)"]) == 1,
               "paydate 날짜가 오늘보다 늦으면 exit 1")
        finally:
            if saved_dir is None:
                os.environ.pop("INVEST_DATA_DIR", None)
            else:
                os.environ["INVEST_DATA_DIR"] = saved_dir

    log("check: %d개 실패" % len(fails) if fails else "check: 모두 통과")
    return 2 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

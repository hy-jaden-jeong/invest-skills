#!/usr/bin/env python3
"""reit-stock-review: facts JSON에서 REIT 지표를 계산해 metrics JSON을 쓴다.

명령
  metrics --facts F [--out P] [--macro-snapshot P] [--reitwatch-dir D]
                         facts를 읽어 지표·check·flag·데이터 구멍을 계산한다.
                         SIC가 6798이 아니면 일반 배당주 경로(EPS·FCF payout, Chowder, yield trap)
  diff --prev M1 --curr M2 [--out P]
                         같은 종목 두 분기 metrics의 지표별 전후 값·변화·status·flag 변화(earnings mode)
  compare --metrics M... [--out P]
                         여러 종목 metrics를 같은 표에 놓는다. subsector가 다르면 경고(compare mode)
  --check                network 없이 fixture로 확인한다

숫자는 이 script만 계산한다. agent는 공시에서 값을 읽어 facts에 위치(locator)와 함께 적는다.
계산은 순수 함수이고, 입력 status를 물려받는다(metric()). 입력이 하나라도 missing이면 계산값도 missing이다.

exit: 0 = 다 채움, 2 = 계산은 했지만 missing이나 check 실패가 있음, 1 = 입력 오류.
Python 3 stdlib만 쓴다(3.9 이상).
"""
import argparse
import copy
import datetime as dt
import glob
import json
import math
import os
import sys
import tempfile

FACTS_SCHEMA = "reit-facts/1"
METRICS_SCHEMA = "reit-metrics/1"
REITWATCH_SCHEMA = "reitwatch-parsed/1"
MACRO_SCHEMA = "macro-snapshot/1"
STATUSES = ["reported", "derived", "stale", "estimated", "missing"]
RANK = {s: i for i, s in enumerate(STATUSES)}  # 뒤로 갈수록 나쁘다

# 값의 종류별 단위. money는 USD thousands, shares는 thousands로 맞춘다.
UNITS = {
    "money": {"USD": 0.001, "USD thousands": 1.0, "USD millions": 1000.0, "USD billions": 1000000.0},
    "shares": {"shares": 0.001, "shares thousands": 1.0, "shares millions": 1000.0},
    "per_share": {"USD/share": 1.0},
    "pct": {"%": 1.0},
    "years": {"years": 1.0},
    "per_year": {"per year": 1.0},
    "x": {"x": 1.0},
}
BASE_UNIT = {"money": "USD thousands", "shares": "shares thousands", "per_share": "USD/share",
             "pct": "%", "years": "years", "per_year": "per year", "x": "x"}

# facts 칸과 종류. 뜻과 어디서 읽는지는 references/facts-schema.md에 있다.
FACT_KINDS = {
    # FFO (Nareit) 입력, 분기
    "net_income_common": "money",
    "depreciation_amortization": "money",
    "non_real_estate_depreciation": "money",
    "impairment_depreciable_re": "money",
    "gain_on_sale_re": "money",
    "unconsolidated_jv_ffo_adjustments": "money",
    "nci_ffo_adjustments": "money",
    "other_nareit_adjustments": "money",
    "ffo_reported": "money",
    "normalized_ffo_reported": "money",
    "affo_reported": "money",
    "ffo_per_share_diluted": "per_share",
    "affo_per_share_diluted": "per_share",
    "diluted_shares_weighted": "shares",
    # guidance·consensus (연간, 주당)
    "ffo_guidance_low": "per_share",
    "ffo_guidance_high": "per_share",
    "affo_guidance_low": "per_share",
    "affo_guidance_high": "per_share",
    "ffo_consensus_fy1": "per_share",
    # 배당
    "dividend_regular_latest": "per_share",
    "dividend_frequency": "per_year",
    "dividends_paid_per_share_quarter": "per_share",
    # 재무
    "total_debt": "money",
    "cash": "money",
    "preferred_equity": "money",
    "fixed_rate_debt": "money",
    "debt_principal_total": "money",
    "ebitdare_quarter": "money",
    "ebitdare_annualized": "money",
    "interest_expense": "money",
    # 신용 (gross assets 기준 비율, fixed-charge coverage)
    "total_assets": "money",
    "accumulated_depreciation": "money",
    "secured_debt": "money",
    "unencumbered_assets": "money",
    "unencumbered_noi": "money",
    "capitalized_interest": "money",
    "preferred_dividends": "money",
    "principal_amortization": "money",
    # 리파이낸싱·유동성
    "wam_years": "years",
    "weighted_avg_rate_pct": "pct",
    "floating_rate_debt": "money",
    "revolver_available": "money",
    "dev_remaining_spend": "money",
    "liquidity_cash": "money",
    # 운영
    "occupancy_pct": "pct",
    "wale_years": "years",
    "investment_grade_share_pct": "pct",
    "same_store_current": "money",
    "same_store_prior": "money",
    "noi_annualized": "money",
    # 시장
    "price": "per_share",
    "common_shares_outstanding": "shares",
    "op_units_outstanding": "shares",
}
SERIES_KINDS = {
    "dividend_per_share_annual": "per_share",
    "debt_maturities": "money",
    "top_tenants": "pct",
    "covenants": "covenant",
}
COVENANT_UNITS = ("%", "x")
BRIDGE_STARTS = ["ffo_reported", "normalized_ffo_reported"]
# AFFO bridge 줄의 성격. recurring_capex·leasing_cost는 부동산을 유지하는 데 드는 돈, straight_line은 현금이 아닌 임대료다.
BRIDGE_CATEGORIES = ("straight_line", "recurring_capex", "leasing_cost", "non_cash", "other")

# 일반 배당주(SIC가 6798이 아님) facts 칸. FFO 대신 EPS·FCF로 배당을 본다. 현금흐름은 회계연도 누적(YTD) 값이다.
DIVIDEND_FACT_KINDS = {
    "net_income_common": "money",
    "eps_diluted_quarter": "per_share",
    "eps_guidance_low": "per_share",
    "eps_guidance_high": "per_share",
    "dividend_regular_latest": "per_share",
    "dividend_frequency": "per_year",
    "dividends_declared_per_share_quarter": "per_share",
    "operating_cash_flow_ytd": "money",
    "capex_ytd": "money",
    "dividends_paid_ytd": "money",
    "price": "per_share",
}
DIVIDEND_SERIES_KINDS = {"dividend_per_share_annual": "per_share"}
FLAG_RANK = {"ok": 0, "watch": 1, "alert": 2}
# 자기 P/FFO history 표본은 1·4·7·10월 호(가격 기준일이 분기말)와 최신 호다. reit-sector-scan의 표본과 같다.
# 매달 넣으면 최근 1년(매달 받은 호)의 무게가 네 배가 된다.
QUARTER_MONTHS = (1, 4, 7, 10)
DIFF_SCHEMA = "reit-diff/1"
COMPARE_SCHEMA = "reit-compare/1"


def is_reit_doc(doc):
    """facts의 company.is_reit, 없으면 SIC로 가른다. 둘 다 없으면 REIT로 본다(S3 facts와 같게)."""
    c = doc.get("company") or {}
    if isinstance(c.get("is_reit"), bool):
        return c["is_reit"]
    return str(c.get("sic") or "6798") == "6798"


def fact_kinds(doc):
    return FACT_KINDS if is_reit_doc(doc) else DIVIDEND_FACT_KINDS


def series_kinds(doc):
    return SERIES_KINDS if is_reit_doc(doc) else DIVIDEND_SERIES_KINDS


class Undefined(Exception):
    """식이 뜻을 잃는 입력(예: FCF가 0 이하인 payout). 값을 내지 않고 이유를 note에 쓴다."""


def log(msg):
    print(msg, file=sys.stderr)


def data_dir():
    return os.path.expanduser(os.environ.get("INVEST_DATA_DIR") or "~/invest-data")


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def skill_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_thresholds(path=None):
    return read_json(path or os.path.join(skill_root(), "assets", "thresholds.json"))


def parse_date(s):
    return dt.date(*[int(x) for x in s[:10].split("-")])


# ---------------------------------------------------------------- 값 다루기

def missing(note):
    return {"value": None, "unit": "", "status": "missing", "as_of": None, "source": "", "locator": "", "note": note}


def is_finite(v):
    """숫자이고 NaN·무한대가 아니다. bool은 숫자로 치지 않는다."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def check_value(key, f):
    """값 하나의 규칙: status는 다섯 가지 중 하나, value는 유한한 숫자, status와 상관없이 source·locator가 있어야 한다.
    (값 그대로, None) 또는 (missing, 어긴 규칙)."""
    if not isinstance(f, dict):
        return missing("%s: 값의 형식이 아님" % key), "형식"
    status = f.get("status")
    if status not in STATUSES:
        return missing("%s: status %r" % (key, status)), "status"
    if status == "missing" or f.get("value") is None:
        return missing(f.get("note") or f.get("reason") or "값 없음"), None
    if not is_finite(f.get("value")):
        return missing("%s: value(%r)가 유한한 숫자가 아님" % (key, f.get("value"))), "숫자"
    if not (str(f.get("source", "")).strip() and str(f.get("locator", "")).strip()):
        return missing("%s: source나 locator가 없어 쓰지 않음(출처 없는 값)" % key), "출처"
    return f, None


def normalize(key, f, kind):
    """facts 칸 하나를 검사하고(check_value) 단위를 맞춘다. 규칙을 어기면 missing으로 내리고 이유를 note에 쓴다."""
    f, bad = check_value(key, f)
    if bad or f["status"] == "missing":
        return f, bad
    scale = UNITS[kind].get(f.get("unit"))
    if scale is None:
        return missing("%s: unit %r는 %s 값에 맞지 않음(%s)" % (
            key, f.get("unit"), kind, ", ".join(UNITS[kind]))), "unit"
    out = dict(f)
    out["value"] = f["value"] * scale
    out["unit"] = BASE_UNIT[kind]
    out.setdefault("note", "")
    out.setdefault("as_of", None)
    return out, None


def read_covenant(key, it):
    """covenant 줄 하나: name, limit, actual, direction(max·min), unit(%·x)과 status·source·locator.
    (줄 envelope, 어긴 규칙 또는 None). 규칙을 어기면 status missing이고 이유를 note에 쓴다."""
    if not isinstance(it, dict):
        return dict(missing("%s: 줄의 형식이 아님" % key), name=None), "형식"
    out = {k: it.get(k) for k in ("name", "limit", "actual", "direction", "unit", "status", "as_of", "source", "locator")}
    out["note"] = it.get("note") or ""
    st = it.get("status")
    if st not in STATUSES:
        return dict(out, status="missing", note="%s: status %r" % (key, st)), "status"
    if st == "missing":
        return dict(out, note=out["note"] or "값 없음"), None
    why = None
    if it.get("direction") not in ("max", "min"):
        why = "direction %r(max·min만)" % it.get("direction")
    elif it.get("unit") not in COVENANT_UNITS:
        why = "unit %r(%s만)" % (it.get("unit"), "·".join(COVENANT_UNITS))
    elif not (is_finite(it.get("limit")) and it["limit"] > 0 and is_finite(it.get("actual")) and it["actual"] >= 0):
        why = "limit(%r)은 양수, actual(%r)은 0 이상인 유한한 숫자여야 함" % (it.get("limit"), it.get("actual"))
    elif not (str(it.get("source", "")).strip() and str(it.get("locator", "")).strip()):
        why = "source나 locator가 없어 쓰지 않음(출처 없는 값)"
    if why:
        return dict(out, status="missing", note="%s: %s" % (key, why)), "규칙"
    return out, None


def covenant_headroom(c):
    """한도에 닿기까지 좋은 쪽 값이 줄 수 있는 비율(%). max: 1 − actual/limit, min: 1 − limit/actual."""
    if c["direction"] == "max":
        return (1 - c["actual"] / c["limit"]) * 100
    if c["actual"] <= 0:
        raise Undefined("actual이 0이라 headroom을 낼 수 없음(한도 아래)")
    return (1 - c["limit"] / c["actual"]) * 100


def worst(statuses):
    """가장 나쁜 status. 나쁜 순서는 STATUSES 순서이고, 모르는 status는 missing으로 본다."""
    return max((s if s in RANK else "missing" for s in statuses), key=RANK.get)


def metric(name, formula, inputs, fn, unit, note=""):
    """inputs: {이름: 값 envelope}. 입력이 하나라도 missing이면 missing, 아니면 가장 나쁜 status를 물려받는다."""
    base = {"metric": name, "formula": formula, "unit": unit, "inputs": list(inputs)}
    miss = [k for k, e in inputs.items() if e.get("value") is None]
    if miss:
        reasons = ["%s: %s" % (k, inputs[k].get("note") or "값 없음") for k in miss]
        return dict(base, value=None, status="missing", as_of=None, missing_inputs=miss,
                    note="; ".join(reasons))
    try:
        v = fn(*[e["value"] for e in inputs.values()])
    except ZeroDivisionError:
        return dict(base, value=None, status="missing", as_of=None, missing_inputs=[], note="0으로 나눔")
    except Undefined as e:
        return dict(base, value=None, status="missing", as_of=None, missing_inputs=[], note=str(e))
    st = worst([e["status"] for e in inputs.values()])
    if st == "reported":
        st = "derived"  # 계산한 값은 reported가 될 수 없다
    dates = [e["as_of"] for e in inputs.values() if e.get("as_of")]
    notes = [note] if note else []
    notes += ["%s %s: %s" % (k, e["status"], e["note"]) for k, e in inputs.items()
              if e["status"] in ("estimated", "stale") and e.get("note")]
    return dict(base, value=round(v, 4), status=st, as_of=min(dates) if dates else None,
                note="; ".join(notes))


def assume(m, note):
    """가정이 든 계산값: status를 estimated로 두고 가정을 note에 쓴다(missing은 그대로)."""
    if m["value"] is None:
        return m
    out = dict(m)
    if RANK[out["status"]] < RANK["estimated"]:
        out["status"] = "estimated"
    out["note"] = "; ".join(x for x in (note, m.get("note")) if x)
    return out


def first_of(*cands):
    """앞 후보부터 값이 있는 것을 쓴다. 모두 missing이면 첫 후보에 이유를 모아 낸다."""
    for c in cands:
        if c["value"] is not None:
            return c
    out = dict(cands[0])
    out["note"] = " | ".join("%s: %s" % (c["metric"], c.get("note", "")) for c in cands)
    return out


def passthrough(name, env, note="회사 발표값"):
    out = {"metric": name, "formula": note, "unit": env.get("unit", ""), "inputs": [name]}
    out.update({k: env.get(k) for k in ("value", "status", "as_of", "source", "locator", "note")})
    return out


def env_of(m):
    """metric 결과를 다른 metric의 입력으로 쓴다."""
    return {"value": m["value"], "status": m["status"], "as_of": m.get("as_of"), "note": m.get("note", "")}


# ---------------------------------------------------------------- 순수 계산

def ffo_nareit(ni, da, non_re, imp, gain, jv, nci, other):
    """Nareit FFO: 순이익 + 부동산 D&A + 감가 부동산 impairment − 부동산 매각 이익 + JV·NCI 조정."""
    return ni + (da - non_re) + imp - gain + jv + nci + other


def cagr_pct(first, last, years):
    if first <= 0 or years <= 0:
        raise ZeroDivisionError
    return ((last / first) ** (1.0 / years) - 1) * 100


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        raise ZeroDivisionError
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def pctile_le(values, x):
    return 100.0 * sum(1 for v in values if v <= x) / len(values)


def flag(name, m, th, basis_key):
    t = th[basis_key]
    basis = "경험칙, %s (watch %s, alert %s)" % (t["source"], t["watch"], t["alert"])
    if m["value"] is None:
        return {"flag": name, "level": "review", "value": None, "basis": basis,
                "note": "데이터가 없어 판단하지 않음(통과로 치지 않는다): " + m.get("note", "")}
    v = m["value"]
    if t["direction"] == "above":
        level = "alert" if v >= t["alert"] else "watch" if v >= t["watch"] else "ok"
    else:
        level = "alert" if v < t["alert"] else "watch" if v < t["watch"] else "ok"
    return {"flag": name, "level": level, "value": v, "status": m["status"], "basis": basis, "note": ""}


def dividend_history(S, M):
    """연간 주당 배당 series → dividend_cagr_3y·5y, dividend_cuts (REIT·일반 배당주 공통)."""
    annual = sorted([e for e in S["dividend_per_share_annual"] if e.get("period")], key=lambda e: e["period"])
    by_year = {str(e["period"]): e for e in annual}
    for n in (3, 5):
        a1 = annual[-1] if annual else None
        a0 = by_year.get(str(int(a1["period"]) - n)) if a1 and str(a1["period"]).isdigit() else None
        if a1 and a0 and a0["value"] is not None and a1["value"] is not None:
            M["dividend_cagr_%dy" % n] = metric("dividend_cagr_%dy" % n,
                                                "(%s 배당 / %s 배당)^(1/%d) − 1" % (a1["period"], a0["period"], n),
                                                {a0["period"]: a0, a1["period"]: a1},
                                                lambda x, y, n=n: cagr_pct(x, y, n), "%")
        else:
            want = (int(a1["period"]) - n) if a1 and str(a1["period"]).isdigit() else "?"
            M["dividend_cagr_%dy" % n] = dict(missing("연간 배당 history에 %s년(%d년 전) 값이나 끝 값이 없음" % (want, n)),
                                              metric="dividend_cagr_%dy" % n, formula="", inputs=[])
    known = [e for e in annual if e["value"] is not None]
    if len(known) >= 2:
        cuts = [b["period"] for a, b in zip(known, known[1:]) if b["value"] < a["value"]]
        M["dividend_cuts"] = {"metric": "dividend_cuts", "formula": "연간 배당이 전년보다 줄어든 해",
                              "unit": "years", "value": cuts, "status": worst([e["status"] for e in known]),
                              "as_of": known[-1].get("as_of"), "inputs": [e["period"] for e in known],
                              "note": "%s~%s 연간 값으로 본다(분기 중 컷은 놓칠 수 있다)" % (known[0]["period"], known[-1]["period"])}
        if M["dividend_cuts"]["status"] == "reported":
            M["dividend_cuts"]["status"] = "derived"
    else:
        M["dividend_cuts"] = dict(missing("연간 배당 history가 2년보다 짧음"), metric="dividend_cuts", formula="", inputs=[])


# ---------------------------------------------------------------- 바깥 데이터 (읽기만)

def load_macro(path, sid="DGS10"):
    """macro snapshot의 series 하나(기본 DGS10) envelope와 쓴 파일. 없으면 missing."""
    if path is None:
        files = sorted(glob.glob(os.path.join(data_dir(), "macro", "snapshots", "*.json")))
        path = files[-1] if files else None
    if not path or not os.path.exists(path):
        return missing("macro snapshot 없음: macro-check skill로 fetch를 먼저 돌린다"), None
    try:
        snap = read_json(path)
    except ValueError:
        return missing("macro snapshot을 읽지 못함: %s" % path), path
    if snap.get("schema") != MACRO_SCHEMA:
        return missing("macro snapshot schema가 %r" % snap.get("schema")), path
    e = (snap.get("series") or {}).get(sid)
    if not e or e.get("value") is None:
        return missing("macro snapshot에 %s 값 없음" % sid), path
    out = {"value": e["value"], "unit": "%", "status": e["status"], "as_of": e.get("as_of"),
           "source": e.get("source", "fred:" + sid), "locator": "%s series.%s" % (path, sid), "note": e.get("note", ""),
           "snapshot_date": snap.get("date")}
    return out, path


def macro_aged(env, price, th):
    """macro snapshot이 가격 기준일보다 허용 일수 넘게 앞서면 stale과 나이 note."""
    if env.get("value") is not None and price.get("as_of") and env.get("snapshot_date"):
        age = (parse_date(price["as_of"]) - parse_date(env["snapshot_date"])).days
        if age > th["macro_max_age_days"]:
            return dict(env, status="stale", note="macro snapshot이 가격 기준일보다 %d일 앞섬(허용 %d일)" % (
                age, th["macro_max_age_days"]))
    return env


def maturities_within_24m(mats, period_end):
    """만기 사다리에서 24개월 안 원금: period_end 해와 다음 해 전부 + 그다음 해 × (period_end 달 / 12).
    (합, 쓴 사다리 줄, 셋째 해를 나눠 넣었다는 note 또는 None)."""
    end = parse_date(period_end)
    total, used, split = 0.0, [], None
    for e in mats:
        p = str(e.get("period"))
        if not p.isdigit():
            continue
        y = int(p)
        if y in (end.year, end.year + 1):
            total += e["value"]
            used.append(e)
        elif y == end.year + 2:
            total += e["value"] * end.month / 12.0
            used.append(e)
            if end.month < 12:
                split = "%s년 만기를 12달에 고르게 나눠 %d/12만 넣음(가정)" % (p, end.month)
    return total, used, split


def load_reitwatch(dirpath, ticker):
    """REITWatch parsed 파일들에서 이 종목 row, 최신 달 peer, P/FFO history를 읽는다."""
    dirpath = dirpath or os.path.join(data_dir(), "reitwatch", "parsed")
    files = sorted(glob.glob(os.path.join(dirpath, "*.json")))
    months = []
    for p in files:
        try:
            d = read_json(p)
        except ValueError:
            continue
        if d.get("schema") == REITWATCH_SCHEMA and d.get("month") and isinstance(d.get("rows"), list):
            months.append((d["month"], p, d))
    months.sort(key=lambda x: x[0])
    if not months:
        return {"note": "REITWatch parsed 없음(%s): reit-sector-scan skill로 fetch를 먼저 돌린다" % dirpath,
                "files": []}
    month, path, latest = months[-1]
    row = next((r for r in latest["rows"] if r.get("ticker") == ticker), None)
    out = {"month": month, "path": path, "as_of": latest.get("as_of"), "files": [p for _, p, _ in months],
           "row": row, "note": ""}
    if row is None:
        out["note"] = "REITWatch %s에 %s row가 없음" % (month, ticker)
        return out
    sub = row.get("subsector")
    out["peers"] = [r for r in latest["rows"]
                    if r.get("subsector") == sub and r.get("ticker") != ticker]
    out["history"] = []
    sample = [x for x in months[:-1] if int(x[0][5:7]) in QUARTER_MONTHS] + [months[-1]]
    for m, p, d in sample:
        r = next((x for x in d["rows"] if x.get("ticker") == ticker), None)
        if r and is_finite(r.get("p_ffo_fy1")):
            out["history"].append((m, r["p_ffo_fy1"]))
    return out


def rw_env(rw, field, note):
    v = (rw.get("row") or {}).get(field)
    if not is_finite(v):
        return missing(rw["note"] or "REITWatch %s row에 %s 없음" % (rw.get("month"), field))
    return aged({"value": v, "unit": "", "status": "estimated", "as_of": rw.get("as_of") or rw.get("month"),
                 "source": "reitwatch:%s" % rw["month"], "locator": "%s rows[ticker=%s].%s" % (
                     rw["path"], rw["row"].get("ticker"), field), "note": note}, rw.get("age_note"))


def aged(env, age_note):
    """기준일이 허용 기간을 넘은 값: status는 지금 status와 stale 중 나쁜 쪽, 나이를 note에 붙인다."""
    if not age_note or env.get("value") is None:
        return env
    return dict(env, status=worst([env["status"], "stale"]),
                note="; ".join(x for x in (env.get("note"), age_note) if x))


def reitwatch_age_note(rw, ref_date, max_days):
    """REITWatch 가격 기준일이 ref_date(가격 기준일, 없으면 분기 끝날)보다 max_days 넘게 앞서면 그 이유 글."""
    if not rw.get("as_of") or not ref_date:
        return ""
    age = (parse_date(ref_date) - parse_date(rw["as_of"])).days
    if age <= max_days:
        return ""
    return "REITWatch %s 호(가격 기준일 %s)는 기준일 %s보다 %d일 앞섬(허용 %d일): reit-sector-scan fetch로 갱신" % (
        rw.get("month"), rw["as_of"], ref_date[:10], age, max_days)


# ---------------------------------------------------------------- metrics

def read_facts(facts_doc):
    """facts·series 칸을 검사하고 단위를 맞춘다. 칸 목록은 경로(REIT·일반 배당주)마다 다르다."""
    kinds, skinds = fact_kinds(facts_doc), series_kinds(facts_doc)
    raw = facts_doc.get("facts") or {}
    gaps = []
    F = {}
    for key, kind in kinds.items():
        env, bad = normalize(key, raw.get(key, missing("facts에 칸이 없음")), kind)
        F[key] = env
        if bad:
            gaps.append({"item": key, "status": "missing", "reason": env["note"]})
    for k in sorted(set(raw) - set(kinds)):
        gaps.append({"item": k, "status": "ignored", "reason": "모르는 facts 칸이라 쓰지 않음(references/facts-schema.md)"})
    S = {}
    rawser = facts_doc.get("series") or {}
    for key, kind in skinds.items():
        items = []
        for i, it in enumerate(rawser.get(key) or []):
            if kind == "covenant":
                env, bad = read_covenant("%s[%d]" % (key, i), it)
                items.append(env)
                if bad:
                    gaps.append({"item": "%s[%d]" % (key, i), "status": "missing", "reason": env["note"]})
                continue
            env, bad = normalize("%s[%d]" % (key, i), it, kind)
            env["period"] = it.get("period")
            env["label"] = it.get("label")
            items.append(env)
            if bad:
                gaps.append({"item": "%s[%d]" % (key, i), "status": "missing", "reason": env["note"]})
        S[key] = items
    return F, S, gaps


def walk(ms):
    for k, m in ms.items():
        if k == "sector_kpis":
            for kk, mm in m.items():
                yield "sector_kpis." + kk, mm
        else:
            yield k, m


def finish(facts_doc, path, M, checks, flags, gaps, inputs, thresholds):
    """metrics 문서를 맺는다: missing·estimated·stale·check 실패를 데이터 구멍에 올리고 exit code를 정한다."""
    for k, m in walk(M):
        if m.get("status") == "missing":
            gaps.append({"item": k, "status": "missing", "reason": m.get("note", "")})
        elif m.get("status") in ("estimated", "stale"):
            gaps.append({"item": k, "status": m["status"], "reason": m.get("note", "")})
    for c in checks:
        if not c["pass"]:
            gaps.append({"item": "check:" + c["check"], "status": "fail" if c["status"] != "missing" else "missing",
                         "reason": c["note"]})
    n_missing = sum(1 for _, m in walk(M) if m.get("status") == "missing")
    n_fail = sum(1 for c in checks if not c["pass"])
    doc = {
        "schema": METRICS_SCHEMA,
        "path": path,
        "ticker": facts_doc.get("ticker"),
        "period": facts_doc.get("period"),
        "period_end": facts_doc.get("period_end"),
        "inputs": inputs,
        "thresholds": thresholds,
        "metrics": M,
        "checks": checks,
        "flags": flags,
        "data_gaps": gaps,
        "summary": {"metrics_missing": n_missing, "checks_failed": n_fail,
                    "flags": {f["flag"]: f["level"] for f in flags}},
    }
    return doc, (2 if (n_missing or n_fail) else 0)


def compute(facts_doc, th, macro=None, rw=None, baa=None):
    """facts 문서 → metrics 문서. 같은 입력이면 같은 출력(시각을 넣지 않는다).
    macro는 DGS10, baa는 BAA10Y envelope(load_macro)."""
    if not is_reit_doc(facts_doc):
        return compute_dividend(facts_doc, th, macro=macro)
    F, S, gaps = read_facts(facts_doc)

    M = {}
    checks = []

    # FFO (Nareit)
    ffo_in = ["net_income_common", "depreciation_amortization", "non_real_estate_depreciation",
              "impairment_depreciable_re", "gain_on_sale_re", "unconsolidated_jv_ffo_adjustments",
              "nci_ffo_adjustments", "other_nareit_adjustments"]
    M["ffo_nareit"] = metric("ffo_nareit",
                             "NI to common + (D&A − 비부동산 감가) + 감가 부동산 impairment − 부동산 매각 이익 + JV 조정 + NCI 조정 + 기타 Nareit 조정",
                             {k: F[k] for k in ffo_in}, ffo_nareit, "USD thousands")
    tol = th["ffo_tie_out_tolerance_pct"]
    rec, rep = M["ffo_nareit"]["value"], F["ffo_reported"]["value"]
    if rec is None or rep is None:
        checks.append({"check": "ffo_tie_out", "pass": False, "status": "missing", "recomputed": rec, "reported": rep,
                       "diff_pct": None, "tolerance_pct": tol,
                       "note": "재계산이나 회사 발표값이 없어 맞춰 보지 못함"})
    elif rep == 0:
        checks.append({"check": "ffo_tie_out", "pass": False, "status": "derived", "recomputed": rec, "reported": rep,
                       "diff_pct": None, "tolerance_pct": tol,
                       "note": "회사 FFO가 0이라 차이 비율을 내지 못함: facts의 ffo_reported를 다시 본다"})
    else:
        diff = (rec - rep) / abs(rep) * 100
        checks.append({"check": "ffo_tie_out", "pass": abs(diff) <= tol, "status": "derived",
                       "recomputed": rec, "reported": rep, "diff_pct": round(diff, 4), "tolerance_pct": tol,
                       "note": "" if abs(diff) <= tol else
                       "Nareit 식 재계산이 회사 FFO와 %.4f%% 다름: facts의 FFO 입력이나 회사 정의를 다시 본다" % diff})

    # AFFO: 회사 정의를 따른다. bridge는 회사 표의 줄을 그대로 옮긴 것이다.
    bridge = facts_doc.get("affo_bridge") or {}
    start = bridge.get("start")
    items = []
    for i, it in enumerate(bridge.get("items") or []):
        env, bad = normalize("affo_bridge.items[%d]" % i, it, "money")
        env["label"] = it.get("label")
        env["category"] = it.get("category")
        items.append(env)
        if bad:
            gaps.append({"item": "affo_bridge.items[%d]" % i, "status": "missing", "reason": env["note"]})
        if env["category"] is not None and env["category"] not in BRIDGE_CATEGORIES:
            gaps.append({"item": "affo_bridge.items[%d]" % i, "status": "ignored",
                         "reason": "category %r는 %s 중 하나가 아니라 category 지표에 쓰지 않음" % (
                             env["category"], ", ".join(BRIDGE_CATEGORIES))})
    btol = th["affo_bridge_tolerance_pct"]
    if start not in BRIDGE_STARTS or not items:
        checks.append({"check": "affo_bridge", "pass": False, "status": "missing", "note":
                       "affo_bridge가 없거나 start가 %s 중 하나가 아님" % "/".join(BRIDGE_STARTS)})
        M["affo_bridge_sum"] = dict(missing("affo_bridge 없음"), metric="affo_bridge_sum", formula="", inputs=[])
    else:
        ins = {start: F[start]}
        ins.update({"item%d" % i: e for i, e in enumerate(items)})
        ins["affo_reported"] = F["affo_reported"]
        M["affo_bridge_sum"] = metric("affo_bridge_sum", "%s + bridge 항목 합" % start,
                                      {k: v for k, v in ins.items() if k != "affo_reported"},
                                      lambda *xs: sum(xs), "USD thousands")
        s, a = M["affo_bridge_sum"]["value"], F["affo_reported"]["value"]
        if s is None or a is None:
            checks.append({"check": "affo_bridge", "pass": False, "status": "missing", "sum": s, "reported": a,
                           "note": "bridge 항목이나 회사 AFFO가 없음"})
        elif a == 0:
            checks.append({"check": "affo_bridge", "pass": False, "status": "derived", "sum": s, "reported": a,
                           "diff_pct": None, "tolerance_pct": btol,
                           "note": "회사 AFFO가 0이라 차이 비율을 내지 못함: facts의 affo_reported를 다시 본다"})
        else:
            d = (s - a) / abs(a) * 100
            checks.append({"check": "affo_bridge", "pass": abs(d) <= btol, "status": "derived", "sum": s,
                           "reported": a, "diff_pct": round(d, 4), "tolerance_pct": btol,
                           "note": "" if abs(d) <= btol else "bridge 합이 회사 AFFO와 다름: 빠진 줄이나 부호를 본다"})
    M["affo_reported"] = passthrough("affo_reported", F["affo_reported"], "회사 정의 AFFO(회사 발표값)")
    M["ffo_reported"] = passthrough("ffo_reported", F["ffo_reported"])
    M["ffo_per_share_diluted"] = passthrough("ffo_per_share_diluted", F["ffo_per_share_diluted"])
    M["affo_per_share_diluted"] = first_of(
        passthrough("affo_per_share_diluted", F["affo_per_share_diluted"]),
        metric("affo_per_share_diluted", "AFFO 총액 / 분기 가중평균 희석 주식 수",
               {"affo_reported": F["affo_reported"], "diluted_shares_weighted": F["diluted_shares_weighted"]},
               lambda a, n: a / n, "USD/share", note="회사가 주당 AFFO를 내지 않아 총액으로 계산"))
    aps = env_of(M["affo_per_share_diluted"])

    # 연간 주당 FFO·AFFO (guidance 가운데 → consensus → 분기×4 가정)
    affo_mid = metric("affo_fy_guidance_mid", "(AFFO guidance low + high) / 2",
                      {"affo_guidance_low": F["affo_guidance_low"], "affo_guidance_high": F["affo_guidance_high"]},
                      lambda lo, hi: (lo + hi) / 2, "USD/share")
    affo_run = assume(metric("affo_run_rate", "분기 AFFO/share × 4",
                             {"affo_per_share_diluted": aps}, lambda a: a * 4, "USD/share"),
                      "guidance가 없어 분기×4로 가정")
    M["affo_fy"] = dict(first_of(affo_mid, affo_run), metric="affo_fy")

    rwd = dict(rw or {"note": "REITWatch를 읽지 않음", "files": []})
    rwd["age_note"] = reitwatch_age_note(rwd, F["price"].get("as_of") or facts_doc.get("period_end"),
                                         th["reitwatch_max_age_days"])
    if rwd["age_note"] and rwd.get("row"):
        gaps.append({"item": "reitwatch", "status": "stale", "reason": rwd["age_note"]})
    consensus = F["ffo_consensus_fy1"]
    if consensus["value"] is None and rwd.get("row"):
        consensus = rw_env(rwd, "ffo_fy1", "Nareit REITWatch consensus FFO FY1(애널리스트 추정, 회사 정의와 다를 수 있음)")
    elif consensus["value"] is None:
        consensus = missing(rwd["note"])
    ffo_cons = metric("ffo_fy_consensus", "consensus FFO FY1", {"ffo_consensus_fy1": consensus},
                      lambda x: x, "USD/share")
    if ffo_cons["value"] is not None:
        ffo_cons["status"] = consensus["status"]
    ffo_mid = metric("ffo_fy_guidance_mid", "(FFO guidance low + high) / 2",
                     {"ffo_guidance_low": F["ffo_guidance_low"], "ffo_guidance_high": F["ffo_guidance_high"]},
                     lambda lo, hi: (lo + hi) / 2, "USD/share")
    ffo_run = assume(metric("ffo_run_rate", "분기 FFO/share × 4",
                            {"ffo_per_share_diluted": F["ffo_per_share_diluted"]}, lambda a: a * 4, "USD/share"),
                     "guidance·consensus가 없어 분기×4로 가정")
    M["ffo_fy"] = dict(first_of(ffo_mid, ffo_cons, ffo_run), metric="ffo_fy")

    # 배당 지속성
    M["forward_dividend"] = metric("forward_dividend", "최근 정기 배당 × 연 지급 횟수(특별배당 제외)",
                                   {"dividend_regular_latest": F["dividend_regular_latest"],
                                    "dividend_frequency": F["dividend_frequency"]},
                                   lambda d, n: d * n, "USD/share")
    M["forward_yield"] = metric("forward_yield", "forward dividend / price × 100",
                                {"forward_dividend": env_of(M["forward_dividend"]), "price": F["price"]},
                                lambda d, p: d / p * 100, "%")
    M["affo_payout_forward"] = metric("affo_payout_forward", "forward dividend / 연간 AFFO per share × 100",
                                      {"forward_dividend": env_of(M["forward_dividend"]), "affo_fy": env_of(M["affo_fy"])},
                                      lambda d, a: d / a * 100, "%")
    M["affo_payout_quarter"] = metric("affo_payout_quarter", "분기 지급 배당/share / 분기 AFFO/share × 100",
                                      {"dividends_paid_per_share_quarter": F["dividends_paid_per_share_quarter"],
                                       "affo_per_share_diluted": aps},
                                      lambda d, a: d / a * 100, "%")
    M["ffo_payout_forward"] = metric("ffo_payout_forward", "forward dividend / 연간 FFO per share × 100",
                                     {"forward_dividend": env_of(M["forward_dividend"]), "ffo_fy": env_of(M["ffo_fy"])},
                                     lambda d, f: d / f * 100, "%",
                                     note="참고 줄(flag 없음). ffo_fy=" + M["ffo_fy"]["formula"])
    dividend_history(S, M)

    # AFFO bridge category: 유지 비용(maintenance capex + TI·LC) / NOI, straight-line rent / FFO
    def cat_sum(cats):
        got = [e for e in items if e.get("category") in cats]
        if not got:
            return missing("AFFO bridge에 %s 줄이 없음(회사가 AFFO에서 빼지 않았거나 category를 달지 않음)" % "·".join(cats)), []
        st = worst([e["status"] for e in got])
        return {"value": None if any(e["value"] is None for e in got) else sum(e["value"] for e in got),
                "status": st, "as_of": None, "note": "; ".join(e.get("note") or "" for e in got if e.get("note")),
                "label": ", ".join(str(e.get("label")) for e in got)}, got
    capex, capex_rows = cat_sum(("recurring_capex", "leasing_cost"))
    if capex["value"] is None and capex_rows:
        capex = missing("recurring_capex·leasing_cost 줄에 missing이 있음")
    sl, sl_rows = cat_sum(("straight_line",))
    if sl["value"] is None and sl_rows:
        sl = missing("straight_line 줄에 missing이 있음")
    M["recurring_capex_to_noi_pct"] = metric("recurring_capex_to_noi_pct",
                                             "−(recurring capex + leasing cost 줄, 분기) × 4 / 연환산 NOI × 100",
                                             {"recurring_capex_leasing": capex, "noi_annualized": F["noi_annualized"]},
                                             lambda c, n: -c * 4 / n * 100, "%",
                                             note="줄: " + capex.get("label", "") if capex["value"] is not None else "")
    M["straight_line_to_ffo_pct"] = metric("straight_line_to_ffo_pct", "−(straight-line 줄, 분기) / 분기 FFO × 100",
                                           {"straight_line": sl, "ffo_reported": F["ffo_reported"]},
                                           lambda x, f: -x / f * 100, "%",
                                           note="줄: " + sl.get("label", "") if sl["value"] is not None else "")

    # 재무
    M["net_debt"] = metric("net_debt", "total debt − cash", {"total_debt": F["total_debt"], "cash": F["cash"]},
                           lambda d, c: d - c, "USD thousands")
    ebitda_ann = first_of(passthrough("ebitdare_annualized", F["ebitdare_annualized"]),
                          assume(metric("ebitdare_run_rate", "분기 EBITDAre × 4", {"ebitdare_quarter": F["ebitdare_quarter"]},
                                        lambda e: e * 4, "USD thousands"),
                                 "회사가 연환산 값을 내지 않아 분기×4로 가정"))
    M["net_debt_to_ebitdare"] = metric("net_debt_to_ebitdare", "net debt / 연환산 EBITDAre",
                                       {"net_debt": env_of(M["net_debt"]), "ebitdare_annualized": env_of(ebitda_ann)},
                                       lambda n, e: n / e, "x")
    M["interest_coverage"] = metric("interest_coverage", "분기 EBITDAre / 분기 interest expense",
                                    {"ebitdare_quarter": F["ebitdare_quarter"], "interest_expense": F["interest_expense"]},
                                    lambda e, i: e / i, "x")
    M["fixed_rate_debt_pct"] = metric("fixed_rate_debt_pct", "fixed rate debt / debt principal × 100",
                                      {"fixed_rate_debt": F["fixed_rate_debt"], "debt_principal_total": F["debt_principal_total"]},
                                      lambda f, t: f / t * 100, "%")
    mats = S["debt_maturities"]
    if mats and all(e["value"] is not None for e in mats):
        total = sum(e["value"] for e in mats)
        by_period = {}
        for e in mats:  # 같은 해 만기가 여러 줄이면(날짜별 표) 해마다 더한다
            by_period[e["period"]] = by_period.get(e["period"], 0) + e["value"]
        ladder = [{"period": k, "value": round(v, 4), "pct": round(v / total * 100, 4)} for k, v in by_period.items()]
        years = [x for x in ladder if str(x["period"]).isdigit()]
        top = max(years, key=lambda x: x["pct"]) if years else None
        st = worst([e["status"] for e in mats])
        M["maturity_ladder"] = {"metric": "maturity_ladder", "formula": "해마다 만기 원금 / 만기 원금 합 × 100",
                                "unit": "%", "value": ladder, "status": "derived" if st == "reported" else st,
                                "as_of": min([e["as_of"] for e in mats if e.get("as_of")] or [None]),
                                "inputs": [e["period"] for e in mats], "note": "합 %.1f USD thousands" % total}
        M["maturity_max_year_pct"] = {"metric": "maturity_max_year_pct", "formula": "한 해(연도 칸) 만기 비중의 최대",
                                      "unit": "%", "value": top["pct"] if top else None,
                                      "status": M["maturity_ladder"]["status"] if top else "missing",
                                      "as_of": M["maturity_ladder"]["as_of"], "inputs": ["debt_maturities"],
                                      "note": ("%s년" % top["period"]) if top else "연도 칸이 없음"}
    else:
        why = "debt_maturities 없음" if not mats else "debt_maturities에 missing 칸이 있음"
        M["maturity_ladder"] = dict(missing(why), metric="maturity_ladder", formula="", inputs=[])
        M["maturity_max_year_pct"] = dict(missing(why), metric="maturity_max_year_pct", formula="", inputs=[])

    # 신용: gross assets(감가상각 전 장부가) 기준 비율, S&P식 fixed-charge coverage, covenant headroom
    M["gross_assets"] = metric("gross_assets", "total assets + accumulated depreciation",
                               {"total_assets": F["total_assets"], "accumulated_depreciation": F["accumulated_depreciation"]},
                               lambda a, d: a + d, "USD thousands")
    ga = env_of(M["gross_assets"])
    M["debt_pref_to_gross_assets"] = metric("debt_pref_to_gross_assets", "(debt principal + 우선주) / gross assets × 100",
                                            {"debt_principal_total": F["debt_principal_total"],
                                             "preferred_equity": F["preferred_equity"], "gross_assets": ga},
                                            lambda d, p, g: (d + p) / g * 100, "%")
    M["secured_debt_to_gross_assets"] = metric("secured_debt_to_gross_assets", "secured debt / gross assets × 100",
                                               {"secured_debt": F["secured_debt"], "gross_assets": ga},
                                               lambda s, g: s / g * 100, "%")
    un_assets = metric("unencumbered_assets_pct", "unencumbered assets / gross assets × 100",
                       {"unencumbered_assets": F["unencumbered_assets"], "gross_assets": ga},
                       lambda u, g: u / g * 100, "%")
    un_noi = metric("unencumbered_noi_pct", "unencumbered NOI / 연환산 NOI × 100",
                    {"unencumbered_noi": F["unencumbered_noi"], "noi_annualized": F["noi_annualized"]},
                    lambda u, n: u / n * 100, "%")
    M["unencumbered_pct"] = dict(first_of(un_assets, un_noi), metric="unencumbered_pct")
    M["fixed_charge_coverage"] = metric("fixed_charge_coverage",
                                        "분기 EBITDAre / (이자비용 + 자본화 이자 + 우선주 배당 + 정기 원금 상환)",
                                        {"ebitdare_quarter": F["ebitdare_quarter"], "interest_expense": F["interest_expense"],
                                         "capitalized_interest": F["capitalized_interest"],
                                         "preferred_dividends": F["preferred_dividends"],
                                         "principal_amortization": F["principal_amortization"]},
                                        lambda e, i, c, p, a: e / (i + c + p + a), "x")
    covs = S["covenants"]
    if not covs:
        why = "covenant 표가 없음(회사가 내지 않았거나 facts series.covenants가 비어 있음)"
        M["covenant_headroom"] = dict(missing(why), metric="covenant_headroom", formula="", inputs=[])
        M["covenant_headroom_min_pct"] = dict(missing(why), metric="covenant_headroom_min_pct", formula="", inputs=[])
    else:
        rows = []
        for c in covs:
            row = {k: c.get(k) for k in ("name", "direction", "limit", "actual", "unit", "status")}
            row["headroom_pct"] = None
            if c["status"] == "missing":
                row["note"] = c.get("note", "")
            else:
                try:
                    row["headroom_pct"] = round(covenant_headroom(c), 4)
                    row["note"] = c.get("note", "")
                except Undefined as e:
                    row.update(status="missing", note=str(e))
            rows.append(row)
        ok_rows = [r for r in rows if r["headroom_pct"] is not None]
        st = worst([r["status"] for r in ok_rows] or ["missing"])
        st = "derived" if st == "reported" else st
        dates = [c["as_of"] for c in covs if c.get("as_of")]
        M["covenant_headroom"] = {"metric": "covenant_headroom", "unit": "%", "value": rows,
                                  "status": st if ok_rows else "missing", "as_of": min(dates) if dates else None,
                                  "formula": "줄마다 max: (1 − actual/limit) × 100, min: (1 − limit/actual) × 100",
                                  "inputs": [r["name"] for r in rows],
                                  "note": "headroom은 한도에 닿기까지 좋은 쪽 값(자산·이익)이 줄 수 있는 비율. 회사 covenant 정의를 따른다"}
        if not ok_rows:
            M["covenant_headroom"]["value"] = None
        bad_rows = [r for r in rows if r["headroom_pct"] is None]
        if bad_rows:
            M["covenant_headroom_min_pct"] = dict(missing("headroom을 못 낸 줄이 있어 최소값을 고르지 않음: " + "; ".join(
                "%s: %s" % (r.get("name"), r.get("note")) for r in bad_rows)),
                metric="covenant_headroom_min_pct", formula="", inputs=[r["name"] for r in rows])
        else:
            low = min(rows, key=lambda r: r["headroom_pct"])
            M["covenant_headroom_min_pct"] = {
                "metric": "covenant_headroom_min_pct", "formula": "covenant 줄 headroom의 최소", "unit": "%",
                "value": low["headroom_pct"], "status": st, "as_of": M["covenant_headroom"]["as_of"],
                "inputs": [r["name"] for r in rows],
                "note": "%s (%s %s%s, actual %s%s)" % (low["name"], "≤" if low["direction"] == "max" else "≥",
                                                     low["limit"], low["unit"], low["actual"], low["unit"])}

    # 리파이낸싱·유동성: 24개월 안 만기를 시장 차입금리로 다시 빌릴 때의 이자 증가, 변동금리 충격, liquidity coverage
    dgs = macro_aged(macro if macro is not None else missing("macro snapshot을 읽지 않음"), F["price"], th)
    baa10y = macro_aged(baa if baa is not None else missing("macro snapshot을 읽지 않음"), F["price"], th)
    M["wam_years"] = passthrough("wam_years", F["wam_years"])
    M["weighted_avg_rate_pct"] = passthrough("weighted_avg_rate_pct", F["weighted_avg_rate_pct"])
    M["market_borrowing_rate_est"] = assume(metric(
        "market_borrowing_rate_est", "DGS10 + BAA10Y(Moody's Baa 회사채 금리)", {"dgs10": dgs, "baa10y": baa10y},
        lambda r, s: r + s, "%"), "투자등급 하단(Baa) 회사채 금리로 신규 차입금리를 어림(등급·만기·통화마다 다르다)")
    if mats and all(e["value"] is not None for e in mats) and facts_doc.get("period_end"):
        m24, used, split = maturities_within_24m(mats, facts_doc["period_end"])
        st = worst([e["status"] for e in used] or ["reported"])
        M["maturities_24m"] = {"metric": "maturities_24m", "unit": "USD thousands", "value": round(m24, 4),
                               "status": "derived" if st == "reported" else st, "as_of": facts_doc["period_end"],
                               "formula": "분기 끝날 해·다음 해 만기 + 그다음 해 만기 × (분기 끝 달 / 12)",
                               "inputs": [e["period"] for e in used],
                               "note": "%s 끝 뒤 24개월(%s년)" % (facts_doc["period_end"], "·".join(e["period"] for e in used))}
        if split:
            M["maturities_24m"] = assume(M["maturities_24m"], split)
    else:
        why = "debt_maturities 없음" if not mats else (
            "debt_maturities에 missing 칸이 있음" if facts_doc.get("period_end") else "facts에 period_end가 없음")
        M["maturities_24m"] = dict(missing(why), metric="maturities_24m", formula="", inputs=[])
    M["affo_annualized"] = assume(metric("affo_annualized", "분기 AFFO 총액 × 4", {"affo_reported": F["affo_reported"]},
                                         lambda a: a * 4, "USD thousands"), "분기 AFFO 총액 × 4로 연환산(가정)")

    def per_affo(scale=100):
        def fn(x, a):
            if a <= 0:
                raise Undefined("연간 AFFO 총액이 0 이하라 비율을 내지 않음")
            return x / a * scale
        return fn
    M["refi_cost_24m"] = metric("refi_cost_24m", "24개월 안 만기 × (시장 차입금리 추정 − 기존 가중평균 금리) / 100, 연간",
                                {"maturities_24m": env_of(M["maturities_24m"]),
                                 "market_borrowing_rate_est": env_of(M["market_borrowing_rate_est"]),
                                 "weighted_avg_rate_pct": F["weighted_avg_rate_pct"]},
                                lambda m, r, w: m * (r - w) / 100, "USD thousands",
                                note="기존 금리는 만기 부채가 아니라 전체 부채의 가중평균")
    M["refi_impact_affo_pct"] = metric("refi_impact_affo_pct", "refi 이자 증가(연간) / 연간 AFFO 총액 × 100",
                                       {"refi_cost_24m": env_of(M["refi_cost_24m"]),
                                        "affo_annualized": env_of(M["affo_annualized"])}, per_affo(), "%")
    M["floating_rate_shock_affo_pct"] = metric("floating_rate_shock_affo_pct",
                                               "변동금리 원금 × 1%p / 연간 AFFO 총액 × 100",
                                               {"floating_rate_debt": F["floating_rate_debt"],
                                                "affo_annualized": env_of(M["affo_annualized"])},
                                               lambda f, a: per_affo()(f * 0.01, a), "%")
    M["retained_cash_flow_24m"] = metric("retained_cash_flow_24m",
                                         "2 × (연간 AFFO 총액 − forward dividend × (보통주 + OP unit))",
                                         {"affo_annualized": env_of(M["affo_annualized"]),
                                          "forward_dividend": env_of(M["forward_dividend"]),
                                          "common_shares_outstanding": F["common_shares_outstanding"],
                                          "op_units_outstanding": F["op_units_outstanding"]},
                                         lambda a, d, s, u: 2 * (a - d * (s + u)), "USD thousands",
                                         note="배당 뒤 남는 AFFO가 24개월 이어진다고 가정. 음수면 그만큼 돈이 더 든다")
    M["liquidity_sources_24m"] = metric("liquidity_sources_24m", "현금 + revolver 잔여 + 유보 현금흐름(24개월)",
                                        {"liquidity_cash": F["liquidity_cash"], "revolver_available": F["revolver_available"],
                                         "retained_cash_flow_24m": env_of(M["retained_cash_flow_24m"])},
                                        lambda c, r, k: c + r + k, "USD thousands")
    M["liquidity_uses_24m"] = metric("liquidity_uses_24m", "24개월 안 만기 + 남은 개발비",
                                     {"maturities_24m": env_of(M["maturities_24m"]),
                                      "dev_remaining_spend": F["dev_remaining_spend"]},
                                     lambda m, d: m + d, "USD thousands")
    M["liquidity_coverage_24m"] = metric("liquidity_coverage_24m", "liquidity sources / uses (24개월)",
                                         {"liquidity_sources_24m": env_of(M["liquidity_sources_24m"]),
                                          "liquidity_uses_24m": env_of(M["liquidity_uses_24m"])},
                                         lambda a, b: a / b, "x")

    # 운영
    for k in ("occupancy_pct", "wale_years", "investment_grade_share_pct"):
        M[k] = passthrough(k, F[k])
    M["same_store_growth_pct"] = metric("same_store_growth_pct", "(same store 이번 기간 / 전년 같은 기간 − 1) × 100",
                                        {"same_store_current": F["same_store_current"], "same_store_prior": F["same_store_prior"]},
                                        lambda c, p: (c / p - 1) * 100, "%",
                                        note=F["same_store_current"].get("note", ""))
    tens = sorted([e for e in S["top_tenants"]], key=lambda e: -(e["value"] or 0))
    for n in (1, 5, 10):
        if len(tens) >= n and all(e["value"] is not None for e in tens[:n]):
            M["top%d_tenant_pct" % n] = metric("top%d_tenant_pct" % n, "임대료(ABR) 비중 상위 %d 임차인 합" % n,
                                               {(e.get("label") or "t%d" % i): e for i, e in enumerate(tens[:n])},
                                               lambda *xs: sum(xs), "%")
        else:
            M["top%d_tenant_pct" % n] = dict(missing("top_tenants가 %d개보다 적거나 missing" % n),
                                             metric="top%d_tenant_pct" % n, formula="", inputs=[])
    kpis = {}
    for k, v in sorted((facts_doc.get("sector_kpis") or {}).items()):
        # %는 단위를 맞추고, 다른 단위(properties, x 등)는 값 그대로 둔다. 값 규칙(check_value)은 단위와 상관없이 같다.
        if isinstance(v, dict) and v.get("unit") == "%":
            env, bad = normalize("sector_kpis.%s" % k, v, "pct")
        else:
            env, bad = check_value("sector_kpis.%s" % k, v)
            env = dict(env)
        if bad:
            gaps.append({"item": "sector_kpis.%s" % k, "status": "missing", "reason": env["note"]})
        kpis[k] = passthrough(k, env)
    M["sector_kpis"] = kpis

    # valuation
    M["market_cap"] = metric("market_cap", "price × (common shares + OP units)",
                             {"price": F["price"], "common_shares_outstanding": F["common_shares_outstanding"],
                              "op_units_outstanding": F["op_units_outstanding"]},
                             lambda p, s, u: p * (s + u), "USD thousands")
    M["p_ffo"] = metric("p_ffo", "price / 연간 FFO per share", {"price": F["price"], "ffo_fy": env_of(M["ffo_fy"])},
                        lambda p, f: p / f, "x", note="ffo_fy=" + M["ffo_fy"]["formula"])
    M["p_affo"] = metric("p_affo", "price / 연간 AFFO per share", {"price": F["price"], "affo_fy": env_of(M["affo_fy"])},
                         lambda p, a: p / a, "x", note="affo_fy=" + M["affo_fy"]["formula"])
    M["affo_yield"] = metric("affo_yield", "연간 AFFO per share / price × 100",
                             {"affo_fy": env_of(M["affo_fy"]), "price": F["price"]}, lambda a, p: a / p * 100, "%")
    M["implied_cap_rate"] = metric("implied_cap_rate", "연환산 NOI / (시총 + net debt + 우선주) × 100",
                                   {"noi_annualized": F["noi_annualized"], "market_cap": env_of(M["market_cap"]),
                                    "net_debt": env_of(M["net_debt"]), "preferred_equity": F["preferred_equity"]},
                                   lambda n, m, d, pf: n / (m + d + pf) * 100, "%")
    M["dgs10"] = passthrough("dgs10", dgs, "macro snapshot의 DGS10")
    M["yield_spread_vs_10y"] = metric("yield_spread_vs_10y", "forward yield − 10년물(DGS10), %p",
                                      {"forward_yield": env_of(M["forward_yield"]), "dgs10": dgs},
                                      lambda y, r: y - r, "%p")
    M["affo_yield_spread_vs_10y"] = metric("affo_yield_spread_vs_10y", "AFFO yield − 10년물(DGS10), %p",
                                           {"affo_yield": env_of(M["affo_yield"]), "dgs10": dgs},
                                           lambda y, r: y - r, "%p")

    # 자기 history·peer (REITWatch, 같은 출처끼리 견준다)
    M["subsector"] = {"metric": "subsector", "formula": "REITWatch subsector", "unit": "",
                      "value": (rwd.get("row") or {}).get("subsector"),
                      "status": "estimated" if rwd.get("row") else "missing",
                      "as_of": rwd.get("as_of"), "inputs": [], "note": rwd.get("note") or "Nareit REITWatch 분류"}
    rw_pffo = rw_env(rwd, "p_ffo_fy1", "Nareit REITWatch P/FFO FY1(가격 / consensus FFO)") if rwd.get("month") else missing(rwd["note"])
    M["p_ffo_reitwatch"] = passthrough("p_ffo_reitwatch", rw_pffo, "REITWatch P/FFO FY1")
    hist = [v for _, v in rwd.get("history", [])]
    if rw_pffo["value"] is None:
        M["p_ffo_history_pctile"] = dict(missing(rw_pffo["note"]), metric="p_ffo_history_pctile", formula="", inputs=[])
    elif len(hist) < th["history_min_points"]:
        M["p_ffo_history_pctile"] = dict(missing("REITWatch history 표본 %d개(필요 %d개, 1·4·7·10월 호 + 최신 호)" % (
            len(hist), th["history_min_points"])), metric="p_ffo_history_pctile", formula="", inputs=[])
    else:
        M["p_ffo_history_pctile"] = dict(metric("p_ffo_history_pctile", "자기 REITWatch P/FFO history 중 최신값 이하 비율(%%), 표본 %d개 호" % len(hist),
                                                {"p_ffo_reitwatch": rw_pffo}, lambda x: pctile_le(hist, x), "%"),
                                         status="estimated")
        M["p_ffo_history_pctile"]["note"] = "; ".join(x for x in (
            "%s~%s의 1·4·7·10월 호와 최신 호" % (rwd["history"][0][0], rwd["history"][-1][0]), rwd["age_note"]) if x)
    peers = [r for r in rwd.get("peers", []) if is_finite(r.get("p_ffo_fy1"))]
    if rw_pffo["value"] is None or not peers:
        why = rw_pffo["note"] if rw_pffo["value"] is None else "같은 subsector peer가 없음"
        M["peer_median_p_ffo"] = dict(missing(why), metric="peer_median_p_ffo", formula="", inputs=[])
        M["p_ffo_vs_peer"] = dict(missing(why), metric="p_ffo_vs_peer", formula="", inputs=[])
    else:
        pm = median([r["p_ffo_fy1"] for r in peers])
        M["peer_median_p_ffo"] = aged({"metric": "peer_median_p_ffo", "formula": "같은 subsector peer의 REITWatch P/FFO median",
                                       "unit": "x", "value": round(pm, 4), "status": "estimated", "as_of": rwd.get("as_of"),
                                       "inputs": sorted(r.get("ticker") for r in peers),
                                       "note": "%s %s, peer %d개" % (rwd["month"], M["subsector"]["value"], len(peers))},
                                      rwd["age_note"])
        M["p_ffo_vs_peer"] = metric("p_ffo_vs_peer", "REITWatch P/FFO / peer median",
                                    {"p_ffo_reitwatch": rw_pffo, "peer_median_p_ffo": env_of(M["peer_median_p_ffo"])},
                                    lambda a, b: a / b, "x")

    flags = [
        flag("affo_payout", M["affo_payout_forward"] if M["affo_payout_forward"]["value"] is not None
             else M["affo_payout_quarter"], th, "affo_payout_pct"),
        flag("maturity_concentration", M["maturity_max_year_pct"], th, "maturity_single_year_pct"),
        flag("net_debt_to_ebitdare", M["net_debt_to_ebitdare"], th, "net_debt_to_ebitdare_x"),
        flag("interest_coverage", M["interest_coverage"], th, "interest_coverage_x"),
        flag("covenant_headroom", M["covenant_headroom_min_pct"], th, "covenant_headroom_pct"),
    ]

    return finish(facts_doc, "reit", M, checks, flags, gaps,
                  {"macro_snapshot": (macro or {}).get("locator", "").split(" ")[0] or None,
                   "reitwatch_files": rwd.get("files", []), "reitwatch_month": rwd.get("month")},
                  {k: th[k] for k in ("affo_payout_pct", "maturity_single_year_pct", "net_debt_to_ebitdare_x",
                                      "interest_coverage_x", "covenant_headroom_pct", "ffo_tie_out_tolerance_pct")})


def chowder_threshold(yield_pct, sic, ch):
    """Chowder rule 기준: utility(SIC 49xx) 8, yield 3% 미만 15, 그 외 12."""
    lo, hi = ch["utility_sic_range"]
    try:
        if lo <= int(str(sic)) <= hi:
            return ch["min_utility"], "utility(SIC %s)" % sic
    except ValueError:
        pass
    if yield_pct < ch["low_yield_pct"]:
        return ch["min_low_yield"], "yield %.2f%% < %.1f%%" % (yield_pct, ch["low_yield_pct"])
    return ch["min_default"], "yield %.2f%% ≥ %.1f%%" % (yield_pct, ch["low_yield_pct"])


def compute_dividend(facts_doc, th, macro=None):
    """일반 배당주(SIC가 6798이 아님) 경로: EPS·FCF payout, 배당 CAGR, Chowder rule, yield trap 신호."""
    F, S, gaps = read_facts(facts_doc)
    M = {}
    sic = (facts_doc.get("company") or {}).get("sic") or ""

    M["net_income_common"] = passthrough("net_income_common", F["net_income_common"], "회사 발표 순이익(분기)")
    M["eps_diluted_quarter"] = passthrough("eps_diluted_quarter", F["eps_diluted_quarter"], "회사 발표 GAAP diluted EPS(분기)")
    eps_mid = metric("eps_fy_guidance_mid", "(EPS guidance low + high) / 2",
                     {"eps_guidance_low": F["eps_guidance_low"], "eps_guidance_high": F["eps_guidance_high"]},
                     lambda lo, hi: (lo + hi) / 2, "USD/share", note=F["eps_guidance_low"].get("note", ""))
    eps_run = assume(metric("eps_run_rate", "분기 diluted EPS × 4", {"eps_diluted_quarter": F["eps_diluted_quarter"]},
                            lambda e: e * 4, "USD/share"), "guidance가 없어 분기×4로 가정")
    M["eps_fy"] = dict(first_of(eps_mid, eps_run), metric="eps_fy")

    M["forward_dividend"] = metric("forward_dividend", "최근 정기 배당 × 연 지급 횟수(특별배당 제외)",
                                   {"dividend_regular_latest": F["dividend_regular_latest"],
                                    "dividend_frequency": F["dividend_frequency"]},
                                   lambda d, n: d * n, "USD/share")
    M["forward_yield"] = metric("forward_yield", "forward dividend / price × 100",
                                {"forward_dividend": env_of(M["forward_dividend"]), "price": F["price"]},
                                lambda d, p: d / p * 100, "%")

    def ratio(why, scale=100):
        def fn(a, b):
            if b <= 0:
                raise Undefined(why)
            return a / b * scale
        return fn
    M["eps_payout_forward"] = metric("eps_payout_forward", "forward dividend / 연간 EPS × 100",
                                     {"forward_dividend": env_of(M["forward_dividend"]), "eps_fy": env_of(M["eps_fy"])},
                                     ratio("연간 EPS가 0 이하라 payout을 내지 않음"), "%",
                                     note="eps_fy=" + M["eps_fy"]["formula"])
    M["eps_payout_quarter"] = metric("eps_payout_quarter", "분기 선언 배당/share / 분기 diluted EPS × 100",
                                     {"dividends_declared_per_share_quarter": F["dividends_declared_per_share_quarter"],
                                      "eps_diluted_quarter": F["eps_diluted_quarter"]},
                                     ratio("분기 EPS가 0 이하라 payout을 내지 않음"), "%")
    M["fcf_ytd"] = metric("fcf_ytd", "영업활동 현금흐름(YTD) − 설비투자(YTD)",
                          {"operating_cash_flow_ytd": F["operating_cash_flow_ytd"], "capex_ytd": F["capex_ytd"]},
                          lambda o, c: o - c, "USD thousands")
    M["fcf_payout_ytd"] = metric("fcf_payout_ytd", "보통주 배당 지급(YTD) / FCF(YTD) × 100",
                                 {"dividends_paid_ytd": F["dividends_paid_ytd"], "fcf_ytd": env_of(M["fcf_ytd"])},
                                 ratio("FCF가 0 이하라 payout을 내지 않음(배당을 FCF로 못 덮음)"), "%")
    dividend_history(S, M)

    ch = th["chowder"]
    M["chowder_number"] = metric("chowder_number", "forward yield + 5년 배당 CAGR (%p)",
                                 {"forward_yield": env_of(M["forward_yield"]),
                                  "dividend_cagr_5y": env_of(M["dividend_cagr_5y"])},
                                 lambda y, g: y + g, "%")
    if M["forward_yield"]["value"] is None:
        M["chowder_threshold"] = dict(missing("forward yield가 없어 기준을 고르지 못함"), metric="chowder_threshold",
                                      formula="", inputs=[])
    else:
        tv, why = chowder_threshold(M["forward_yield"]["value"], sic, ch)
        M["chowder_threshold"] = dict(metric("chowder_threshold", "Chowder 기준: utility 8, yield 3%% 미만 15, 그 외 12",
                                             {"forward_yield": env_of(M["forward_yield"])}, lambda y: tv, "%"),
                                      note=why)
    M["p_e"] = metric("p_e", "price / 연간 EPS", {"price": F["price"], "eps_fy": env_of(M["eps_fy"])},
                      ratio("연간 EPS가 0 이하라 P/E를 내지 않음", 1), "x", note="eps_fy=" + M["eps_fy"]["formula"])
    dgs = macro if macro is not None else missing("macro snapshot을 읽지 않음")
    if dgs.get("value") is not None and F["price"].get("as_of") and dgs.get("snapshot_date"):
        age = (parse_date(F["price"]["as_of"]) - parse_date(dgs["snapshot_date"])).days
        if age > th["macro_max_age_days"]:
            dgs = dict(dgs, status="stale", note="macro snapshot이 가격 기준일보다 %d일 앞섬(허용 %d일)" % (
                age, th["macro_max_age_days"]))
    M["dgs10"] = passthrough("dgs10", dgs, "macro snapshot의 DGS10")
    M["yield_spread_vs_10y"] = metric("yield_spread_vs_10y", "forward yield − 10년물(DGS10), %p",
                                      {"forward_yield": env_of(M["forward_yield"]), "dgs10": dgs},
                                      lambda y, r: y - r, "%p")

    # yield trap 신호: True/False, 판단할 값이 없으면 None
    yt = th["yield_trap"]

    def over(m, limit, base_env=None):
        if m["value"] is not None:
            return m["value"] >= limit
        if base_env is not None and base_env.get("value") is not None and base_env["value"] <= 0:
            return True  # 분모(EPS·FCF)가 0 이하
        return None
    pay = M["eps_payout_forward"] if M["eps_payout_forward"]["value"] is not None else M["eps_payout_quarter"]
    pay_base = M["eps_fy"] if M["eps_payout_forward"]["value"] is not None or M["eps_fy"]["value"] is not None \
        else F["eps_diluted_quarter"]
    cuts = M["dividend_cuts"]["value"]
    last_year = max([int(e["period"]) for e in S["dividend_per_share_annual"] if str(e.get("period", "")).isdigit()]
                    or [0])
    signals = {
        "high_yield": None if M["forward_yield"]["value"] is None else M["forward_yield"]["value"] >= yt["high_yield_pct"],
        "payout_over_100": over(pay, 100, pay_base),
        "fcf_shortfall": over(M["fcf_payout_ytd"], 100, M["fcf_ytd"]),
        "recent_cut": None if cuts is None else any(int(y) > last_year - yt["cut_lookback_years"] for y in cuts),
        "no_growth": None if M["dividend_cagr_5y"]["value"] is None else M["dividend_cagr_5y"]["value"] <= 0,
    }
    n_true = sum(1 for v in signals.values() if v)
    unknown = [k for k, v in signals.items() if v is None]
    fcf_used = M["fcf_payout_ytd"] if M["fcf_payout_ytd"]["value"] is not None else M["fcf_ytd"]
    used = [M["forward_yield"], pay if pay["value"] is not None else pay_base, fcf_used, M["dividend_cuts"],
            M["dividend_cagr_5y"]]
    known_status = worst(["derived"] + [e["status"] for e in used if e.get("value") is not None])
    sig_status = "missing" if unknown else known_status
    M["yield_trap_signals"] = {"metric": "yield_trap_signals", "unit": "",
                               "formula": "높은 yield(≥%s%%), payout ≥100%%, 배당 ≥ FCF, 최근 %d년 안 컷, 5년 배당 CAGR ≤ 0" % (
                                   yt["high_yield_pct"], yt["cut_lookback_years"]),
                               "value": signals, "status": sig_status,
                               "as_of": None, "inputs": list(signals),
                               "note": ("판단 못 한 신호: " + ", ".join(unknown)) if unknown else ""}
    basis = "경험칙, %s" % yt["source"]
    trap_level = "alert" if n_true >= 2 else "watch" if n_true == 1 else "review" if unknown else "ok"

    cm, ct = M["chowder_number"], M["chowder_threshold"]
    ch_basis = "경험칙, %s" % ch["source"]
    if cm["value"] is None or ct["value"] is None:
        ch_flag = {"flag": "chowder", "level": "review", "value": None, "basis": ch_basis,
                   "note": "데이터가 없어 판단하지 않음(통과로 치지 않는다): " + (cm.get("note") or ct.get("note") or "")}
    else:
        ch_flag = {"flag": "chowder", "level": "ok" if cm["value"] >= ct["value"] else "watch", "value": cm["value"],
                   "status": cm["status"], "basis": ch_basis, "note": "기준 %s (%s)" % (ct["value"], ct["note"])}
    flags = [
        flag("eps_payout", pay, th, "eps_payout_pct"),
        flag("fcf_payout", M["fcf_payout_ytd"], th, "fcf_payout_pct"),
        ch_flag,
        dict({"flag": "yield_trap", "level": trap_level, "value": n_true, "basis": basis,
              "note": "참인 신호: %s" % (", ".join(k for k, v in signals.items() if v) or "없음")},
             **({} if trap_level == "review" else {"status": known_status})),
    ]
    q = M["eps_payout_quarter"]["value"]
    if pay is M["eps_payout_forward"] and q is not None and q >= 100:
        flags[0] = dict(flags[0], note="flag는 연간(guidance) EPS로 본다. 분기 GAAP EPS payout은 %.1f%%로 100%%를 넘는다: "
                                       "그 분기 일회성 항목(소송·M&A 비용 등)을 확인한다" % q)
    if pay["value"] is None and signals["payout_over_100"]:
        flags[0] = dict(flags[0], level="alert", note="EPS가 0 이하(이익보다 배당이 많음)")
    if M["fcf_payout_ytd"]["value"] is None and signals["fcf_shortfall"]:
        flags[1] = dict(flags[1], level="alert", note="FCF가 0 이하(배당을 FCF로 못 덮음)")
    return finish(facts_doc, "dividend", M, [], flags, gaps,
                  {"macro_snapshot": (macro or {}).get("locator", "").split(" ")[0] or None},
                  {k: th[k] for k in ("eps_payout_pct", "fcf_payout_pct", "chowder", "yield_trap")})


# ---------------------------------------------------------------- diff·compare (metrics 파일끼리)

def scalar_rows(doc):
    """metrics 문서의 지표를 (이름, envelope)로 편다. sector_kpis는 sector_kpis.<이름>이다."""
    return list(walk(doc.get("metrics") or {}))


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def diff_metrics(prev, curr):
    """같은 종목 두 분기 metrics → (diff 문서, exit). 지표별 전후 값·변화, status·flag가 나빠진 것."""
    bad = []
    for name, d in (("prev", prev), ("curr", curr)):
        if d.get("schema") != METRICS_SCHEMA:
            bad.append("%s schema가 %r" % (name, d.get("schema")))
    if prev.get("ticker") != curr.get("ticker"):
        bad.append("ticker가 다름: %s, %s" % (prev.get("ticker"), curr.get("ticker")))
    if not (str(prev.get("period") or "") < str(curr.get("period") or "")):
        bad.append("prev 분기(%s)가 curr 분기(%s)보다 앞서야 함" % (prev.get("period"), curr.get("period")))
    if bad:
        return {"error": bad}, 1
    P, C = dict(scalar_rows(prev)), dict(scalar_rows(curr))
    names = list(C) + [k for k in P if k not in C]
    rows, worse_status, n_missing = [], [], 0
    for k in names:
        p = P.get(k) or missing("이전 metrics에 없는 지표")
        c = C.get(k) or missing("지금 metrics에 없는 지표")
        unit = c.get("unit") or p.get("unit") or ""
        pv, cv = p.get("value"), c.get("value")
        ps, cs = p.get("status", "missing"), c.get("status", "missing")
        pct_unit = unit in ("%", "%p")
        row = {"metric": k, "unit": unit,
               "prev": {"value": pv, "status": ps}, "curr": {"value": cv, "status": cs},
               "change": None, "change_unit": "%p" if pct_unit else unit, "change_pct": None,
               "status_change": "same" if ps == cs else ("worse" if RANK.get(cs, 4) > RANK.get(ps, 4) else "better"),
               "changed": pv != cv or ps != cs, "note": ""}
        if is_num(pv) and is_num(cv):
            row["change"] = round(cv - pv, 4)
            if not pct_unit and pv != 0:
                row["change_pct"] = round((cv - pv) / abs(pv) * 100, 4)
        elif pv is None or cv is None:
            n_missing += 1
            row["note"] = "; ".join(x for x in (
                "prev: " + (p.get("note") or "값 없음") if pv is None else "",
                "curr: " + (c.get("note") or "값 없음") if cv is None else "") if x)
        if row["status_change"] == "worse":
            worse_status.append(k)
        rows.append(row)
    pf = {f["flag"]: f.get("level") for f in prev.get("flags") or []}
    cf = {f["flag"]: f.get("level") for f in curr.get("flags") or []}
    flags, worse_flags = [], []
    for f in list(cf) + [x for x in pf if x not in cf]:
        a, b = pf.get(f, "review"), cf.get(f, "review")
        if a == b:
            ch = "same"
        elif a not in FLAG_RANK or b not in FLAG_RANK:
            ch = "review"
        else:
            ch = "worse" if FLAG_RANK[b] > FLAG_RANK[a] else "better"
        if ch == "worse":
            worse_flags.append(f)
        flags.append({"flag": f, "prev": a, "curr": b, "change": ch})
    pc = {c["check"]: c.get("pass") for c in prev.get("checks") or []}
    cc = {c["check"]: c.get("pass") for c in curr.get("checks") or []}
    checks = [{"check": k, "prev_pass": pc.get(k), "curr_pass": cc.get(k)} for k in list(cc) + [x for x in pc if x not in cc]]
    doc = {"schema": DIFF_SCHEMA, "ticker": curr.get("ticker"), "path": curr.get("path", "reit"),
           "prev_period": prev.get("period"), "curr_period": curr.get("period"),
           "rows": rows, "flags": flags, "checks": checks,
           "worse": {"status": worse_status, "flags": worse_flags},
           "summary": {"rows": len(rows), "changed": sum(1 for r in rows if r["changed"]),
                       "missing": n_missing, "status_worse": len(worse_status), "flags_worse": len(worse_flags),
                       "checks_failed_prev": sum(1 for v in pc.values() if v is False),
                       "checks_failed_curr": sum(1 for v in cc.values() if v is False)}}
    failed = [k for k in (list(pc) + list(cc)) if pc.get(k) is False or cc.get(k) is False]
    return doc, (2 if n_missing or failed else 0)


def compare_metrics(docs):
    """여러 종목 metrics → (compare 문서, exit). 같은 표에 놓고, subsector·경로·분기가 다르면 경고한다."""
    tickers = [d.get("ticker") for d in docs]
    bad = []
    if len(docs) < 2:
        bad.append("종목이 둘 이상이어야 함")
    if len(set(tickers)) != len(tickers):
        bad.append("같은 종목이 두 번 있음: %s" % tickers)
    bad += ["%s schema가 %r" % (d.get("ticker"), d.get("schema")) for d in docs if d.get("schema") != METRICS_SCHEMA]
    if bad:
        return {"error": bad}, 1
    subs = [((d.get("metrics") or {}).get("subsector") or {}).get("value") for d in docs]
    known = [s for s in subs if s]
    counts = {s: known.count(s) for s in known}
    top = max(counts.values()) if counts else 0
    mode = [s for s in counts if counts[s] == top]
    major = mode[0] if len(mode) == 1 and top > 1 else None
    warnings, columns = [], []
    for d, s in zip(docs, subs):
        w = ""
        if not s:
            w = "subsector를 모름(REITWatch에 row 없음): 같은 섹터인지 확인하지 못함"
        elif major is None and len(set(known)) > 1:
            w = "subsector %s: 다른 종목과 subsector가 다름" % s
        elif major is not None and s != major:
            w = "subsector %s가 다수(%s)와 다름: P/FFO·cap rate를 같은 기준으로 견주지 않는다" % (s, major)
        columns.append({"ticker": d.get("ticker"), "period": d.get("period"), "path": d.get("path", "reit"),
                        "subsector": s, "warning": w,
                        "metrics_missing": (d.get("summary") or {}).get("metrics_missing"),
                        "checks_failed": (d.get("summary") or {}).get("checks_failed")})
        if w:
            warnings.append("%s: %s" % (d.get("ticker"), w))
    paths = sorted(set(c["path"] for c in columns))
    if len(paths) > 1:
        warnings.append("REIT와 일반 배당주를 섞음(%s): FFO·AFFO 지표와 EPS·FCF 지표는 서로 견주지 않는다" % ", ".join(
            "%s=%s" % (c["ticker"], c["path"]) for c in columns))
    periods = sorted(set(str(c["period"]) for c in columns))
    if len(periods) > 1:
        warnings.append("분기가 다름(%s): 같은 시점 값이 아니다" % ", ".join(
            "%s=%s" % (c["ticker"], c["period"]) for c in columns))
    per = [dict(scalar_rows(d)) for d in docs]
    names = []
    for p in per:
        names += [k for k in p if k not in names]
    rows = []
    for k in names:
        vals, nums = {}, []
        unit = next((p[k].get("unit") for p in per if k in p and p[k].get("unit")), "")
        for t, p in zip(tickers, per):
            e = p.get(k)
            if e is None:
                vals[t] = {"value": None, "status": "missing", "note": "이 종목 metrics에 없는 지표"}
            else:
                vals[t] = {"value": e.get("value"), "status": e.get("status", "missing")}
                if is_num(e.get("value")):
                    nums.append((e["value"], e.get("status", "missing")))
        if len(nums) >= 2 and all(is_num(v["value"]) or v["value"] is None for v in vals.values()):
            st = worst([s for _, s in nums])
            med = {"value": round(median([v for v, _ in nums]), 4), "status": "derived" if st == "reported" else st,
                   "n": len(nums)}
        else:
            med = {"value": None, "status": "missing", "n": len(nums)}
        rows.append({"metric": k, "unit": unit, "values": vals, "median": med})
    flags = {d.get("ticker"): {f["flag"]: f.get("level") for f in d.get("flags") or []} for d in docs}
    checks = {d.get("ticker"): {c["check"]: c.get("pass") for c in d.get("checks") or []} for d in docs}
    incomplete = [c["ticker"] for c in columns if (c["metrics_missing"] or 0) or (c["checks_failed"] or 0)]
    doc = {"schema": COMPARE_SCHEMA, "tickers": tickers, "columns": columns, "warnings": warnings,
           "rows": rows, "flags": flags, "checks": checks,
           "summary": {"tickers": len(docs), "warnings": len(warnings), "incomplete": incomplete,
                       "median_note": "median은 이 표에 놓인 종목끼리의 값이다(REITWatch peer median과 다르다)"}}
    return doc, (2 if warnings or incomplete else 0)


# ---------------------------------------------------------------- CLI

def metrics_path(doc):
    return os.path.join(data_dir(), "tickers", doc["ticker"], "metrics", "%s.json" % doc["period"])


def write_json(doc, path):
    text = json.dumps(doc, ensure_ascii=False, indent=2, allow_nan=False)  # NaN·Infinity 토큰을 쓰지 않는다
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    os.replace(tmp, path)


def cmd_metrics(args):
    try:
        facts = read_json(args.facts)
    except (OSError, ValueError) as e:
        log("facts를 읽지 못함: %s" % e)
        return 1
    if facts.get("schema") != FACTS_SCHEMA or not facts.get("ticker") or not facts.get("period"):
        log("facts 첫 칸이 \"schema\": \"%s\"이고 ticker·period가 있어야 함" % FACTS_SCHEMA)
        return 1
    th = load_thresholds()
    macro, _ = load_macro(args.macro_snapshot)
    baa, _ = load_macro(args.macro_snapshot, "BAA10Y")
    rw = load_reitwatch(args.reitwatch_dir, facts["ticker"])
    doc, code = compute(facts, th, macro=macro, rw=rw, baa=baa)
    doc["facts_file"] = os.path.abspath(args.facts)
    path = args.out or metrics_path(doc)
    write_json(doc, path)
    for c in doc["checks"]:
        log("%s %s: %s" % ("PASS" if c["pass"] else "FAIL", c["check"],
                           c.get("note") or "diff %s%%" % c.get("diff_pct")))
    for f in doc["flags"]:
        log("flag %s: %s (%s)" % (f["flag"], f["level"], f["value"]))
    log("missing metric %d, check 실패 %d, 데이터 구멍 %d" % (
        doc["summary"]["metrics_missing"], doc["summary"]["checks_failed"], len(doc["data_gaps"])))
    print(path)
    return code


def load_metrics(paths):
    docs = []
    for path in paths:
        try:
            d = read_json(path)
        except (OSError, ValueError) as e:
            log("metrics를 읽지 못함: %s" % e)
            return None
        d["_file"] = os.path.abspath(path)
        docs.append(d)
    return docs


def emit(doc, out):
    if out:
        write_json(doc, out)
        print(out)
    else:
        print(json.dumps(doc, ensure_ascii=False, indent=2, allow_nan=False))


def cmd_diff(args):
    docs = load_metrics([args.prev, args.curr])
    if docs is None:
        return 1
    doc, code = diff_metrics(docs[0], docs[1])
    if code == 1:
        log("; ".join(doc["error"]))
        return 1
    doc["files"] = {"prev": docs[0]["_file"], "curr": docs[1]["_file"]}
    for r in doc["rows"]:
        if r["status_change"] == "worse":
            log("status 나빠짐 %s: %s → %s" % (r["metric"], r["prev"]["status"], r["curr"]["status"]))
    for f in doc["flags"]:
        if f["change"] != "same":
            log("flag %s: %s → %s (%s)" % (f["flag"], f["prev"], f["curr"], f["change"]))
    s = doc["summary"]
    log("지표 %d, 바뀜 %d, 한쪽 missing %d, status 나빠짐 %d, flag 나빠짐 %d" % (
        s["rows"], s["changed"], s["missing"], s["status_worse"], s["flags_worse"]))
    emit(doc, args.out)
    return code


def cmd_compare(args):
    docs = load_metrics(args.metrics)
    if docs is None:
        return 1
    doc, code = compare_metrics(docs)
    if code == 1:
        log("; ".join(doc["error"]))
        return 1
    doc["files"] = {d.get("ticker"): d["_file"] for d in docs}
    for w in doc["warnings"]:
        log("경고: " + w)
    if doc["summary"]["incomplete"]:
        log("missing이나 check 실패가 있는 종목: %s" % ", ".join(doc["summary"]["incomplete"]))
    log("종목 %d, 지표 %d, 경고 %d" % (len(docs), len(doc["rows"]), len(doc["warnings"])))
    emit(doc, args.out)
    return code


def main(argv):
    p = argparse.ArgumentParser(prog="reit_calc.py", description="REIT facts → metrics")
    p.add_argument("--check", action="store_true", help="network 없이 fixture로 확인")
    sub = p.add_subparsers(dest="cmd")
    m = sub.add_parser("metrics", help="facts에서 지표를 계산한다")
    m.add_argument("--facts", required=True)
    m.add_argument("--out")
    m.add_argument("--macro-snapshot", help="기본: 가장 최근 macro snapshot")
    m.add_argument("--reitwatch-dir", help="기본: $INVEST_DATA_DIR/reitwatch/parsed")
    d = sub.add_parser("diff", help="같은 종목 두 분기 metrics의 차이(earnings mode)")
    d.add_argument("--prev", required=True)
    d.add_argument("--curr", required=True)
    d.add_argument("--out", help="없으면 stdout")
    c = sub.add_parser("compare", help="여러 종목 metrics를 같은 표에(compare mode)")
    c.add_argument("--metrics", nargs="+", required=True)
    c.add_argument("--out", help="없으면 stdout")
    try:
        args = p.parse_args(argv)
    except SystemExit as e:
        return 0 if e.code == 0 else 1
    if args.check:
        return run_check()
    if args.cmd == "metrics":
        return cmd_metrics(args)
    if args.cmd == "diff":
        return cmd_diff(args)
    if args.cmd == "compare":
        return cmd_compare(args)
    p.print_help(sys.stderr)
    return 1


# ---------------------------------------------------------------- --check

def run_check():
    root = skill_root()
    fx = os.path.join(root, "tests", "fixtures")
    exp = read_json(os.path.join(root, "tests", "expected.json"))["reit_calc"]
    fails = []

    def ok(cond, label):
        if not cond:
            fails.append(label)
        log(("PASS " if cond else "FAIL ") + label)

    def near(a, b, tol=1e-3):
        return a is not None and b is not None and abs(a - b) <= tol

    th = load_thresholds()
    for k in ("affo_payout_pct", "maturity_single_year_pct", "net_debt_to_ebitdare_x", "interest_coverage_x"):
        t = th.get(k, {})
        ok(all(x in t for x in ("watch", "alert", "direction", "source")), "thresholds.json %s 모양" % k)
    ok(th["affo_payout_pct"]["watch"] == 85 and th["affo_payout_pct"]["alert"] == 100
       and th["maturity_single_year_pct"]["watch"] == 15 and th["maturity_single_year_pct"]["alert"] == 20,
       "thresholds.json이 정한 기준값(AFFO payout 85/100, 만기 한 해 15/20)")

    base = read_json(os.path.join(fx, "facts_o_2026q3.json"))
    macro, _ = load_macro(os.path.join(fx, "macro_snapshot.json"))
    baa, _ = load_macro(os.path.join(fx, "macro_snapshot.json"), "BAA10Y")
    th_rw = dict(th, history_min_points=3)
    rw = load_reitwatch(os.path.join(fx, "reitwatch"), "O")
    doc, code = compute(copy.deepcopy(base), th_rw, macro=macro, rw=rw, baa=baa)
    M = doc["metrics"]

    # 1. 회사 발표값·손계산과 맞춘다
    for name, want in exp["values"].items():
        got, gst = M.get(name, {}).get("value"), M.get(name, {}).get("status")
        ok(near(got, want["value"], want.get("tol", 1e-3)) and gst == want["status"],
           "%s = %s %s (got %s %s; %s)" % (name, want["value"], want["status"], got, gst, want["why"]))
    tie = doc["checks"][0]
    ok(tie["check"] == "ffo_tie_out" and tie["pass"] and tie["recomputed"] == 1041026, "FFO tie-out 1,041,026 통과")
    ok(doc["checks"][1]["check"] == "affo_bridge" and doc["checks"][1]["pass"], "AFFO bridge 합이 회사 AFFO 1,061,097과 맞음")
    ok(M["maturity_max_year_pct"]["note"] == "2029년", "가장 큰 만기 해는 2029년")
    ok(M["dividend_cuts"]["value"] == [], "O 연간 배당 컷 없음")
    ok(doc["summary"]["flags"] == exp["flags"], "flag %s" % doc["summary"]["flags"])
    ok(code == 0, "다 채운 fixture는 exit 0 (got %s, gaps %s)" % (code, [g["item"] for g in doc["data_gaps"]
                                                                   if g["status"] in ("missing", "fail")]))
    ok(M["subsector"]["value"] == "Free Standing (가상)" and M["peer_median_p_ffo"]["value"] == 14.0
       and near(M["p_ffo_history_pctile"]["value"], 66.6667), "REITWatch peer median 14.0, 자기 history percentile 66.6667")
    again, _ = compute(copy.deepcopy(base), th_rw, macro=macro, rw=rw, baa=baa)
    ok(json.dumps(again, sort_keys=True) == json.dumps(doc, sort_keys=True), "같은 입력이면 같은 출력")

    # 2. FFO 입력 하나를 바꾸면 tie-out 실패, exit 2, 데이터 구멍
    bad = copy.deepcopy(base)
    bad["facts"]["gain_on_sale_re"]["value"] = 13865
    d2, c2 = compute(bad, th_rw, macro=macro, rw=rw)
    ok(c2 == 2 and not d2["checks"][0]["pass"] and any(g["item"] == "check:ffo_tie_out" for g in d2["data_gaps"]),
       "FFO 입력을 바꾸면 tie-out 실패 → exit 2, 데이터 구멍에 올라감 (diff %s%%)" % d2["checks"][0]["diff_pct"])

    # 3. 값이 빠지면 0으로 채우지 않고 missing을 물려준다
    nop = copy.deepcopy(base)
    nop["facts"]["price"] = {"value": None, "status": "missing", "note": "Yahoo chart 받기 실패, 웹 채우기 전"}
    d3, c3 = compute(nop, th_rw, macro=macro, rw=rw)
    ok(c3 == 2 and d3["metrics"]["forward_yield"]["value"] is None and d3["metrics"]["forward_yield"]["status"] == "missing"
       and d3["metrics"]["implied_cap_rate"]["status"] == "missing" and d3["metrics"]["affo_payout_forward"]["value"] is not None,
       "price가 없으면 yield·cap rate는 missing, payout은 계산")
    ok("price" in d3["metrics"]["forward_yield"]["missing_inputs"] and "Yahoo chart" in d3["metrics"]["forward_yield"]["note"],
       "missing 이유가 note에 이어짐")

    # 4. 출처(source·locator) 없는 값은 쓰지 않는다
    nol = copy.deepcopy(base)
    nol["facts"]["net_income_common"]["locator"] = ""
    d4, _ = compute(nol, th_rw, macro=macro, rw=rw)
    ok(d4["metrics"]["ffo_nareit"]["value"] is None and any(
        g["item"] == "net_income_common" and "locator" in g["reason"] for g in d4["data_gaps"]),
       "locator 없는 값은 missing으로 내리고 데이터 구멍에 적음")

    # 5. status 전파: estimated·stale 입력
    est = copy.deepcopy(base)
    est["facts"]["price"].update({"status": "stale", "note": "가격 기준일이 9일 전(가상)"})
    d5, _ = compute(est, th_rw, macro=macro, rw=rw)
    ok(d5["metrics"]["forward_yield"]["status"] == "stale" and d5["metrics"]["p_affo"]["status"] == "stale"
       and d5["metrics"]["affo_payout_forward"]["status"] == "derived", "stale 가격은 yield·P/AFFO에만 stale을 물려줌")
    est["facts"]["dividend_frequency"].update({"status": "estimated", "note": "지급 기록 수로 추정(가상)"})
    d5, _ = compute(est, th_rw, macro=macro, rw=rw)
    ok(d5["metrics"]["forward_yield"]["status"] == "estimated", "estimated가 stale보다 나쁜 쪽으로 물려짐")

    # 6. fallback: guidance가 없으면 분기×4 가정(estimated)
    nog = copy.deepcopy(base)
    for k in ("affo_guidance_low", "affo_guidance_high"):
        nog["facts"][k] = {"value": None, "status": "missing", "note": "guidance 없음(가상)"}
    d6, _ = compute(nog, th_rw, macro=macro, rw=rw)
    ok(near(d6["metrics"]["affo_fy"]["value"], 4.48) and d6["metrics"]["affo_fy"]["status"] == "estimated"
       and "가정" in d6["metrics"]["affo_fy"]["note"], "AFFO guidance가 없으면 1.12×4=4.48 estimated")

    # 7. REITWatch·macro가 없으면 그 칸만 missing으로 두고 계속한다
    d7, c7 = compute(copy.deepcopy(base), th, macro=missing("macro snapshot 없음(가상)"),
                     rw={"note": "REITWatch parsed 없음(가상)", "files": []})
    ok(c7 == 2 and d7["metrics"]["peer_median_p_ffo"]["status"] == "missing"
       and d7["metrics"]["yield_spread_vs_10y"]["status"] == "missing" and d7["metrics"]["forward_yield"]["value"] is not None
       and "REITWatch parsed 없음" in d7["metrics"]["p_ffo_history_pctile"]["note"],
       "REITWatch·macro가 없으면 peer·spread만 missing")
    ok(d7["metrics"]["ffo_fy"]["status"] == "estimated" and near(d7["metrics"]["ffo_fy"]["value"], 4.40),
       "consensus가 없으면 FFO FY는 분기×4(1.10×4) estimated")

    # 8. 단위 맞추기: USD millions
    um = copy.deepcopy(base)
    um["facts"]["total_debt"] = dict(um["facts"]["total_debt"], value=31738.055, unit="USD millions")
    d8, _ = compute(um, th_rw, macro=macro, rw=rw)
    ok(near(d8["metrics"]["net_debt"]["value"], 31369521), "USD millions를 thousands로 바꿔 계산")
    um["facts"]["total_debt"]["unit"] = "%"
    d8, _ = compute(um, th_rw, macro=macro, rw=rw)
    ok(d8["metrics"]["net_debt"]["status"] == "missing", "맞지 않는 unit은 missing")

    # 9. 배당 컷과 낮은 coverage (가상)
    cut = copy.deepcopy(base)
    cut["series"]["dividend_per_share_annual"] = [
        dict(e, value=v) for e, v in zip(cut["series"]["dividend_per_share_annual"], exp["cut_series"]["values"])]
    cut["facts"]["interest_expense"] = dict(cut["facts"]["interest_expense"], value=1000000)
    d9, _ = compute(cut, th_rw, macro=macro, rw=rw)
    ok(d9["metrics"]["dividend_cuts"]["value"] == exp["cut_series"]["cuts"], "배당 컷 해 %s" % d9["metrics"]["dividend_cuts"]["value"])
    ok(next(f for f in d9["flags"] if f["flag"] == "interest_coverage")["level"] == "alert",
       "coverage 1.45x(가상)는 alert")
    pay = copy.deepcopy(base)
    pay["facts"]["dividend_regular_latest"] = {"value": None, "status": "missing", "note": "가상"}
    pay["facts"]["dividends_paid_per_share_quarter"] = {"value": None, "status": "missing", "note": "가상"}
    d10, _ = compute(pay, th_rw, macro=macro, rw=rw)
    ok(next(f for f in d10["flags"] if f["flag"] == "affo_payout")["level"] == "review",
       "payout을 못 구하면 flag는 review(통과로 치지 않음)")

    dup = copy.deepcopy(base)
    m0 = dup["series"]["debt_maturities"]
    big = max((e for e in m0 if str(e["period"]).isdigit()), key=lambda e: e["value"])
    half = dict(big, value=big["value"] / 2.0)
    dup["series"]["debt_maturities"] = [e for e in m0 if e is not big] + [half, dict(half)]
    d12, _ = compute(dup, th_rw, macro=macro, rw=rw)
    ok(near(d12["metrics"]["maturity_max_year_pct"]["value"], 13.8817) and d12["metrics"]["maturity_max_year_pct"]["note"] == "2029년",
       "같은 해 만기가 두 줄이면 더해서 본다(2029년 반씩 두 줄 → 13.8817%)")
    noa = copy.deepcopy(base)
    noa["facts"]["ebitdare_annualized"] = {"value": None, "status": "missing", "note": "회사가 내지 않음(가상)"}
    d11, _ = compute(noa, th_rw, macro=macro, rw=rw)
    ok(near(d11["metrics"]["net_debt_to_ebitdare"]["value"], 5.399) and d11["metrics"]["net_debt_to_ebitdare"]["status"] == "estimated",
       "연환산 EBITDAre가 없으면 분기×4 가정(31,369,521 / (1,452,572×4) = 5.399, estimated)")

    # 10. CLI: 입력 오류는 exit 1
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "f.json")
        with open(p, "w") as f:
            json.dump({"schema": "x"}, f)
        ok(main(["metrics", "--facts", p, "--out", os.path.join(tmp, "m.json")]) == 1, "schema가 틀린 facts는 exit 1")
        ok(main(["metrics", "--facts", os.path.join(tmp, "none.json")]) == 1, "facts 파일이 없으면 exit 1")

    mx = os.path.join(fx, "metrics")

    # 11. diff: 지표별 전후 값·변화·flag 변화 (가상 fixture, 손으로 뺀 값)
    ed = exp["diff"]
    dd, dc = diff_metrics(read_json(os.path.join(mx, "diff_prev.json")), read_json(os.path.join(mx, "diff_curr.json")))
    rows = {r["metric"]: r for r in dd["rows"]}
    for name, want in ed["rows"].items():
        r = rows.get(name, {})
        got = {k: r.get(k) for k in want}
        same = all((got[k] is None and want[k] is None) or (isinstance(want[k], str) and got[k] == want[k])
                   or (isinstance(want[k], (int, float)) and near(got[k], want[k])) for k in want)
        ok(same, "diff %s %s (got %s)" % (name, want, got))
    ok({f["flag"]: f["change"] for f in dd["flags"]} == ed["flags"], "diff flag 변화 %s" % [
        (f["flag"], f["prev"], f["curr"], f["change"]) for f in dd["flags"]])
    ok(dd["worse"]["status"] == ed["worse_status"] and dd["worse"]["flags"] == ed["worse_flags"],
       "나빠진 status·flag 목록 %s" % dd["worse"])
    ok(rows["dividend_cuts"]["changed"] is False and rows["interest_coverage"]["changed"] is False
       and rows["p_affo"]["changed"] is True, "값·status가 같으면 changed false")
    ok(any(c["check"] == "ffo_tie_out" and c["prev_pass"] and not c["curr_pass"] for c in dd["checks"]),
       "check 통과 여부의 전후")
    ok(dc == 2, "한쪽이 missing인 지표가 있으면 exit 2 (got %s)" % dc)
    same_doc = read_json(os.path.join(mx, "diff_prev.json"))
    ds, sc = diff_metrics(same_doc, read_json(os.path.join(mx, "diff_prev.json")))
    ok(sc == 1, "같은 분기끼리 diff는 입력 오류 exit 1")
    nxt = dict(copy.deepcopy(same_doc), period="2026Q3")
    ds, sc = diff_metrics(same_doc, nxt)
    ok(sc == 0 and not any(r["changed"] for r in ds["rows"]), "값이 같은 두 분기는 changed 0, exit 0")
    ok(diff_metrics(nxt, same_doc)[1] == 1, "prev가 curr보다 늦은 분기면 exit 1")
    ok(diff_metrics(same_doc, dict(nxt, ticker="OTHER"))[1] == 1, "ticker가 다르면 exit 1")

    # 12. compare: 같은 표, subsector가 다르면 경고
    ec = exp["compare"]
    files = [read_json(os.path.join(mx, n)) for n in ("cmp_aaa.json", "cmp_bbb.json", "cmp_ccc.json")]
    c2, k2 = compare_metrics(files[:2])
    r2 = {r["metric"]: r for r in c2["rows"]}
    ok(all(near(r2[k]["median"]["value"], v) for k, v in ec["two"].items()), "compare 두 종목 median %s" % {
        k: r2[k]["median"]["value"] for k in ec["two"]})
    ok(k2 == 0 and not c2["warnings"] and all(not col["warning"] for col in c2["columns"]),
       "같은 subsector 두 종목은 경고 없음, exit 0")
    ok(r2["p_ffo"]["values"]["AAA"]["value"] == 20.0 and r2["forward_yield"]["median"]["status"] == "estimated",
       "칸마다 값·status, median은 가장 나쁜 status")
    ok(r2["sector_kpis.cash_rent_change_pct"]["values"]["BBB"]["status"] == "missing"
       and r2["sector_kpis.cash_rent_change_pct"]["median"]["value"] is None, "한 종목에만 있는 지표는 나머지가 missing, median 없음")
    c3, k3 = compare_metrics(files)
    r3 = {r["metric"]: r for r in c3["rows"]}
    ok(all(near(r3[k]["median"]["value"], v) for k, v in ec["three"].items()), "compare 세 종목 median")
    ok(k3 == 2 and [col["ticker"] for col in c3["columns"] if col["warning"]] == ec["warned"]
       and any("subsector" in w for w in c3["warnings"]), "다른 subsector를 섞으면 그 종목 경고 칸과 exit 2")
    ok(compare_metrics(files[:1])[1] == 1 and compare_metrics([files[0], files[0]])[1] == 1,
       "한 종목이나 같은 종목 두 번은 exit 1")
    mixed = copy.deepcopy(files[1])
    mixed["path"] = "dividend"
    c4, k4 = compare_metrics([files[0], mixed])
    ok(k4 == 2 and any("일반 배당주" in w for w in c4["warnings"]), "REIT와 일반 배당주를 섞으면 경고")

    # 13. 일반 배당주 경로 (SIC가 6798이 아님): EPS·FCF payout, 배당 CAGR, Chowder, yield trap
    ev = exp["dividend"]
    dbase = read_json(os.path.join(fx, "facts_div_2026q3.json"))
    g, gc = compute(copy.deepcopy(dbase), th, macro=macro, rw=None)
    GM = g["metrics"]
    ok(g["path"] == "dividend" and "ffo_nareit" not in GM, "is_reit false면 일반 배당주 경로(FFO 계산 없음)")
    for name, want in ev["values"].items():
        got = GM.get(name, {})
        ok(near(got.get("value"), want["value"]) and got.get("status") == want["status"],
           "배당주 %s = %s %s (got %s %s; %s)" % (name, want["value"], want["status"], got.get("value"),
                                               got.get("status"), want["why"]))
    ok(g["summary"]["flags"] == ev["flags"], "배당주 flag %s" % g["summary"]["flags"])
    ok(gc == 0, "다 채운 배당주 fixture는 exit 0 (gaps %s)" % [x["item"] for x in g["data_gaps"]])
    ut = copy.deepcopy(dbase)
    ut["company"]["sic"] = "4911"
    gu, _ = compute(ut, th, macro=macro, rw=None)
    ok(gu["metrics"]["chowder_threshold"]["value"] == ev["utility_threshold"]
       and gu["summary"]["flags"]["chowder"] == "ok", "utility(SIC 49xx)는 Chowder 기준 8")
    lo = copy.deepcopy(dbase)
    lo["facts"]["price"]["value"] = 200.0
    gl, _ = compute(lo, th, macro=macro, rw=None)
    ok(near(gl["metrics"]["forward_yield"]["value"], ev["low_yield"]["forward_yield"])
       and gl["metrics"]["chowder_threshold"]["value"] == ev["low_yield"]["chowder_threshold"],
       "yield 3% 미만이면 Chowder 기준 15")
    tr = copy.deepcopy(dbase)
    tr["facts"]["price"]["value"] = 60.0
    tr["series"]["dividend_per_share_annual"][-1]["value"] = 4.00
    gt, _ = compute(tr, th, macro=macro, rw=None)
    sig = gt["metrics"]["yield_trap_signals"]["value"]
    ok(near(gt["metrics"]["forward_yield"]["value"], ev["trap"]["forward_yield"])
       and sorted(k for k, v in sig.items() if v) == ev["trap"]["signals_true"]
       and gt["summary"]["flags"]["yield_trap"] == ev["trap"]["flag"], "yield trap 신호 %s" % sig)
    nf = copy.deepcopy(dbase)
    nf["facts"]["capex_ytd"]["value"] = 20000
    gn, _ = compute(nf, th, macro=macro, rw=None)
    ok(gn["metrics"]["fcf_payout_ytd"]["value"] is None and "FCF" in gn["metrics"]["fcf_payout_ytd"]["note"]
       and gn["metrics"]["yield_trap_signals"]["value"]["fcf_shortfall"] is True,
       "FCF가 0 이하면 payout을 내지 않고 fcf_shortfall 신호")
    one = copy.deepcopy(dbase)
    one["facts"]["eps_diluted_quarter"]["value"] = 0.09
    go, _ = compute(one, th, macro=macro, rw=None)
    efl = next(x for x in go["flags"] if x["flag"] == "eps_payout")
    ok(efl["level"] == "ok" and "1444.4" in efl["note"], "분기 GAAP payout이 100%%를 넘으면(1.30/0.09) flag note에 적는다: %s" % efl["note"])
    nm = copy.deepcopy(dbase)
    nm["facts"]["dividends_paid_ytd"] = {"value": None, "status": "missing", "note": "가상"}
    nm["series"]["dividend_per_share_annual"] = []
    gm, gmc = compute(nm, th, macro=macro, rw=None)
    ok(gmc == 2 and gm["summary"]["flags"]["fcf_payout"] == "review" and gm["summary"]["flags"]["chowder"] == "review"
       and gm["summary"]["flags"]["yield_trap"] == "review", "값이 빠지면 flag review, exit 2")
    gap = copy.deepcopy(dbase)
    gap["series"]["dividend_per_share_annual"] = [e for e in gap["series"]["dividend_per_share_annual"] if e["period"] != "2021"]
    gg, _ = compute(gap, th, macro=macro, rw=None)
    ok(near(gg["metrics"]["dividend_cagr_5y"]["value"], ev["values"]["dividend_cagr_5y"]["value"]),
       "배당 CAGR은 n년 전 연도 값으로 본다(중간 해가 빠져도 2020→2025)")
    gap["series"]["dividend_per_share_annual"] = [e for e in gap["series"]["dividend_per_share_annual"] if e["period"] != "2020"]
    gg, _ = compute(gap, th, macro=macro, rw=None)
    ok(gg["metrics"]["dividend_cagr_5y"]["value"] is None and "2020" in gg["metrics"]["dividend_cagr_5y"]["note"],
       "n년 전 연도 값이 없으면 CAGR missing(옆 해로 대신하지 않음)")
    ok(set(DIVIDEND_FACT_KINDS) & {"ffo_reported", "affo_reported"} == set()
       and fact_kinds(dbase) is DIVIDEND_FACT_KINDS and fact_kinds(base) is FACT_KINDS, "facts 칸 목록이 경로마다 따로")

    # 14. 출처·숫자 검사는 status와 상관없이 모든 값에
    nd = copy.deepcopy(base)
    nd["facts"]["total_debt"] = {"value": 1000, "unit": "USD thousands", "status": "derived", "note": "출처 없음(가상)"}
    nd["facts"]["ffo_reported"] = {"value": 1041026, "unit": "USD thousands", "status": "derived", "note": "출처 없음(가상)"}
    d14, c14 = compute(nd, th_rw, macro=macro, rw=rw)
    ok(d14["metrics"]["net_debt"]["status"] == "missing" and d14["metrics"]["ffo_reported"]["value"] is None
       and {"total_debt", "ffo_reported"} <= {g["item"] for g in d14["data_gaps"] if g["status"] == "missing"}
       and c14 == 2, "derived라고 적어도 source·locator가 없으면 missing, 데이터 구멍")
    for bad_v in (float("nan"), float("inf")):
        nn = copy.deepcopy(base)
        nn["facts"]["price"] = dict(nn["facts"]["price"], value=bad_v)
        d15, _ = compute(nn, th_rw, macro=macro, rw=rw)
        ok(d15["metrics"]["forward_yield"]["status"] == "missing"
           and any(g["item"] == "price" for g in d15["data_gaps"]), "price %r는 숫자로 치지 않음(missing)" % bad_v)
    zr = copy.deepcopy(base)
    zr["facts"]["ffo_reported"] = dict(zr["facts"]["ffo_reported"], value=0)
    d16, _ = compute(zr, th_rw, macro=macro, rw=rw)
    try:
        json.dumps(d16, allow_nan=False)
        finite_out = True
    except ValueError:
        finite_out = False
    ok(finite_out and not d16["checks"][0]["pass"] and d16["checks"][0]["diff_pct"] is None,
       "회사 FFO가 0이면 tie-out 실패, diff_pct null(Infinity를 쓰지 않음)")
    with tempfile.TemporaryDirectory() as tmp:
        try:
            write_json({"x": float("nan")}, os.path.join(tmp, "n.json"))
            nan_written = True
        except ValueError:
            nan_written = False
        ok(not nan_written, "JSON에 NaN을 쓰려 하면 멈춤(allow_nan=False)")

    # 15. REITWatch가 가격 기준일보다 허용 일수를 넘게 앞서면 나이를 적는다(estimated는 stale보다 나빠 그대로)
    old_rw = dict(rw, as_of="2024-09-30")
    d17, _ = compute(copy.deepcopy(base), th_rw, macro=macro, rw=old_rw)
    M17 = d17["metrics"]
    ok(all(M17[k]["status"] == "estimated" and "허용 %d일" % th["reitwatch_max_age_days"] in M17[k]["note"]
           for k in ("ffo_fy", "p_ffo_reitwatch", "peer_median_p_ffo", "p_ffo_history_pctile"))
       and any(g["item"] == "reitwatch" and g["status"] == "stale" for g in d17["data_gaps"]),
       "REITWatch 가격 기준일 2024-09-30(가격 2026-11-17, 778일): 값마다 나이 note, 데이터 구멍 reitwatch stale")
    ok(not any(g["item"] == "reitwatch" for g in doc["data_gaps"]), "fixture REITWatch(20일 앞섬)는 나이 표시 없음")

    # 16. 자기 P/FFO history 표본은 1·4·7·10월 호 + 최신 호(reit-sector-scan과 같은 표본), 8개 미만이면 missing
    ok(M["p_ffo_history_pctile"]["note"].startswith("2026-04~2026-10"),
       "history 표본에서 8·9월 호를 뺌(%s)" % M["p_ffo_history_pctile"]["note"])
    d18, _ = compute(copy.deepcopy(base), th, macro=macro, rw=rw)
    ok(th["history_min_points"] == 8 and d18["metrics"]["p_ffo_history_pctile"]["status"] == "missing"
       and "3개" in d18["metrics"]["p_ffo_history_pctile"]["note"], "표본 3개(8개 미만)면 history percentile missing")

    # 17. 일반 배당주 yield trap 신호는 입력의 가장 나쁜 status를 물려받는다
    ok(GM["yield_trap_signals"]["status"] == "derived", "reported 입력이면 yield trap 신호 derived")
    ye = copy.deepcopy(dbase)
    ye["facts"]["price"] = dict(ye["facts"]["price"], status="estimated", note="2차 자료(가상)")
    gy, _ = compute(ye, th, macro=macro, rw=None)
    ok(gy["metrics"]["forward_yield"]["status"] == "estimated" and gy["metrics"]["yield_trap_signals"]["status"] == "estimated",
       "estimated 가격이면 yield trap 신호도 estimated")

    # 18. sector_kpis: unit과 상관없이 status가 다섯 가지 중 하나가 아니면 missing, compare가 멈추지 않음
    sk = copy.deepcopy(base)
    sk["sector_kpis"]["k_x"] = {"value": 1.07, "unit": "x", "status": "verified", "source": "s (가상)", "locator": "l (가상)"}
    d19, _ = compute(sk, th_rw, macro=macro, rw=rw)
    ok(d19["metrics"]["sector_kpis"]["k_x"]["status"] == "missing"
       and any(g["item"] == "sector_kpis.k_x" for g in d19["data_gaps"]), "sector_kpis status 'verified'는 missing")
    odd1 = copy.deepcopy(files[0])
    odd1["metrics"]["sector_kpis"] = {"k_x": {"value": 1.07, "unit": "x", "status": "verified"}}
    odd2 = dict(copy.deepcopy(odd1), ticker="ODD2")
    try:
        c5, k5 = compare_metrics([odd1, odd2])
        r5 = {r["metric"]: r for r in c5["rows"]}["sector_kpis.k_x"]["median"]
        no_crash = r5["status"] == "missing"
    except TypeError:
        no_crash = False
    ok(no_crash, "metrics 파일에 모르는 status가 있어도 compare는 멈추지 않고 missing으로 본다")

    # 19. diff: 읽은 metrics에 check 실패가 있으면 missing이 없어도 exit 2
    failed = copy.deepcopy(nxt)
    failed["checks"] = [dict(c, **{"pass": False}) for c in failed["checks"]]
    ok(diff_metrics(same_doc, failed)[1] == 2 and diff_metrics(dict(copy.deepcopy(same_doc), checks=failed["checks"]),
                                                               copy.deepcopy(nxt))[1] == 2,
       "curr이나 prev metrics에 check 실패가 있으면 diff exit 2")

    # 20. 신용: gross assets 기준 비율, fixed-charge coverage, covenant headroom (O 2026Q3 covenant 표, 손계산)
    ec = exp["credit"]
    t = th.get("covenant_headroom_pct", {})
    ok(t.get("watch") == 15 and t.get("alert") == 5 and t.get("direction") == "below" and t.get("source"),
       "thresholds.json covenant headroom 15% 미만 watch, 5% 미만 alert")
    cov = M.get("covenant_headroom", {})
    got = {c.get("name"): c.get("headroom_pct") for c in (cov.get("value") or [])}
    ok(set(got) == set(ec["headroom"]) and all(near(got[k], v) for k, v in ec["headroom"].items())
       and cov.get("status") == "derived", "covenant 줄마다 headroom %s" % got)
    ok(M.get("covenant_headroom_min_pct", {}).get("note", "").startswith("Limitation on incurrence of total debt"),
       "최소 headroom 줄 이름을 note에 적음")
    ok("unencumbered_assets" in M.get("unencumbered_pct", {}).get("inputs", []), "unencumbered는 자산 기준을 먼저 씀")
    un = copy.deepcopy(base)
    un["facts"]["unencumbered_assets"] = {"value": None, "status": "missing", "note": "회사가 금액을 내지 않음(가상)"}
    d20, _ = compute(un, th_rw, macro=macro, rw=rw)
    ok(near(d20["metrics"]["unencumbered_pct"]["value"], ec["unencumbered_noi_pct"])
       and "unencumbered_noi" in d20["metrics"]["unencumbered_pct"]["inputs"],
       "unencumbered 자산이 없으면 NOI 기준 5,700,000 / 5,761,304 = 98.9359%")
    nc = copy.deepcopy(base)
    nc["series"]["covenants"] = []
    d21, c21 = compute(nc, th_rw, macro=macro, rw=rw)
    ok(c21 == 2 and d21["metrics"]["covenant_headroom_min_pct"]["status"] == "missing"
       and d21["summary"]["flags"].get("covenant_headroom") == "review", "covenant 표가 없으면 missing, flag review, exit 2")
    br = copy.deepcopy(base)
    br["series"]["covenants"][2] = dict(br["series"]["covenants"][2], actual=1.4)
    d22, _ = compute(br, th_rw, macro=macro, rw=rw)
    ok(near(d22["metrics"]["covenant_headroom_min_pct"]["value"], ec["breach_headroom"])
       and d22["summary"]["flags"].get("covenant_headroom") == "alert", "한도를 넘은 줄(1.4x < 1.5x)은 음수 headroom, alert")
    wa = copy.deepcopy(base)
    wa["series"]["covenants"][0] = dict(wa["series"]["covenants"][0], actual=52)
    d23, _ = compute(wa, th_rw, macro=macro, rw=rw)
    ok(near(d23["metrics"]["covenant_headroom_min_pct"]["value"], ec["watch_headroom"])
       and d23["summary"]["flags"].get("covenant_headroom") == "watch", "총부채 52% ≤ 60%는 13.3333%, watch")
    for i, change, why in ((1, {"direction": "below"}, "direction"), (3, {"locator": ""}, "locator"),
                           (0, {"limit": 0}, "limit 0"), (2, {"unit": "USD"}, "unit"), (0, {"actual": float("nan")}, "NaN")):
        bd = copy.deepcopy(base)
        bd["series"]["covenants"][i] = dict(bd["series"]["covenants"][i], **change)
        d24, c24 = compute(bd, th_rw, macro=macro, rw=rw)
        rows24 = d24["metrics"]["covenant_headroom"]["value"] or []
        ok(c24 == 2 and d24["metrics"]["covenant_headroom_min_pct"]["value"] is None
           and any(r.get("headroom_pct") is None for r in rows24)
           and any(g["item"] == "covenants[%d]" % i for g in d24["data_gaps"]),
           "covenant 줄의 %s가 틀리면 그 줄 missing, 최소값 missing, 데이터 구멍" % why)
    fc = copy.deepcopy(base)
    fc["facts"]["principal_amortization"] = {"value": None, "status": "missing", "note": "회사가 내지 않음(가상)"}
    d25, _ = compute(fc, th_rw, macro=macro, rw=rw)
    ok(d25["metrics"]["fixed_charge_coverage"]["value"] is None
       and near(d25["metrics"]["interest_coverage"]["value"], 4.3547),
       "원금 상환을 못 찾으면 FCC missing(interest coverage는 그대로)")
    pf = copy.deepcopy(base)
    pf["facts"]["preferred_equity"] = dict(pf["facts"]["preferred_equity"], value=1000000)
    pf["facts"]["preferred_dividends"] = dict(pf["facts"]["preferred_dividends"], value=10000)
    d26, _ = compute(pf, th_rw, macro=macro, rw=rw)
    ok(near(d26["metrics"]["debt_pref_to_gross_assets"]["value"], 37.6078)
       and near(d26["metrics"]["fixed_charge_coverage"]["value"], 4.1769),
       "우선주 1,000,000·우선주 배당 10,000(가상)이면 (32,004,100 + 1,000,000) / 87,758,545 = 37.6078%, "
       "FCC 1,452,572 / 347,764 = 4.1769x")

    # 21. 리파이낸싱·유동성 (bc 손계산), interest coverage alert 2.0x
    er = exp["refi"]
    ok(th["interest_coverage_x"]["alert"] == 2.0 and th["interest_coverage_x"]["watch"] == 3.0,
       "interest coverage 3.0x 미만 watch, 2.0x 미만 alert")
    lc = copy.deepcopy(base)
    lc["facts"]["interest_expense"] = dict(lc["facts"]["interest_expense"], value=800000)
    d27, _ = compute(lc, th_rw, macro=macro, rw=rw, baa=baa)
    ok(near(d27["metrics"]["interest_coverage"]["value"], er["coverage_1_8"])
       and next(f for f in d27["flags"] if f["flag"] == "interest_coverage")["level"] == "alert",
       "coverage 1.8157x(1,452,572 / 800,000, 가상)는 alert")
    ok("가정" in M.get("maturities_24m", {}).get("note", "") and "2028" in M.get("maturities_24m", {}).get("note", ""),
       "24개월 만기의 셋째 해(2028)를 9/12만 넣었다고 note에 적음")
    ye = copy.deepcopy(base)
    ye["period_end"] = "2026-12-31"
    d28, _ = compute(ye, th_rw, macro=macro, rw=rw, baa=baa)
    ok(near(d28["metrics"]["maturities_24m"]["value"], er["maturities_24m_year_end"])
       and d28["metrics"]["maturities_24m"]["status"] == "derived",
       "분기 끝날이 12월 31일이면 셋째 해 전부(2,201.4 + 2,884.1 + 3,784.2 = 8,869.7M), 나누지 않아 derived")
    d29, _ = compute(copy.deepcopy(base), th_rw, macro=macro, rw=rw, baa=missing("macro snapshot에 BAA10Y 없음(가상)"))
    ok(d29["metrics"]["market_borrowing_rate_est"]["status"] == "missing"
       and d29["metrics"]["refi_impact_affo_pct"]["value"] is None
       and near(d29["metrics"]["liquidity_coverage_24m"]["value"], exp["values"]["liquidity_coverage_24m"]["value"]),
       "BAA10Y가 없으면 시장금리·refi 영향만 missing, liquidity coverage는 그대로")
    na = copy.deepcopy(base)
    na["facts"]["affo_reported"] = {"value": None, "status": "missing", "note": "AFFO를 내지 않음(가상)"}
    d30, _ = compute(na, th_rw, macro=macro, rw=rw, baa=baa)
    ok(all(d30["metrics"][k]["value"] is None for k in ("refi_impact_affo_pct", "floating_rate_shock_affo_pct",
                                                         "liquidity_coverage_24m"))
       and d30["metrics"]["maturities_24m"]["value"] is not None, "AFFO 총액이 없으면 AFFO로 나누는 줄과 유보 현금흐름이 missing")

    # 22. AFFO bridge category, 주당 AFFO가 없을 때 총액 / 희석 주식 수, FFO payout 참고 줄 (bc 손계산)
    ea = exp["affo"]
    ok(not any(f["flag"] == "ffo_payout" for f in doc["flags"]), "FFO payout은 flag 없는 참고 줄")
    nps = copy.deepcopy(base)
    nps["facts"]["affo_per_share_diluted"] = {"value": None, "status": "missing", "note": "주당 AFFO를 내지 않음(가상)"}
    d31, _ = compute(nps, th_rw, macro=macro, rw=rw, baa=baa)
    M31 = d31["metrics"]
    ok(near(M31["affo_per_share_diluted"]["value"], ea["per_share"]) and M31["affo_per_share_diluted"]["status"] == "derived"
       and near(M31["affo_payout_quarter"]["value"], ea["payout_quarter"])
       and near(M31["affo_payout_forward"]["value"], exp["values"]["affo_payout_forward"]["value"]),
       "주당 AFFO가 없으면 1,061,097 / 948,797 = 1.1184(derived)로 분기 payout 72.6931%, forward는 guidance 그대로")
    for k in ("affo_guidance_low", "affo_guidance_high"):
        nps["facts"][k] = {"value": None, "status": "missing", "note": "guidance 없음(가상)"}
    d32, _ = compute(nps, th_rw, macro=macro, rw=rw, baa=baa)
    M32 = d32["metrics"]
    ok(near(M32["affo_fy"]["value"], ea["affo_fy_run"]) and M32["affo_fy"]["status"] == "estimated"
       and near(M32["affo_payout_forward"]["value"], ea["payout_forward_run"])
       and next(f for f in d32["flags"] if f["flag"] == "affo_payout")["level"] == "ok",
       "guidance도 없으면 연간 AFFO 1.1184 × 4 = 4.4736(estimated), forward payout 72.8273%, flag가 review가 아님")
    nps["facts"]["diluted_shares_weighted"] = {"value": None, "status": "missing", "note": "가상"}
    d33, _ = compute(nps, th_rw, macro=macro, rw=rw, baa=baa)
    ok(d33["metrics"]["affo_payout_forward"]["value"] is None
       and next(f for f in d33["flags"] if f["flag"] == "affo_payout")["level"] == "review",
       "주당 AFFO도 희석 주식 수도 없으면 payout missing, flag review")
    cat = copy.deepcopy(base)
    for it in cat["affo_bridge"]["items"]:
        if it["category"] in ("recurring_capex", "leasing_cost"):
            it["category"] = "other"
        if it["category"] == "straight_line":
            del it["category"]
    cat["affo_bridge"]["items"][0]["category"] = "capex"
    d34, _ = compute(cat, th_rw, macro=macro, rw=rw, baa=baa)
    ok(d34["metrics"]["recurring_capex_to_noi_pct"]["value"] is None
       and d34["metrics"]["straight_line_to_ffo_pct"]["value"] is None
       and any(g["item"] == "affo_bridge.items[0]" and "category" in g["reason"] for g in d34["data_gaps"])
       and d34["checks"][1]["pass"],
       "그 category 줄이 없거나 category가 없으면 missing(0이 아님), 모르는 category는 데이터 구멍, bridge 합은 그대로")

    log("check: %d개 실패" % len(fails) if fails else "check: 모두 통과")
    return 2 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

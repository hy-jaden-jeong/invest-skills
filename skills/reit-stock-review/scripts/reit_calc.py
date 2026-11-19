#!/usr/bin/env python3
"""reit-stock-review: facts JSON에서 REIT 지표를 계산해 metrics JSON을 쓴다.

명령
  metrics --facts F [--out P] [--macro-snapshot P] [--reitwatch-dir D] [--tracker-dir D]
                         facts를 읽어 지표·check·flag·데이터 구멍을 계산한다.
                         SIC가 6798이 아니면 일반 배당주 경로(EPS·FCF payout, Chowder, yield trap)
  diff --prev M1 --curr M2 [--out P]
                         같은 종목 두 분기 metrics의 지표별 전후 값·변화·status·flag 변화(earnings mode)
  compare --metrics M... [--out P]
                         여러 종목 metrics를 같은 표에 놓는다. subsector가 다르면 경고(compare mode)
  peers T [--with A,B] [--period P] [--out P]
                         같은 subsector에서 REITWatch 시총이 가장 가까운 2개(또는 --with)와 같은 분기 metrics 재사용 여부
  --check                network 없이 fixture로 확인한다

숫자는 이 script만 계산한다. agent는 공시에서 값을 읽어 facts에 위치(locator)와 함께 적는다.
계산은 순수 함수이고, 입력 status를 물려받는다(metric()). 입력이 하나라도 missing이면 계산값도 missing이다.

exit: 0 = 다 채움, 2 = 계산은 했지만 missing이나 check 실패가 있음, 1 = 입력 오류.
Python 3 stdlib만 쓴다(3.9 이상).
"""
import argparse
import contextlib
import copy
import datetime as dt
import glob
import io
import json
import math
import os
import sys
import tempfile

FACTS_SCHEMA = "reit-facts/1"
METRICS_SCHEMA = "reit-metrics/1"
REITWATCH_SCHEMA = "reitwatch-parsed/1"
MACRO_SCHEMA = "macro-snapshot/1"
TRACKER_SCHEMA = "nareit-tracker/1"
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
    "other_committed_uses": "money",  # 개발비 말고 앞으로 내줄 약정(대출 funding commitment 등). 있으면 liquidity uses에 더한다
    "recent_issuance_rate_pct": "pct",  # 최근 12개월 안 회사 채권 발행 금리. issue_date(발행일)를 함께 적는다
    # 운영
    "occupancy_pct": "pct",
    "wale_years": "years",
    "investment_grade_share_pct": "pct",
    "same_store_current": "money",
    "same_store_prior": "money",
    "noi_annualized": "money",
    "noi_quarter": "money",  # 분기 NOI. 연환산 NOI가 없을 때만 분기×4(estimated)로 쓴다
    "straight_line_rent": "money",  # 분기 straight-line rent(현금보다 더 인식한 임대료). AFFO bridge에 그 줄이 없을 때만 쓴다
    # valuation 기준선(economic implied cap rate). NOI와 같은 기준(pro-rata·consolidated)으로 적는다
    "construction_in_progress": "money",
    "land_held": "money",
    "other_non_operating_assets": "money",
    "jv_debt_pro_rata": "money",
    "capex_reserve": "money",
    "debt_fair_value": "money",
    "debt_carrying_value": "money",
    "acquisitions_cap_rate": "pct",
    "dispositions_cap_rate": "pct",
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
    "credit_ratings": "rating",
}
COVENANT_UNITS = ("%", "x")
# 신용등급 눈금(1이 가장 높다). S&P·Fitch와 Moody's. 10까지 투자등급, 11~13은 BB, 14부터 BB 아래다.
SP_SCALE = ("AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-",
            "B+", "B", "B-", "CCC+", "CCC", "CCC-", "CC", "C")
MOODY_SCALE = ("Aaa", "Aa1", "Aa2", "Aa3", "A1", "A2", "A3", "Baa1", "Baa2", "Baa3", "Ba1", "Ba2", "Ba3",
               "B1", "B2", "B3", "Caa1", "Caa2", "Caa3", "Ca", "C")
ISSUANCE_MAX_DAYS = 365  # 회사 발행 금리는 기준일 전 12개월 안 것만 차환 금리로 쓴다(D12)
# 칸의 기준. 회사 표의 consolidated(연결)·pro_rata(지분 몫) 열 중 어느 것인지 적는다(선택). 식의 입력끼리 다르면 estimated.
BASES = ("consolidated", "pro_rata")
# 0 이상이어야 하는 칸(음수면 부호를 잘못 옮긴 것이라 거절한다)
NONNEG_FACTS = ("dev_remaining_spend", "capex_reserve", "other_committed_uses")
# 그 분기 거래가 없을 수 있는 칸(none_in_period). 다른 칸에 적으면 missing이다
NONE_IN_PERIOD_FACTS = ("acquisitions_cap_rate", "dispositions_cap_rate")
# 가격이 든 지표: as_of를 가격 기준일로 둔다(입력 중 가장 이른 날이 아니라)
PRICED_METRICS = ("forward_yield", "market_cap", "p_ffo", "p_affo", "affo_yield", "implied_cap_rate", "economic_ev",
                  "economic_implied_cap_rate", "economic_implied_cap_rate_debt_mtm", "yield_spread_vs_10y",
                  "affo_yield_spread_vs_10y", "implied_cap_rate_vs_tracker", "p_e", "chowder_number")
# 가볍게 보기(depth light, D15)의 핵심 칸: 보도자료 첫 표 몇 개와 XBRL로 채울 수 있는 칸. 묶음(tuple)은 그중 하나면 된다.
# 이 칸이 비면(값도 공시 없음도 아님) exit 2다. references/facts-schema.md "가볍게 보기"와 같아야 한다.
LIGHT_CORE_FACTS = ("ffo_reported", "ffo_per_share_diluted", "affo_reported", ("affo_per_share_diluted", "diluted_shares_weighted"),
                    "dividend_regular_latest", "dividend_frequency", "price", "common_shares_outstanding",
                    "op_units_outstanding", "total_debt", "cash", "ebitdare_quarter", "interest_expense", "occupancy_pct",
                    "series.top_tenants")
# light에서도 내는 지표(핵심 칸과 REITWatch·macro로 셈한다). 나머지가 비면 '가볍게 봄'(not_assessed)이다
LIGHT_METRICS = ("ffo_reported", "ffo_per_share_diluted", "affo_reported", "affo_per_share_diluted", "affo_fy", "ffo_fy",
                 "forward_dividend", "forward_yield", "affo_payout_forward", "ffo_payout_forward", "net_debt",
                 "net_debt_to_ebitdare", "interest_coverage", "occupancy_pct", "top1_tenant_pct", "market_cap", "p_ffo",
                 "p_affo", "affo_yield", "affo_annualized", "dgs10", "yield_spread_vs_10y", "affo_yield_spread_vs_10y",
                 "subsector", "p_ffo_reitwatch", "p_ffo_history_pctile", "peer_median_p_ffo", "p_ffo_vs_peer",
                 "ffo_fy1_rev_3m", "ffo_growth_fy2", "market_borrowing_rate_est")
CONSENSUS_GAP_PCT = 10  # consensus FFO가 회사 분기 FFO × 4와 이만큼 넘게 다르면 정의가 다를 수 있다고 적는다
# 희석 주식 수가 기말 보통주와 이 배수 넘게 다르면 단위를 잘못 적은 것으로 본다
SHARE_UNIT_RATIO = 10
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
REVISION_MONTHS = 3  # consensus FFO 변화는 정확히 3개월 앞 호와 견준다(reit-sector-scan과 같은 규칙)
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


def positive(x, why):
    """분모가 0 이하면 Undefined(why). 양수면 True."""
    if x <= 0:
        raise Undefined(why)
    return True


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


def to_date(s):
    """날짜 글이면 date, 아니면 None(멈추지 않는다)."""
    try:
        return parse_date(str(s))
    except (ValueError, TypeError):
        return None


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
    if f.get("none_in_period") is True:
        if key not in NONE_IN_PERIOD_FACTS:
            return missing("%s: none_in_period(그 분기 거래 없음)는 %s에만 적는다" % (key, "·".join(NONE_IN_PERIOD_FACTS))), \
                "none_in_period"
        if f.get("value") is not None:
            return missing("%s: value와 none_in_period를 함께 적음(거래 없음이면 value는 비운다)" % key), "none_in_period"
        if not (str(f.get("source", "")).strip() and str(f.get("locator", "")).strip()):
            return missing("%s: none_in_period에는 그 분기 거래가 없다는 근거(source·locator)가 있어야 함" % key), "출처"
        return dict(missing(f.get("note") or "그 분기 거래 없음"), none_in_period=True, source=f["source"],
                    locator=f["locator"]), None
    if f.get("not_disclosed") is True and f.get("value") is not None:
        return missing("%s: value와 not_disclosed를 함께 적음(공시 없음이면 value는 비운다)" % key), "not_disclosed"
    if status == "missing" or f.get("value") is None:
        note = f.get("note") or f.get("reason") or "값 없음"
        if f.get("not_disclosed") is True:
            if not (str(f.get("source", "")).strip() and str(f.get("locator", "")).strip()):
                return missing("%s: not_disclosed에는 회사가 내지 않는다는 근거(source·locator)가 있어야 함: %s" % (
                    key, note)), "출처"
            return dict(missing(note), not_disclosed=True, source=f["source"], locator=f["locator"]), None
        return missing(note), None
    if not is_finite(f.get("value")):
        return missing("%s: value(%r)가 유한한 숫자가 아님" % (key, f.get("value"))), "숫자"
    if not (str(f.get("source", "")).strip() and str(f.get("locator", "")).strip()):
        return missing("%s: source나 locator가 없어 쓰지 않음(출처 없는 값)" % key), "출처"
    return f, None


def sum_parts(key, f, kind):
    """회사 표의 여러 줄 합인 칸(parts): 줄마다 값 규칙과 단위를 맞춘 뒤 script가 더한다(agent가 더하지 않는다).
    status는 줄의 가장 나쁜 것(reported면 derived). 한 줄이라도 규칙을 어기거나 missing이면 칸 전체가 missing."""
    parts = f.get("parts")
    if kind not in ("money", "shares") or not isinstance(parts, list) or not parts:
        return missing("%s: parts는 돈·주식 수 칸에 줄이 하나 이상일 때만 쓴다" % key), "parts"
    got = []
    for i, p in enumerate(parts):
        env, bad = normalize("%s.parts[%d]" % (key, i), p, kind)
        if bad:
            return missing(env["note"]), bad
        if env["status"] == "missing":
            return missing("%s.parts[%d]: %s" % (key, i, env["note"])), None
        got.append(env)
    st = worst([e["status"] for e in got])
    dates = [e["as_of"] for e in got if e.get("as_of")]
    return {**({"basis": f["basis"]} if f.get("basis") else {}),
            "value": round(sum(e["value"] for e in got), 4), "unit": BASE_UNIT[kind],
            "status": "derived" if st == "reported" else st, "as_of": min(dates) if dates else None,
            "source": "; ".join(sorted(set(e["source"] for e in got))),
            "locator": " + ".join(e["locator"] for e in got),
            "note": "; ".join(x for x in (f.get("note") or "", "회사 표 %d줄의 합(script가 더함)" % len(got)) if x)}, None


def normalize(key, f, kind):
    """facts 칸 하나를 검사하고(check_value) 단위를 맞춘다. 규칙을 어기면 missing으로 내리고 이유를 note에 쓴다.
    value 대신 parts(회사 표의 여러 줄)가 있으면 줄마다 검사해 더한다."""
    if isinstance(f, dict) and f.get("basis") is not None and f.get("basis") not in BASES:
        return missing("%s: basis %r는 %s 중 하나여야 함" % (key, f.get("basis"), "·".join(BASES))), "basis"
    if isinstance(f, dict) and "parts" in f:
        if f.get("value") is not None:
            return missing("%s: parts와 value를 함께 적음(parts면 value는 비운다)" % key), "parts"
        return sum_parts(key, f, kind)
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
    for k in ("limit", "actual"):  # NaN·Infinity·문자는 출력에 null로(원래 값은 note에)
        if out[k] is not None and not is_finite(out[k]):
            out[k] = None
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


def rating_notch(s):
    """신용등급 글 → 눈금(1 = AAA·Aaa). 첫 낱말만 본다("BB- (stable)" → BB-). 읽지 못하면 None."""
    t = str(s or "").split()
    t = t[0] if t else ""
    for scale in (SP_SCALE, MOODY_SCALE):
        if t in scale:
            return scale.index(t) + 1
    return len(SP_SCALE) + 1 if t in ("D", "SD", "RD") else None


def rating_band(notch):
    return "investment_grade" if notch <= 10 else "bb" if notch <= 13 else "below_bb"


def read_rating(key, it):
    """신용등급 줄 하나: agency(신용평가사), rating, status·source·locator. (줄, 어긴 규칙 또는 None)."""
    if not isinstance(it, dict):
        return dict(missing("%s: 줄의 형식이 아님" % key), agency=None, rating=None), "형식"
    out = {k: it.get(k) for k in ("agency", "rating", "status", "as_of", "source", "locator")}
    out["note"] = it.get("note") or ""
    st = it.get("status")
    if st not in STATUSES:
        return dict(out, status="missing", note="%s: status %r" % (key, st)), "status"
    if st == "missing":
        return dict(out, note=out["note"] or "값 없음"), None
    notch = rating_notch(it.get("rating"))
    why = None
    if not str(it.get("agency") or "").strip():
        why = "agency(신용평가사)가 없음"
    elif notch is None:
        why = "rating %r를 읽지 못함(S&P·Fitch AAA~C·D, Moody's Aaa~C)" % (it.get("rating"),)
    elif not (str(it.get("source", "")).strip() and str(it.get("locator", "")).strip()):
        why = "source나 locator가 없어 쓰지 않음(출처 없는 값)"
    if why:
        return dict(out, status="missing", note="%s: %s" % (key, why)), "규칙"
    return dict(out, notch=notch), None


def market_rate(F, ratings, rates, ref_date, gaps):
    """차환 금리 추정(D12): 회사 최근 12개월 안 발행 금리 > 신용등급별(투자등급 Baa, BB는 DGS10 + BB OAS,
    그 아래는 DGS10 + HY OAS) > 등급을 모르면 Baa(추정). rates: dgs10·baa10y·dbaa·hy·bb envelope."""
    notes = []
    good = [r for r in ratings if r.get("status") != "missing" and r.get("notch")]
    low = max(good, key=lambda r: r["notch"]) if good else None
    extra = {"ratings_used": ["%s %s" % (r["agency"], r["rating"]) for r in good],
             "rating_band": rating_band(low["notch"]) if low else None}
    iss = F["recent_issuance_rate_pct"]
    if iss["value"] is not None:
        d, ref = to_date(iss.get("issue_date")), to_date(ref_date)
        why = None
        if d is None:
            why = "발행일(issue_date)이 날짜가 아니라 쓰지 않음"
        elif ref is not None and not 0 <= (ref - d).days <= ISSUANCE_MAX_DAYS:
            why = "발행일 %s가 기준일 %s 전 12개월 안이 아니라 쓰지 않음" % (d, str(ref_date)[:10])
        if why:
            notes.append("recent_issuance_rate_pct " + why)
            gaps.append({"item": "recent_issuance_rate_pct", "status": "ignored", "reason": why})
        else:
            m = assume(metric("market_borrowing_rate_est", "회사 최근 12개월 안 발행 금리(recent_issuance_rate_pct)",
                              {"recent_issuance_rate_pct": iss}, lambda r: r, "%"),
                       "회사가 %s에 발행한 금리 %s%%로 24개월 안 만기를 다시 빌린다고 가정(담보·만기·통화가 다를 수 있다)" % (
                           d, round(iss["value"], 4)))
            return dict(m, rate_source="issuance", **extra)
    baa_note = "투자등급 하단(Baa) 회사채 금리로 신규 차입금리를 어림(등급·만기·통화마다 다르다)"
    if rates["dbaa"].get("value") is not None:  # Baa 금리 차례는 reit-sector-scan과 같다: DBAA, 없으면 BAA10Y + DGS10
        baa_m = metric("market_borrowing_rate_est", "DBAA(Moody's Baa 회사채 금리)", {"dbaa": rates["dbaa"]}, lambda b: b, "%")
    else:
        baa_m = metric("market_borrowing_rate_est", "DGS10 + BAA10Y(Moody's Baa 회사채 금리)",
                       {"dgs10": rates["dgs10"], "baa10y": rates["baa10y"]}, lambda r, s: r + s, "%")
    if low is None:
        notes += ["신용등급을 몰라 Baa 금리로 가정(추정): facts series.credit_ratings에 읽을 등급이 없음", baa_note]
        return dict(assume(baa_m, "; ".join(notes)), rate_source="unknown_rating", **extra)
    used = "신용등급 %s %s(%s 중 가장 낮은 것)" % (low["agency"], low["rating"], ", ".join(extra["ratings_used"]))
    band = extra["rating_band"]
    if band == "investment_grade":
        notes += [used + ": 투자등급이라 Baa 금리", baa_note]
        m = baa_m
    elif band == "bb" and rates["bb"].get("value") is not None:
        notes.append(used + ": BB라 DGS10 + ICE BofA BB OAS")
        m = metric("market_borrowing_rate_est", "DGS10 + BB OAS(BAMLH0A1HYBB)",
                   {"dgs10": rates["dgs10"], "bb_oas": rates["bb"]}, lambda r, s: r + s, "%")
    else:
        if band == "bb":
            notes.append(used + ": BB인데 macro snapshot에 BB OAS(BAMLH0A1HYBB)가 없어 HY OAS(BAMLH0A0HYM2)로 대신"
                                "(HY 전체라 BB보다 넓게 잡힌다)")
        else:
            notes.append(used + ": BB 아래라 DGS10 + ICE BofA HY OAS(HY 지수 평균보다 등급이 낮으면 spread가 더 넓을 수 있다)")
        m = metric("market_borrowing_rate_est", "DGS10 + HY OAS(BAMLH0A0HYM2)",
                   {"dgs10": rates["dgs10"], "hy_oas": rates["hy"]}, lambda r, s: r + s, "%")
    return dict(assume(m, "; ".join(notes)), rate_source="rating", **extra)


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
        out = dict(base, value=None, status="missing", as_of=None, missing_inputs=miss, note="; ".join(reasons))
        if all(inputs[k].get("not_disclosed") for k in miss):
            out["not_disclosed"] = True  # 빠진 입력이 모두 회사가 내지 않는 값이다
        return out
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
    bases = {k: e["basis"] for k, e in inputs.items() if e.get("basis")}
    extra = {}
    if len(set(bases.values())) > 1:
        st = worst([st, "estimated"])
        notes.append("기준이 섞임(%s): 같은 기준 값끼리의 식이 아니라 어림" % ", ".join(
            "%s %s" % kv for kv in sorted(bases.items())))
        extra["basis"] = "mixed"
    elif bases:
        extra["basis"] = next(iter(bases.values()))
    return dict(base, value=round(v, 4), status=st, as_of=min(dates) if dates else None,
                note="; ".join(notes), **extra)


def assume(m, note):
    """가정이 든 계산값: status를 estimated로 두고 가정을 note에 쓴다(missing은 그대로)."""
    if m["value"] is None:
        return m
    out = dict(m)
    if RANK[out["status"]] < RANK["estimated"]:
        out["status"] = "estimated"
    out["note"] = "; ".join(x for x in (note, m.get("note")) if x)
    return out


def price_dated(M, price):
    """B21: 가격이 든 지표(PRICED_METRICS)의 as_of를 가격 기준일로 둔다. 가격 기준일이 없거나 값이 없으면 그대로."""
    if not price.get("as_of"):
        return
    for k in PRICED_METRICS:
        if k in M and M[k].get("value") is not None:
            M[k]["as_of"] = price["as_of"]


def first_of(*cands):
    """앞 후보부터 값이 있는 것을 쓴다. 모두 missing이면 첫 후보에 이유를 모아 낸다."""
    for c in cands:
        if c["value"] is not None:
            return c
    out = dict(cands[0])
    out["note"] = " | ".join("%s: %s" % (c["metric"], c.get("note", "")) for c in cands)
    out.pop("not_disclosed", None)
    if all(c.get("not_disclosed") for c in cands):
        out["not_disclosed"] = True
    return out


def passthrough(name, env, note="회사 발표값"):
    out = {"metric": name, "formula": note, "unit": env.get("unit", ""), "inputs": [name]}
    out.update({k: env.get(k) for k in ("value", "status", "as_of", "source", "locator", "note")})
    out.update({k: env[k] for k in ("basis", "not_disclosed", "none_in_period", "not_assessed") if env.get(k)})
    return out


def light_pass(F, S, SND, M, checks, gaps):
    """depth light(D15): 핵심 칸 목록(LIGHT_CORE_FACTS) 중 빈 칸을 돌려주고, LIGHT_METRICS 밖의 빈 지표와 맞춰 보지 못한
    check를 '가볍게 봄'(not_assessed)으로 둔다. 값이 나온 지표는 그대로 둔다(agent가 더 채운 칸)."""
    def filled(k):
        if k == "series.top_tenants":
            return bool(S["top_tenants"]) or "top_tenants" in SND
        return F[k]["value"] is not None or bool(F[k].get("not_disclosed"))
    lacking = []
    for c in LIGHT_CORE_FACTS:
        if not any(filled(k) for k in (c if isinstance(c, tuple) else (c,))):
            lacking.append("·".join(c) if isinstance(c, tuple) else c)
    for k in lacking:
        gaps.append({"item": "light_core." + k, "status": "missing", "reason": "가볍게 보기의 핵심 칸이 비어 있음(값이나 공시 없음을 적는다)"})
    for k, m in walk(M):
        if k not in LIGHT_METRICS and m.get("status") == "missing" and not empty_reason(m):
            m["not_assessed"] = True
            m["note"] = "; ".join(x for x in ("핵심 칸으로 내는 지표가 아님", m.get("note")) if x)
    for c in checks:
        if c["pass"] is False and c["status"] == "missing":
            c.update({"pass": None, "not_assessed": True, "note": "가볍게 보기라 맞춰 보지 않음(입력이 핵심 칸이 아님): " + c["note"]})
    return lacking


def env_of(m):
    """metric 결과를 다른 metric의 입력으로 쓴다."""
    out = {"value": m["value"], "status": m["status"], "as_of": m.get("as_of"), "note": m.get("note", "")}
    out.update({k: m[k] for k in ("basis", "not_disclosed") if m.get(k)})
    return out


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
        why = ("공시 없음(회사가 내지 않음)이라" if m.get("not_disclosed") else
               "가볍게 봄(depth light)이라" if m.get("not_assessed") else "데이터가 없어")
        return {"flag": name, "level": "review", "value": None, "basis": basis,
                "note": "%s 판단하지 않음(통과로 치지 않는다): %s" % (why, m.get("note", ""))}
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
    if not is_finite(e.get("value")):
        return missing("macro snapshot의 %s 값(%r)이 유한한 숫자가 아님" % (sid, e.get("value"))), path
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


def is_ladder_period(p):
    """만기 사다리 period는 연도(YYYY)나 thereafter다."""
    t = str(p) if isinstance(p, (str, int)) and not isinstance(p, bool) else ""
    return (len(t) == 4 and t.isdigit()) or t.lower() == "thereafter"


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


def load_ticker_aliases():
    """ticker가 바뀐 종목의 예전 ticker(assets/ticker_aliases.json): {지금 ticker: [예전 ticker, ...]}."""
    d = read_json(os.path.join(skill_root(), "assets", "ticker_aliases.json"))
    return {k: list(v) for k, v in d.items() if not k.startswith("_")}


def read_reitwatch_months(dirpath):
    """REITWatch parsed 파일들 [(달, 경로, 문서)], 달 차례."""
    months = []
    for p in sorted(glob.glob(os.path.join(dirpath, "*.json"))):
        try:
            d = read_json(p)
        except ValueError:
            continue
        if d.get("schema") == REITWATCH_SCHEMA and d.get("month") and isinstance(d.get("rows"), list):
            months.append((d["month"], p, d))
    return sorted(months, key=lambda x: x[0])


def rw_row(doc, names):
    """REITWatch 한 호에서 names(지금 ticker, 예전 ticker 차례) 중 먼저 있는 row."""
    rows = {r.get("ticker"): r for r in doc.get("rows") or []}
    return next((rows[n] for n in names if n in rows), None)


def load_reitwatch(dirpath, ticker, aliases=()):
    """REITWatch parsed 파일들에서 이 종목 row, 최신 달 peer, P/FFO history를 읽는다.
    aliases(예전 ticker, B16)가 있으면 그 ticker로 실린 호도 같은 종목으로 잇는다."""
    dirpath = dirpath or os.path.join(data_dir(), "reitwatch", "parsed")
    names = [ticker] + [a for a in aliases if a != ticker]
    months = read_reitwatch_months(dirpath)
    if not months:
        return {"note": "REITWatch parsed 없음(%s): reit-sector-scan skill로 fetch를 먼저 돌린다" % dirpath,
                "files": []}
    month, path, latest = months[-1]
    row = rw_row(latest, names)
    k = int(month[:4]) * 12 + int(month[5:7]) - 1 - REVISION_MONTHS
    old_month = "%04d-%02d" % (k // 12, k % 12 + 1)
    old = next(((p, d) for m, p, d in months if m == old_month), None)
    out = {"month": month, "path": path, "as_of": latest.get("as_of"), "files": [p for _, p, _ in months],
           "row": row, "note": "", "fy1": latest.get("fy1"), "rev_old_month": old_month, "rev_old": old, "names": names}
    if row is None:
        out["note"] = "REITWatch %s에 %s row가 없음" % (month, ticker)
        return out
    sub = row.get("subsector")
    out["peers"] = [r for r in latest["rows"]
                    if r.get("subsector") == sub and r.get("ticker") not in names]
    out["history"], out["alias_months"] = [], []
    sample = [x for x in months[:-1] if int(x[0][5:7]) in QUARTER_MONTHS] + [months[-1]]
    for m, p, d in sample:
        r = rw_row(d, names)
        if r and is_finite(r.get("p_ffo_fy1")):
            out["history"].append((m, r["p_ffo_fy1"]))
            if r.get("ticker") != ticker:
                out["alias_months"].append((m, r.get("ticker")))
    return out


def load_tracker(dirpath):
    """Tracker parsed(reit-sector-scan이 쓴다). {"doc", "path", "note", "older"}: doc은 파일 이름이 가장 늦은 것,
    older는 그 앞 파일들(늦은 것부터). 가격 기준일에 맞는 파일은 pick_tracker가 고른다."""
    dirpath = dirpath or os.path.join(data_dir(), "tracker", "parsed")
    found = []
    for p in sorted(glob.glob(os.path.join(dirpath, "*.json")), reverse=True):
        try:
            d = read_json(p)
        except ValueError:
            continue
        if d.get("schema") == TRACKER_SCHEMA and isinstance(d.get("rows"), list):
            found.append((p, d))
    if found:
        return {"doc": found[0][1], "path": found[0][0], "note": "", "older": found[1:]}
    return {"doc": None, "path": None, "older": [],
            "note": "Tracker parsed 없음(%s): reit-sector-scan skill로 tracker fetch를 먼저 돌린다" % dirpath}


def pick_tracker(trk, ref_date):
    """가격 기준일(ref_date) 뒤에 끝난 분기 파일은 고르지 않는다(그날 아직 없던 자료다).
    분기 끝날(as_of)이 날짜가 아니면 그 파일을 건너뛰지 않고 missing 이유로 낸다."""
    if trk.get("doc") is None:
        return trk
    cands = [(trk.get("path"), trk["doc"])] + list(trk.get("older") or [])
    ref, later = to_date(ref_date), []
    for p, d in cands:
        q = to_date(d.get("as_of"))
        if q is None:
            return {"doc": None, "path": p, "note": "Tracker parsed %s의 as_of %r가 날짜가 아님: reit-sector-scan "
                                                    "tracker fetch로 다시 받는다" % (p, d.get("as_of"))}
        if ref is not None and q > ref:
            later.append(str(d.get("quarter")))
            continue
        return {"doc": d, "path": p, "note": ("가격 기준일 %s 뒤에 끝난 분기 %s는 고르지 않음" % (
            str(ref_date)[:10], "·".join(later))) if later else ""}
    return {"doc": None, "path": None, "note": "가격 기준일 %s 전에 끝난 분기의 Tracker parsed가 없음(%s는 그 뒤 분기)" % (
        str(ref_date)[:10], "·".join(later))}


def load_tracker_map():
    """REITWatch subsector → Tracker row 대응(assets/tracker_map.json, reit-sector-scan sector_map.json의 복사)."""
    d = read_json(os.path.join(skill_root(), "assets", "tracker_map.json"))
    return {k: v for k, v in d.items() if not k.startswith("_")}


def tracker_env(trk, subsector, tmap, ref_date, max_days):
    """REITWatch subsector의 Tracker implied cap rate envelope. 자기 row에 값이 없으면 parent row 값(estimated).
    Tracker 분기 끝날이 ref_date(가격 기준일, 없으면 분기 끝날)보다 max_days 넘게 앞서면 stale."""
    doc = trk.get("doc")
    if doc is None:
        return missing(trk.get("note") or "Tracker parsed를 읽지 않음")
    qend = parse_date(doc["as_of"])  # pick_tracker가 날짜인 것만 넘긴다
    if not subsector:
        return missing("REITWatch subsector를 몰라 Tracker row를 고르지 못함")
    entry = tmap.get(subsector)
    if not entry:
        return missing("assets/tracker_map.json에 subsector %s가 없음" % subsector)
    rows = {r.get("name"): r for r in doc.get("rows") or []}
    name, parent = entry.get("name"), entry.get("parent")
    row, st, notes = rows.get(name) if name else None, "reported", [trk["note"]] if trk.get("note") else []
    if row is None or not is_finite(row.get("implied_cap_rate")):
        row = rows.get(parent) if parent else None
        if row is None or not is_finite(row.get("implied_cap_rate")):
            return missing("Tracker %s에 %s implied cap rate가 없음" % (doc.get("quarter"), name or subsector))
        st = "estimated"
        notes.append("Tracker에 %s 값이 없어 %s 전체 값" % (name or subsector, parent))
    ref = to_date(ref_date)
    if ref is not None:
        age = (ref - qend).days
        if age > max_days:
            st = worst([st, "stale"])
            notes.append("Tracker %s 분기 끝날 %s는 기준일 %s보다 %d일 앞섬(허용 %d일): reit-sector-scan tracker fetch로 갱신" % (
                doc.get("quarter"), doc["as_of"], str(ref_date)[:10], age, max_days))
    return {"value": round(row["implied_cap_rate"], 4), "unit": "%", "status": st, "as_of": doc.get("as_of"),
            "source": "nareit-tracker:%s" % doc.get("source"),
            "locator": "%s %s!%s (%s)" % (trk.get("path"), doc.get("sheet"),
                                         (row.get("cells") or {}).get("implied_cap_rate", "?"), row.get("name")),
            "note": "; ".join(notes)}


def ffo_revision_env(rw):
    """consensus FFO FY1의 3개월 변화(%) envelope. 새 호 FY1 연도와 같은 회계연도 column을 3개월 앞 호에서 고른다
    (앞 호 FY1이 같은 해면 FY1, 앞 호 FY2가 같은 해면 FY2. REITWatch는 2월 호에 FY가 바뀐다). 앞 호 값이 없거나 0 이하면 missing."""
    row = rw.get("row")
    if not row:
        return missing(rw.get("note") or "REITWatch row 없음")
    if rw.get("rev_old") is None:
        return missing("REITWatch 3개월 앞 호(%s)가 없음" % rw.get("rev_old_month"))
    opath, odoc = rw["rev_old"]
    year = rw.get("fy1")
    if year is None:
        return missing("REITWatch %s 호의 FY1 연도를 모름" % rw["month"])
    col = "ffo_fy1" if odoc.get("fy1") == year else "ffo_fy2" if odoc.get("fy2") == year else None
    if col is None:
        return missing("REITWatch %s 호에 %s년 column이 없음" % (odoc.get("month"), year))
    t = row.get("ticker")
    orow = rw_row(odoc, rw.get("names") or [t]) or {}
    old = orow.get(col)
    new = row.get("ffo_fy1")
    if not is_finite(new):
        return missing("REITWatch %s 호에 %s FFO FY1이 없음" % (rw["month"], t))
    if not is_finite(old) or old <= 0:
        return missing("REITWatch %s 호에 %s %s 값이 없거나 0 이하" % (odoc.get("month"), t, col))
    return aged({"value": round((new / old - 1) * 100, 4), "unit": "%", "status": "derived", "as_of": rw.get("as_of"),
                 "source": "reitwatch:%s,%s" % (rw["month"], odoc.get("month")),
                 "locator": "%s rows[ticker=%s].ffo_fy1 %s / %s rows[ticker=%s].%s %s" % (
                     rw["path"], t, new, opath, orow.get("ticker", t), col, old),
                 "note": "%s 호 FY%s %s / %s 호 %s(FY%s) %s − 1. consensus(애널리스트 추정)" % (
                     rw["month"], year, new, odoc.get("month"), col, year, old)}, rw.get("age_note"))


def ffo_growth_env(rw):
    """consensus FFO FY2 / FY1 − 1(%) envelope. FY1이 0 이하면 missing."""
    row = rw.get("row")
    if not row:
        return missing(rw.get("note") or "REITWatch row 없음")
    a, b = row.get("ffo_fy1"), row.get("ffo_fy2")
    if not (is_finite(a) and is_finite(b)) or a <= 0:
        return missing("REITWatch %s 호에 %s FFO FY1·FY2가 없거나 FY1이 0 이하" % (rw["month"], row.get("ticker")))
    return aged({"value": round((b / a - 1) * 100, 4), "unit": "%", "status": "derived", "as_of": rw.get("as_of"),
                 "source": "reitwatch:%s" % rw["month"],
                 "locator": "%s rows[ticker=%s].ffo_fy2 %s / ffo_fy1 %s" % (rw["path"], row.get("ticker"), b, a),
                 "note": "consensus(애널리스트 추정)"}, rw.get("age_note"))


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
            if kind in ("covenant", "rating"):
                env, bad = (read_covenant if kind == "covenant" else read_rating)("%s[%d]" % (key, i), it)
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


EVENT_TYPES = {"tenant_credit": "임차인 신용", "rating": "신용등급", "dividend": "배당", "guidance": "guidance",
               "legal": "소송", "subsequent": "결산 후 사건", "other": "기타"}


def read_events(facts_doc, gaps):
    """facts의 events(숫자 밖 사건): type·date·title·source·quote(원문 문장)가 꼭 있고 source가 web이면 URL이 있어야 한다.
    어긴 항목은 버리고 데이터 구멍(ignored)에 알린다(exit에는 세지 않는다). 날짜 최신순."""
    raw = facts_doc.get("events") or []
    if not isinstance(raw, list):
        gaps.append({"item": "events", "status": "ignored", "reason": "events는 사건 목록이어야 함"})
        return []
    out = []
    for i, e in enumerate(raw):
        why, url = None, ""
        if not isinstance(e, dict):
            why = "형식이 아님"
        elif e.get("type") not in EVENT_TYPES:
            why = "type %r는 %s 중 하나가 아님" % (e.get("type"), ", ".join(EVENT_TYPES))
        elif not (isinstance(e.get("date"), str) and len(e["date"]) == 10 and to_date(e["date"])):
            why = "date %r가 날짜(YYYY-MM-DD)가 아님" % (e.get("date"),)
        else:
            src = str(e.get("source") or "").strip()
            url = str(e.get("url") or (src[4:] if src.startswith("web:") else "")).strip()
            for k, label in (("title", "제목(title)이"), ("source", "출처(source)가"), ("quote", "원문 문장(quote)이")):
                if not str(e.get(k) or "").strip():
                    why = "%s 없음" % label
                    break
            if why is None and src.startswith("web") and not url.startswith(("http://", "https://")):
                why = "source가 web인데 URL이 없음"
        if why:
            gaps.append({"item": "events[%d]" % i, "status": "ignored", "reason": "사건을 쓰지 않음: " + why})
            continue
        out.append({"type": e["type"], "type_name": EVENT_TYPES[e["type"]], "date": e["date"], "title": e["title"],
                    "source": e["source"], "url": url or None, "quote": e["quote"], "locator": e.get("locator") or "",
                    "note": e.get("note") or ""})
    return sorted(out, key=lambda x: x["date"], reverse=True)


SERIES_ND_KEYS = ("covenants", "top_tenants")  # 회사가 표 전체(또는 일부 줄)를 내지 않을 수 있는 series


def read_series_nd(facts_doc, S, gaps):
    """facts의 series_not_disclosed: {series 이름: {source, locator, note}}. 회사가 그 표를 내지 않는다는 근거다(B11).
    근거가 없으면 쓰지 않고(보통 missing) 데이터 구멍에 올린다. covenant 줄이 있으면 줄을 쓴다."""
    raw = facts_doc.get("series_not_disclosed") or {}
    out = {}
    if not isinstance(raw, dict):
        gaps.append({"item": "series_not_disclosed", "status": "ignored", "reason": "series 이름을 key로 하는 객체가 아님"})
        return out
    for k, v in sorted(raw.items()):
        item = "series_not_disclosed." + k
        if k not in SERIES_ND_KEYS:
            gaps.append({"item": item, "status": "ignored",
                         "reason": "series 공시 없음은 %s에만 적는다" % "·".join(SERIES_ND_KEYS)})
        elif not (isinstance(v, dict) and str(v.get("source", "")).strip() and str(v.get("locator", "")).strip()):
            gaps.append({"item": item, "status": "missing",
                         "reason": "공시 없음에는 회사가 내지 않는다는 근거(source·locator)가 있어야 함"})
        elif k == "covenants" and S["covenants"]:
            gaps.append({"item": item, "status": "ignored", "reason": "covenant 줄이 있어 공시 없음을 쓰지 않고 줄로 셈"})
        else:
            out[k] = {"source": v["source"], "locator": v["locator"], "note": v.get("note") or ""}
    return out


def nd_env(note, basis):
    """series 공시 없음 근거(basis)로 만든 공시 없음 envelope. note는 script의 설명, basis note는 agent의 설명."""
    return dict(missing("; ".join(x for x in (note, basis["note"]) if x)), not_disclosed=True,
                source=basis["source"], locator=basis["locator"])


def walk(ms):
    for k, m in ms.items():
        if k == "sector_kpis":
            for kk, mm in m.items():
                yield "sector_kpis." + kk, mm
        else:
            yield k, m


# 값이 없는 것이 정상인 지표의 표시와 사람 말. status는 missing이고 이 표시가 붙으면 exit 2와 metrics_missing에 세지 않는다.
EMPTY_OK = (("not_disclosed", "공시 없음(회사가 내지 않음)"), ("none_in_period", "그 분기 거래 없음"),
            ("not_comparable", "견줄 수 없음"), ("not_assessed", "가볍게 봄(depth light)"))


def empty_reason(m):
    """값이 없는 것이 정상인 지표면 그 표시 이름(not_disclosed 등), 아니면 None."""
    if m.get("status", "missing") != "missing":
        return None
    return next((k for k, _ in EMPTY_OK if m.get(k)), None)


def finish(facts_doc, path, M, checks, flags, gaps, inputs, thresholds, extra=None):
    """metrics 문서를 맺는다: missing·estimated·stale·check 실패를 데이터 구멍에 올리고 exit code를 정한다."""
    labels = dict(EMPTY_OK)
    for k, m in walk(M):
        r = empty_reason(m)
        if r:
            gaps.append({"item": k, "status": r, "reason": "%s: %s" % (labels[r], m.get("note", ""))})
        elif m.get("status") == "missing":
            gaps.append({"item": k, "status": "missing", "reason": m.get("note", "")})
        elif m.get("status") in ("estimated", "stale") and m.get("value") is not None:
            gaps.append({"item": k, "status": m["status"], "reason": m.get("note", "")})
    for c in checks:
        if c["pass"] is None and c.get("not_assessed"):  # 가볍게 보기라 맞춰 보지 않았다
            gaps.append({"item": "check:" + c["check"], "status": "not_assessed", "reason": "가볍게 봄: " + c["note"]})
        elif c["pass"] is None:  # 회사가 그 값을 내지 않아 맞춰 볼 것이 없다
            gaps.append({"item": "check:" + c["check"], "status": "not_disclosed", "reason": "공시 없음: " + c["note"]})
        elif not c["pass"]:
            gaps.append({"item": "check:" + c["check"], "status": "fail" if c["status"] != "missing" else "missing",
                         "reason": c["note"]})
    seen, uniq = set(), []
    for g in gaps:  # 같은 칸의 같은 이유는 한 번만(facts 검사와 그 칸의 passthrough가 같은 글을 낸다)
        key = (g["item"], g["status"], g["reason"])
        if key not in seen:
            seen.add(key)
            uniq.append(g)
    gaps = uniq
    n_missing = sum(1 for _, m in walk(M) if m.get("status") == "missing" and not empty_reason(m))
    n_empty = {r: sum(1 for _, m in walk(M) if empty_reason(m) == r) for r, _ in EMPTY_OK}
    n_fail = sum(1 for c in checks if c["pass"] is False)
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
        "summary": {"metrics_missing": n_missing, "metrics_not_disclosed": n_empty["not_disclosed"],
                    "metrics_none_in_period": n_empty["none_in_period"], "metrics_not_comparable": n_empty["not_comparable"],
                    "metrics_not_assessed": n_empty["not_assessed"],
                    "checks_failed": n_fail,
                    "flags": {f["flag"]: f["level"] for f in flags}},
    }
    doc.update(extra or {})
    return doc, (2 if (n_missing or n_fail) else 0)


def compute(facts_doc, th, macro=None, rw=None, baa=None, tracker=None, tmap=None, dbaa=None, hy=None, bb=None):
    """facts 문서 → metrics 문서. 같은 입력이면 같은 출력(시각을 넣지 않는다).
    macro는 DGS10, baa는 BAA10Y, dbaa는 DBAA, hy는 BAMLH0A0HYM2(HY OAS), bb는 BAMLH0A1HYBB(BB OAS)
    envelope(load_macro), tracker는 load_tracker 결과,
    tmap은 REITWatch subsector → Tracker row 대응(없으면 assets/tracker_map.json)."""
    if not is_reit_doc(facts_doc):
        return compute_dividend(facts_doc, th, macro=macro)
    F, S, gaps = read_facts(facts_doc)
    SND = read_series_nd(facts_doc, S, gaps)
    events = read_events(facts_doc, gaps)
    depth = facts_doc.get("depth") or "deep"
    if depth not in ("deep", "light"):
        gaps.append({"item": "depth", "status": "ignored", "reason": "depth %r는 deep·light가 아니라 deep으로 셈" % (depth,)})
        depth = "deep"

    def reject(key, why):
        F[key] = missing(why)
        gaps.append({"item": key, "status": "missing", "reason": why})
    for k in NONNEG_FACTS:  # 0 이상이어야 하는 칸: 음수는 부호를 잘못 옮긴 것이다
        if F[k]["value"] is not None and F[k]["value"] < 0:
            reject(k, "%s %s는 0 이상이어야 함(음수 거절): 부호를 회사 표 방향대로 다시 본다" % (k, F[k]["value"]))
    dsw, cso = F["diluted_shares_weighted"], F["common_shares_outstanding"]
    if dsw["value"] is not None and cso["value"] is not None and dsw["value"] > 0 and cso["value"] > 0:
        r = dsw["value"] / cso["value"]
        if r >= SHARE_UNIT_RATIO or r <= 1.0 / SHARE_UNIT_RATIO:
            reject("diluted_shares_weighted", "diluted_shares_weighted %s천 주가 기말 보통주 %s천 주와 %.4g배라 단위를 의심함"
                   "(%d배 넘게 다름): unit을 다시 본다" % (round(dsw["value"], 4), round(cso["value"], 4), r, SHARE_UNIT_RATIO))

    noi = F["noi_annualized"]  # B14: 연환산 NOI가 없고 분기 NOI만 있으면 분기×4(가정)
    if noi["value"] is None and F["noi_quarter"]["value"] is not None:
        noi = env_of(assume(metric("noi_run_rate", "분기 NOI × 4", {"noi_quarter": F["noi_quarter"]}, lambda q: q * 4,
                                   "USD thousands"), "회사가 연환산 NOI를 내지 않아 분기 NOI × 4로 가정"))

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
        nd = bool(F["affo_reported"].get("not_disclosed")) and not items
        checks.append({"check": "affo_bridge", "pass": None if nd else False, "status": "missing", "note":
                       "회사가 AFFO를 내지 않아 bridge가 없음" if nd else
                       "affo_bridge가 없거나 start가 %s 중 하나가 아님" % "/".join(BRIDGE_STARTS)})
        M["affo_bridge_sum"] = dict(missing("affo_bridge 없음"), metric="affo_bridge_sum", formula="", inputs=[],
                                    **({"not_disclosed": True} if nd else {}))
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
               lambda a, n: positive(n, "희석 주식 수가 0 이하라 주당 AFFO를 내지 않음") and a / n, "USD/share",
               note="회사가 주당 AFFO를 내지 않아 총액으로 계산"))
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
    if ffo_mid["value"] is None and ffo_cons["value"] is not None:  # consensus가 회사 Nareit FFO와 정의가 다를 수 있다
        cv, q, am = ffo_cons["value"], F["ffo_per_share_diluted"]["value"], affo_mid["value"]
        why = []
        if q is not None and q > 0 and abs(cv / (q * 4) - 1) * 100 > CONSENSUS_GAP_PCT:
            why.append("회사 분기 FFO/share × 4(%s)와 %.1f%% 다름" % (round(q * 4, 4), (cv / (q * 4) - 1) * 100))
        if am is not None and am > 0 and abs(cv / am - 1) * 100 <= 1:
            why.append("AFFO guidance 가운데(%s)와 거의 같음" % round(am, 4))
        if why:
            M["ffo_fy"]["note"] = "; ".join(x for x in (
                "consensus 정의 점검: consensus FFO FY1 %s가 %s. consensus가 회사 Nareit FFO가 아니라 AFFO·NFFO 기준일 수 있어 "
                "P/FFO를 다른 종목과 바로 견주지 않는다" % (cv, ", ".join(why)), M["ffo_fy"]["note"]) if x)

    # 배당 지속성
    M["forward_dividend"] = metric("forward_dividend", "최근 정기 배당 × 연 지급 횟수(특별배당 제외)",
                                   {"dividend_regular_latest": F["dividend_regular_latest"],
                                    "dividend_frequency": F["dividend_frequency"]},
                                   lambda d, n: d * n, "USD/share")
    M["forward_yield"] = metric("forward_yield", "forward dividend / price × 100",
                                {"forward_dividend": env_of(M["forward_dividend"]), "price": F["price"]},
                                lambda d, p: d / p * 100, "%")
    def payout(why):  # 분모(AFFO·FFO)가 0 이하면 payout이 뜻을 잃는다(일반 배당주 EPS와 같은 규칙)
        return lambda d, a: positive(a, why) and d / a * 100
    M["affo_payout_forward"] = metric("affo_payout_forward", "forward dividend / 연간 AFFO per share × 100",
                                      {"forward_dividend": env_of(M["forward_dividend"]), "affo_fy": env_of(M["affo_fy"])},
                                      payout("연간 AFFO가 0 이하라 payout을 내지 않음"), "%")
    M["affo_payout_quarter"] = metric("affo_payout_quarter", "분기 지급 배당/share / 분기 AFFO/share × 100",
                                      {"dividends_paid_per_share_quarter": F["dividends_paid_per_share_quarter"],
                                       "affo_per_share_diluted": aps},
                                      payout("분기 AFFO/share가 0 이하라 payout을 내지 않음"), "%")
    M["ffo_payout_forward"] = metric("ffo_payout_forward", "forward dividend / 연간 FFO per share × 100",
                                     {"forward_dividend": env_of(M["forward_dividend"]), "ffo_fy": env_of(M["ffo_fy"])},
                                     payout("연간 FFO가 0 이하라 payout을 내지 않음"), "%",
                                     note="참고 줄(flag 없음). ffo_fy=" + M["ffo_fy"]["formula"])
    dividend_history(S, M)

    # AFFO bridge category: 유지 비용(maintenance capex + TI·LC) / NOI, straight-line rent / FFO
    no_bridge_nd = bool(M["affo_bridge_sum"].get("not_disclosed"))  # 회사가 AFFO를 내지 않아 bridge 줄이 없다

    def cat_sum(cats):
        got = [e for e in items if e.get("category") in cats]
        if not got and no_bridge_nd:
            return dict(missing("회사가 AFFO를 내지 않아 AFFO bridge에 %s 줄이 없음" % "·".join(cats)), not_disclosed=True), []
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
    elif sl["value"] is None and F["straight_line_rent"]["value"] is not None:  # FFO·NFFO 표에만 그 줄이 있는 회사(MPT)
        sf = F["straight_line_rent"]
        sl = {"value": -sf["value"], "status": sf["status"], "as_of": sf.get("as_of"), "label": "facts straight_line_rent",
              "note": "AFFO bridge에 straight-line 줄이 없어 facts straight_line_rent(%s)를 씀" % sf["locator"]}
    # 유지 비용 줄은 AFFO에서 빼는 줄이라 0 이하다. 양수면 부호를 뒤집어 옮긴 것이다(bridge 합은 허용 오차 안일 수 있다)
    pos = [i for i, e in enumerate(items) if e.get("category") in ("recurring_capex", "leasing_cost")
           and e["value"] is not None and e["value"] > 0]
    for i in pos:
        gaps.append({"item": "affo_bridge.items[%d]" % i, "status": "missing",
                     "reason": "%s 줄 %r이 +%s: AFFO에서 빼는 유지 비용 줄은 0 이하여야 함(부호를 회사 표 방향대로)" % (
                         items[i]["category"], items[i].get("label"), items[i]["value"])})
    if pos:
        capex = missing("recurring_capex·leasing_cost 줄 items[%s]이 양수라(부호 뒤집힘) 쓰지 않음" % ",".join(map(str, pos)))
    unknown = [i for i, e in enumerate(items) if e["category"] is not None and e["category"] not in BRIDGE_CATEGORIES]
    if unknown:  # 그 줄이 어느 category인지 몰라 합이 틀릴 수 있다. 0이나 일부 합으로 내지 않는다
        why = "AFFO bridge에 모르는 category 줄(items[%s])이 있어 category 지표를 내지 않음: category를 고친다" % ",".join(
            map(str, unknown))
        capex, sl = missing(why), missing(why)
    elif capex["value"] is None and not capex_rows and not no_bridge_nd and F["capex_reserve"].get("not_disclosed"):
        # 회사가 capex reserve를 내지 않고 AFFO에서 유지 capex를 빼지도 않는다(triple-net 등): 못 찾은 것이 아니라 공시 없음
        capex = dict(missing("회사가 capex reserve를 내지 않고 AFFO bridge에도 recurring_capex·leasing_cost 줄이 없음"
                             "(capex_reserve 근거: %s)" % F["capex_reserve"]["locator"]), not_disclosed=True)
    M["recurring_capex_to_noi_pct"] = metric("recurring_capex_to_noi_pct",
                                             "−(recurring capex + leasing cost 줄, 분기) × 4 / 연환산 NOI × 100",
                                             {"recurring_capex_leasing": capex, "noi_annualized": noi},
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
    bad_periods = [i for i, e in enumerate(mats) if not is_ladder_period(e.get("period"))]
    for i in bad_periods:
        gaps.append({"item": "debt_maturities[%d]" % i, "status": "missing",
                     "reason": "period %r는 연도(YYYY)나 thereafter가 아님: 날짜별 줄이면 연도를 period로 붙인다" % (
                         mats[i].get("period"),)})
    ladder_why = ("debt_maturities의 period가 연도·thereafter가 아닌 줄(%s)이 있음" % ",".join(map(str, bad_periods))
                  if bad_periods else None)
    if mats and not ladder_why and all(e["value"] is not None for e in mats):
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
        why = "debt_maturities 없음" if not mats else ladder_why or "debt_maturities에 missing 칸이 있음"
        M["maturity_ladder"] = dict(missing(why), metric="maturity_ladder", formula="", inputs=[])
        M["maturity_max_year_pct"] = dict(missing(why), metric="maturity_max_year_pct", formula="", inputs=[])

    # 신용: gross assets(감가상각 전 장부가) 기준 비율, S&P식 fixed-charge coverage, covenant headroom
    M["gross_assets"] = metric("gross_assets", "total assets + accumulated depreciation",
                               {"total_assets": F["total_assets"], "accumulated_depreciation": F["accumulated_depreciation"]},
                               lambda a, d: a + d, "USD thousands")
    if F["accumulated_depreciation"]["value"] == 0 and M["gross_assets"]["value"] is not None:
        M["gross_assets"]["note"] = "; ".join(x for x in (
            "감가상각 누계가 0이라 gross assets = total assets(lease·financing 회계 등으로 부동산을 감가상각하지 않는 회사): %s" % (
                F["accumulated_depreciation"].get("note") or "회사 표 그대로"), M["gross_assets"]["note"]) if x)
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
                    {"unencumbered_noi": F["unencumbered_noi"], "noi_annualized": noi},
                    lambda u, n: u / n * 100, "%")
    M["unencumbered_pct"] = dict(first_of(un_assets, un_noi), metric="unencumbered_pct")
    M["fixed_charge_coverage"] = metric("fixed_charge_coverage",
                                        "분기 EBITDAre / (이자비용 + 자본화 이자 + 우선주 배당 + 정기 원금 상환)",
                                        {"ebitdare_quarter": F["ebitdare_quarter"], "interest_expense": F["interest_expense"],
                                         "capitalized_interest": F["capitalized_interest"],
                                         "preferred_dividends": F["preferred_dividends"],
                                         "principal_amortization": F["principal_amortization"]},
                                        lambda e, i, c, p, a: e / (i + c + p + a), "x")
    pa = F["principal_amortization"]
    if pa["value"] == 0 and M["fixed_charge_coverage"]["value"] is not None:  # B13: 만기 일시 상환(bullet)뿐인 부채
        M["fixed_charge_coverage"]["note"] = "; ".join(x for x in (
            "정기 원금 상환을 0으로 셈(만기 일시 상환, facts 근거: %s)" % pa["locator"], M["fixed_charge_coverage"]["note"]) if x)
    covs = S["covenants"]
    if not covs and "covenants" in SND:
        env = nd_env("회사가 covenant 표를 내지 않음", SND["covenants"])
        M["covenant_headroom"] = dict(env, metric="covenant_headroom", formula="", inputs=[])
        M["covenant_headroom_min_pct"] = dict(env, metric="covenant_headroom_min_pct", formula="", inputs=[])
    elif not covs:
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
    dbaa_env = macro_aged(dbaa if dbaa is not None else missing("macro snapshot을 읽지 않음"), F["price"], th)
    rates = {"dgs10": dgs, "baa10y": baa10y, "dbaa": dbaa_env,
             "hy": macro_aged(hy if hy is not None else missing("macro snapshot을 읽지 않음"), F["price"], th),
             "bb": macro_aged(bb if bb is not None else missing("macro snapshot을 읽지 않음"), F["price"], th)}
    M["wam_years"] = passthrough("wam_years", F["wam_years"])
    M["weighted_avg_rate_pct"] = passthrough("weighted_avg_rate_pct", F["weighted_avg_rate_pct"])
    M["market_borrowing_rate_est"] = market_rate(F, S["credit_ratings"], rates,
                                                 F["price"].get("as_of") or facts_doc.get("period_end"), gaps)
    if mats and not ladder_why and all(e["value"] is not None for e in mats) and to_date(facts_doc.get("period_end")):
        m24, used, split = maturities_within_24m(mats, facts_doc["period_end"])
        st = worst([e["status"] for e in used] or ["reported"])
        M["maturities_24m"] = {"metric": "maturities_24m", "unit": "USD thousands", "value": round(m24, 4),
                               "status": "derived" if st == "reported" else st, "as_of": facts_doc["period_end"],
                               "formula": "분기 끝날 해·다음 해 만기 + 그다음 해 만기 × (분기 끝 달 / 12)",
                               "inputs": [e["period"] for e in used],
                               "note": "%s 끝 뒤 24개월(%s년)" % (facts_doc["period_end"],
                                                               "·".join(sorted(set(str(e["period"]) for e in used))))}
        if split:
            M["maturities_24m"] = assume(M["maturities_24m"], split)
    else:
        why = "debt_maturities 없음" if not mats else ladder_why or (
            "debt_maturities에 missing 칸이 있음" if to_date(facts_doc.get("period_end")) else "facts에 period_end(날짜)가 없음")
        M["maturities_24m"] = dict(missing(why), metric="maturities_24m", formula="", inputs=[])
    M["affo_annualized"] = assume(metric("affo_annualized", "분기 AFFO 총액 × 4", {"affo_reported": F["affo_reported"]},
                                         lambda a: a * 4, "USD thousands"), "분기 AFFO 총액 × 4로 연환산(가정)")

    def per_affo(scale=100, what="AFFO"):
        def fn(x, a):
            if a <= 0:
                raise Undefined("연간 %s 총액이 0 이하라 비율을 내지 않음" % what)
            return x / a * scale
        return fn
    # 회사가 AFFO를 내지 않으면(공시 없음) Normalized FFO(없으면 FFO) × 4를 참고 분모로 쓴다(findings 8)
    affo_nd = bool(F["affo_reported"].get("not_disclosed"))
    if affo_nd:
        fk = "normalized_ffo_reported" if F["normalized_ffo_reported"]["value"] is not None else "ffo_reported"
        fname = "Normalized FFO" if fk == "normalized_ffo_reported" else "FFO"
        M["ffo_annualized"] = assume(metric("ffo_annualized", "분기 %s 총액 × 4" % fname, {fk: F[fk]}, lambda a: a * 4,
                                            "USD thousands"),
                                     "회사가 AFFO를 내지 않아 %s 총액 × 4를 참고 분모로 씀(유지 capex를 빼지 않아 AFFO보다 크게 잡힌다)" % fname)
    M["refi_cost_24m"] = metric("refi_cost_24m", "24개월 안 만기 × (시장 차입금리 추정 − 기존 가중평균 금리) / 100, 연간",
                                {"maturities_24m": env_of(M["maturities_24m"]),
                                 "market_borrowing_rate_est": env_of(M["market_borrowing_rate_est"]),
                                 "weighted_avg_rate_pct": F["weighted_avg_rate_pct"]},
                                lambda m, r, w: m * (r - w) / 100, "USD thousands",
                                note="기존 금리는 만기 부채가 아니라 전체 부채의 가중평균")
    mr, wr = M["market_borrowing_rate_est"]["value"], F["weighted_avg_rate_pct"]["value"]
    if M["refi_cost_24m"]["value"] is not None and mr < wr:  # D12: 시장 금리가 지금 금리보다 낮게 나오면 그 사실을 적는다
        M["refi_cost_24m"]["note"] = "; ".join(x for x in (
            "차환 금리 추정 %s%%가 지금 가중평균 금리 %s%%보다 낮아 이자가 줄어드는 쪽으로 나옴" % (round(mr, 4), round(wr, 4)),
            M["refi_cost_24m"]["note"]) if x)
    M["refi_impact_affo_pct"] = metric("refi_impact_affo_pct", "refi 이자 증가(연간) / 연간 AFFO 총액 × 100",
                                       {"refi_cost_24m": env_of(M["refi_cost_24m"]),
                                        "affo_annualized": env_of(M["affo_annualized"])}, per_affo(), "%")
    M["floating_rate_shock_affo_pct"] = metric("floating_rate_shock_affo_pct",
                                               "변동금리 원금 × 1%p / 연간 AFFO 총액 × 100",
                                               {"floating_rate_debt": F["floating_rate_debt"],
                                                "affo_annualized": env_of(M["affo_annualized"])},
                                               lambda f, a: per_affo()(f * 0.01, a), "%")
    if affo_nd:
        M["refi_impact_ffo_pct"] = metric("refi_impact_ffo_pct", "refi 이자 증가(연간) / 연간 %s 총액 × 100(참고)" % fname,
                                          {"refi_cost_24m": env_of(M["refi_cost_24m"]),
                                           "ffo_annualized": env_of(M["ffo_annualized"])}, per_affo(what=fname), "%")
        M["floating_rate_shock_ffo_pct"] = metric("floating_rate_shock_ffo_pct",
                                                  "변동금리 원금 × 1%%p / 연간 %s 총액 × 100(참고)" % fname,
                                                  {"floating_rate_debt": F["floating_rate_debt"],
                                                   "ffo_annualized": env_of(M["ffo_annualized"])},
                                                  lambda f, a: per_affo(what=fname)(f * 0.01, a), "%")
    M["retained_cash_flow_24m"] = metric("retained_cash_flow_24m",
                                         "2 × (연간 AFFO 총액 − forward dividend × (보통주 + OP unit))",
                                         {"affo_annualized": env_of(M["affo_annualized"]),
                                          "forward_dividend": env_of(M["forward_dividend"]),
                                          "common_shares_outstanding": F["common_shares_outstanding"],
                                          "op_units_outstanding": F["op_units_outstanding"]},
                                         lambda a, d, s, u: 2 * (a - d * (s + u)), "USD thousands",
                                         note="배당 뒤 남는 AFFO가 24개월 이어진다고 가정. 음수면 그만큼 돈이 더 든다")
    if affo_nd:
        M["retained_cash_flow_24m"] = first_of(M["retained_cash_flow_24m"], metric(
            "retained_cash_flow_24m", "2 × (연간 %s 총액(참고) − forward dividend × (보통주 + OP unit))" % fname,
            {"ffo_annualized": env_of(M["ffo_annualized"]), "forward_dividend": env_of(M["forward_dividend"]),
             "common_shares_outstanding": F["common_shares_outstanding"], "op_units_outstanding": F["op_units_outstanding"]},
            lambda a, d, s, u: 2 * (a - d * (s + u)), "USD thousands",
            note="회사가 AFFO를 내지 않아 %s로 대신(참고): 배당 뒤 남는 돈이 24개월 이어진다고 가정" % fname))
    M["liquidity_sources_24m"] = metric("liquidity_sources_24m", "현금 + revolver 잔여 + 유보 현금흐름(24개월)",
                                        {"liquidity_cash": F["liquidity_cash"], "revolver_available": F["revolver_available"],
                                         "retained_cash_flow_24m": env_of(M["retained_cash_flow_24m"])},
                                        lambda c, r, k: c + r + k, "USD thousands")
    uses_in = {"maturities_24m": env_of(M["maturities_24m"]), "dev_remaining_spend": F["dev_remaining_spend"]}
    raw_oc = (facts_doc.get("facts") or {}).get("other_committed_uses")
    if isinstance(raw_oc, dict) and (raw_oc.get("value") is not None or "parts" in raw_oc):  # agent가 값을 적었을 때만 더한다
        uses_in["other_committed_uses"] = F["other_committed_uses"]
    M["liquidity_uses_24m"] = metric("liquidity_uses_24m", "24개월 안 만기 + 남은 개발비" + (
        " + 그 밖의 약정(대출 funding commitment 등)" if "other_committed_uses" in uses_in else ""), uses_in,
        lambda *xs: sum(xs), "USD thousands",
        note="그 밖의 약정은 24개월 안에 모두 쓴다고 가정(보수적)" if "other_committed_uses" in uses_in else "")
    uses = M["liquidity_uses_24m"]
    if uses["value"] == 0:  # 24개월 안에 갚을 만기와 남은 개발비가 없다. 비율이 뜻을 잃지만 빠진 값은 아니다
        M["liquidity_coverage_24m"] = {
            "metric": "liquidity_coverage_24m", "formula": "liquidity sources / uses (24개월)", "unit": "x",
            "inputs": ["liquidity_sources_24m", "liquidity_uses_24m"], "value": None, "status": uses["status"],
            "as_of": uses.get("as_of"), "note": "갚을 것 없음: 24개월 안 만기와 남은 개발비가 0이라 비율을 내지 않음"}
    else:
        M["liquidity_coverage_24m"] = metric("liquidity_coverage_24m", "liquidity sources / uses (24개월)",
                                             {"liquidity_sources_24m": env_of(M["liquidity_sources_24m"]),
                                              "liquidity_uses_24m": env_of(uses)},
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
            ms = sorted(set(str(e.get("measure") or "") for e in tens[:n]))  # 비중의 기준(ABR, revenue 등). 없으면 ABR
            what = "임대료(ABR)" if ms == [""] else ms[0] if len(ms) == 1 else "기준이 섞인(%s)" % "·".join(x or "?" for x in ms)
            M["top%d_tenant_pct" % n] = metric("top%d_tenant_pct" % n, "%s 비중 상위 %d 임차인 합" % (what, n),
                                               {(e.get("label") or "t%d" % i): e for i, e in enumerate(tens[:n])},
                                               lambda *xs: sum(xs), "%")
            if len(ms) > 1:
                M["top%d_tenant_pct" % n] = assume(M["top%d_tenant_pct" % n],
                                                   "임차인마다 비중의 기준(measure)이 섞임: 같은 기준의 합이 아니라 어림")
        elif len(tens) < n and "top_tenants" in SND:
            M["top%d_tenant_pct" % n] = dict(nd_env("회사가 임차인 상위 %d곳까지만 냄" % len(tens), SND["top_tenants"]),
                                             metric="top%d_tenant_pct" % n, formula="", inputs=[])
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
                                   {"noi_annualized": noi, "market_cap": env_of(M["market_cap"]),
                                    "net_debt": env_of(M["net_debt"]), "preferred_equity": F["preferred_equity"]},
                                   lambda n, m, d, pf: n / (m + d + pf) * 100, "%")

    # valuation 기준선: Green Street식 economic implied cap rate(운영 부동산만, JV pro-rata, NOI − capex reserve)와 부채 시가
    if any(g["item"] == "capex_reserve" for g in gaps):  # 회사 값을 거절했으면 bridge 대리값으로 덮지 않는다
        M["capex_reserve"] = passthrough("capex_reserve", F["capex_reserve"], "회사 발표 capex reserve(연간)")
    else:
        M["capex_reserve"] = dict(first_of(
            passthrough("capex_reserve", F["capex_reserve"], "회사 발표 capex reserve(연간)"),
            assume(metric("capex_reserve_run_rate", "−(AFFO bridge recurring capex + leasing cost 줄, 분기) × 4",
                          {"recurring_capex_leasing": capex}, lambda c: -c * 4, "USD thousands"),
                   "회사가 capex reserve를 내지 않아 AFFO bridge의 분기 recurring capex·leasing cost × 4로 대신(가정)")),
            metric="capex_reserve")
    M["noi_economic"] = metric("noi_economic", "연환산 NOI − capex reserve(연간)",
                               {"noi_annualized": noi, "capex_reserve": env_of(M["capex_reserve"])},
                               lambda n, c: n - c, "USD thousands")
    M["non_operating_assets"] = metric("non_operating_assets", "건설 중 자산(CIP) + 개발용 토지 + 그 밖의 비운영 자산",
                                       {k: F[k] for k in ("construction_in_progress", "land_held", "other_non_operating_assets")},
                                       lambda a, b, c: a + b + c, "USD thousands", note="NOI에 수익이 들어 있지 않은 자산")
    M["jv_debt_pro_rata"] = passthrough("jv_debt_pro_rata", F["jv_debt_pro_rata"],
                                        "미연결 JV 부채의 지분 몫(회사 표, 여러 줄이면 script가 더함)")
    M["economic_ev"] = metric("economic_ev", "시총 + net debt + 우선주 + 미연결 JV 부채(지분 몫) − 비운영 자산",
                              {"market_cap": env_of(M["market_cap"]), "net_debt": env_of(M["net_debt"]),
                               "preferred_equity": F["preferred_equity"], "jv_debt_pro_rata": F["jv_debt_pro_rata"],
                               "non_operating_assets": env_of(M["non_operating_assets"])},
                              lambda m, d, pf, j, x: m + d + pf + j - x, "USD thousands")

    def cap_rate(n, ev):
        if ev <= 0:
            raise Undefined("EV가 0 이하라 cap rate를 내지 않음")
        return n / ev * 100
    M["economic_implied_cap_rate"] = metric("economic_implied_cap_rate", "(연환산 NOI − capex reserve) / economic EV × 100",
                                            {"noi_economic": env_of(M["noi_economic"]), "economic_ev": env_of(M["economic_ev"])},
                                            cap_rate, "%", note="운영 부동산만, capex reserve를 뺀 NOI(Green Street식 economic cap rate)")
    M["debt_fair_value"] = passthrough("debt_fair_value", F["debt_fair_value"], "10-Q·10-K fair value 표의 부채 fair value")
    M["debt_carrying_value"] = passthrough("debt_carrying_value", F["debt_carrying_value"], "같은 fair value 표의 carrying value")
    M["debt_mtm_adjustment"] = metric("debt_mtm_adjustment", "부채 fair value − 같은 표의 carrying value",
                                      {"debt_fair_value": F["debt_fair_value"], "debt_carrying_value": F["debt_carrying_value"]},
                                      lambda fv, cv: fv - cv, "USD thousands",
                                      note="음수면 부채 시가가 장부가보다 낮다(시장금리가 기존 금리보다 높다)")
    M["economic_implied_cap_rate_debt_mtm"] = metric(
        "economic_implied_cap_rate_debt_mtm", "(연환산 NOI − capex reserve) / (economic EV + 부채 시가 조정) × 100",
        {"noi_economic": env_of(M["noi_economic"]), "economic_ev": env_of(M["economic_ev"]),
         "debt_mtm_adjustment": env_of(M["debt_mtm_adjustment"])},
        lambda n, ev, a: cap_rate(n, ev + a), "%", note="fair value 표에 없는 부채(JV 부채 등)는 장부가 그대로")
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
        am = rwd.get("alias_months") or []
        M["p_ffo_history_pctile"]["note"] = "; ".join(x for x in (
            "%s~%s의 1·4·7·10월 호와 최신 호" % (rwd["history"][0][0], rwd["history"][-1][0]),
            "예전 ticker %s로 실린 %s~%s 호 %d개를 이음(assets/ticker_aliases.json·facts former_tickers)" % (
                "·".join(sorted(set(t for _, t in am))), am[0][0], am[-1][0], len(am)) if am else "",
            rwd["age_note"]) if x)
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
    M["ffo_fy1_rev_3m"] = passthrough("ffo_fy1_rev_3m", ffo_revision_env(rwd),
                                      "새 호 consensus FFO FY1 / 3개월 앞 호의 같은 회계연도 consensus FFO − 1, %")
    M["ffo_growth_fy2"] = passthrough("ffo_growth_fy2", ffo_growth_env(rwd), "consensus FFO FY2 / FY1 − 1, %")

    # cap rate 비교 줄: 회사 매입·매각 cap rate(회사 정의), Tracker subsector implied cap rate(명목)
    M["acquisitions_cap_rate"] = passthrough("acquisitions_cap_rate", F["acquisitions_cap_rate"], "회사 발표 최근 매입 cap rate(회사 정의)")
    M["dispositions_cap_rate"] = passthrough("dispositions_cap_rate", F["dispositions_cap_rate"], "회사 발표 최근 매각 cap rate(회사 정의)")
    ref_date = F["price"].get("as_of") or facts_doc.get("period_end")
    trk = pick_tracker(tracker if tracker is not None else {"doc": None, "path": None, "note": "Tracker parsed를 읽지 않음"},
                       ref_date)
    tmap = tmap if tmap is not None else load_tracker_map()
    tenv = tracker_env(trk, M["subsector"]["value"], tmap, ref_date, th["tracker_max_age_days"])
    M["tracker_subsector_cap_rate"] = passthrough("tracker_subsector_cap_rate", tenv,
                                                  "Nareit REIT Industry Tracker subsector implied cap rate(명목)")
    tm = tmap.get(M["subsector"]["value"]) or {}
    if tm.get("cap_rate_comparable") is False:  # B12: 회계 기준이 달라 회사 cap rate와 견줄 수 없는 Tracker subsector
        why = "Tracker %s 값과 회사 implied cap rate는 견줄 수 없음: %s(assets/tracker_map.json)" % (
            M["subsector"]["value"], tm.get("why") or "회계 기준이 다름")
        M["implied_cap_rate_vs_tracker"] = dict(missing(why), unit="%p", not_comparable=True, metric="implied_cap_rate_vs_tracker",
                                                formula="implied cap rate(명목) − Tracker subsector implied cap rate, %p",
                                                inputs=["implied_cap_rate", "tracker_subsector_cap_rate"])
        if tenv.get("value") is not None:
            M["tracker_subsector_cap_rate"]["note"] = "; ".join(
                x for x in (M["tracker_subsector_cap_rate"].get("note"), why) if x)
    else:
        M["implied_cap_rate_vs_tracker"] = metric("implied_cap_rate_vs_tracker",
                                                  "implied cap rate(명목) − Tracker subsector implied cap rate, %p",
                                                  {"implied_cap_rate": env_of(M["implied_cap_rate"]),
                                                   "tracker_subsector_cap_rate": tenv},
                                                  lambda a, b: a - b, "%p")

    lacking = light_pass(F, S, SND, M, checks, gaps) if depth == "light" else []
    price_dated(M, F["price"])
    flags = [
        flag("affo_payout", M["affo_payout_forward"] if M["affo_payout_forward"]["value"] is not None
             else M["affo_payout_quarter"], th, "affo_payout_pct"),
        flag("maturity_concentration", M["maturity_max_year_pct"], th, "maturity_single_year_pct"),
        flag("net_debt_to_ebitdare", M["net_debt_to_ebitdare"], th, "net_debt_to_ebitdare_x"),
        flag("interest_coverage", M["interest_coverage"], th, "interest_coverage_x"),
        flag("covenant_headroom", M["covenant_headroom_min_pct"], th, "covenant_headroom_pct"),
    ]

    doc, code = finish(facts_doc, "reit", M, checks, flags, gaps,
                       {"macro_snapshot": (macro or {}).get("locator", "").split(" ")[0] or None,
                        "reitwatch_files": rwd.get("files", []), "reitwatch_month": rwd.get("month"),
                        "tracker_file": trk.get("path")},
                       {k: th[k] for k in ("affo_payout_pct", "maturity_single_year_pct", "net_debt_to_ebitdare_x",
                                           "interest_coverage_x", "covenant_headroom_pct", "ffo_tie_out_tolerance_pct")},
                       extra={"depth": depth, "scorecard": build_scorecard(M, th, load_scorecard(), facts_doc.get("period")),
                              "events": events})
    doc["summary"]["light_core_missing"] = lacking
    return doc, (2 if lacking else code)


# ---------------------------------------------------------------- 등급(scorecard, D11)

SCORECARD_SCHEMA = "reit-scorecard/1"
GRADE_ORDER = ("ample", "moderate", "tight", "risk")  # 나쁜 쪽으로 간다


def load_scorecard(path=None):
    return read_json(path or os.path.join(skill_root(), "assets", "scorecard.json"))


def resolve_cut(c, th, spec):
    """cuts 칸 하나: 숫자면 그대로, "watch"·"alert"면 thresholds.json의 그 값(flag와 같은 숫자)."""
    return th[spec["thresholds"]][c] if isinstance(c, str) else c


def num(x):
    return ("%.4f" % x).rstrip("0").rstrip(".") if isinstance(x, float) else str(x)


def band_grade(v, spec, th):
    cuts = [resolve_cut(c, th, spec) for c in spec["cuts"]]
    for g, c in zip(GRADE_ORDER, cuts):
        if (v < c) if spec["direction"] == "above" else (v >= c):
            return g
    return "risk"


def band_text(spec, th, unit, names):
    a, b, c = [num(resolve_cut(x, th, spec)) + unit for x in spec["cuts"]]
    if spec["direction"] == "above":
        t = "%s < %s, %s %s~%s, %s %s~%s, %s ≥ %s" % (names["ample"], a, names["moderate"], a, b, names["tight"], b, c,
                                                     names["risk"], c)
    else:
        t = "%s ≥ %s, %s %s~%s, %s %s~%s, %s < %s" % (names["ample"], a, names["moderate"], b, a, names["tight"], c, b,
                                                     names["risk"], c)
    src = "경험칙, assets/scorecard.json"
    if spec.get("thresholds"):
        src += "; 경계 %s·%s는 thresholds.json %s watch·alert" % (b, c, spec["thresholds"])
    return "%s (%s)" % (t, src)


def worse(g, n=1):
    return GRADE_ORDER[min(GRADE_ORDER.index(g) + n, len(GRADE_ORDER) - 1)]


def empty_note(m):
    """값이 없는 지표의 사람 말: 공시 없음·가볍게 봄 등, 아니면 '없음: 이유'."""
    r = empty_reason(m)
    return "%s: %s" % (dict(EMPTY_OK)[r], m.get("note", "")) if r else "없음: %s" % (m.get("note") or "값 없음")


def build_scorecard(M, th, sc, period):
    """축 5개의 등급(D11). 축마다 근거 지표(값·단위·단계·기준 문구)와 한 단계 내림의 이유를 낸다.
    축의 핵심 지표가 모두 없음·공시 없음이면 판정 못 함(unknown)이다. 같은 입력이면 같은 출력."""
    names, vnames, specs = sc["grades"], sc["valuation_grades"], sc["metrics"]
    year = int(str(period)[:4]) if str(period or "")[:4].isdigit() else None

    def ev(key, role="core"):
        m = M.get(key) or dict(missing("metrics에 없는 지표"))
        spec = specs.get(key) or {}
        unit = m.get("unit") or ""
        e = {"metric": key, "name": spec.get("name", key), "value": m.get("value"), "unit": unit,
             "status": m.get("status"), "role": role, "grade": None, "grade_name": None, "criterion": "", "note": ""}
        if "cuts" in spec:
            e["criterion"] = band_text(spec, th, "%" if unit == "%" else unit, names)
        elif "low_below" in spec:
            e["criterion"] = "%s < %s, %s %s~%s, %s ≥ %s (경험칙, assets/scorecard.json)" % (
                vnames["low"], num(spec["low_below"]), vnames["mid"], num(spec["low_below"]), num(spec["high_at"]),
                vnames["high"], num(spec["high_at"]))
        v = m.get("value")
        if is_finite(v) and "cuts" in spec:
            e["grade"] = band_grade(v, spec, th)
        elif is_finite(v) and "low_below" in spec:
            e["grade"] = "low" if v < spec["low_below"] else "high" if v >= spec["high_at"] else "mid"
        elif v is None and key == "liquidity_coverage_24m" and m.get("status") != "missing":
            e["grade"], e["note"] = "ample", "갚을 것 없음: 24개월 안 만기와 남은 개발비가 0"
        elif v is None:
            e["note"] = empty_note(m)
        if e["grade"]:
            e["grade_name"] = (vnames if "low_below" in spec else names)[e["grade"]]
        return e

    def axis(key, evs, base, reasons, gnames=names):
        meta = next(a for a in sc["axes"] if a["key"] == key)
        if base is None:
            grade = "unknown"
        else:  # 한 단계 내림은 좋고 나쁨이 있는 축에만(valuation은 위치라 내리지 않는다)
            grade = worse(base, len(reasons)) if gnames is names else base
        out = {"key": key, "name": meta["name"], "grade": grade, "grade_name": gnames[grade],
               "base_grade": base or "unknown", "downgrade": None, "rule": meta["rule"], "evidence": evs, "note": ""}
        if base is None:
            out["note"] = "핵심 지표가 모두 없음·공시 없음이라 판정 못 함"
        elif reasons and gnames is names:
            out["downgrade"] = {"notches": len(reasons), "reasons": reasons}
        return out

    def worst_of(evs):
        gs = [e.get("grade_used") or e["grade"] for e in evs if e["grade"]]
        return max(gs, key=GRADE_ORDER.index) if gs else None

    axes = []
    # 배당 여력: AFFO payout(forward → 분기), 없으면 FFO payout + 한 단계, 최근 컷이면 한 단계
    a_f, a_q, f_f, cuts = (ev("affo_payout_forward"), ev("affo_payout_quarter", "fallback"),
                           ev("ffo_payout_forward", "fallback"), ev("dividend_cuts", "downgrade"))
    reasons = []
    prim = a_f if a_f["grade"] else a_q if a_q["grade"] else None
    if prim:
        base = prim["grade"]
    elif f_f["grade"]:
        base = f_f["grade"]
        reasons.append("AFFO payout이 없어 FFO payout %s%%를 같은 구간에 넣고 한 단계 내림(FFO는 유지 capex를 빼지 않아 "
                       "payout이 낮게 나온다)" % num(f_f["value"]))
    else:
        base = None
    n_cut = sc["downgrades"]["recent_cut_years"]
    cuts["criterion"] = "기준 해(%s)에서 %d년 안에 배당 컷이 있으면 한 단계 내림(경험칙, assets/scorecard.json)" % (year, n_cut)
    recent = [str(y) for y in (cuts["value"] or []) if year and str(y).isdigit() and int(y) > year - n_cut]
    if recent:
        reasons.append("최근 %d년 안 배당 컷(%s)" % (n_cut, "·".join(recent)))
    elif cuts["value"] is None:
        cuts["note"] = cuts["note"] or "컷 이력이 없어 내림을 판단하지 않음"
    axes.append(axis("dividend_capacity", [a_f, a_q, f_f, cuts], base, reasons))
    # 부채: net debt/EBITDAre와 interest coverage 중 나쁜 쪽, covenant 최소 여유가 watch 미만이면 한 단계
    nd_, ic, cov = ev("net_debt_to_ebitdare"), ev("interest_coverage"), ev("covenant_headroom_min_pct", "downgrade")
    ct = th[sc["downgrades"]["covenant_headroom"]["thresholds"]][sc["downgrades"]["covenant_headroom"]["level"]]
    cov["criterion"] = "%s%% 미만이면 한 단계 내림(thresholds.json %s %s)" % (
        num(ct), sc["downgrades"]["covenant_headroom"]["thresholds"], sc["downgrades"]["covenant_headroom"]["level"])
    reasons = ["covenant 최소 여유 %s%%가 %s%% 미만" % (num(cov["value"]), num(ct))] \
        if is_finite(cov["value"]) and cov["value"] < ct else []
    axes.append(axis("leverage", [nd_, ic, cov], worst_of([nd_, ic]), reasons))
    # 만기·유동성: 최대 만기 해 비중, 24개월 liquidity coverage, 차환 영향/AFFO(없으면 /FFO를 한 단계 보수적으로) 중 가장 나쁜 것
    refi = ev("refi_impact_affo_pct")
    if refi["value"] is None and "refi_impact_ffo_pct" in M:
        refi = ev("refi_impact_ffo_pct")
        if refi["grade"]:
            refi["grade_used"] = worse(refi["grade"])
            refi["note"] = "AFFO가 없어 FFO로 나눈 참고 값이라 한 단계 보수적으로 봄(%s → %s)" % (
                names[refi["grade"]], names[refi["grade_used"]])
    mats = [ev("maturity_max_year_pct"), ev("liquidity_coverage_24m"), refi]
    axes.append(axis("maturity_liquidity", mats, worst_of(mats), []))
    # 임차인 집중: top1·top10 중 나쁜 쪽. WALE·occupancy는 곁에
    ts = [ev("top1_tenant_pct"), ev("top10_tenant_pct")]
    axes.append(axis("tenant_concentration", ts + [ev("wale_years", "side"), ev("occupancy_pct", "side")], worst_of(ts), []))
    # valuation 위치: history percentile과 peer 대비의 위치(−1·0·+1) 합
    vs = [ev("p_ffo_history_pctile"), ev("p_ffo_vs_peer")]
    pos = [{"low": -1, "mid": 0, "high": 1}[e["grade"]] for e in vs if e["grade"]]
    vbase = None if not pos else "low" if sum(pos) < 0 else "high" if sum(pos) > 0 else "mid"
    axes.append(axis("valuation", vs, vbase, [], gnames=vnames))
    return {"schema": SCORECARD_SCHEMA, "axes": axes,
            "note": "경험칙 기준표(assets/scorecard.json)로 매긴 판단 재료다. 매수·매도 권유가 아니다"}


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
    events = read_events(facts_doc, gaps)
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
    price_dated(M, F["price"])
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
                  {k: th[k] for k in ("eps_payout_pct", "fcf_payout_pct", "chowder", "yield_trap")},
                  extra={"depth": "deep", "scorecard": {"schema": SCORECARD_SCHEMA, "axes": [],
                                                        "note": "일반 배당주 경로는 REIT 등급 축(assets/scorecard.json)을 매기지 않음"},
                         "events": events})


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
            gone = [e for v, e in ((pv, p), (cv, c)) if v is None]  # 공시 없음·"갚을 것 없음"(값 없음이 정상)은 세지 않는다
            if any(e.get("status", "missing") == "missing" and not empty_reason(e) for e in gone):
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
                        "checks_failed": (d.get("summary") or {}).get("checks_failed"),
                        "depth": d.get("depth") or "deep", "scorecard": d.get("scorecard")})
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
    srows = []  # 축마다 종목 등급(scorecard가 없는 옛 metrics는 비움)
    for d in docs:
        for a in (d.get("scorecard") or {}).get("axes") or []:
            r = next((x for x in srows if x["axis"] == a["key"]), None)
            if r is None:
                r = {"axis": a["key"], "name": a.get("name"), "grades": {}}
                srows.append(r)
            r["grades"][d.get("ticker")] = {"grade": a.get("grade"), "grade_name": a.get("grade_name")}
    doc = {"schema": COMPARE_SCHEMA, "tickers": tickers, "columns": columns, "warnings": warnings,
           "rows": rows, "flags": flags, "checks": checks, "scorecard_rows": srows,
           "summary": {"tickers": len(docs), "warnings": len(warnings), "incomplete": incomplete,
                       "median_note": "median은 이 표에 놓인 종목끼리의 값이다(REITWatch peer median과 다르다)"}}
    return doc, (2 if warnings or incomplete else 0)


# ---------------------------------------------------------------- peers (경쟁 종목 고르기, B7)

PEERS_SCHEMA = "reit-peers/1"
PEER_COUNT = 2


def latest_period(root, ticker):
    """그 종목의 가장 늦은 metrics 분기, 없으면 facts 분기(파일 이름 YYYYQn). 없으면 None."""
    for kind in ("metrics", "facts"):
        ps = sorted(os.path.basename(p)[:-5] for p in glob.glob(os.path.join(root, "tickers", ticker, kind, "*.json"))
                    if len(os.path.basename(p)) == 11)
        if ps:
            return ps[-1]
    return None


def peer_metrics(root, ticker, period):
    """같은 분기 metrics가 있으면 (경로, depth), 없으면 (None, None). depth가 없는 옛 metrics는 deep이다."""
    p = os.path.join(root, "tickers", ticker, "metrics", "%s.json" % period)
    try:
        d = read_json(p)
    except (OSError, ValueError):
        return None, None
    if d.get("schema") != METRICS_SCHEMA or d.get("period") != period:
        return None, None
    return p, d.get("depth") or "deep"


def find_peers(ticker, with_=None, period=None, root=None, aliases=()):
    """같은 subsector에서 REITWatch 시총(mkt_cap)이 가장 가까운(log 비율) 2개, 또는 with_로 정한 종목.
    종목마다 같은 분기 metrics가 있으면 재사용(reuse), 없으면 light로 만들라고 낸다(D6). (문서, exit)."""
    root = root or data_dir()
    rw_dir = os.path.join(root, "reitwatch", "parsed")
    period = period or latest_period(root, ticker)
    doc = {"schema": PEERS_SCHEMA, "ticker": ticker, "period": period, "subsector": None, "market_cap_usd_millions": None,
           "reitwatch_month": None, "rule": "", "peers": [], "reusable_others": [], "note": "",
           "next": "action이 make_light인 종목은 edgar.py xbrl <종목> --period <분기>로 facts를 만들고 depth: \"light\"와 "
                   "핵심 칸(references/facts-schema.md '가볍게 보기')만 채워 reit_calc.py metrics를 돌린다. reuse면 그 metrics를 쓴다"}
    months = read_reitwatch_months(rw_dir)
    if not months:
        doc["note"] = ("REITWatch parsed 없음(%s): reit-sector-scan skill로 fetch를 먼저 돌린다. "
                       "그 전에는 --with로 경쟁 종목을 정해도 subsector·시총을 확인하지 못한다" % rw_dir)
        return doc, 2
    if not period:
        doc["note"] = "%s의 metrics·facts가 없어 분기를 모름: --period로 정한다" % ticker
        return doc, 2
    month, path, latest = months[-1]
    names = [ticker] + [a for a in aliases if a != ticker]
    me = rw_row(latest, names)
    doc.update(reitwatch_month=month, reitwatch_file=path)
    if me is None:
        doc["note"] = "REITWatch %s에 %s row가 없음: ticker(바뀐 이름은 assets/ticker_aliases.json)를 확인하거나 --with로 정한다" % (
            month, ticker)
        return doc, 2
    sub, cap = me.get("subsector"), me.get("mkt_cap")
    doc.update(subsector=sub, market_cap_usd_millions=cap)
    rows = {r.get("ticker"): r for r in latest["rows"]}
    same = [r for r in latest["rows"] if r.get("subsector") == sub and r.get("ticker") not in names]

    def dist(r):
        c = r.get("mkt_cap")
        return abs(math.log(c / cap)) if is_finite(c) and c > 0 and is_finite(cap) and cap > 0 else float("inf")
    if with_:
        chosen = [rows.get(t) or {"ticker": t} for t in with_]
        doc["rule"] = "--with로 정한 종목"
    else:
        chosen = sorted(same, key=lambda r: (dist(r), r.get("ticker")))[:PEER_COUNT]
        doc["rule"] = "같은 subsector(%s)에서 REITWatch %s 시총이 가장 가까운 %d개(log 비율 거리)" % (sub, month, PEER_COUNT)
    warn = []
    for r in chosen:
        t = r.get("ticker")
        mp, depth = peer_metrics(root, t, period)
        c = r.get("mkt_cap")
        item = {"ticker": t, "subsector": r.get("subsector"), "market_cap_usd_millions": c,
                "cap_ratio": round(c / cap, 4) if is_finite(c) and is_finite(cap) and cap > 0 else None,
                "metrics_path": mp, "depth": depth, "reuse": mp is not None, "action": "reuse" if mp else "make_light",
                "facts_path": os.path.join(root, "tickers", t, "facts", "%s.json" % period), "note": ""}
        if t not in rows:
            item["note"] = "REITWatch %s에 row가 없음" % month
            warn.append(t)
        elif r.get("subsector") != sub:
            item["note"] = "subsector %s가 %s와 다름: P/FFO·cap rate를 같은 기준으로 견주지 않는다" % (r.get("subsector"), sub)
            warn.append(t)
        doc["peers"].append(item)
    picked = {x["ticker"] for x in doc["peers"]}
    for r in sorted(same, key=lambda r: (dist(r), r.get("ticker"))):
        mp, depth = peer_metrics(root, r.get("ticker"), period)
        if mp and r.get("ticker") not in picked:
            doc["reusable_others"].append({"ticker": r.get("ticker"), "metrics_path": mp, "depth": depth,
                                           "market_cap_usd_millions": r.get("mkt_cap")})
    if warn:
        doc["note"] = "확인할 종목: %s" % ", ".join(warn)
    if len(doc["peers"]) < PEER_COUNT and not with_:
        doc["note"] = "; ".join(x for x in (doc["note"], "같은 subsector 종목이 %d개뿐" % len(doc["peers"])) if x)
        return doc, 2
    return doc, 2 if warn else 0


def cmd_peers(args):
    with_ = [t.strip() for t in args.with_.split(",") if t.strip()] if args.with_ else None
    doc, code = find_peers(args.ticker, with_=with_, period=args.period,
                           aliases=load_ticker_aliases().get(args.ticker, []))
    if doc.get("note"):
        log(doc["note"])
    for x in doc["peers"]:
        log("peer %s: %s%s" % (x["ticker"], x["action"], " (%s)" % x["depth"] if x["depth"] else ""))
    emit(doc, args.out)
    return code


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
    dbaa, _ = load_macro(args.macro_snapshot, "DBAA")
    hy, _ = load_macro(args.macro_snapshot, "BAMLH0A0HYM2")
    bb, _ = load_macro(args.macro_snapshot, "BAMLH0A1HYBB")
    aliases = load_ticker_aliases().get(facts["ticker"], []) + [
        t for t in (facts.get("former_tickers") or []) if isinstance(t, str)]
    rw = load_reitwatch(args.reitwatch_dir, facts["ticker"], aliases=aliases)
    doc, code = compute(facts, th, macro=macro, rw=rw, baa=baa, tracker=load_tracker(args.tracker_dir), dbaa=dbaa,
                        hy=hy, bb=bb)
    doc["facts_file"] = os.path.abspath(args.facts)
    path = args.out or metrics_path(doc)
    write_json(doc, path)
    for c in doc["checks"]:
        log("%s %s: %s" % (("가볍게 봄" if c.get("not_assessed") else "공시 없음") if c["pass"] is None
                           else "PASS" if c["pass"] else "FAIL", c["check"],
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
    m.add_argument("--tracker-dir", help="기본: $INVEST_DATA_DIR/tracker/parsed")
    d = sub.add_parser("diff", help="같은 종목 두 분기 metrics의 차이(earnings mode)")
    d.add_argument("--prev", required=True)
    d.add_argument("--curr", required=True)
    d.add_argument("--out", help="없으면 stdout")
    pe = sub.add_parser("peers", help="같은 subsector에서 시총이 가장 가까운 경쟁 종목 2개와 metrics 재사용 여부")
    pe.add_argument("ticker")
    pe.add_argument("--with", dest="with_", help="경쟁 종목을 직접 정함(A,B)")
    pe.add_argument("--period", help="기본: 그 종목의 가장 늦은 metrics(없으면 facts) 분기")
    pe.add_argument("--out", help="없으면 stdout")
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
    if args.cmd == "peers":
        return cmd_peers(args)
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
    trk = load_tracker(os.path.join(fx, "tracker"))
    tmap = {"Free Standing (가상)": {"name": "Free Standing", "parent": "Retail"}}
    doc, code = compute(copy.deepcopy(base), th_rw, macro=macro, rw=rw, baa=baa, tracker=trk, tmap=tmap)
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
    again, _ = compute(copy.deepcopy(base), th_rw, macro=macro, rw=rw, baa=baa, tracker=trk, tmap=tmap)
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

    # 23. valuation 기준선(economic implied cap rate, 부채 시가, Tracker·회사 cap rate)과 consensus revision (bc 손계산)
    ev = exp["valuation"]
    ok(th.get("tracker_max_age_days") == 150, "thresholds.json Tracker 나이 150일")
    for name, want in ev["values"].items():
        got = M.get(name, {})
        ok(near(got.get("value"), want["value"]) and got.get("status") == want["status"],
           "valuation %s = %s %s (got %s %s; %s)" % (name, want["value"], want["status"], got.get("value"),
                                                    got.get("status"), want["why"]))
    ok(near(M["implied_cap_rate"]["value"], exp["values"]["implied_cap_rate"]["value"])
       and M["implied_cap_rate"]["formula"] == "연환산 NOI / (시총 + net debt + 우선주) × 100", "명목 implied cap rate 줄은 그대로")
    ok(M.get("tracker_subsector_cap_rate", {}).get("locator", "").endswith("DD423 (Free Standing)")
       and doc["inputs"].get("tracker_file", "").endswith("2026Q3.json"), "Tracker 셀 위치를 locator에, 읽은 파일을 inputs에")
    ok([f["flag"] for f in doc["flags"]] == ["affo_payout", "maturity_concentration", "net_debt_to_ebitdare",
                                             "interest_coverage", "covenant_headroom"], "cap rate·revision 줄에는 flag가 없음")

    def vcompute(d, **kw):
        args = dict(macro=macro, rw=rw, baa=baa, tracker=trk, tmap=tmap)
        args.update(kw)
        return compute(d, th_rw, **args)
    pb = copy.deepcopy(base)
    pb["facts"]["jv_debt_pro_rata"]["parts"][1]["locator"] = ""
    d40, c40 = vcompute(pb)
    ok(c40 == 2 and d40["metrics"]["economic_implied_cap_rate"]["value"] is None
       and d40["metrics"]["implied_cap_rate"]["value"] is not None
       and any(g["item"] == "jv_debt_pro_rata" and g["status"] == "missing" for g in d40["data_gaps"]),
       "parts 한 줄에 locator가 없으면 그 칸 missing·데이터 구멍, economic cap rate missing, 명목은 그대로")
    pu = copy.deepcopy(base)
    pu["facts"]["debt_fair_value"]["parts"][0]["unit"] = "%"
    d41, _ = vcompute(pu)
    ok(d41["metrics"]["debt_mtm_adjustment"]["value"] is None
       and d41["metrics"]["economic_implied_cap_rate_debt_mtm"]["value"] is None
       and near(d41["metrics"]["economic_implied_cap_rate"]["value"], ev["values"]["economic_implied_cap_rate"]["value"]),
       "parts 한 줄의 unit이 틀리면 부채 시가 줄만 missing, economic cap rate는 그대로")
    pm = copy.deepcopy(base)
    pm["facts"]["debt_fair_value"]["parts"][2] = {"value": None, "status": "missing", "note": "못 찾음(가상)"}
    d42, _ = vcompute(pm)
    ok(d42["metrics"]["debt_fair_value"]["value"] is None, "parts 한 줄이 missing이면 합도 missing(0으로 채우지 않음)")
    cr = copy.deepcopy(base)
    cr["facts"]["capex_reserve"] = {"value": 300000, "unit": "USD thousands", "status": "reported", "as_of": "2026-09-30",
                                    "source": "(가상)", "locator": "capex reserve (가상)"}
    d43, _ = vcompute(cr)
    w = ev["capex_reserve_reported"]
    ok(d43["metrics"]["capex_reserve"]["value"] == w["value"] and d43["metrics"]["capex_reserve"]["status"] == "reported"
       and near(d43["metrics"]["noi_economic"]["value"], w["noi_economic"])
       and near(d43["metrics"]["economic_implied_cap_rate"]["value"], w["economic_implied_cap_rate"])
       and d43["metrics"]["economic_implied_cap_rate"]["status"] == "derived", w["why"])
    nc = copy.deepcopy(base)
    for it in nc["affo_bridge"]["items"]:
        if it.get("category") in ("recurring_capex", "leasing_cost"):
            it["category"] = "other"
    d44, _ = vcompute(nc)
    ok(d44["metrics"]["capex_reserve"]["value"] is None and d44["metrics"]["economic_implied_cap_rate"]["value"] is None,
       "capex reserve도 bridge의 recurring capex·leasing cost 줄도 없으면 economic cap rate missing(0으로 두지 않음)")
    neg = copy.deepcopy(base)
    neg["facts"]["construction_in_progress"] = dict(neg["facts"]["construction_in_progress"], value=90000000)
    d45, _ = vcompute(neg)
    ok(d45["metrics"]["economic_implied_cap_rate"]["value"] is None and "0 이하" in d45["metrics"]["economic_implied_cap_rate"]["note"],
       "economic EV가 0 이하면 cap rate를 내지 않음")
    par = copy.deepcopy(trk)
    par["doc"] = copy.deepcopy(trk["doc"])
    next(r for r in par["doc"]["rows"] if r["name"] == "Free Standing")["implied_cap_rate"] = None
    d46, _ = vcompute(copy.deepcopy(base), tracker=par)
    t46 = d46["metrics"]["tracker_subsector_cap_rate"]
    ok(t46["value"] == ev["parent_cap_rate"] and t46["status"] == "estimated" and "Retail" in t46["note"],
       "subsector row에 값이 없으면 parent(Retail) 6.4를 estimated로")
    old_t = copy.deepcopy(trk)
    old_t["doc"] = dict(trk["doc"], quarter="2026Q1", as_of="2026-03-31")
    t47 = vcompute(copy.deepcopy(base), tracker=old_t)[0]["metrics"]["tracker_subsector_cap_rate"]
    ok(t47["status"] == "stale" and "231일" in t47["note"], "Tracker 분기 끝날이 가격 기준일보다 150일 넘게 앞서면 stale(231일)")
    d48, c48 = vcompute(copy.deepcopy(base), tracker=load_tracker(os.path.join(fx, "nope")))
    M48 = d48["metrics"]
    ok(c48 == 2 and M48["tracker_subsector_cap_rate"]["value"] is None and M48["implied_cap_rate_vs_tracker"]["value"] is None
       and "Tracker parsed" in M48["tracker_subsector_cap_rate"]["note"]
       and M48["economic_implied_cap_rate"]["value"] is not None and M48["acquisitions_cap_rate"]["value"] is not None,
       "Tracker가 없으면 Tracker 비교 줄만 missing, exit 2")
    d49, _ = vcompute(copy.deepcopy(base), tmap={})
    ok(d49["metrics"]["tracker_subsector_cap_rate"]["value"] is None
       and "tracker_map" in d49["metrics"]["tracker_subsector_cap_rate"]["note"], "대응표에 없는 subsector는 missing")
    with tempfile.TemporaryDirectory() as tmp:
        for m, fy, v1, v2 in (("2026-01", (2025, 2026), 4.10, 4.20), ("2026-04", (2026, 2027), 4.25, 4.40)):
            with open(os.path.join(tmp, m + ".json"), "w", encoding="utf-8") as f:
                json.dump({"schema": REITWATCH_SCHEMA, "month": m, "as_of": m + "-28", "fy1": fy[0], "fy2": fy[1],
                           "rows": [{"ticker": "O", "subsector": "Free Standing (가상)", "ffo_fy1": v1, "ffo_fy2": v2,
                                     "p_ffo_fy1": 12.0}]}, f)
        rw2 = load_reitwatch(tmp, "O")
        r50 = vcompute(copy.deepcopy(base), rw=rw2)[0]["metrics"]["ffo_fy1_rev_3m"]
        ok(near(r50["value"], ev["fy_switch_rev"]) and "ffo_fy2" in r50["note"],
           "FY 교체 뒤면 앞 호의 FY2(같은 회계연도)와 견줌: 4.25 / 4.20 − 1 = 1.1905")
        os.remove(os.path.join(tmp, "2026-01.json"))
        r51 = vcompute(copy.deepcopy(base), rw=load_reitwatch(tmp, "O"))[0]["metrics"]["ffo_fy1_rev_3m"]
        ok(r51["value"] is None and r51["status"] == "missing" and "2026-01" in r51["note"], "3개월 앞 호가 없으면 missing")
    ok("AFFO bridge" in M["capex_reserve"]["note"] and "가정" in M["capex_reserve"]["note"],
       "capex reserve를 AFFO bridge로 대신하면 그 가정을 note에 적음")
    zero = dict(rw, rev_old=(rw["rev_old"][0], copy.deepcopy(rw["rev_old"][1])))
    next(r for r in zero["rev_old"][1]["rows"] if r["ticker"] == "O")["ffo_fy1"] = 0
    r53 = vcompute(copy.deepcopy(base), rw=zero)[0]["metrics"]["ffo_fy1_rev_3m"]
    ok(r53["value"] is None and "0 이하" in r53["note"], "앞 호 FFO가 0이면 변화를 내지 않음")
    with tempfile.TemporaryDirectory() as tmp:
        for q, v in (("2026Q2", 9.9), ("2026Q3", 6.5)):
            with open(os.path.join(tmp, q + ".json"), "w", encoding="utf-8") as f:
                json.dump(dict(trk["doc"], quarter=q, rows=[{"name": "Free Standing", "implied_cap_rate": v}]), f)
        lt = load_tracker(tmp)
        ok(lt["path"].endswith("2026Q3.json") and lt["doc"]["rows"][0]["implied_cap_rate"] == 6.5,
           "Tracker parsed가 여럿이면 파일 이름이 가장 늦은 분기를 읽음")
    r52 = vcompute(copy.deepcopy(base), rw=dict(rw, as_of="2024-09-30"))[0]["metrics"]["ffo_fy1_rev_3m"]
    ok(r52["status"] == "stale" and "허용 %d일" % th["reitwatch_max_age_days"] in r52["note"],
       "REITWatch가 오래되면 revision은 stale과 note에 나이")
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "f.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(base, f)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            main(["metrics", "--facts", p, "--out", os.path.join(tmp, "m.json"), "--tracker-dir", os.path.join(fx, "tracker"),
                  "--reitwatch-dir", os.path.join(fx, "reitwatch"), "--macro-snapshot", os.path.join(fx, "macro_snapshot.json")])
        cli = read_json(os.path.join(tmp, "m.json"))
        ok(cli["inputs"].get("tracker_file", "").endswith(os.path.join("tracker", "2026Q3.json")),
           "metrics --tracker-dir로 Tracker 파일을 고름")

    # 24. 2차 검증 결함: 값이 이상해도 멈추지 않고 그 줄만 missing, JSON에 NaN을 쓰지 않음
    def cli_metrics(facts, extra=()):
        """facts를 파일로 써서(NaN 토큰 허용) metrics CLI로 돌리고 실제로 쓴 JSON을 되읽는다. (exit, 문서 또는 None)."""
        with tempfile.TemporaryDirectory() as tmp:
            p, out = os.path.join(tmp, "f.json"), os.path.join(tmp, "m.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(facts, f)
            args = ["metrics", "--facts", p, "--out", out, "--tracker-dir", os.path.join(fx, "tracker"),
                    "--reitwatch-dir", os.path.join(fx, "reitwatch"), "--macro-snapshot", os.path.join(fx, "macro_snapshot.json")]
            args += list(extra)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                try:
                    code = main(args)
                except Exception:  # noqa: BLE001 — 멈추면 그 자체가 실패다
                    code = "exception"
            return code, (read_json(out) if os.path.exists(out) else None)

    cv = copy.deepcopy(base)
    cv["series"]["covenants"][0]["actual"] = float("nan")
    cv["series"]["covenants"][1]["limit"] = float("inf")
    cv["series"]["covenants"][2] = dict(cv["series"]["covenants"][2], status="missing", actual=float("nan"))
    c60, m60 = cli_metrics(cv)
    rows60 = (m60 or {}).get("metrics", {}).get("covenant_headroom", {}).get("value") or []
    ok(c60 == 2 and m60 is not None and m60["metrics"]["covenant_headroom_min_pct"]["value"] is None
       and rows60 and rows60[0]["actual"] is None and rows60[1]["limit"] is None and rows60[2]["actual"] is None
       and rows60[0]["headroom_pct"] is None and any(g["item"] == "covenants[0]" for g in m60["data_gaps"]),
       "F1 covenant actual NaN·limit Infinity면 그 줄 missing, 출력에는 null, metrics 파일을 씀, exit 2 (got %s)" % c60)

    nps2 = copy.deepcopy(base)
    for k in ("affo_per_share_diluted", "affo_guidance_low", "affo_guidance_high"):
        nps2["facts"][k] = {"value": None, "status": "missing", "note": "가상"}
    na4 = copy.deepcopy(nps2)
    na4["facts"]["affo_reported"]["value"] = -50000
    na4["affo_bridge"]["items"] = []
    d61, _ = compute(na4, th_rw, macro=macro, rw=rw, baa=baa)
    f61 = next(f for f in d61["flags"] if f["flag"] == "affo_payout")
    ok(d61["metrics"]["affo_payout_forward"]["value"] is None and d61["metrics"]["affo_payout_quarter"]["value"] is None
       and "0 이하" in d61["metrics"]["affo_payout_forward"]["note"] and f61["level"] == "review",
       "F4 AFFO 총액이 음수면 AFFO payout을 내지 않고 flag review (got %s %s)" % (
           d61["metrics"]["affo_payout_forward"]["value"], f61["level"]))
    ns4 = copy.deepcopy(nps2)
    ns4["facts"]["diluted_shares_weighted"]["value"] = -948797
    d62, _ = compute(ns4, th_rw, macro=macro, rw=rw, baa=baa)
    ok(d62["metrics"]["affo_per_share_diluted"]["value"] is None and d62["metrics"]["affo_payout_forward"]["value"] is None
       and next(f for f in d62["flags"] if f["flag"] == "affo_payout")["level"] == "review",
       "F4 희석 주식 수가 0 이하면 주당 AFFO·payout missing, flag review")
    np4 = copy.deepcopy(nps2)
    np4["facts"]["affo_per_share_diluted"] = dict(base["facts"]["affo_per_share_diluted"], value=-0.1)
    np4["facts"]["ffo_guidance_low"] = dict(base["facts"]["affo_guidance_low"], value=-1.0)
    np4["facts"]["ffo_guidance_high"] = dict(base["facts"]["affo_guidance_high"], value=-1.0)
    d63, _ = compute(np4, th_rw, macro=macro, rw=rw, baa=baa)
    ok(d63["metrics"]["affo_payout_quarter"]["value"] is None and d63["metrics"]["affo_payout_forward"]["value"] is None
       and d63["metrics"]["ffo_payout_forward"]["value"] is None and "0 이하" in d63["metrics"]["ffo_payout_forward"]["note"],
       "F4 회사 주당 AFFO·연간 FFO가 0 이하면 AFFO payout·FFO payout(참고) missing")

    su = copy.deepcopy(nps2)
    su["facts"]["diluted_shares_weighted"]["unit"] = "shares"
    d64, _ = compute(su, th_rw, macro=macro, rw=rw, baa=baa)
    ok(d64["metrics"]["affo_per_share_diluted"]["value"] is None
       and any(g["item"] == "diluted_shares_weighted" and "단위" in g["reason"] for g in d64["data_gaps"])
       and next(f for f in d64["flags"] if f["flag"] == "affo_payout")["level"] == "review",
       "F5 희석 주식 수가 기말 보통주와 10배 넘게 다르면(948.797천 vs 950,964천) 단위 의심 missing, 데이터 구멍")

    sg = copy.deepcopy(base)
    for it in sg["affo_bridge"]["items"]:
        if it.get("category") in ("recurring_capex", "leasing_cost"):
            it["value"] = abs(it["value"])
    d65, _ = vcompute(sg)
    M65 = d65["metrics"]
    ok(M65["recurring_capex_to_noi_pct"]["value"] is None and M65["capex_reserve"]["value"] is None
       and M65["economic_implied_cap_rate"]["value"] is None
       and any(g["item"] == "affo_bridge.items[2]" and "0 이하" in g["reason"] for g in d65["data_gaps"]),
       "F6 recurring capex·leasing cost 줄이 양수(부호 뒤집힘)면 capex 지표·capex reserve missing, 데이터 구멍 (got %s)" % (
           M65["capex_reserve"]["value"],))
    ncr = copy.deepcopy(base)
    ncr["facts"]["capex_reserve"] = dict(cr["facts"]["capex_reserve"], value=-300000)
    d66, _ = vcompute(ncr)
    ok(d66["metrics"]["capex_reserve"]["value"] is None and d66["metrics"]["noi_economic"]["value"] is None
       and any(g["item"] == "capex_reserve" and "0 이상" in g["reason"] for g in d66["data_gaps"]),
       "F6 회사 capex reserve가 음수면 missing(AFFO bridge로 대신하지 않음), 데이터 구멍")

    ty = copy.deepcopy(base)
    for it in ty["affo_bridge"]["items"]:
        if it.get("category") == "leasing_cost":
            it["category"] = "Leasing_Cost"
    d67, c67 = vcompute(ty)
    M67 = d67["metrics"]
    ok(c67 == 2 and all(M67[k]["value"] is None and "모르는 category" in M67[k]["note"]
                        for k in ("recurring_capex_to_noi_pct", "straight_line_to_ffo_pct", "capex_reserve")),
       "F7 모르는 category가 하나라도 있으면 category 지표·capex reserve 대리값 missing(0.0019로 내지 않음), exit 2")

    dl = copy.deepcopy(base)
    dl["series"]["debt_maturities"][0]["period"] = "2026-12-31"
    d68, _ = vcompute(dl)
    M68 = d68["metrics"]
    ok(all(M68[k]["value"] is None for k in ("maturities_24m", "maturity_max_year_pct", "liquidity_coverage_24m"))
       and any(g["item"] == "debt_maturities[0]" and "period" in g["reason"] for g in d68["data_gaps"]),
       "F8 만기 사다리 period가 연도·thereafter가 아니면 24개월 만기·최대 해 비중 missing, 데이터 구멍")

    bt = dict(trk, doc=dict(trk["doc"], as_of="2026-Q3"))
    try:
        d69, c69 = vcompute(copy.deepcopy(base), tracker=bt)
        t69 = d69["metrics"]["tracker_subsector_cap_rate"]
        f9 = c69 == 2 and t69["value"] is None and "날짜" in t69["note"] \
            and d69["metrics"]["economic_implied_cap_rate"]["value"] is not None
    except (ValueError, TypeError):
        f9 = False
    ok(f9, "F9 Tracker parsed as_of가 날짜가 아니면 Tracker 비교 줄만 missing, 계속")

    pv = copy.deepcopy(base)
    pv["facts"]["jv_debt_pro_rata"].update(value=1, status="reported", source="x (가상)", locator="y (가상)")
    d70, _ = vcompute(pv)
    ok(d70["metrics"]["jv_debt_pro_rata"]["value"] is None and d70["metrics"]["economic_ev"]["value"] is None
       and any(g["item"] == "jv_debt_pro_rata" and "parts" in g["reason"] for g in d70["data_gaps"]),
       "F10 parts와 value를 함께 적으면 missing, 데이터 구멍(value로 대신하지 않음)")

    snap = read_json(os.path.join(fx, "macro_snapshot.json"))
    with tempfile.TemporaryDirectory() as tmp:
        for sid in ("BAA10Y", "DGS10"):
            bad_s = copy.deepcopy(snap)
            bad_s["series"][sid]["value"] = float("nan")
            sp = os.path.join(tmp, sid + ".json")
            with open(sp, "w", encoding="utf-8") as f:
                json.dump(bad_s, f)
            env71, _ = load_macro(sp, sid)
            c71, m71 = cli_metrics(base, ["--macro-snapshot", sp])
            ok(env71["value"] is None and env71["status"] == "missing" and c71 == 2 and m71 is not None
               and m71["metrics"]["market_borrowing_rate_est"]["value"] is None,
               "macro snapshot %s가 NaN이면 그 값 missing, metrics는 계속(exit 2, got %s)" % (sid, c71))

    dn = copy.deepcopy(base)
    dn["facts"]["dev_remaining_spend"]["value"] = -5
    d72, _ = vcompute(dn)
    ok(d72["metrics"]["liquidity_uses_24m"]["value"] is None
       and any(g["item"] == "dev_remaining_spend" and "0 이상" in g["reason"] for g in d72["data_gaps"]),
       "남은 개발비가 음수면 거절(missing, 데이터 구멍)")

    uz = copy.deepcopy(base)
    uz["series"]["debt_maturities"] = [e for e in uz["series"]["debt_maturities"] if e["period"] in ("2030", "thereafter")]
    uz["facts"]["dev_remaining_spend"]["value"] = 0
    d73, c73 = vcompute(uz)
    l73 = d73["metrics"]["liquidity_coverage_24m"]
    ok(l73["value"] is None and l73["status"] != "missing" and "갚을 것 없음" in l73["note"]
       and not any(g["item"] == "liquidity_coverage_24m" for g in d73["data_gaps"]) and c73 == 0,
       "24개월 안 만기와 남은 개발비가 0이면 coverage는 값 없이 note '갚을 것 없음', 데이터 구멍이 아님, exit 0 (got %s %s)" % (
           l73["status"], c73))

    ok(sum(1 for g in d40["data_gaps"] if g["item"] == "jv_debt_pro_rata") == 1,
       "parts 오류는 데이터 구멍에 한 번만")

    bs = copy.deepcopy(base)
    bs["facts"]["debt_principal_total"]["basis"] = "pro_rata"
    for k in ("total_assets", "accumulated_depreciation"):
        bs["facts"][k]["basis"] = "consolidated"
    d74, _ = vcompute(bs)
    g74 = d74["metrics"]["debt_pref_to_gross_assets"]
    ok(near(g74["value"], 36.4684) and g74["status"] == "estimated" and "기준" in g74["note"],
       "debt 원금(pro_rata)과 gross assets(consolidated) 기준이 다르면 값은 그대로 estimated와 note (got %s)" % g74["status"])
    bs["facts"]["debt_principal_total"]["basis"] = "consolidated"
    ok(vcompute(bs)[0]["metrics"]["debt_pref_to_gross_assets"]["status"] == "derived", "같은 기준이면 derived")
    bs["facts"]["debt_principal_total"]["basis"] = "pro-rata"
    d75, _ = vcompute(bs)
    ok(d75["metrics"]["debt_pref_to_gross_assets"]["value"] is None
       and any(g["item"] == "debt_principal_total" and "basis" in g["reason"] for g in d75["data_gaps"]),
       "basis가 consolidated·pro_rata가 아니면 그 칸 missing, 데이터 구멍")
    bm = copy.deepcopy(cr)
    bm["facts"]["noi_annualized"]["basis"] = "pro_rata"
    for k in ("debt_fair_value", "debt_carrying_value"):
        bm["facts"][k]["basis"] = "consolidated"
    d76, _ = vcompute(bm)
    ok(d76["metrics"]["economic_implied_cap_rate"]["status"] == "derived"
       and near(d76["metrics"]["economic_implied_cap_rate_debt_mtm"]["value"], ev["debt_mtm_basis_mix"])
       and d76["metrics"]["economic_implied_cap_rate_debt_mtm"]["status"] == "estimated"
       and "기준" in d76["metrics"]["economic_implied_cap_rate_debt_mtm"]["note"],
       "NOI(pro_rata)와 부채 시가 조정(consolidated)이 섞이면 부채 시가 반영 cap rate만 estimated(5,461,304 / 77,871,795)")

    def nd(note):
        return {"value": None, "status": "missing", "not_disclosed": True, "note": note + " (가상)",
                "source": "sec:8-K (가상)", "locator": "debt summary 표에 그 줄이 없음 (가상)"}
    nd1 = copy.deepcopy(base)
    nd1["facts"]["principal_amortization"] = nd("회사가 정기 원금 상환을 내지 않음")
    d77, c77 = vcompute(nd1)
    f77 = d77["metrics"]["fixed_charge_coverage"]
    ok(f77["value"] is None and f77.get("not_disclosed") is True and c77 == 0
       and d77["summary"]["metrics_missing"] == 0 and d77["summary"].get("metrics_not_disclosed") == 1
       and any(g["item"] == "fixed_charge_coverage" and g["status"] == "not_disclosed" and "공시 없음" in g["reason"]
               for g in d77["data_gaps"]),
       "not_disclosed(근거 source·locator)인 칸 때문에 빠진 지표는 missing이지만 exit에 세지 않고 데이터 구멍에 '공시 없음' (got %s)" % c77)
    nd2 = copy.deepcopy(nd1)
    nd2["facts"]["principal_amortization"]["locator"] = ""
    d78, c78 = vcompute(nd2)
    ok(c78 == 2 and d78["summary"]["metrics_missing"] == 1
       and any(g["item"] == "principal_amortization" and "locator" in g["reason"] for g in d78["data_gaps"]),
       "not_disclosed에 근거(locator)가 없으면 보통 missing으로 셈, exit 2")
    nd3 = copy.deepcopy(nd1)
    nd3["facts"]["capitalized_interest"] = {"value": None, "status": "missing", "note": "못 찾음(가상)"}
    ok(vcompute(nd3)[1] == 2, "공시 없음과 못 찾음이 섞인 지표는 셈(exit 2)")
    nd4 = copy.deepcopy(base)
    nd4["facts"]["unencumbered_assets"] = nd("자산 금액을 내지 않음")
    nd4["facts"]["unencumbered_noi"] = nd("NOI를 내지 않음")
    ok(vcompute(nd4)[0]["metrics"]["unencumbered_pct"].get("not_disclosed") is True,
       "후보(자산·NOI)가 모두 공시 없음이면 unencumbered 비율도 공시 없음")
    nd5 = copy.deepcopy(base)
    nd5["facts"]["affo_reported"] = nd("AFFO를 내지 않음")
    nd5["affo_bridge"] = {"start": None, "items": []}
    nd5["facts"]["capex_reserve"] = nd("capex reserve를 내지 않음")
    d79, c79 = vcompute(nd5)
    ab = next(c for c in d79["checks"] if c["check"] == "affo_bridge")
    ok(ab["pass"] is None and c79 == 0 and d79["summary"]["checks_failed"] == 0
       and any(g["item"] == "check:affo_bridge" and g["status"] == "not_disclosed" for g in d79["data_gaps"]),
       "회사가 AFFO를 내지 않아 bridge가 없으면 affo_bridge check는 pass null(공시 없음), exit 0 (got %s %s)" % (ab["pass"], c79))
    nd6 = copy.deepcopy(base)
    nd6["facts"]["principal_amortization"] = dict(nd("x"), value=1000, unit="USD thousands")
    d80, _ = vcompute(nd6)
    ok(d80["metrics"]["fixed_charge_coverage"]["value"] is None
       and any(g["item"] == "principal_amortization" and "not_disclosed" in g["reason"] for g in d80["data_gaps"]),
       "값과 not_disclosed를 함께 적으면 missing, 데이터 구멍")
    pr77 = dict(d77, period="2026Q2")
    ok(diff_metrics(pr77, d77)[1] == 0, "diff: 두 분기 모두 공시 없음인 지표는 셈하지 않음(exit 0)")
    cm77 = compare_metrics([d77, dict(d77, ticker="O2")])
    ok(cm77[0]["summary"]["incomplete"] == [], "compare: 공시 없음만 있는 종목은 incomplete가 아님")

    db = dict(baa, value=6.3, source="fred:DBAA", locator="fixture series.DBAA (가상)")
    d81, _ = vcompute(copy.deepcopy(base), dbaa=db)
    mb = d81["metrics"]["market_borrowing_rate_est"]
    ok(near(mb["value"], 6.3) and mb["status"] == "estimated" and "DBAA" in mb["formula"],
       "macro snapshot에 DBAA가 있으면 시장 차입금리 추정은 DBAA(reit-sector-scan과 같은 차례)")
    with tempfile.TemporaryDirectory() as tmp:
        sd = copy.deepcopy(snap)
        sd["series"]["DBAA"] = dict(sd["series"]["BAA10Y"], value=6.3, source="fred:DBAA")
        sp = os.path.join(tmp, "s.json")
        with open(sp, "w", encoding="utf-8") as f:
            json.dump(sd, f)
        _, m82 = cli_metrics(base, ["--macro-snapshot", sp])
        ok(m82 is not None and near(m82["metrics"]["market_borrowing_rate_est"]["value"], 6.3),
           "metrics CLI가 snapshot의 DBAA를 읽음")

    ok(M["ffo_fy1_rev_3m"]["status"] == "derived" and M["ffo_growth_fy2"]["status"] == "derived",
       "consensus revision·FY2 성장은 REITWatch 값의 계산이라 derived(reit-sector-scan과 같다)")

    with tempfile.TemporaryDirectory() as tmp:
        for q, a, v in (("2026Q3", "2026-09-30", 6.5), ("2026Q4", "2026-12-31", 9.9)):
            with open(os.path.join(tmp, q + ".json"), "w", encoding="utf-8") as f:
                json.dump(dict(trk["doc"], quarter=q, as_of=a,
                               rows=[{"name": "Free Standing", "implied_cap_rate": v, "cells": {"implied_cap_rate": "X1"}}]), f)
        d83, _ = vcompute(copy.deepcopy(base), tracker=load_tracker(tmp))
        t83 = d83["metrics"]["tracker_subsector_cap_rate"]
        ok(t83["value"] == 6.5 and "2026Q3.json" in t83["locator"] and "2026Q4" in t83["note"]
           and d83["inputs"]["tracker_file"].endswith("2026Q3.json"),
           "가격 기준일(2026-11-17) 뒤에 끝난 분기(2026Q4) Tracker 파일은 고르지 않음 (got %s)" % t83["value"])
        early = copy.deepcopy(base)
        early["facts"]["price"]["as_of"] = "2026-08-01"
        t84 = vcompute(early, tracker=load_tracker(tmp))[0]["metrics"]["tracker_subsector_cap_rate"]
        ok(t84["value"] is None and "가격 기준일" in t84["note"], "가격 기준일 전에 끝난 분기 파일이 없으면 missing")

    # 25. B10 차환 금리(D12): 회사 최근 12개월 발행 금리 > 신용등급별(투자등급 Baa, BB는 BB OAS, 그 아래 HY OAS) > 등급 모름 Baa(추정)
    e1 = exp["s1"]
    er1 = e1["refi"]
    hy = {"value": 3.43, "unit": "%", "status": "reported", "as_of": "2026-11-14", "source": "fred:BAMLH0A0HYM2",
          "locator": "fixture series.BAMLH0A0HYM2 (가상)", "note": "", "snapshot_date": "2026-11-16"}
    bb = dict(hy, value=2.04, source="fred:BAMLH0A1HYBB", locator="fixture series.BAMLH0A1HYBB (가상)")

    def rcompute(d, **kw):
        args = dict(macro=macro, rw=rw, baa=baa, tracker=trk, tmap=tmap, hy=hy, bb=bb)
        args.update(kw)
        return compute(d, th_rw, **args)

    def rating(agency, r):
        return {"agency": agency, "rating": r, "status": "reported", "as_of": "2026-09-30",
                "source": "sec:10-Q (가상)", "locator": "10-Q L1 credit ratings %s %s (가상)" % (agency, r)}

    def rated(*rs, **facts):
        d = copy.deepcopy(base)
        d["series"]["credit_ratings"] = [rating(a, r) for a, r in rs]
        d["facts"].update(facts)
        return d
    d90, c90 = rcompute(copy.deepcopy(base))
    mb = d90["metrics"]["market_borrowing_rate_est"]
    ok(near(mb["value"], er1["baa"]["rate"]) and mb["status"] == "estimated" and mb.get("rate_source") == "unknown_rating"
       and "등급을 몰라" in mb["note"] and near(d90["metrics"]["refi_cost_24m"]["value"], er1["baa"]["cost"]) and c90 == 0,
       "B10 신용등급을 모르면 Baa 6.0(추정)이고 note에 등급 모름 (got %s %s)" % (mb.get("rate_source"), mb["value"]))
    issue = {"value": 5.25, "unit": "%", "status": "reported", "issue_date": "2026-08-12", "as_of": "2026-08-12",
             "source": "sec:8-K (가상)", "locator": "8-K L10 5.25% Senior Notes due 2031 (가상)"}
    d91, _ = rcompute(rated(("S&P", "BBB+"), recent_issuance_rate_pct=issue))
    m91 = d91["metrics"]
    ok(near(m91["market_borrowing_rate_est"]["value"], er1["issuance"]["rate"])
       and m91["market_borrowing_rate_est"]["status"] == "estimated"
       and m91["market_borrowing_rate_est"].get("rate_source") == "issuance"
       and near(m91["refi_cost_24m"]["value"], er1["issuance"]["cost"])
       and near(m91["refi_impact_affo_pct"]["value"], er1["issuance"]["impact"]),
       "B10 최근 12개월 안 회사 발행 금리 5.25%%를 먼저 씀(등급보다 앞섬): refi %s" % m91["refi_cost_24m"]["value"])
    d92, _ = rcompute(rated(("S&P", "BBB+"), recent_issuance_rate_pct=dict(issue, issue_date="2025-06-01")))
    m92 = d92["metrics"]["market_borrowing_rate_est"]
    ok(near(m92["value"], er1["baa"]["rate"]) and m92.get("rate_source") == "rating" and "12개월" in m92["note"]
       and m92.get("rating_band") == "investment_grade",
       "B10 발행일이 12개월보다 오래되면 쓰지 않고 등급(BBB+, 투자등급 → Baa)으로: %s" % m92["note"][:80])
    d93, _ = rcompute(rated(("S&P", "BB")))
    ok(near(d93["metrics"]["market_borrowing_rate_est"]["value"], er1["bb"]["rate"])
       and d93["metrics"]["market_borrowing_rate_est"].get("rating_band") == "bb"
       and near(d93["metrics"]["refi_cost_24m"]["value"], er1["bb"]["cost"]),
       "B10 BB는 DGS10 + BB OAS(4.5 + 2.04 = 6.54)")
    d94, _ = rcompute(rated(("S&P", "BB")), bb=missing("macro snapshot에 BAMLH0A1HYBB 값 없음"))
    m94 = d94["metrics"]["market_borrowing_rate_est"]
    ok(near(m94["value"], er1["hy"]["rate"]) and "BAMLH0A0HYM2" in m94["note"] and "BAMLH0A1HYBB" in m94["note"],
       "B10 snapshot에 BB OAS가 없으면 HY OAS로 대신하고 note에 적음 (got %s)" % m94["value"])
    d95, _ = rcompute(rated(("S&P", "B+")))
    ok(near(d95["metrics"]["market_borrowing_rate_est"]["value"], er1["hy"]["rate"])
       and d95["metrics"]["market_borrowing_rate_est"].get("rating_band") == "below_bb"
       and near(d95["metrics"]["refi_cost_24m"]["value"], er1["hy"]["cost"]),
       "B10 BB 아래(B+)는 DGS10 + HY OAS(4.5 + 3.43 = 7.93)")
    d96, _ = rcompute(rated(("S&P", "BBB-"), ("Moody's", "Ba1")))
    ok(near(d96["metrics"]["market_borrowing_rate_est"]["value"], er1["bb"]["rate"]),
       "B10 기관마다 등급이 다르면 가장 낮은 등급(Moody's Ba1 → BB)")
    d97, c97 = rcompute(rated(("Fitch", "B")), hy=missing("macro snapshot에 BAMLH0A0HYM2 값 없음"))
    ok(d97["metrics"]["market_borrowing_rate_est"]["value"] is None and d97["metrics"]["refi_cost_24m"]["value"] is None
       and c97 == 2, "B10 BB 아래인데 HY OAS가 없으면 Baa로 대신하지 않고 그 칸만 missing")
    d98, _ = rcompute(rated(("S&P", "AAA+")))
    ok(d98["metrics"]["market_borrowing_rate_est"].get("rate_source") == "unknown_rating"
       and any(g["item"] == "credit_ratings[0]" for g in d98["data_gaps"]),
       "B10 읽지 못한 등급 글은 데이터 구멍에 올리고 등급 모름으로")
    hi = copy.deepcopy(base)
    hi["facts"]["weighted_avg_rate_pct"] = dict(hi["facts"]["weighted_avg_rate_pct"], value=7.0)
    r99 = rcompute(hi)[0]["metrics"]["refi_cost_24m"]
    ok(near(r99["value"], er1["negative"]["cost"]) and "낮" in r99["note"],
       "B10 차환 금리 추정이 지금 가중평균 금리보다 낮으면 그 사실을 note에 적음")
    with tempfile.TemporaryDirectory() as tmp:
        sh = copy.deepcopy(snap)
        sh["series"]["BAMLH0A0HYM2"] = dict(sh["series"]["BAA10Y"], value=3.43, source="fred:BAMLH0A0HYM2")
        sp = os.path.join(tmp, "s.json")
        with open(sp, "w", encoding="utf-8") as f:
            json.dump(sh, f)
        _, m100 = cli_metrics(rated(("S&P", "B")), ["--macro-snapshot", sp])
        ok(m100 is not None and near(m100["metrics"]["market_borrowing_rate_est"]["value"], er1["hy"]["rate"]),
           "B10 metrics CLI가 snapshot의 BAMLH0A0HYM2를 읽음")

    # 26. B11 covenant·임차인 표를 내지 않는 회사: series 단위 공시 없음(series_not_disclosed). exit 2로 세지 않는다
    def snd(**keys):
        return {k: dict({"source": "sec:8-K (가상)", "locator": "supplemental 전체에 %s 표가 없음 (가상)" % k,
                         "note": "회사가 %s 표를 내지 않음 (가상)" % k}, **v) for k, v in keys.items()}
    nc1 = copy.deepcopy(base)
    nc1["series"]["covenants"] = []
    nc1["series_not_disclosed"] = snd(covenants={})
    d101, c101 = rcompute(nc1)
    m101 = d101["metrics"]
    fl101 = next(f for f in d101["flags"] if f["flag"] == "covenant_headroom")
    ok(c101 == 0 and all(m101[k]["value"] is None and m101[k].get("not_disclosed") for k in
                         ("covenant_headroom", "covenant_headroom_min_pct"))
       and d101["summary"]["metrics_missing"] == 0 and fl101["level"] == "review" and "공시 없음" in fl101["note"]
       and any(g["item"] == "covenant_headroom_min_pct" and g["status"] == "not_disclosed" for g in d101["data_gaps"]),
       "B11 covenant 표가 없다고 근거와 함께 적으면 headroom은 공시 없음, flag review(공시 없음), exit 0 (got %s)" % c101)
    nc2 = copy.deepcopy(nc1)
    nc2["series_not_disclosed"]["covenants"]["locator"] = ""
    d102, c102 = rcompute(nc2)
    ok(c102 == 2 and d102["metrics"]["covenant_headroom_min_pct"].get("not_disclosed") is None
       and any(g["item"] == "series_not_disclosed.covenants" for g in d102["data_gaps"]),
       "B11 series 공시 없음에 근거(locator)가 없으면 보통 missing, 데이터 구멍, exit 2")
    nc3 = copy.deepcopy(base)
    nc3["series_not_disclosed"] = snd(covenants={})
    d103, _ = rcompute(nc3)
    ok(near(d103["metrics"]["covenant_headroom_min_pct"]["value"], exp["values"]["covenant_headroom_min_pct"]["value"])
       and any(g["item"] == "series_not_disclosed.covenants" and g["status"] == "ignored" for g in d103["data_gaps"]),
       "B11 covenant 줄이 있는데 공시 없음도 적으면 줄을 쓰고 공시 없음은 무시(데이터 구멍 ignored)")
    t5 = copy.deepcopy(base)
    t5["series"]["top_tenants"] = t5["series"]["top_tenants"][:5]
    t5["series_not_disclosed"] = snd(top_tenants={"note": "회사가 상위 5곳까지만 냄 (가상)"})
    d104, c104 = rcompute(t5)
    m104 = d104["metrics"]
    ok(c104 == 0 and near(m104["top5_tenant_pct"]["value"], exp["values"]["top5_tenant_pct"]["value"])
       and m104["top10_tenant_pct"]["value"] is None and m104["top10_tenant_pct"].get("not_disclosed")
       and "5곳" in m104["top10_tenant_pct"]["note"],
       "B11 임차인 표가 5곳만 있고 공시 없음을 적으면 top5는 계산, top10은 공시 없음, exit 0 (got %s)" % c104)
    t6 = copy.deepcopy(base)
    t6["series"]["top_tenants"] = t6["series"]["top_tenants"][:5]
    ok(rcompute(t6)[1] == 2, "B11 공시 없음을 적지 않고 임차인이 5곳뿐이면 top10은 보통 missing(exit 2)")
    t7 = copy.deepcopy(base)
    t7["series_not_disclosed"] = {"debt_maturities": snd(x={})["x"]}
    ok(any(g["item"] == "series_not_disclosed.debt_maturities" and g["status"] == "ignored"
           for g in rcompute(t7)[0]["data_gaps"]), "B11 series 공시 없음은 covenants·top_tenants만(다른 이름은 ignored)")

    # 27. capex reserve 공시 없음(findings 7): 회사가 capex reserve를 내지 않고 AFFO bridge에도 유지 capex 줄이 없으면 공시 없음
    nx = copy.deepcopy(base)
    for it in nx["affo_bridge"]["items"]:
        if it.get("category") in ("recurring_capex", "leasing_cost"):
            it["category"] = "other"
    nx["facts"]["capex_reserve"] = nd("triple-net이라 capex reserve를 내지 않고 AFFO에서 유지 capex를 빼지 않음")
    d105, c105 = rcompute(nx)
    m105 = d105["metrics"]
    ok(c105 == 0 and all(m105[k]["value"] is None and m105[k].get("not_disclosed") for k in
                         ("recurring_capex_to_noi_pct", "capex_reserve", "noi_economic", "economic_implied_cap_rate",
                          "economic_implied_cap_rate_debt_mtm"))
       and m105["implied_cap_rate"]["value"] is not None,
       "capex reserve가 공시 없음이고 bridge에 유지 capex 줄이 없으면 capex 지표·economic cap rate는 공시 없음, exit 0 (got %s)" % c105)

    # 28. B12 회계 기준이 달라 견줄 수 없는 Tracker subsector: 차이 줄을 내지 않고 이유를 적는다(exit에 세지 않음)
    gm = {"Free Standing (가상)": {"name": "Free Standing", "parent": "Retail", "cap_rate_comparable": False,
                                 "why": "회계 기준이 달라 견줄 수 없음(가상)"}}
    d106, c106 = rcompute(copy.deepcopy(base), tmap=gm)
    v106 = d106["metrics"]["implied_cap_rate_vs_tracker"]
    ok(c106 == 0 and v106["value"] is None and v106.get("not_comparable") is True and "회계 기준" in v106["note"]
       and d106["metrics"]["tracker_subsector_cap_rate"]["value"] == 6.5
       and any(g["item"] == "implied_cap_rate_vs_tracker" and g["status"] == "not_comparable" for g in d106["data_gaps"])
       and d106["summary"].get("metrics_not_comparable") == 1,
       "B12 견줄 수 없는 Tracker subsector면 차이 줄 값 없음·이유, Tracker 값은 그대로, exit 0 (got %s)" % c106)
    gam = load_tracker_map().get("Gaming") or {}
    ok(gam.get("cap_rate_comparable") is False and gam.get("why"), "B12 assets/tracker_map.json의 Gaming은 견줄 수 없음과 이유")

    # 29. B13 부채가 전부 만기 일시 상환이면 정기 원금 상환 0(근거 위치)으로 fixed-charge coverage를 낸다
    bl = copy.deepcopy(base)
    bl["facts"]["principal_amortization"] = {"value": 0, "unit": "USD thousands", "status": "reported", "as_of": "2026-09-30",
                                             "source": "sec:8-K (가상)", "locator": "Debt Maturities 표: 모두 만기 일시 상환 (가상)",
                                             "note": "회사 표에 정기 원금 상환 줄이 없음(bullet)"}
    f107 = rcompute(bl)[0]["metrics"]["fixed_charge_coverage"]
    ok(near(f107["value"], e1["fcc_bullet"]["value"]) and f107["status"] == "derived" and "정기 원금 상환을 0으로" in f107["note"],
       "B13 정기 원금 상환 0이면 FCC 1,452,572 / 336,764 = 4.3133x, note에 근거 (got %s)" % f107["value"])

    # 30. B14 연환산 NOI가 없고 분기 NOI만 있으면 분기×4(estimated)
    nq = copy.deepcopy(base)
    nq["facts"]["noi_annualized"] = nd("연환산 NOI를 내지 않음")
    nq["facts"]["noi_quarter"] = dict(base["facts"]["noi_annualized"], value=1500000,
                                      locator="NOI 표 three months 1,500,000 (가상)")
    d108, c108 = rcompute(nq)
    i108 = d108["metrics"]["implied_cap_rate"]
    ok(near(i108["value"], e1["noi_quarter_cap_rate"]["value"]) and i108["status"] == "estimated" and "분기 NOI" in i108["note"]
       and d108["metrics"]["economic_implied_cap_rate"]["status"] == "estimated" and c108 == 0,
       "B14 분기 NOI 1,500,000 × 4로 implied cap rate 7.59%%(estimated) (got %s %s)" % (i108["value"], i108["status"]))
    nq2 = copy.deepcopy(nq)
    del nq2["facts"]["noi_quarter"]
    d109, c109 = rcompute(nq2)
    ok(d109["metrics"]["implied_cap_rate"].get("not_disclosed") is True and c109 == 0,
       "B14 분기 NOI도 없으면 연환산 NOI의 공시 없음 그대로(exit 0)")

    # 31. AFFO를 내지 않는 회사(findings 8): Normalized FFO(없으면 FFO)를 분모로 한 참고 줄(estimated)과 유동성
    ef = e1["ffo_reference"]
    na5 = copy.deepcopy(base)
    na5["facts"]["affo_reported"] = nd("AFFO를 내지 않음")
    na5["affo_bridge"] = {"start": None, "items": []}
    na5["facts"]["capex_reserve"] = nd("capex reserve를 내지 않음")
    d110, c110 = rcompute(na5)
    m110 = d110["metrics"]
    ok(c110 == 0 and near(m110.get("ffo_annualized", {}).get("value"), ef["ffo_annualized"])
       and all(near(m110.get(k, {}).get("value"), ef[k]) and m110[k]["status"] == "estimated"
               for k in ("refi_impact_ffo_pct", "floating_rate_shock_ffo_pct", "retained_cash_flow_24m", "liquidity_coverage_24m"))
       and m110["refi_impact_affo_pct"].get("not_disclosed") is True and "FFO" in m110["retained_cash_flow_24m"]["note"],
       "AFFO 공시 없음이면 Normalized FFO × 4로 refi 영향 4.1185%%·변동금리 0.4291%%·유보 2,135,802.664·coverage 0.9269 (got %s)" % (
           {k: m110.get(k, {}).get("value") for k in ("refi_impact_ffo_pct", "liquidity_coverage_24m")},))
    ok("refi_impact_ffo_pct" not in doc["metrics"], "회사가 AFFO를 내면 FFO 참고 줄을 내지 않음")
    na5["facts"]["straight_line_rent"] = dict(base["facts"]["ffo_reported"], value=40000,
                                              locator="NFFO 표 Straight-line rent (40,000) (가상)")
    s111 = rcompute(na5)[0]["metrics"]["straight_line_to_ffo_pct"]
    ok(near(s111["value"], e1["straight_line_fact"]["value"]) and s111["status"] == "derived",
       "AFFO bridge에 straight-line 줄이 없으면 facts straight_line_rent 40,000 / FFO = 3.8424%")

    # 32. B21 표시 결함: 24개월 만기 note의 해 중복, AFFO 공시 없음의 stderr FAIL, 가격 지표의 기준일, 임차인 비중의 기준
    md = copy.deepcopy(base)
    y27 = next(e for e in md["series"]["debt_maturities"] if e["period"] == "2027")
    md["series"]["debt_maturities"] += [dict(y27, value=0.0)]
    n112 = rcompute(md)[0]["metrics"]["maturities_24m"]["note"]
    ok("2026·2027·2028년" in n112 and "2027·2027" not in n112, "B21 24개월 만기 note에 해를 한 번씩: %s" % n112)
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "f.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(na5, f)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            main(["metrics", "--facts", p, "--out", os.path.join(tmp, "m.json"), "--tracker-dir", os.path.join(fx, "tracker"),
                  "--reitwatch-dir", os.path.join(fx, "reitwatch"), "--macro-snapshot", os.path.join(fx, "macro_snapshot.json")])
        ok("FAIL affo_bridge" not in err.getvalue() and "공시 없음 affo_bridge" in err.getvalue(),
           "B21 AFFO 공시 없음이면 stderr에 FAIL이 아니라 '공시 없음 affo_bridge'")
    pa_ = base["facts"]["price"]["as_of"]
    ok(all(M[k]["as_of"] == pa_ for k in ("forward_yield", "market_cap", "p_affo", "p_ffo", "affo_yield", "implied_cap_rate",
                                          "economic_implied_cap_rate", "yield_spread_vs_10y", "implied_cap_rate_vs_tracker"))
       and GM["forward_yield"]["as_of"] == dbase["facts"]["price"]["as_of"] and GM["p_e"]["as_of"] == dbase["facts"]["price"]["as_of"],
       "B21 가격이 든 지표의 as_of는 가격 기준일 %s (got %s)" % (pa_, M["forward_yield"]["as_of"]))
    tm_ = copy.deepcopy(base)
    for e in tm_["series"]["top_tenants"]:
        e["measure"] = "Total revenues"
    t113 = rcompute(tm_)[0]["metrics"]
    tm_["series"]["top_tenants"][1]["measure"] = "ABR"
    t114 = rcompute(tm_)[0]["metrics"]["top5_tenant_pct"]
    ok("Total revenues" in t113["top1_tenant_pct"]["formula"] and "ABR" not in t113["top1_tenant_pct"]["formula"]
       and "임대료(ABR)" in M["top1_tenant_pct"]["formula"]
       and t114["status"] == "estimated" and "섞" in t114["note"],
       "B21 임차인 비중의 기준(measure)을 formula에 쓰고(없으면 ABR), 기준이 섞이면 estimated")
    # 그 밖의 표시: gross assets가 total assets와 같을 때, consensus 정의 점검(findings 10·11)
    ga0 = copy.deepcopy(base)
    ga0["facts"]["accumulated_depreciation"] = dict(ga0["facts"]["accumulated_depreciation"], value=0,
                                                    note="회사 표에 이 줄이 없음(가상)")
    ok("감가상각 누계가 0" in rcompute(ga0)[0]["metrics"]["gross_assets"]["note"],
       "감가상각 누계가 0이면 gross assets = total assets라고 note에 적음")
    cs = copy.deepcopy(base)
    cs["facts"]["ffo_per_share_diluted"] = dict(cs["facts"]["ffo_per_share_diluted"], value=0.31)
    m115 = rcompute(cs)[0]["metrics"]
    cs2 = copy.deepcopy(base)
    for k, v in (("affo_guidance_low", 4.24), ("affo_guidance_high", 4.26)):
        cs2["facts"][k] = dict(cs2["facts"][k], value=v)
    m116 = rcompute(cs2)[0]["metrics"]
    ok("consensus 정의 점검" in m115["ffo_fy"]["note"] and "consensus 정의 점검" in m115["p_ffo"]["note"]
       and "consensus 정의 점검" not in M["ffo_fy"]["note"] and "AFFO guidance" in m116["ffo_fy"]["note"],
       "consensus FFO가 회사 분기 FFO × 4와 10% 넘게 다르거나 AFFO guidance와 같으면 정의 점검 note")

    # 33. 그 밖의 약정 사용처(findings 12): 대출 funding commitment 등은 liquidity uses에 더한다
    oc = copy.deepcopy(base)
    oc["facts"]["other_committed_uses"] = dict(base["facts"]["dev_remaining_spend"], value=500000,
                                               locator="10-Q 대출 funding commitment 500,000 (가상)")
    m117 = rcompute(oc)[0]["metrics"]
    oc["facts"]["other_committed_uses"]["value"] = -1
    d118, _ = rcompute(oc)
    ok(near(m117["liquidity_uses_24m"]["value"], e1["other_committed_uses"]["uses"])
       and near(m117["liquidity_coverage_24m"]["value"], e1["other_committed_uses"]["coverage"])
       and "other_committed_uses" in m117["liquidity_uses_24m"]["inputs"]
       and "other_committed_uses" not in M["liquidity_uses_24m"]["inputs"]
       and d118["metrics"]["liquidity_uses_24m"]["value"] is None
       and any(g["item"] == "other_committed_uses" and "0 이상" in g["reason"] for g in d118["data_gaps"]),
       "그 밖의 약정 500,000(가상)이 있으면 uses 9,105,401·coverage 0.8913, 없으면 그대로, 음수면 거절")

    # 34. 그 분기 거래 없음(findings 13): 매입·매각 cap rate에 none_in_period(근거)를 적으면 공시 없음과 따로 세지 않는다
    def none_q(**kw):
        return dict({"value": None, "status": "missing", "none_in_period": True, "source": "sec:8-K (가상)",
                     "locator": "Dispositions 표: 그 분기 매각 없음 (가상)", "note": "그 분기 매각 없음 (가상)"}, **kw)
    nq3 = copy.deepcopy(base)
    nq3["facts"]["dispositions_cap_rate"] = none_q()
    d119, c119 = rcompute(nq3)
    m119 = d119["metrics"]["dispositions_cap_rate"]
    ok(c119 == 0 and m119["value"] is None and m119.get("none_in_period") is True and not m119.get("not_disclosed")
       and d119["summary"].get("metrics_none_in_period") == 1
       and any(g["item"] == "dispositions_cap_rate" and g["status"] == "none_in_period" for g in d119["data_gaps"]),
       "그 분기 거래 없음은 값 없음·none_in_period, 공시 없음과 따로 세고 exit 0 (got %s)" % c119)
    nq3["facts"]["dispositions_cap_rate"] = none_q(locator="")
    ok(rcompute(nq3)[1] == 2, "거래 없음에 근거(locator)가 없으면 보통 missing(exit 2)")
    nq3["facts"]["dispositions_cap_rate"] = copy.deepcopy(base["facts"]["dispositions_cap_rate"])
    nq3["facts"]["price"] = none_q()
    d120, c120 = rcompute(nq3)
    ok(c120 == 2 and any(g["item"] == "price" and "none_in_period" in g["reason"] for g in d120["data_gaps"]),
       "거래 없음은 매입·매각 cap rate에만(다른 칸은 missing, 데이터 구멍)")
    ok(diff_metrics(dict(d119, period="2026Q2"), d119)[1] == 0, "diff: 두 분기 모두 거래 없음인 줄은 missing으로 세지 않음")

    # 35. B3 등급(scorecard, D11): 축 5개, 등급마다 근거 지표·값·단위·단계·기준 문구, 한 단계 내림의 이유 (손으로 매긴 등급)
    e2 = exp["s2"]
    sc = load_scorecard()
    ok(sc["schema"] == "reit-scorecard/1" and [a["key"] for a in sc["axes"]] == list(e2["axes"])
       and all(resolve_cut(c, th, sc["metrics"]["affo_payout_forward"]) == w for c, w in zip(["watch", "alert"], (85, 100))),
       "B3 assets/scorecard.json 축 5개, watch·alert 경계는 thresholds.json 값(AFFO payout 85·100)")

    def grades(d):
        return {a["key"]: a["grade"] for a in d.get("scorecard", {}).get("axes", [])}
    s130 = d90.get("scorecard") or {}
    ok(grades(d90) == e2["base"]["grades"], "B3 O fixture 등급 %s (손으로: %s)" % (grades(d90), e2["base"]["why"]))
    ax = {a["key"]: a for a in s130.get("axes", [])}
    ev = {e["metric"]: e for e in (ax.get("leverage") or {}).get("evidence", [])}
    ok(ax.get("leverage", {}).get("grade_name") == "보통" and ax.get("leverage", {}).get("name") == "부채"
       and ev.get("net_debt_to_ebitdare", {}).get("grade") == "moderate" and ev["net_debt_to_ebitdare"].get("unit") == "x"
       and near(ev["net_debt_to_ebitdare"].get("value"), exp["values"]["net_debt_to_ebitdare"]["value"])
       and "6" in ev["net_debt_to_ebitdare"].get("criterion", "") and ev.get("interest_coverage", {}).get("grade") == "ample"
       and ax["leverage"].get("downgrade") is None and ax.get("valuation", {}).get("grade_name") == "중간",
       "B3 축마다 이름·등급 이름, 근거 지표(key·값·단위·단계·기준 문구), 내림 없음은 null")
    cut2 = copy.deepcopy(base)
    cut2["series"]["dividend_per_share_annual"] = [
        dict(e, value=v) for e, v in zip(cut2["series"]["dividend_per_share_annual"], exp["cut_series"]["values"])]
    a131 = {a["key"]: a for a in rcompute(cut2)[0]["scorecard"]["axes"]}["dividend_capacity"]
    ok(a131["grade"] == e2["cut"]["grade"] and a131.get("base_grade") == "ample"
       and "2025" in " ".join(a131["downgrade"]["reasons"]) and a131["downgrade"]["notches"] == 1,
       "B3 최근 3년 안 배당 컷(2025)이 있으면 배당 여력을 한 단계 내림(넉넉 → 보통)")
    a132 = {a["key"]: a for a in rcompute(wa)[0]["scorecard"]["axes"]}["leverage"]
    ok(a132["grade"] == e2["covenant"]["grade"] and "covenant" in " ".join(a132["downgrade"]["reasons"]),
       "B3 covenant 최소 여유 13.3%(15% 미만)면 부채를 한 단계 내림(보통 → 빠듯)")
    na6 = copy.deepcopy(na5)
    for k in ("affo_per_share_diluted", "affo_guidance_low", "affo_guidance_high"):
        na6["facts"][k] = nd("AFFO를 내지 않음")
    a133 = {a["key"]: a for a in rcompute(na6)[0]["scorecard"]["axes"]}
    ok(a133["dividend_capacity"]["grade"] == e2["ffo_fallback"]["grade"]
       and "FFO payout" in " ".join(a133["dividend_capacity"]["downgrade"]["reasons"])
       and a133["maturity_liquidity"]["grade"] == e2["ffo_fallback"]["maturity_grade"]
       and {e["metric"]: (e["grade"], e.get("grade_used")) for e in a133["maturity_liquidity"]["evidence"]}.get(
           "refi_impact_ffo_pct") == ("moderate", "tight"),
       "B3 AFFO가 없으면 FFO payout 76.66%(보통)을 한 단계 내림(빠듯), 차환 영향/FFO는 그 지표만 한 단계 보수적으로")
    un = copy.deepcopy(base)
    for k in ("ebitdare_quarter", "ebitdare_annualized"):
        un["facts"][k] = {"value": None, "status": "missing", "note": "못 찾음(가상)"}
    a134 = {a["key"]: a for a in rcompute(un)[0]["scorecard"]["axes"]}["leverage"]
    nd7 = copy.deepcopy(nc1)
    nd7["series"]["top_tenants"] = []
    nd7["series_not_disclosed"]["top_tenants"] = snd(top_tenants={})["top_tenants"]
    a135 = {a["key"]: a for a in rcompute(nd7)[0]["scorecard"]["axes"]}["tenant_concentration"]
    ok(a134["grade"] == "unknown" and a134["grade_name"] == "판정 못 함" and a135["grade"] == "unknown"
       and any(e.get("note", "").startswith("공시 없음") for e in a135["evidence"]),
       "B3 축의 핵심 지표가 모두 없음·공시 없음이면 판정 못 함")
    rp = dict(rw, peers=[dict(r, p_ffo_fy1=15.0) for r in rw["peers"]])
    a136 = {a["key"]: a for a in rcompute(copy.deepcopy(base), rw=rp)[0]["scorecard"]["axes"]}["valuation"]
    ok(a136["grade"] == e2["valuation_low"]["grade"] and a136["grade_name"] == "낮은 편",
       "B3 valuation: history 중간(0) + peer 대비 0.853(낮은 편 −1) → 합 −1 낮은 편")
    ok(g.get("scorecard", {}).get("axes") == [], "B3 일반 배당주 경로는 등급 축이 없음(빈 목록과 note)")

    # 36. B9 사건(events): type·날짜·제목·source·원문 문장(quote), web이면 URL. 어긴 항목은 버리고 데이터 구멍에 알린다
    def evt(**kw):
        return dict({"type": "tenant_credit", "date": "2026-10-20", "title": "임차인 A 임대료 미납(가상)",
                     "source": "sec:10-Q (가상)", "locator": "10-Q L100 (가상)",
                     "quote": "Tenant A did not pay September rent (가상)"}, **kw)
    evs = copy.deepcopy(base)
    evs["events"] = [evt(), evt(type="dividend", date="2026-11-01", title="배당 유지(가상)", source="web:https://example.com/a (가상)",
                             quote="declared a monthly dividend (가상)"),
                     evt(type="rating", source="web", quote="x (가상)"), evt(type="merger"), evt(quote=""),
                     evt(date="2026-13-40"), evt(type="legal", source="web", url="https://example.com/b (가상)", date="2026-09-01",
                                                 title="소송(가상)", quote="filed a complaint (가상)")]
    d140, c140 = rcompute(evs)
    e140 = d140.get("events") or []
    bad140 = sorted(g["item"] for g in d140["data_gaps"] if g["item"].startswith("events["))
    ok(c140 == 0 and [e["date"] for e in e140] == ["2026-11-01", "2026-10-20", "2026-09-01"]
       and e140[0].get("type_name") == "배당" and e140[2].get("url") == "https://example.com/b (가상)"
       and bad140 == ["events[2]", "events[3]", "events[4]", "events[5]"]
       and all(g["status"] == "ignored" for g in d140["data_gaps"] if g["item"].startswith("events[")),
       "B9 맞는 사건 3개를 최신순으로, URL 없는 web·모르는 type·빈 원문·날짜 아닌 것은 버리고 데이터 구멍(ignored), exit 그대로 (got %s %s)" % (
           [e["date"] for e in e140], bad140))
    ok(d90.get("events") == [], "B9 facts에 events가 없으면 빈 목록")

    # 37. B8 가볍게 보기(depth light, D15): 핵심 칸만으로 exit 0, 내지 않은 지표는 '가볍게 봄'(not_assessed), 핵심 칸이 비면 exit 2
    core = set()
    for c_ in LIGHT_CORE_FACTS:
        core |= set(c_) if isinstance(c_, tuple) else {c_}
    lt = copy.deepcopy(base)
    lt["depth"] = "light"
    lt["facts"] = {k: v for k, v in lt["facts"].items() if k in core}
    lt["series"] = {"top_tenants": lt["series"]["top_tenants"]}
    for k in ("affo_bridge", "sector_kpis"):
        lt.pop(k)
    d150, c150 = rcompute(lt)
    m150 = d150["metrics"]
    tie150 = next(c_ for c_ in d150["checks"] if c_["check"] == "ffo_tie_out")
    ax150 = {a["key"]: a for a in d150["scorecard"]["axes"]}
    ok(c150 == 0 and d150.get("depth") == "light" and d150["summary"]["metrics_missing"] == 0
       and d150["summary"].get("metrics_not_assessed", 0) > 10 and d150["summary"].get("light_core_missing") == []
       and m150["ffo_nareit"].get("not_assessed") and m150["covenant_headroom_min_pct"].get("not_assessed")
       and tie150["pass"] is None and tie150.get("not_assessed")
       and any(g["status"] == "not_assessed" and "가볍게 봄" in g["reason"] for g in d150["data_gaps"])
       and near(m150["affo_payout_forward"]["value"], e2["light"]["affo_payout_forward"])
       and near(m150["net_debt_to_ebitdare"]["value"], e2["light"]["net_debt_to_ebitdare"])
       and ax150["maturity_liquidity"]["grade"] == "unknown"
       and any("가볍게 봄" in e["note"] for e in ax150["maturity_liquidity"]["evidence"]),
       "B8 핵심 칸 %d개만으로 exit 0, 나머지 지표·FFO tie-out은 가볍게 봄, 만기·유동성 축은 판정 못 함 (got %s, missing %s)" % (
           len(LIGHT_CORE_FACTS), c150, [g["item"] for g in d150["data_gaps"] if g["status"] == "missing"][:5]))
    schema_md = open(os.path.join(root, "references", "facts-schema.md"), encoding="utf-8").read()
    light_md = schema_md.split("## 가볍게 보기", 1)[-1].split("\n## ", 1)[0]
    ok(all("`%s`" % k in light_md for k in core | {"series.top_tenants"}),
       "B8 facts-schema.md '가볍게 보기'의 핵심 칸이 LIGHT_CORE_FACTS와 같음")
    lt2 = copy.deepcopy(lt)
    del lt2["facts"]["occupancy_pct"]
    d151, c151 = rcompute(lt2)
    ok(c151 == 2 and d151["summary"].get("light_core_missing") == ["occupancy_pct"],
       "B8 핵심 칸(occupancy)이 비면 exit 2, summary.light_core_missing")
    ok(d90.get("depth") == "deep" and d90["summary"].get("metrics_not_assessed") == 0, "B8 depth가 없으면 deep")

    # 38. B16 ticker가 바뀐 종목: assets/ticker_aliases.json과 facts former_tickers로 REITWatch history를 잇는다
    ok(load_ticker_aliases().get("MPT") == ["MPW"], "B16 assets/ticker_aliases.json에 MPT ← MPW")
    with tempfile.TemporaryDirectory() as tmp:
        hist = [("2024-01", 10), ("2024-04", 11), ("2024-07", 12), ("2024-10", 13), ("2025-01", 14), ("2025-04", 15),
                ("2025-07", 16), ("2025-10", 17), ("2026-01", 18), ("2026-04", 13.5)]
        for m_, v in hist:
            t_ = "NEW" if m_ == "2026-04" else "OLD"
            with open(os.path.join(tmp, m_ + ".json"), "w", encoding="utf-8") as f:
                json.dump({"schema": REITWATCH_SCHEMA, "month": m_, "as_of": m_ + "-28", "fy1": int(m_[:4]),
                           "fy2": int(m_[:4]) + 1, "rows": [{"ticker": t_, "subsector": "Free Standing (가상)", "ffo_fy1": 4.0,
                                                             "ffo_fy2": 4.2, "p_ffo_fy1": v, "mkt_cap": 1000}]}, f)
        no_alias = load_reitwatch(tmp, "NEW")
        with_alias = load_reitwatch(tmp, "NEW", aliases=["OLD"])
        ok(len(no_alias["history"]) == 1 and len(with_alias["history"]) == 10, "B16 별칭이 없으면 history 1개, 있으면 10개")
        nw = dict(copy.deepcopy(base), ticker="NEW", former_tickers=["OLD"])
        _, m160 = cli_metrics(nw, ["--reitwatch-dir", tmp])
        p160 = (m160 or {}).get("metrics", {}).get("p_ffo_history_pctile", {})
        r160 = (m160 or {}).get("metrics", {}).get("ffo_fy1_rev_3m", {})
        ok(near(p160.get("value"), e2["alias"]["pctile"]) and "OLD" in p160.get("note", "")
           and r160.get("value") is not None,
           "B16 facts former_tickers [OLD]로 history 10개 중 13.5 이하 5개 = 50%%, 3개월 앞 호(OLD)와 revision (got %s)" % (
               p160.get("value"),))

    # 39. B7 peers: 같은 subsector에서 REITWatch 시총이 가장 가까운 2개, --with, 같은 분기 metrics 재사용 여부
    with tempfile.TemporaryDirectory() as root:
        os.makedirs(os.path.join(root, "reitwatch", "parsed"))
        rows = [("NNN", "Free Standing", 7800), ("O", "Free Standing", 50000), ("ADC", "Free Standing", 8300),
                ("EPRT", "Free Standing", 5600), ("FCPT", "Free Standing", 2400), ("AAA", "Free Standing", 900),
                ("DDD", "Industrial", 7900)]
        with open(os.path.join(root, "reitwatch", "parsed", "2026-10.json"), "w", encoding="utf-8") as f:
            json.dump({"schema": REITWATCH_SCHEMA, "month": "2026-10", "as_of": "2026-09-30",
                       "rows": [{"ticker": t_, "subsector": s_, "mkt_cap": c_} for t_, s_, c_ in rows]}, f)
        for t_, depth_, per_ in (("O", None, "2026Q3"), ("ADC", "light", "2026Q3"), ("EPRT", None, "2026Q2")):
            mp = os.path.join(root, "tickers", t_, "metrics", "2026Q3.json")  # EPRT는 이름만 2026Q3인 다른 분기 파일
            os.makedirs(os.path.dirname(mp))
            with open(mp, "w", encoding="utf-8") as f:
                json.dump(dict({"schema": METRICS_SCHEMA, "ticker": t_, "period": per_}, **({"depth": depth_} if depth_ else {})), f)
        os.makedirs(os.path.join(root, "tickers", "NNN", "facts"))
        with open(os.path.join(root, "tickers", "NNN", "facts", "2026Q3.json"), "w", encoding="utf-8") as f:
            json.dump({"schema": FACTS_SCHEMA, "ticker": "NNN", "period": "2026Q3"}, f)
        p170, c170 = find_peers("NNN", root=root)
        pp = {x["ticker"]: x for x in p170.get("peers", [])}
        ok(c170 == 0 and list(pp) == ["ADC", "EPRT"] and p170.get("period") == "2026Q3"
           and pp["ADC"]["reuse"] is True and pp["ADC"]["depth"] == "light" and pp["EPRT"]["action"] == "make_light"
           and pp["EPRT"]["reuse"] is False and [x["ticker"] for x in p170.get("reusable_others", [])] == ["O"]
           and p170["reusable_others"][0]["depth"] == "deep",
           "B7 NNN(7,800) peers는 시총이 가까운 ADC(8,300)·EPRT(5,600)(AAA 900은 멂), ADC는 light metrics 재사용, "
           "EPRT는 파일 속 분기가 달라 light로 만들기, O는 deep metrics가 있어 reusable_others (got %s)" % list(pp))
        p171, c171 = find_peers("NNN", with_=["O", "ADC"], root=root)
        q = {x["ticker"]: x for x in p171.get("peers", [])}
        ok(c171 == 0 and list(q) == ["O", "ADC"] and q["O"]["reuse"] is True and q["O"]["depth"] == "deep"
           and q["O"]["action"] == "reuse" and q["O"]["metrics_path"].endswith(os.path.join("O", "metrics", "2026Q3.json")),
           "B7 --with O,ADC면 그 종목, O는 같은 분기 deep metrics를 재사용")
        ok(find_peers("ZZZ", root=root)[1] == 2 and find_peers("NNN", period="2026Q2", root=root)[0]["peers"][0]["reuse"] is False,
           "B7 REITWatch에 없는 종목은 exit 2, 다른 분기 metrics는 재사용하지 않음")
        cli_out = io.StringIO()
        old_env = os.environ.get("INVEST_DATA_DIR")
        os.environ["INVEST_DATA_DIR"] = root
        try:
            with contextlib.redirect_stdout(cli_out), contextlib.redirect_stderr(io.StringIO()):
                c172 = main(["peers", "NNN", "--with", "O,ADC"])
        finally:
            if old_env is None:
                os.environ.pop("INVEST_DATA_DIR")
            else:
                os.environ["INVEST_DATA_DIR"] = old_env
        ok(c172 == 0 and json.loads(cli_out.getvalue()).get("peers", [{}])[0].get("ticker") == "O",
           "B7 peers CLI(--with)가 JSON을 stdout에 씀")
    with tempfile.TemporaryDirectory() as root:
        p173, c173 = find_peers("NNN", root=root)
        ok(c173 == 2 and "reit-sector-scan" in p173.get("note", ""), "B7 REITWatch parsed가 없으면 exit 2와 안내")

    # 40. compare: 종목마다 depth와 scorecard를 싣는다(기존 칸은 그대로)
    c180, k180 = compare_metrics([d90, dict(d150, ticker="LT")])
    cols = {c_["ticker"]: c_ for c_ in c180.get("columns", [])}
    ok(cols.get("O", {}).get("depth") == "deep" and cols.get("LT", {}).get("depth") == "light"
       and [a["key"] for a in (cols["O"].get("scorecard") or {}).get("axes", [])] == list(e2["axes"])
       and cols["O"].get("warning") == "" and "metrics_missing" in cols["O"]
       and c180.get("scorecard_rows", [{}])[0].get("grades", {}).get("O", {}).get("grade") == e2["base"]["grades"]["dividend_capacity"],
       "compare columns에 depth·scorecard, scorecard_rows에 축마다 종목 등급")
    old_m = {k_: v for k_, v in d90.items() if k_ not in ("depth", "scorecard", "events")}
    c181, _ = compare_metrics([old_m, dict(old_m, ticker="O2")])
    ok(c181["columns"][0]["depth"] == "deep" and c181["columns"][0]["scorecard"] is None,
       "compare: depth·scorecard가 없는 옛 metrics는 deep과 null")

    log("check: %d개 실패" % len(fails) if fails else "check: 모두 통과")
    return 2 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

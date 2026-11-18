#!/usr/bin/env python3
"""reit-stock-review: facts JSON에서 REIT 지표를 계산해 metrics JSON을 쓴다.

명령
  metrics --facts F [--out P] [--macro-snapshot P] [--reitwatch-dir D]
                         facts를 읽어 지표·check·flag·데이터 구멍을 계산한다
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
}
BRIDGE_STARTS = ["ffo_reported", "normalized_ffo_reported"]


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


def normalize(key, f, kind):
    """facts 칸 하나를 검사하고 단위를 맞춘다. 규칙을 어기면 missing으로 내리고 이유를 note에 쓴다."""
    if not isinstance(f, dict):
        return missing("%s: 값의 형식이 아님" % key), "형식"
    status = f.get("status")
    if status not in STATUSES:
        return missing("%s: status %r" % (key, status)), "status"
    if status == "missing" or f.get("value") is None:
        return missing(f.get("note") or f.get("reason") or "값 없음"), None
    if not isinstance(f.get("value"), (int, float)) or isinstance(f.get("value"), bool):
        return missing("%s: value가 숫자가 아님" % key), "숫자"
    if status in ("reported", "estimated", "stale") and not (str(f.get("source", "")).strip()
                                                          and str(f.get("locator", "")).strip()):
        return missing("%s: source나 locator가 없어 쓰지 않음(출처 없는 값)" % key), "출처"
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


def worst(statuses):
    return max(statuses, key=RANK.get)


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


# ---------------------------------------------------------------- 바깥 데이터 (읽기만)

def load_macro(path):
    """DGS10 envelope와 쓴 파일. 없으면 missing."""
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
    e = (snap.get("series") or {}).get("DGS10")
    if not e or e.get("value") is None:
        return missing("macro snapshot에 DGS10 값 없음"), path
    out = {"value": e["value"], "unit": "%", "status": e["status"], "as_of": e.get("as_of"),
           "source": e.get("source", "fred:DGS10"), "locator": "%s series.DGS10" % path, "note": e.get("note", ""),
           "snapshot_date": snap.get("date")}
    return out, path


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
    for m, p, d in months:
        r = next((x for x in d["rows"] if x.get("ticker") == ticker), None)
        if r and isinstance(r.get("p_ffo_fy1"), (int, float)):
            out["history"].append((m, r["p_ffo_fy1"]))
    return out


def rw_env(rw, field, note):
    v = (rw.get("row") or {}).get(field)
    if not isinstance(v, (int, float)):
        return missing(rw["note"] or "REITWatch %s row에 %s 없음" % (rw.get("month"), field))
    return {"value": v, "unit": "", "status": "estimated", "as_of": rw.get("as_of") or rw.get("month"),
            "source": "reitwatch:%s" % rw["month"], "locator": "%s rows[ticker=%s].%s" % (
                rw["path"], rw["row"].get("ticker"), field), "note": note}


# ---------------------------------------------------------------- metrics

def compute(facts_doc, th, macro=None, rw=None):
    """facts 문서 → metrics 문서. 같은 입력이면 같은 출력(시각을 넣지 않는다)."""
    raw = facts_doc.get("facts") or {}
    gaps = []
    F = {}
    for key, kind in FACT_KINDS.items():
        env, bad = normalize(key, raw.get(key, missing("facts에 칸이 없음")), kind)
        F[key] = env
        if bad:
            gaps.append({"item": key, "status": "missing", "reason": env["note"]})
    unknown = sorted(set(raw) - set(FACT_KINDS))
    for k in unknown:
        gaps.append({"item": k, "status": "ignored", "reason": "모르는 facts 칸이라 쓰지 않음(references/facts-schema.md)"})

    S = {}
    rawser = facts_doc.get("series") or {}
    for key, kind in SERIES_KINDS.items():
        items = []
        for i, it in enumerate(rawser.get(key) or []):
            env, bad = normalize("%s[%d]" % (key, i), it, kind)
            env["period"] = it.get("period")
            env["label"] = it.get("label")
            items.append(env)
            if bad:
                gaps.append({"item": "%s[%d]" % (key, i), "status": "missing", "reason": env["note"]})
        S[key] = items

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
    else:
        diff = (rec - rep) / abs(rep) * 100 if rep else float("inf")
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
        items.append(env)
        if bad:
            gaps.append({"item": "affo_bridge.items[%d]" % i, "status": "missing", "reason": env["note"]})
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
        else:
            d = (s - a) / abs(a) * 100 if a else float("inf")
            checks.append({"check": "affo_bridge", "pass": abs(d) <= btol, "status": "derived", "sum": s,
                           "reported": a, "diff_pct": round(d, 4), "tolerance_pct": btol,
                           "note": "" if abs(d) <= btol else "bridge 합이 회사 AFFO와 다름: 빠진 줄이나 부호를 본다"})
    M["affo_reported"] = passthrough("affo_reported", F["affo_reported"], "회사 정의 AFFO(회사 발표값)")
    M["ffo_reported"] = passthrough("ffo_reported", F["ffo_reported"])
    M["ffo_per_share_diluted"] = passthrough("ffo_per_share_diluted", F["ffo_per_share_diluted"])
    M["affo_per_share_diluted"] = passthrough("affo_per_share_diluted", F["affo_per_share_diluted"])

    # 연간 주당 FFO·AFFO (guidance 가운데 → consensus → 분기×4 가정)
    affo_mid = metric("affo_fy_guidance_mid", "(AFFO guidance low + high) / 2",
                      {"affo_guidance_low": F["affo_guidance_low"], "affo_guidance_high": F["affo_guidance_high"]},
                      lambda lo, hi: (lo + hi) / 2, "USD/share")
    affo_run = assume(metric("affo_run_rate", "분기 AFFO/share × 4",
                             {"affo_per_share_diluted": F["affo_per_share_diluted"]}, lambda a: a * 4, "USD/share"),
                      "guidance가 없어 분기×4로 가정")
    M["affo_fy"] = dict(first_of(affo_mid, affo_run), metric="affo_fy")

    rwd = rw or {"note": "REITWatch를 읽지 않음", "files": []}
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
                                       "affo_per_share_diluted": F["affo_per_share_diluted"]},
                                      lambda d, a: d / a * 100, "%")
    annual = sorted([e for e in S["dividend_per_share_annual"] if e.get("period")], key=lambda e: e["period"])
    for n in (3, 5):
        if len(annual) >= n + 1 and annual[-1 - n]["value"] is not None and annual[-1]["value"] is not None:
            a0, a1 = annual[-1 - n], annual[-1]
            M["dividend_cagr_%dy" % n] = metric("dividend_cagr_%dy" % n,
                                                "(%s 배당 / %s 배당)^(1/%d) − 1" % (a1["period"], a0["period"], n),
                                                {a0["period"]: a0, a1["period"]: a1},
                                                lambda x, y, n=n: cagr_pct(x, y, n), "%")
        else:
            M["dividend_cagr_%dy" % n] = dict(missing("연간 배당 history가 %d년보다 짧거나 끝 값이 없음" % (n + 1)),
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

    # 재무
    M["net_debt"] = metric("net_debt", "total debt − cash", {"total_debt": F["total_debt"], "cash": F["cash"]},
                           lambda d, c: d - c, "USD thousands")
    M["net_debt_to_ebitdare"] = metric("net_debt_to_ebitdare", "net debt / 연환산 EBITDAre",
                                       {"net_debt": env_of(M["net_debt"]), "ebitdare_annualized": F["ebitdare_annualized"]},
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
        ladder = [{"period": e["period"], "value": e["value"], "pct": round(e["value"] / total * 100, 4)} for e in mats]
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
        env, bad = normalize("sector_kpis.%s" % k, v, "pct") if isinstance(v, dict) and v.get("unit") == "%" else (None, None)
        if env is None:
            env = dict(v) if isinstance(v, dict) else missing("형식이 아님")
            if env.get("value") is not None and not (env.get("source") and env.get("locator")):
                env, bad = missing("sector_kpis.%s: source나 locator가 없어 쓰지 않음" % k), "출처"
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
        M["p_ffo_history_pctile"] = dict(missing("REITWatch history가 %d달(필요 %d달)" % (len(hist), th["history_min_points"])),
                                         metric="p_ffo_history_pctile", formula="", inputs=[])
    else:
        M["p_ffo_history_pctile"] = dict(metric("p_ffo_history_pctile", "자기 REITWatch P/FFO history 중 최신값 이하 비율(%%), %d달" % len(hist),
                                                {"p_ffo_reitwatch": rw_pffo}, lambda x: pctile_le(hist, x), "%"),
                                         status="estimated")
        M["p_ffo_history_pctile"]["note"] = "%s~%s" % (rwd["history"][0][0], rwd["history"][-1][0])
    peers = [r for r in rwd.get("peers", []) if isinstance(r.get("p_ffo_fy1"), (int, float))]
    if rw_pffo["value"] is None or not peers:
        why = rw_pffo["note"] if rw_pffo["value"] is None else "같은 subsector peer가 없음"
        M["peer_median_p_ffo"] = dict(missing(why), metric="peer_median_p_ffo", formula="", inputs=[])
        M["p_ffo_vs_peer"] = dict(missing(why), metric="p_ffo_vs_peer", formula="", inputs=[])
    else:
        pm = median([r["p_ffo_fy1"] for r in peers])
        M["peer_median_p_ffo"] = {"metric": "peer_median_p_ffo", "formula": "같은 subsector peer의 REITWatch P/FFO median",
                                  "unit": "x", "value": round(pm, 4), "status": "estimated", "as_of": rwd.get("as_of"),
                                  "inputs": sorted(r.get("ticker") for r in peers),
                                  "note": "%s %s, peer %d개" % (rwd["month"], M["subsector"]["value"], len(peers))}
        M["p_ffo_vs_peer"] = metric("p_ffo_vs_peer", "REITWatch P/FFO / peer median",
                                    {"p_ffo_reitwatch": rw_pffo, "peer_median_p_ffo": env_of(M["peer_median_p_ffo"])},
                                    lambda a, b: a / b, "x")

    flags = [
        flag("affo_payout", M["affo_payout_forward"] if M["affo_payout_forward"]["value"] is not None
             else M["affo_payout_quarter"], th, "affo_payout_pct"),
        flag("maturity_concentration", M["maturity_max_year_pct"], th, "maturity_single_year_pct"),
        flag("net_debt_to_ebitdare", M["net_debt_to_ebitdare"], th, "net_debt_to_ebitdare_x"),
        flag("interest_coverage", M["interest_coverage"], th, "interest_coverage_x"),
    ]

    def walk(ms):
        for k, m in ms.items():
            if k == "sector_kpis":
                for kk, mm in m.items():
                    yield "sector_kpis." + kk, mm
            else:
                yield k, m
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
        "ticker": facts_doc.get("ticker"),
        "period": facts_doc.get("period"),
        "period_end": facts_doc.get("period_end"),
        "inputs": {"macro_snapshot": (macro or {}).get("locator", "").split(" ")[0] or None,
                   "reitwatch_files": rwd.get("files", []), "reitwatch_month": rwd.get("month")},
        "thresholds": {k: th[k] for k in ("affo_payout_pct", "maturity_single_year_pct", "net_debt_to_ebitdare_x",
                                          "interest_coverage_x", "ffo_tie_out_tolerance_pct")},
        "metrics": M,
        "checks": checks,
        "flags": flags,
        "data_gaps": gaps,
        "summary": {"metrics_missing": n_missing, "checks_failed": n_fail,
                    "flags": {f["flag"]: f["level"] for f in flags}},
    }
    return doc, (2 if (n_missing or n_fail) else 0)


# ---------------------------------------------------------------- CLI

def metrics_path(doc):
    return os.path.join(data_dir(), "tickers", doc["ticker"], "metrics", "%s.json" % doc["period"])


def write_json(doc, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
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
    rw = load_reitwatch(args.reitwatch_dir, facts["ticker"])
    doc, code = compute(facts, th, macro=macro, rw=rw)
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


def main(argv):
    p = argparse.ArgumentParser(prog="reit_calc.py", description="REIT facts → metrics")
    p.add_argument("--check", action="store_true", help="network 없이 fixture로 확인")
    sub = p.add_subparsers(dest="cmd")
    m = sub.add_parser("metrics", help="facts에서 지표를 계산한다")
    m.add_argument("--facts", required=True)
    m.add_argument("--out")
    m.add_argument("--macro-snapshot", help="기본: 가장 최근 macro snapshot")
    m.add_argument("--reitwatch-dir", help="기본: $INVEST_DATA_DIR/reitwatch/parsed")
    try:
        args = p.parse_args(argv)
    except SystemExit as e:
        return 0 if e.code == 0 else 1
    if args.check:
        return run_check()
    if args.cmd == "metrics":
        return cmd_metrics(args)
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
       "thresholds.json이 D11 값(85/100, 15/20)")

    base = read_json(os.path.join(fx, "facts_o_2026q3.json"))
    macro, _ = load_macro(os.path.join(fx, "macro_snapshot.json"))
    th_rw = dict(th, history_min_points=3)
    rw = load_reitwatch(os.path.join(fx, "reitwatch"), "O")
    doc, code = compute(copy.deepcopy(base), th_rw, macro=macro, rw=rw)
    M = doc["metrics"]

    # 1. 회사 발표값·손계산과 맞춘다
    for name, want in exp["values"].items():
        got = M[name]["value"]
        ok(near(got, want["value"], want.get("tol", 1e-3)) and M[name]["status"] == want["status"],
           "%s = %s %s (got %s %s; %s)" % (name, want["value"], want["status"], got, M[name]["status"], want["why"]))
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
    again, _ = compute(copy.deepcopy(base), th_rw, macro=macro, rw=rw)
    ok(json.dumps(again, sort_keys=True) == json.dumps(doc, sort_keys=True), "같은 입력이면 같은 출력")

    # 2. FFO 입력 하나를 바꾸면 tie-out 실패, exit 2, 데이터 구멍
    bad = copy.deepcopy(base)
    bad["facts"]["gain_on_sale_re"]["value"] = 13865
    d2, c2 = compute(bad, th_rw, macro=macro, rw=rw)
    ok(c2 == 2 and not d2["checks"][0]["pass"] and any(g["item"] == "check:ffo_tie_out" for g in d2["data_gaps"]),
       "FFO 입력을 바꾸면 tie-out 실패 → exit 2, 데이터 구멍에 올라감 (diff %s%%)" % d2["checks"][0]["diff_pct"])

    # 3. 값이 빠지면 0으로 채우지 않고 missing을 물려준다
    nop = copy.deepcopy(base)
    nop["facts"]["price"] = {"value": None, "status": "missing", "note": "FINNHUB_API_KEY 없음, 웹 채우기 전"}
    d3, c3 = compute(nop, th_rw, macro=macro, rw=rw)
    ok(c3 == 2 and d3["metrics"]["forward_yield"]["value"] is None and d3["metrics"]["forward_yield"]["status"] == "missing"
       and d3["metrics"]["implied_cap_rate"]["status"] == "missing" and d3["metrics"]["affo_payout_forward"]["value"] is not None,
       "price가 없으면 yield·cap rate는 missing, payout은 계산")
    ok("price" in d3["metrics"]["forward_yield"]["missing_inputs"] and "FINNHUB" in d3["metrics"]["forward_yield"]["note"],
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

    # 10. CLI: 입력 오류는 exit 1
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "f.json")
        with open(p, "w") as f:
            json.dump({"schema": "x"}, f)
        ok(main(["metrics", "--facts", p, "--out", os.path.join(tmp, "m.json")]) == 1, "schema가 틀린 facts는 exit 1")
        ok(main(["metrics", "--facts", os.path.join(tmp, "none.json")]) == 1, "facts 파일이 없으면 exit 1")

    log("check: %d개 실패" % len(fails) if fails else "check: 모두 통과")
    return 2 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""metrics에서 사람이 읽는 보고서를 만든다(읽는 보고서, 부록, 장표).

사용법
  report.py build --metrics M [--peers M2 M3] [--trend T] [--narrative N] --out-dir D [--date YYYY-MM-DD]
      단일 보고서: <날짜>-deep.md(읽는 보고서), <날짜>-deep-audit.md(부록), <날짜>-deep.view.json(view model),
      slides/<날짜>-<T>-0N-*.html(장표, diagram-slides가 있으면 .png도)
  report.py earnings --diff E [--narrative N] --out-dir D [--date YYYY-MM-DD]
      실적 뒤 점검: <날짜>-earnings.md. E는 reit_calc.py diff의 출력. 두 metrics는 E의 files에서 읽는다
  report.py --check
      network 없이 tests/fixtures로 확인한다

숫자는 metrics·trend JSON에서 그대로 옮긴다(다시 계산하지 않는다). report.py가 새로 내는 숫자는
순위(몇째), 추세 표의 처음→끝 변화, 장표 막대의 묶음 합뿐이다. agent는 풀이 글만 narrative.json에 쓴다
(칸과 한도는 references/report-format.md, 검사 기준은 assets/narrative_rules.json).

종료 코드
  0  다 만들었다
  1  입력 오류(파일 없음, schema, 인자), 일반 배당주 metrics
  2  풀이 글이 없다(숫자로 만들 수 있는 부분만 채우고 "풀이 글 없음"을 적는다),
     풀이 글 검사에 걸렸다(금지어·길이·칸 이름·metrics에 없는 숫자. 아무 파일도 쓰지 않는다),
     장표가 render_slide.py 검사에 걸렸다(그 장표만 실패로 적고 나머지는 만든다),
     보고서 자체 검사에 걸렸다(60줄 초과, 칸 이름 누출)

장표 굽기: diagram-slides 폴더를 DIAGRAM_SLIDES_DIR 환경 변수, 없으면 이 skill 폴더 옆 ../diagram-slides,
없으면 ~/.claude/skills/diagram-slides에서 찾는다. DIAGRAM_SLIDES_DIR이 있는데 render_slide.py가 없거나
어디에도 없으면 장표 HTML만 쓰고 보고서에 한 줄 적는다.
"""
import argparse
import datetime
import html
import json
import math
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import reit_calc  # noqa: E402  같은 skill 폴더의 script

SKILL = os.path.dirname(HERE)
ASSETS = os.path.join(SKILL, "assets")
VIEW_SCHEMA = "reit-report-view/1"
MAX_LINES = 60
GLOSSARY_HEAD = "## 용어"
GRADE_ORDER = ("ample", "moderate", "tight", "risk")
GRADE_MARK = {"ample": "◎", "moderate": "○", "tight": "△", "risk": "✕", "unknown": "?",
              "low": "↓", "mid": "–", "high": "↑"}
MARKERS = ("not_disclosed", "not_assessed", "not_comparable", "none_in_period")
EN_STATUS = ("reported", "derived", "estimated", "stale", "missing") + MARKERS
SNAKE = re.compile(r"(?<![A-Za-z0-9_])[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?![A-Za-z0-9_])")
NUM_IN_TEXT = re.compile(r"(?<![\w.])[+\-−]?(\d+(?:\.\d+)?)\s*(%p|%|x|배)(?![A-Za-z])")
KEY_ROWS = ("forward_yield", "yield_spread_vs_10y", "implied_cap_rate_vs_tracker", "fixed_rate_debt_pct", "wale_years",
            "ffo_fy1_rev_3m")
PEER_ROWS = ("forward_yield", "affo_payout_forward", "net_debt_to_ebitdare", "interest_coverage", "top1_tenant_pct",
             "p_ffo_reitwatch", "p_ffo_vs_peer")
RAW_VALUATION = ("p_ffo_reitwatch", "p_ffo", "p_affo", "implied_cap_rate", "affo_yield")
TREND_ROWS = ("revenue", "interest_expense", "debt", "diluted_shares", "dividend_per_share")
EVENT_MAX = 5

_CACHE = {}


def asset(name):
    if name not in _CACHE:
        _CACHE[name] = reit_calc.read_json(os.path.join(ASSETS, name))
    return _CACHE[name]


def lab():
    return asset("labels.json")


def log(msg):
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------- 숫자와 상태를 사람 말로

def fnum(v, nd=2):
    """소수 nd자리까지 반올림하고 끝의 0은 지우되 소수 한 자리는 남긴다(69.4678 → 69.47, 4.1 → 4.1, 6 → 6.0)."""
    s = "%.*f" % (nd, v)
    if "." in s:
        s = s.rstrip("0")
        if s.endswith("."):
            s += "0"
    return "0.0" if s in ("-0.0", "-0") else s


def fthr(v):
    """기준값: 정수면 소수 없이(15, 85), 아니면 fnum."""
    return str(int(v)) if float(v).is_integer() else fnum(v)


def per_share(v):
    s = "%.4f" % v
    head, tail = s.split(".")
    tail = tail.rstrip("0")
    while len(tail) < 2:
        tail += "0"
    return "$%s.%s" % (head, tail)


def money_k(v):
    """USD thousands → $7.87B, $432.9M."""
    if abs(v) >= 1e6:
        return "$%sB" % fnum(v / 1e6, 2)
    return "$%sM" % "{:,.1f}".format(v / 1e3)


def fval(v, unit, key=None):
    if v is None:
        return None
    if isinstance(v, list):
        return "·".join(str(x) for x in v) if v else "없음"
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "예" if v else "아니오"
    unit = unit or (lab()["metrics"].get(key or "", {}).get("unit")) or ""
    if unit == "%":
        sign = "+" if (lab()["metrics"].get(key or "", {}).get("signed") and v > 0) else ""
        return sign + fnum(v) + "%"
    if unit == "%p":
        return ("+" if v > 0 else "") + fnum(v) + "%p"
    if unit == "x":
        return fnum(v) + "x"
    if unit == "years":
        return fnum(v) + "년"
    if unit == "USD thousands":
        return money_k(v)
    if unit == "USD/share":
        return per_share(v)
    if unit == "USD":
        return "$%sM" % "{:,.1f}".format(v / 1e6) if abs(v) >= 1e6 else "$%s" % "{:,.0f}".format(v)
    if unit in ("shares", "shares thousands"):
        n = v / 1e3 if unit == "shares thousands" else v / 1e6
        return "%s백만 주" % "{:,.1f}".format(n)
    if unit == "per year":
        return "%d회" % v
    return fnum(v)


def full_k(v):
    """부록용: USD thousands를 그대로(7,868,446.46)."""
    if v is None:
        return "–"
    return "{:,.0f}".format(v) if float(v).is_integer() else "{:,.2f}".format(v)


def skey(e):
    for m in MARKERS:
        if (e or {}).get(m):
            return m
    return (e or {}).get("status") or "missing"


def sword(e):
    return lab()["status"].get(skey(e), "없음")


def show(e, unit=None, key=None):
    """값 + (추정)·(오래됨). 값이 없으면 상태 이름(공시 없음, 가볍게 봄, 없음)."""
    if not e or e.get("value") is None:
        return sword(e or {})
    t = fval(e["value"], unit or e.get("unit"), key)
    if skey(e) in ("estimated", "stale"):
        t += "(%s)" % sword(e)
    return t


def plain(e, key=None):
    """상태 칸이 따로 있는 표: 값만(없으면 상태 이름)."""
    if not e or e.get("value") is None:
        return sword(e or {})
    return fval(e["value"], e.get("unit"), key)


def mname(key, fallback=None):
    m = lab()["metrics"].get(key)
    if m:
        return m["name"]
    return fallback or key.replace("_", " ")


def short(key):
    return (lab()["metrics"].get(key) or {}).get("short", "")


def reason(note):
    for pat, why in lab()["reasons"]:
        if pat in (note or ""):
            return why
    return ""


def tidy_numbers(s):
    """metrics 글 안의 긴 소수(66.6667%)를 보고서와 같은 자리(66.67%)로 맞춘다."""
    return re.sub(r"(?<![\w.])(\d+\.\d{3,})(?!\d)", lambda m: fnum(float(m.group(1))), s or "")


def humanize(s, data_root=None):
    """note를 사람 말로: 칸 이름 → labels 이름, 영문 status → 상태 이름, 데이터 경로 → $INVEST_DATA_DIR."""
    s = tidy_numbers(s or "")
    if data_root:
        s = s.replace(data_root.rstrip("/"), "$INVEST_DATA_DIR")
    s = s.replace(os.path.expanduser("~"), "~")
    names = dict(lab()["facts"])
    names.update({k: v["name"] for k, v in lab()["metrics"].items()})
    def sub(m):
        a, b = m.start(), m.end()
        if (a and s[a - 1] in "-/.") or s[b:b + 1] == ".":  # 파일 이름(nnn-ex99_1.txt)은 그대로 둔다
            return m.group(0)
        return names.get(m.group(0), m.group(0).replace("_", " "))
    s = SNAKE.sub(sub, s)
    return re.sub(r"(?<![A-Za-z_])(%s)(?![A-Za-z_])" % "|".join(EN_STATUS), lambda m: lab()["status"].get(m.group(1), m.group(1)), s)


def leaks(text):
    """읽는 보고서 본문에 남으면 안 되는 것: snake_case 칸 이름, exit, 영문 status. 링크 주소는 보지 않는다."""
    t = re.sub(r"\]\([^)]*\)", "]", text)
    found = SNAKE.findall(t)
    found += re.findall(r"(?i)(?<![a-z])exit(?![a-z])", t)
    found += re.findall(r"(?<![A-Za-z_])(?:%s)(?![A-Za-z_])" % "|".join(EN_STATUS), t)
    return found


def body_lines(md):
    return md.split("\n" + GLOSSARY_HEAD)[0].rstrip("\n").split("\n")


def md_cell(s):
    return str(s if s is not None else "").replace("|", "/").replace("\n", " ")


def md_table(head, rows):
    out = ["| " + " | ".join(md_cell(h) for h in head) + " |", "| " + " | ".join("---" for _ in head) + " |"]
    out += ["| " + " | ".join(md_cell(c) for c in r) + " |" for r in rows]
    return out


# ---------------------------------------------------------------- 읽기

def read_doc(path, schema=None):
    try:
        d = reit_calc.read_json(path)
    except (OSError, ValueError) as e:
        raise InputError("읽지 못함: %s (%s)" % (path, e))
    if schema and d.get("schema") != schema:
        raise InputError("%s의 schema가 %r(%s이어야 함)" % (path, d.get("schema"), schema))
    d["_file"] = os.path.abspath(path)
    return d


class InputError(Exception):
    pass


def read_facts(mdoc):
    """metrics의 facts_file, 없으면 metrics 옆 ../facts/<분기>.json."""
    cands = [mdoc.get("facts_file"),
             os.path.join(os.path.dirname(os.path.dirname(mdoc["_file"])), "facts", "%s.json" % mdoc.get("period"))]
    for c in cands:
        if c and os.path.isfile(c):
            try:
                return reit_calc.read_json(c), c
            except (OSError, ValueError):
                continue
    return {}, None


def data_root_of(mdoc):
    """metrics 경로 .../tickers/<T>/metrics/<P>.json → 데이터 폴더."""
    p = mdoc.get("_file") or ""
    parts = p.split(os.sep)
    if len(parts) > 4 and parts[-4] == "tickers":
        return os.sep.join(parts[:-4])
    return reit_calc.data_dir()


def menv(doc, key):
    M = doc.get("metrics") or {}
    if key.startswith("sector_kpis."):
        return (M.get("sector_kpis") or {}).get(key.split(".", 1)[1]) or {}
    return M.get(key) or {}


def num_of(e):
    v = (e or {}).get("value")
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


# ---------------------------------------------------------------- 풀이 글 검사 (B20)

def narrative_strings(nar, prefix=""):
    if isinstance(nar, str):
        yield prefix, nar
    elif isinstance(nar, dict):
        for k, v in nar.items():
            yield from narrative_strings(v, "%s.%s" % (prefix, k) if prefix else k)
    elif isinstance(nar, list):
        for i, v in enumerate(nar):
            yield from narrative_strings(v, "%s[%d]" % (prefix, i))


def check_narrative(nar, kind, allowed, tickers=()):
    """풀이 글 검사. 문제 목록(비면 통과)."""
    rules = asset("narrative_rules.json")
    spec = rules["fields"][kind]
    probs = []
    if not isinstance(nar, dict):
        return ["풀이 글이 JSON object가 아니다"]
    for k in nar:
        if k not in spec and not k.startswith("_"):
            probs.append("모르는 칸 %r (쓸 수 있는 칸: %s)" % (k, ", ".join(spec)))
    for k, lim in spec.items():
        v = nar.get(k)
        if v is None:
            continue
        if isinstance(lim, int):
            if isinstance(v, dict) and k in ("strengths", "weaknesses"):
                for t, s in v.items():
                    if t not in tickers:
                        probs.append("%s에 모르는 종목 %r" % (k, t))
                    elif isinstance(s, str) and len(s) > lim:
                        probs.append("%s.%s가 %d자다(%d자까지)" % (k, t, len(s), lim))
            elif not isinstance(v, str):
                probs.append("%s는 글(string)이어야 한다" % k)
            elif len(v) > lim:
                probs.append("%s가 %d자다(%d자까지)" % (k, len(v), lim))
        elif "count" in lim:
            if not isinstance(v, list):
                probs.append("%s는 목록이어야 한다" % k)
                continue
            if len(v) != lim["count"] and k == "risks":
                probs.append("%s는 %d개여야 한다(지금 %d개)" % (k, lim["count"], len(v)))
            if len(v) > lim["count"]:
                probs.append("%s는 %d개까지다(지금 %d개)" % (k, lim["count"], len(v)))
            for i, it in enumerate(v):
                if k == "risks":
                    if not isinstance(it, dict) or set(it) - {"risk", "trigger"} or not it.get("risk") or not it.get("trigger"):
                        probs.append("risks[%d]는 {risk, trigger} 둘 다 있어야 한다" % i)
                        continue
                    for f in ("risk", "trigger"):
                        if len(it[f]) > lim[f]:
                            probs.append("risks[%d].%s가 %d자다(%d자까지)" % (i, f, len(it[f]), lim[f]))
                elif isinstance(it, str) and len(it) > lim["item"]:
                    probs.append("%s[%d]가 %d자다(%d자까지)" % (k, i, len(it), lim["item"]))
        else:
            if not isinstance(v, dict):
                probs.append("%s는 {%s} object여야 한다" % (k, ", ".join(lim)))
                continue
            for sk, s in v.items():
                if sk not in lim:
                    probs.append("%s에 모르는 칸 %r (쓸 수 있는 칸: %s)" % (k, sk, ", ".join(lim)))
                    continue
                n = len(re.sub(r"\s+", "", s)) if k == "slide_keys" else len(s)
                if n > lim[sk]:
                    probs.append("%s.%s가 %d자다(%d자까지%s)" % (k, sk, n, lim[sk], ", 공백 빼고" if k == "slide_keys" else ""))
    for path, text in narrative_strings(nar):
        if path.startswith("_"):
            continue
        for f in rules["forbidden"]:
            if re.search(f["pattern"], text):
                probs.append("금지어 %r (%s): 결과는 판단 재료이고 권유·목표가·NAV/share를 쓰지 않는다" % (f["name"], path))
        bad = leaks(text)
        if bad:
            probs.append("칸 이름·영문 status %s (%s): 사람 말로 쓴다" % (", ".join(sorted(set(bad))[:3]), path))
        for m in NUM_IN_TEXT.finditer(text):
            n = m.group(1)
            if n not in allowed and fnum(float(n)) not in allowed:
                probs.append("숫자 %s%s가 보고서 숫자에 없다 (%s): metrics 값을 그대로 쓰거나 숫자를 뺀다" % (
                    n, m.group(2), path))
    return probs


def facts_numbers(facts):
    """facts의 공시값(칸, series 줄, parts)."""
    out = []

    def take(f):
        if isinstance(f, dict):
            if isinstance(f.get("value"), (int, float)):
                out.append(f["value"])
            for p in f.get("parts") or []:
                take(p)
    for f in (facts.get("facts") or {}).values():
        take(f)
    for rows in (facts.get("series") or {}).values():
        for f in rows if isinstance(rows, list) else []:
            take(f)
            for k in ("limit", "actual"):
                if isinstance((f or {}).get(k), (int, float)):
                    out.append(f[k])
    return out


def allowed_numbers(docs, extra_texts=(), facts_list=()):
    """풀이 글에 쓸 수 있는 숫자(보고서에 나오는 자리로 맞춘 글자): metrics 값, 등급 기준, 사건 글, facts 공시값."""
    out = set()

    def add(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            return
        out.add(fnum(abs(v)))
        if float(v).is_integer():
            out.add(str(int(abs(v))))
    texts = list(extra_texts)
    for d in docs:
        for _, e in reit_calc.walk(d.get("metrics") or {}):
            v = e.get("value")
            if isinstance(v, list):
                for it in v:
                    if isinstance(it, dict):
                        for f in ("pct", "value", "headroom_pct", "limit", "actual"):
                            add(it.get(f))
            else:
                add(v)
        for a in (d.get("scorecard") or {}).get("axes") or []:
            for e in a.get("evidence") or []:
                texts.append(e.get("criterion") or "")
            texts += (a.get("downgrade") or {}).get("reasons") or []
        for ev in d.get("events") or []:
            texts += [ev.get("title") or "", ev.get("quote") or ""]
        for t in (d.get("thresholds") or {}).values():
            if isinstance(t, dict):
                add(t.get("watch"))
                add(t.get("alert"))
    for f in facts_list:
        for v in facts_numbers(f):
            add(v)
    for t in texts:
        for m in re.finditer(r"\d+(?:\.\d+)?", t or ""):
            out.add(m.group(0))
            out.add(fnum(float(m.group(0))))
    return out


def load_narrative(path, kind):
    if not path:
        return None, "풀이 글 없음: --narrative를 주지 않았다"
    if not os.path.isfile(path):
        return None, "풀이 글 없음: %s 파일이 없다" % path
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), None
    except ValueError as e:
        raise InputError("풀이 글 JSON을 읽지 못함: %s (%s)" % (path, e))


# ---------------------------------------------------------------- 등급 (축별 등급 표)

def crit_segments(crit):
    return [s.strip() for s in (crit or "").split(" (")[0].split(",") if s.strip()]


def crit_seg(crit, gname):
    for s in crit_segments(crit):
        if gname and s.startswith(gname):
            return s[len(gname):].strip()
    return ""


def band_words(seg):
    """'< 75%' → '75% 미만', '≥ 4x' → '4x 이상', '5x~6x'는 그대로."""
    m = re.match(r"^(<|≥|>=|>|≤)\s*(.+)$", seg or "")
    if not m:
        return seg or ""
    return "%s %s" % (m.group(2), {"<": "미만", ">": "초과", "≥": "이상", ">=": "이상", "≤": "이하"}[m.group(1)])


def ev_value(e):
    if e.get("value") is None:
        note = e.get("note") or ""
        for w in ("공시 없음", "가볍게 봄", "견줄 수 없음"):
            if note.startswith(w):
                return w
        return "없음"
    t = fval(e["value"], e.get("unit"), e.get("metric"))
    if e.get("status") in ("estimated", "stale"):
        t += "(%s)" % lab()["status"][e["status"]]
    return t


def axis_view(a):
    """축 하나: 등급, 가장 약한 고리(근거), 기준 구간, 내림, 다음 경계."""
    ev = a.get("evidence") or []
    core = [e for e in ev if e.get("role") == "core"]
    fallback = [e for e in ev if e.get("role") == "fallback" and e.get("value") is not None]
    shown = list(core)
    if not any(e.get("value") is not None for e in core) and fallback:
        shown.append(fallback[0])
    base = a.get("base_grade")
    binding = [e for e in shown if e.get("value") is not None and (e.get("grade_used") or e.get("grade")) == base]
    if a.get("key") == "valuation":
        binding = [e for e in shown if e.get("value") is not None]
    parts, plain = [], []
    for e in shown:
        name = mname(e.get("metric"), e.get("name"))
        txt = "%s %s" % (name, ev_value(e))
        if e.get("grade_name") and e.get("value") is not None:
            txt += " " + e["grade_name"]
        if e in binding and a.get("key") != "valuation":
            seg = crit_seg(e.get("criterion"), e.get("grade_name"))
            parts.append("**%s**%s" % (txt, "(%s 구간 %s)" % (e["grade_name"], band_words(seg)) if seg else ""))
        else:
            parts.append(txt)
        plain.append(txt)
    down = a.get("downgrade") or {}
    if down.get("reasons"):
        parts.append("%d단계 내림: %s" % (down.get("notches", 1), "; ".join(tidy_numbers(r) for r in down["reasons"])))
    first = (binding or shown or [{}])[0]
    return {"key": a.get("key"), "name": a.get("name"), "grade": a.get("grade"), "grade_name": a.get("grade_name"),
            "mark": GRADE_MARK.get(a.get("grade"), "?"), "evidence_md": " · ".join(parts),
            "evidence_plain": " · ".join(plain), "binding_metric": first.get("metric"),
            "binding_text": ("%s %s" % (mname(first.get("metric"), first.get("name")), ev_value(first))) if first else "",
            "binding_short": ev_value(first) if first else "",
            "band": band_words(crit_seg(first.get("criterion"), first.get("grade_name"))) if first else "",
            "gloss": short(first.get("metric") or ""), "trigger": next_boundary(first, a) if first else "",
            "edge": next_edge(first, a) if first else ""}


def next_edge(e, a):
    """장표용 짧은 경계: '6x 이상 → 빠듯'."""
    t = next_boundary(e, a)
    if not t or "이미 위험 구간" in t:
        return "이미 가장 나쁜 구간" if t else ""
    m = re.search(r"(\S+) (이상이면|아래면) (\S+)$", t)
    return "%s %s → %s" % (m.group(1), "이상" if m.group(2) == "이상이면" else "아래", m.group(3)) if m else t


def next_boundary(e, a):
    """등급이 한 단계 나빠지는 경계: 'AFFO payout(forward) 75% 이상이면 보통'."""
    if a.get("key") == "valuation" or e.get("value") is None or a.get("grade") not in GRADE_ORDER:
        return ""
    segs = crit_segments(e.get("criterion"))
    names = [grade_names()[g] for g in GRADE_ORDER]
    cur = e.get("grade_name")
    if cur not in names or len(segs) != 4:
        return ""
    i = names.index(cur)
    name = mname(e.get("metric"), e.get("name"))
    if i == 3:
        return "%s: 이미 위험 구간(%s)" % (name, band_words(crit_seg(e.get("criterion"), cur)))
    seg = crit_seg(e.get("criterion"), cur)
    below = segs[0].split()[1:2] == ["≥"] if len(segs[0].split()) > 1 else False
    nums = re.findall(r"\d+(?:\.\d+)?[%x]?", seg)
    if not nums:
        return ""
    bound = nums[-1] if not below else nums[0]
    if below and "~" in seg:
        bound = nums[0]
    return "%s %s %s %s" % (name, bound, "아래면" if below else "이상이면", names[i + 1])


def grade_names():
    if "_grades" not in _CACHE:
        sc = asset("scorecard.json")
        _CACHE["_grades"] = dict(sc["grades"], **{k: v for k, v in sc["valuation_grades"].items()})
    return _CACHE["_grades"]


# ---------------------------------------------------------------- 단일 보고서 view model (B1, B2, B4, B28)

def build_single_view(main, peers, trend, nar, date, facts, facts_path):
    T, P = main.get("ticker"), main.get("period")
    root = data_root_of(main)
    company = (facts.get("company") or {}).get("name") or T
    filing = facts.get("filing") or {}
    price = (facts.get("facts") or {}).get("price") or {}
    sub = menv(main, "subsector").get("value")
    axes = [axis_view(a) for a in (main.get("scorecard") or {}).get("axes") or []]
    glossed = set()
    for ax in axes:
        if nar and isinstance(nar.get("axes"), dict):
            ax["note"] = nar["axes"].get(ax["key"], "")
        else:
            ax["note"] = ""
        if ax["binding_metric"]:
            glossed.add(ax["binding_metric"])

    keys = []
    for k in KEY_ROWS:
        e = menv(main, k)
        row = {"metric": k, "name": mname(k), "text": plain(e, k), "status": sword(e) if e else "없음",
               "why": reason(e.get("note"))}
        if k == "implied_cap_rate_vs_tracker" and e.get("not_comparable"):
            ic = menv(main, "implied_cap_rate")
            row.update(metric="implied_cap_rate", name=mname("implied_cap_rate"), text=plain(ic, "implied_cap_rate"),
                       status=sword(ic), why="Tracker 값과 견줄 수 없음(회계 기준이 다른 subsector)")
            k = "implied_cap_rate"
        row["gloss"] = "" if k in glossed else short(k)
        glossed.add(k)
        keys.append(row)

    peer_view = single_peers(main, peers, glossed)
    tview = trend_view(trend) if trend else None
    events = event_rows([main])
    risks = []
    if nar and isinstance(nar.get("risks"), list):
        risks = [{"risk": r.get("risk", ""), "trigger": r.get("trigger", "")} for r in nar["risks"]]
    else:
        order = sorted([a for a in axes if a["grade"] in GRADE_ORDER], key=lambda a: -GRADE_ORDER.index(a["grade"]))
        for a in order[:3]:
            risks.append({"risk": "%s %s: %s" % (a["name"], a["grade_name"], a["binding_text"]),
                          "trigger": a["trigger"] or "다음 실적 8-K가 나오면 다시 계산"})
    view = {"schema": VIEW_SCHEMA, "kind": "single", "date": date, "ticker": T, "period": P, "company": company,
            "depth": main.get("depth") or "deep", "subsector": sub,
            "filing": "%s %s(%s 제출)" % (filing.get("form", ""), filing.get("accession", ""), filing.get("filed", "")),
            "price": show(price, unit="USD/share") if price.get("value") is not None else "없음",
            "price_as_of": price.get("as_of") or "", "market_cap": show(menv(main, "market_cap")),
            "narrative_missing": nar is None,
            "conclusion": (nar or {}).get("conclusion") or auto_conclusion(T, axes),
            "axes": axes, "key_numbers": keys, "peers": peer_view, "trend": tview, "events": events,
            "risks": risks, "trend_note": (nar or {}).get("trend_note", ""),
            "slide_titles": (nar or {}).get("slide_titles") or {}, "slide_keys": (nar or {}).get("slide_keys") or {},
            "files": {}, "notes": [],
            "sources": {"metrics": rel_data(main["_file"], root), "facts": rel_data(facts_path, root),
                        "peers": [rel_data(p["_file"], root) for p in peers],
                        "trend": rel_data(trend["_file"], root) if trend else None}}
    view["audit"] = audit_view(main, facts, root)
    view["charts"] = single_charts(main, peers, axes)
    return view


def rel_data(path, root):
    if not path:
        return None
    p = os.path.abspath(path)
    if root and p.startswith(os.path.abspath(root) + os.sep):
        return "$INVEST_DATA_DIR/" + os.path.relpath(p, root)
    return p.replace(os.path.expanduser("~"), "~")


def worst_axes(axes):
    g = [a for a in axes if a["key"] != "valuation" and a["grade"] in GRADE_ORDER]
    if not g:
        return []
    top = max(GRADE_ORDER.index(a["grade"]) for a in g)
    return [a for a in g if GRADE_ORDER.index(a["grade"]) == top]


def auto_conclusion(T, axes):
    g = [a for a in axes if a["key"] != "valuation"]
    head = " · ".join("%s %s" % (a["name"], a["grade_name"]) for a in g)
    w = worst_axes(axes)
    wt = ("가장 약한 축(%s): %s. " % (w[0]["grade_name"], ", ".join("%s(%s)" % (a["name"], a["binding_text"]) for a in w))) if w else ""
    return "%s 등급: %s. %s(풀이 글 없음)" % (T, head, wt)


def peer_value_row(key, docs, subs, main_sub, glossed):
    """docs[0]이 주인공. 셀, 순위(주인공의 몇째), 순위를 매긴 방향."""
    meta = lab()["metrics"].get(key) or {}
    cells, vals = [], []
    for i, d in enumerate(docs):
        e = menv(d, key)
        used = key
        if key == "affo_payout_forward" and e.get("value") is None:
            f = menv(d, "ffo_payout_forward")
            if f.get("value") is not None:
                e, used = f, "ffo_payout_forward"
        t = show(e, key=used)
        if used != key and e.get("value") is not None:
            t += " FFO 기준"
        other_sub = key in RAW_VALUATION and subs[i] != main_sub
        if other_sub and e.get("value") is not None:
            t += " (subsector 다름)"
        cells.append(t)
        vals.append(None if other_sub else num_of(e))
    rank = ""
    order = meta.get("order")
    nums = [v for v in vals if v is not None]
    if order and vals[0] is not None and len(nums) >= 2:
        better = sum(1 for v in nums if (v < vals[0] if order == "asc" else v > vals[0]))
        rank = "%d/%d(%s 순)" % (better + 1, len(nums), "낮은" if order == "asc" else "높은")
    gloss = "" if key in glossed else short(key)
    glossed.add(key)
    return {"metric": key, "name": mname(key), "cells": cells, "rank": rank, "gloss": gloss}


def single_peers(main, peers, glossed):
    docs = [main] + list(peers)
    subs = [menv(d, "subsector").get("value") for d in docs]
    cols = []
    for d, s in zip(docs, subs):
        cols.append({"ticker": d.get("ticker"), "depth": d.get("depth") or "deep",
                     "depth_name": lab()["depth"].get(d.get("depth") or "deep", ""), "subsector": s,
                     "market_cap": show(menv(d, "market_cap")), "same_subsector": s == subs[0]})
    rows = [peer_value_row(k, docs, subs, subs[0], glossed) for k in PEER_ROWS]
    med = menv(main, "peer_median_p_ffo")
    medtxt = ""
    if med.get("value") is not None:
        n = re.search(r"peer (\d+)개", med.get("note") or "")
        bits = ([sword(med)] if skey(med) in ("estimated", "stale") else []) + [
            "%s%s" % (subs[0] or "subsector", " %s개" % n.group(1) if n else "")]
        medtxt = "%s(%s)" % (fval(med["value"], "x"), ", ".join(bits))
    for r in rows:
        r["median"] = medtxt if r["metric"] == "p_ffo_reitwatch" else ("1.0x = median" if r["metric"] == "p_ffo_vs_peer" else "")
    return {"columns": cols, "rows": rows}


def with_unit(label, unit):
    return label[:-1] + ", " + unit + ")" if label.endswith(")") else "%s(%s)" % (label, unit)


def trend_view(trend):
    qs = trend.get("quarters") or []
    rows = []
    for k in TREND_ROWS:
        m = (trend.get("metrics") or {}).get(k)
        if not m:
            continue
        vals = [(m.get("values") or {}).get(q, {}).get("value") for q in qs]
        nums = [v for v in vals if isinstance(v, (int, float))]
        unit = m.get("unit") or ""
        big = nums and max(abs(v) for v in nums) >= 1e6
        if unit == "USD/share":
            label, cells = with_unit(m.get("label", k), "$"), [per_share(v)[1:] if v is not None else "–" for v in vals]
        elif unit == "USD":
            label = with_unit(m.get("label", k), "$M" if big else "$")
            cells = ["–" if v is None else ("{:,.1f}".format(v / 1e6) if big else "{:,.0f}".format(v)) for v in vals]
        elif unit == "shares":
            label = with_unit(m.get("label", k), "백만 주" if big else "주")
            cells = ["–" if v is None else ("{:,.1f}".format(v / 1e6) if big else "{:,.0f}".format(v)) for v in vals]
        else:
            label, cells = m.get("label", k), ["–" if v is None else fnum(v) for v in vals]
        change = None
        if len(nums) >= 2 and nums[0] != 0:
            change = (nums[-1] / nums[0] - 1) * 100
        rows.append({"metric": k, "name": label, "cells": cells, "values": vals, "unit": unit,
                     "change": change, "change_text": ("+" if change and change > 0 else "") + fnum(change) + "%"
                     if change is not None else "–"})
    return {"quarters": qs, "head": [q[2:] for q in qs], "rows": rows,
            "dropped": [d.get("label") or d.get("metric") for d in trend.get("dropped") or []],
            "derived": any(v.get("status") == "derived" for m in (trend.get("metrics") or {}).values()
                           for v in (m.get("values") or {}).values())}


def source_short(src):
    s = src or ""
    m = re.match(r"sec:(\S+)", s)
    if m:
        kind = m.group(1)
        if kind == "8-K":
            return "8-K 보도자료" if re.search(r"ex99", s, flags=re.I) else "8-K"
        return kind
    if s.startswith("web:"):
        host = re.sub(r"^https?://", "", s[4:]).split("/")[0]
        return "웹 " + host
    return s


def event_rows(docs, with_ticker=False, limit=EVENT_MAX):
    rows = []
    for d in docs:
        for e in d.get("events") or []:
            rows.append({"ticker": d.get("ticker"), "date": e.get("date"), "type_name": e.get("type_name") or e.get("type"),
                         "title": e.get("title"), "source": source_short(e.get("source")), "quote": e.get("quote"),
                         "locator": e.get("locator"), "url": e.get("url") or (
                             e.get("source", "")[4:] if e.get("source", "").startswith("web:") else ""),
                         "full_source": e.get("source")})
    rows.sort(key=lambda r: (r["date"] or "", r["ticker"] or ""), reverse=True)
    return rows[:limit]


# ---------------------------------------------------------------- 부록 view (B2)

def audit_view(doc, facts, root):
    F = facts.get("facts") or {}
    M = doc.get("metrics") or {}
    ffo_rows = []
    for k, sign in (("net_income_common", ""), ("depreciation_amortization", "+"), ("non_real_estate_depreciation", "−"),
                    ("impairment_depreciable_re", "+"), ("gain_on_sale_re", "−"), ("unconsolidated_jv_ffo_adjustments", "+"),
                    ("nci_ffo_adjustments", "+"), ("other_nareit_adjustments", "+")):
        f = F.get(k) or {}
        ffo_rows.append({"name": (sign + " " if sign else "") + lab()["facts"].get(k, k), "value": fact_text(f),
                         "unit": f.get("unit", ""), "status": sword(f), "locator": loc_text(f, root)})
    checks = {c.get("check"): c for c in doc.get("checks") or []}
    tie = checks.get("ffo_tie_out") or {}
    bridge = (facts.get("affo_bridge") or {})
    brows = []
    if bridge.get("start"):
        s = F.get(bridge["start"]) or {}
        brows.append({"name": "시작: " + lab()["facts"].get(bridge["start"], bridge["start"]), "category": "",
                      "value": fact_text(s), "locator": loc_text(s, root)})
    cats = {"straight_line": "straight-line", "recurring_capex": "유지 capex", "leasing_cost": "leasing cost",
            "non_cash": "비현금", "other": "기타"}
    for it in bridge.get("items") or []:
        brows.append({"name": it.get("label", ""), "category": cats.get(it.get("category"), it.get("category") or ""),
                      "value": fact_text(it), "locator": loc_text(it, root)})
    ab = checks.get("affo_bridge") or {}
    ladder = (M.get("maturity_ladder") or {})
    mrows = [{"period": "그 뒤" if r.get("period") == "thereafter" else r.get("period"), "value": full_k(r.get("value")),
              "pct": fnum(r["pct"]) + "%" if isinstance(r.get("pct"), (int, float)) else "–"}
             for r in ladder_sorted(ladder.get("value"))]
    cov = M.get("covenant_headroom") or {}
    crows = [{"name": c.get("name"), "dir": "최대" if c.get("direction") == "max" else "최소",
              "limit": "–" if c.get("limit") is None else fnum(c["limit"]) + (c.get("unit") or ""),
              "actual": "–" if c.get("actual") is None else fnum(c["actual"]) + (c.get("unit") or ""),
              "headroom": "–" if c.get("headroom_pct") is None else fnum(c["headroom_pct"]) + "%"}
             for c in (cov.get("value") or []) if isinstance(c, dict)]
    cap_keys = ("market_cap", "net_debt", "jv_debt_pro_rata", "non_operating_assets", "economic_ev", "capex_reserve",
                "noi_economic", "debt_fair_value", "debt_carrying_value", "debt_mtm_adjustment", "implied_cap_rate",
                "economic_implied_cap_rate", "economic_implied_cap_rate_debt_mtm", "tracker_subsector_cap_rate",
                "implied_cap_rate_vs_tracker", "acquisitions_cap_rate", "dispositions_cap_rate")
    caprows = [{"name": mname(k), "value": audit_value(M.get(k) or {}), "status": sword(M.get(k) or {}),
                "note": humanize((M.get(k) or {}).get("note"), root)[:160]} for k in cap_keys if k in M]
    gaps = [{"item": mname(g.get("item", ""), lab()["facts"].get(g.get("item", ""), g.get("item", ""))),
             "status": lab()["status"].get(g.get("status"), "실패" if g.get("status") == "fail" else g.get("status")),
             "reason": humanize(g.get("reason"), root)} for g in doc.get("data_gaps") or []]
    allm = []
    for k, e in reit_calc.walk(M):
        if k in ("maturity_ladder", "covenant_headroom"):
            continue
        name = mname(k, (e.get("note") or k.split(".")[-1].replace("_", " ")) if k.startswith("sector_kpis.") else None)
        allm.append({"name": name, "value": audit_value(e), "status": sword(e), "formula": humanize(e.get("formula"), root),
                     "as_of": e.get("as_of") or ""})
    srcs = []
    for k, f in F.items():
        if isinstance(f, dict) and (f.get("source") or f.get("parts")):
            srcs.append({"name": lab()["facts"].get(k, k.replace("_", " ")), "value": fact_text(f),
                         "source": f.get("source") or "parts", "locator": loc_text(f, root)})
    axes_full = []
    for a in (doc.get("scorecard") or {}).get("axes") or []:
        for e in a.get("evidence") or []:
            axes_full.append({"axis": a.get("name"), "metric": mname(e.get("metric"), e.get("name")),
                              "role": {"core": "핵심", "fallback": "대신", "downgrade": "내림", "side": "곁"}.get(e.get("role"), ""),
                              "value": ev_value(e), "grade": e.get("grade_name") or "",
                              "criterion": tidy_numbers(e.get("criterion") or ""), "note": humanize(e.get("note"), root)[:120]})
    return {"ffo": {"rows": ffo_rows, "recomputed": full_k(tie.get("recomputed")), "reported": full_k(tie.get("reported")),
                    "diff": "–" if tie.get("diff_pct") is None else fnum(tie["diff_pct"]) + "%",
                    "pass": tie.get("pass"), "note": humanize(tie.get("note"), root)},
            "bridge": {"rows": brows, "sum": full_k(ab.get("sum")), "reported": full_k(ab.get("reported")),
                       "diff": "–" if ab.get("diff_pct") is None else fnum(ab["diff_pct"]) + "%", "pass": ab.get("pass"),
                       "note": humanize(ab.get("note"), root)},
            "ladder": mrows, "ladder_note": "", "ladder_total": full_k(sum(r.get("value") or 0 for r in (ladder.get("value") or []))), "covenants": crows,
            "covenant_note": humanize(cov.get("note"), root) if not crows else "", "cap": caprows, "gaps": gaps,
            "all": allm, "sources": srcs, "axes": axes_full,
            "events": [{"date": e.get("date"), "title": e.get("title"), "quote": e.get("quote"), "source": e.get("source"),
                        "locator": e.get("locator") or "", "url": e.get("url") or ""} for e in doc.get("events") or []]}


def fact_text(f):
    if not isinstance(f, dict):
        return "–"
    if f.get("parts"):
        return " + ".join(fact_text(p) for p in f["parts"])
    if f.get("value") is None:
        return "공시 없음" if f.get("not_disclosed") else "없음"
    v = f["value"]
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return "{:,}".format(v) if float(v).is_integer() else "{:,}".format(round(v, 6))
    return str(v)


def loc_text(f, root):
    if not isinstance(f, dict):
        return ""
    if f.get("parts"):
        return " · ".join(loc_text(p, root) for p in f["parts"])
    s = (f.get("locator") or "")
    if root:
        s = s.replace(root.rstrip("/"), "$INVEST_DATA_DIR")
    return s.replace(os.path.expanduser("~"), "~")[:180]


def audit_value(e):
    v = e.get("value")
    if v is None:
        return sword(e)
    if isinstance(v, list):
        if v and isinstance(v[0], dict):
            return "%d줄(아래 표)" % len(v)
        return fval(v, e.get("unit"))
    if e.get("unit") == "USD thousands":
        return full_k(v) + "천 달러"
    return fval(v, e.get("unit"), e.get("metric"))


# ---------------------------------------------------------------- 장표 (B5)

def ladder_sorted(rows):
    return sorted(rows or [], key=lambda r: (str(r.get("period")) == "thereafter", str(r.get("period"))))


def single_charts(main, peers, axes):
    ladder = ladder_sorted(menv(main, "maturity_ladder").get("value"))
    bars, omitted = [], ""
    for r in ladder:
        if isinstance(r.get("pct"), (int, float)):
            bars.append({"label": "그 뒤(합)" if r.get("period") == "thereafter" else "%s년" % r.get("period"), "value": r["pct"]})
    if len(bars) > 10:
        y0 = int(str(main.get("period") or "0000")[:4] or 0)
        near = [b for b in bars if b["label"][:4].isdigit() and int(b["label"][:4]) <= y0 + 10][:9]
        tail = bars[len(near):]
        if max(b["value"] for b in tail) <= max(b["value"] for b in near):
            omitted = "%s~%s %d개 해(각 %s%% 이하)는 뺐다" % (tail[0]["label"], tail[-1]["label"], len(tail),
                                                     fnum(max(b["value"] for b in tail)))
            bars = near
        else:
            bars = near + [{"label": "%s년 이후 %d개 해 합" % (tail[0]["label"][:4] if tail[0]["label"][:4].isdigit()
                                                           else "그 뒤", len(tail)),
                            "value": sum(b["value"] for b in tail), "group": True}]
    th = (main.get("thresholds") or {}).get("maturity_single_year_pct") or {}
    years = [i for i, b in enumerate(bars) if not b.get("group") and b["label"][:4].isdigit()]
    if years:
        top = max(years, key=lambda i: bars[i]["value"])
        bars[top]["mark"] = "bad" if th.get("alert") is not None and bars[top]["value"] >= th["alert"] else "hl"
    pay = []
    for d in [main] + list(peers):
        e, label = menv(d, "affo_payout_forward"), "AFFO"
        if e.get("value") is None and menv(d, "ffo_payout_forward").get("value") is not None:
            e, label = menv(d, "ffo_payout_forward"), "FFO"
        if e.get("value") is not None:
            pay.append({"label": d.get("ticker") + ("" if d is main else " " + lab()["depth"].get(d.get("depth") or "deep")) + (
                " FFO 기준" if label == "FFO" else ""),
                        "value": e["value"], "status": skey(e), "mark": "hl" if d is main else ""})
    pth = (main.get("thresholds") or {}).get("affo_payout_pct") or {}
    return {"maturity": bars, "maturity_omitted": omitted, "maturity_watch": th.get("watch"), "payout": pay, "payout_watch": pth.get("watch"),
            "grades": [{"name": a["name"], "grade": a["grade"], "grade_name": a["grade_name"], "mark": a["mark"],
                        "binding": a["binding_text"], "band": a["band"], "edge": a["edge"]} for a in axes]}


def slide_html(title, subtitle, diagram, key, source):
    return ("<h1>%s</h1>\n<p class=\"subtitle\">%s</p>\n<div class=\"stage\">\n<div class=\"diagram\">\n%s\n</div>\n"
            "<div class=\"panel\">\n<p class=\"key\">%s</p>\n</div>\n</div>\n<p class=\"source\"><b>근거</b> %s</p>\n") % (
        html.escape(title), html.escape(subtitle), diagram, html.escape(key), html.escape(source))


def bar_block(bars, unit, ref=None, ref_name=""):
    """값 막대(diagram-slides bar). 기준선이 어느 막대의 값 글자를 지나가면(render_slide.py가 막는다) 선을 빼고
    (html, 기준선을 그렸는지)를 돌려준다. 그 기준은 부제에 글로 적는다."""
    lines = []
    for b in bars:
        name = b["label"].replace(",", " ")
        lines.append("%s, %s%s" % (name, fnum(b["value"]), ", " + b["mark"] if b.get("mark") else ""))
    drawn = False
    if ref is not None:
        top = max([b["value"] for b in bars] + [ref])
        if not any(ref - 0.12 * top <= b["value"] < ref for b in bars):
            lines.append("| %s, %s" % (fthr(ref), ref_name))
            drawn = True
    return "<pre class=\"bar\" data-unit=\"%s\">\n%s\n</pre>" % (html.escape(unit), "\n".join(lines)), drawn


def single_slides(view):
    T, P = view["ticker"], view["period"]
    st, sk = view["slide_titles"], view["slide_keys"]
    ch = view["charts"]
    out = []
    rows = []
    for g in ch["grades"]:
        cls = " class=\"bad\"" if g["grade"] == "risk" else (" class=\"no\"" if g["grade"] == "unknown" else "")
        rows.append("<tr><th>%s</th><td%s>%s %s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            html.escape(g["name"]), cls, g["mark"], html.escape(g["grade_name"]), html.escape(g["binding"]),
            html.escape(g["band"] or "–"), html.escape(g["edge"] or "–")))
    table = ("<table class=\"matrix\">\n<colgroup><col><col><col><col><col></colgroup>\n<thead><tr><th></th><th>등급</th>"
             "<th>가장 약한 고리</th><th>그 등급 구간</th><th>한 단계 나빠지는 곳</th></tr></thead>\n%s\n</table>") % "\n".join(rows)
    counts = {}
    for g in ch["grades"]:
        if g["grade"] in GRADE_ORDER:
            counts[g["grade_name"]] = counts.get(g["grade_name"], 0) + 1
    auto_t = "%s 등급: %s" % (T, " · ".join("%s %d" % (k, v) for k, v in counts.items()))
    w = worst_axes(view["axes"])
    auto_k = ("가장 약한 축(%s): %s." % (w[0]["grade_name"], ", ".join(a["name"] for a in w))) if w else "등급을 매길 근거가 모자라다."
    out.append({"name": "grades", "title": st.get("grades") or auto_t, "key": sk.get("grades") or auto_k,
                "html": slide_html(st.get("grades") or auto_t, "축마다 가장 약한 지표로 매긴 등급은? · %s %s 공시, 경험칙 기준표" % (T, P),
                                   table, sk.get("grades") or auto_k, "metrics %s scorecard · assets/scorecard.json" % P)})
    if ch["maturity"]:
        top = max(ch["maturity"], key=lambda b: b["value"] if not b.get("group") else -1)
        auto_t = "%s의 가장 큰 만기 해는 %s %s%%" % (T, top["label"], fnum(top["value"]))
        auto_k = "한 해 만기 비중이 %s%%를 넘으면 그해 차환 금리에 크게 기댄다(경험칙)." % fthr(ch["maturity_watch"] or 15)
        block, drawn = bar_block(ch["maturity"], "%", ch["maturity_watch"], "주의 기준")
        sub = "해마다 갚을 원금이 얼마나 몰렸나? · 만기 사다리(원금 비중)" + (
            " · " + ch["maturity_omitted"] if ch.get("maturity_omitted") else "") + (
            "" if drawn or ch["maturity_watch"] is None else " · 주의 기준 %s%%(경험칙)" % fthr(ch["maturity_watch"]))
        out.append({"name": "maturity", "title": st.get("maturity") or auto_t, "key": sk.get("maturity") or auto_k,
                    "html": slide_html(st.get("maturity") or auto_t, sub, block,
                                       sk.get("maturity") or auto_k, "metrics %s maturity_ladder" % P)})
    else:
        view["notes"].append("만기 사다리가 없어 만기 장표를 만들지 않았다")
    if ch["payout"]:
        vals = [b["value"] for b in ch["payout"]]
        auto_t = "%s payout %s%%, 경쟁 종목 %s~%s%%" % (T, fnum(vals[0]), fnum(min(vals[1:] or vals)), fnum(max(vals[1:] or vals)))
        auto_k = "payout이 낮을수록 배당 뒤 남는 AFFO가 많다. %s%% 넘으면 주의(경험칙)." % fthr(ch["payout_watch"] or 85)
        block, drawn = bar_block(ch["payout"], "%", ch["payout_watch"], "주의 기준")
        sub = "배당이 AFFO로 얼마나 덮이나? · forward payout, 경쟁 종목은 숫자만" + (
            "" if drawn or ch["payout_watch"] is None else " · 주의 기준 %s%%(경험칙)" % fthr(ch["payout_watch"]))
        out.append({"name": "dividend", "title": st.get("dividend") or auto_t, "key": sk.get("dividend") or auto_k,
                    "html": slide_html(st.get("dividend") or auto_t, sub, block,
                                       sk.get("dividend") or auto_k, "metrics %s affo_payout_forward" % P)})
    else:
        view["notes"].append("payout 값이 없어 배당 여력 장표를 만들지 않았다")
    return out


def find_diagram_slides():
    env = os.environ.get("DIAGRAM_SLIDES_DIR")
    cands = [env] if env is not None else [os.path.join(os.path.dirname(SKILL), "diagram-slides"),
                                          os.path.expanduser("~/.claude/skills/diagram-slides")]
    for c in cands:
        if c and os.path.isfile(os.path.join(c, "scripts", "render_slide.py")):
            return c
    return None


CHROME_APPS = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
               "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
               "/Applications/Chromium.app/Contents/MacOS/Chromium",
               "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
CHROME_COMMANDS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge")


def find_chrome():
    """CHROME_PATH, 기본 설치 위치, PATH 차례. 없으면 None."""
    import shutil
    custom = os.environ.get("CHROME_PATH")
    if custom is not None:
        p = os.path.expanduser(custom)
        return p if os.path.isfile(p) else None
    for app in CHROME_APPS:
        p = os.path.expanduser(app)
        if os.path.isfile(p):
            return p
    for c in CHROME_COMMANDS:
        f = shutil.which(c)
        if f:
            return f
    return None


def write_slides(slides, out_dir, stem, view):
    """장표 HTML을 쓰고 diagram-slides가 있으면 굽는다. 실패한 장표 수."""
    sdir = os.path.join(out_dir, "slides")
    os.makedirs(sdir, exist_ok=True)
    ds = find_diagram_slides()
    failed = 0
    for i, s in enumerate(slides, 1):
        base = os.path.join(sdir, "%s-%02d-%s" % (stem, i, s["name"]))
        with open(base + ".html", "w", encoding="utf-8") as f:
            f.write(s["html"])
        s.update(html_path=base + ".html", png_path=None, status="HTML만")
        if ds is None:
            continue
        try:
            r = subprocess.run([sys.executable, os.path.join(ds, "scripts", "render_slide.py"), base + ".html", base + ".png"],
                               capture_output=True, text=True, timeout=180)
            code, err = r.returncode, (r.stderr or "").strip()
        except (OSError, subprocess.TimeoutExpired) as e:
            code, err = 1, str(e)
        if code == 0 and os.path.isfile(base + ".png"):
            s.update(png_path=base + ".png", status="구움")
        else:
            failed += 1
            lines = [x.strip(" -") for x in err.split("\n") if x.strip()]
            s["status"] = ("규칙 위반: " if code == 2 else "굽지 못함: ") + (lines[1] if code == 2 and len(lines) > 1
                                                                          else (lines[0] if lines else "이유 없음"))
            log("장표 %s %s" % (os.path.basename(base), s["status"]))
    if ds is None:
        view["notes"].append("장표 PNG를 굽지 않았다(diagram-slides 없음). HTML은 slides 폴더에 있다")
    view["slides"] = [{k: s.get(k) for k in ("name", "title", "key", "html_path", "png_path", "status")} for s in slides]
    return failed


# ---------------------------------------------------------------- 용어 표

def glossary(text):
    out = []
    for term, t in lab()["terms"].items():
        keys = [term] + list(t.get("match") or [])
        if any((k.lower() in text.lower()) if re.match(r"^[\x00-\x7f]+$", k) else (k in text) for k in keys):
            out.append((term, t["text"]))
    return out


def glossary_md(text):
    rows = glossary(text)
    return [GLOSSARY_HEAD, ""] + md_table(["용어", "뜻"], rows) if rows else [GLOSSARY_HEAD, "", "(없음)"]


# ---------------------------------------------------------------- 단일 보고서 md

def link(path, out_dir):
    return os.path.relpath(path, out_dir) if path else ""


def render_single_md(view, out_dir):
    T = view["ticker"]
    L = ["# %s · %s · %s 읽는 보고서" % (T, view["company"], view["period"]), ""]
    L.append("**결론** " + view["conclusion"])
    L.append("")
    sl = [s for s in view.get("slides") or []]
    slide_txt = " · ".join("[%s](%s)" % ({"grades": "등급", "maturity": "만기", "dividend": "배당 여력"}.get(s["name"], s["name"]),
                                          link(s["png_path"] or s["html_path"], out_dir)) for s in sl)
    meta = ["%s 작성" % view["date"], "%s %s" % (view["period"], view["filing"]),
            "주가 %s(%s 종가)" % (view["price"], view["price_as_of"]), "시총 %s" % view["market_cap"],
            "subsector %s" % (view["subsector"] or "모름"), "%s 봄" % lab()["depth"].get(view["depth"], view["depth"]),
            "[부록](%s)" % link(view["files"].get("audit"), out_dir)]
    if slide_txt:
        meta.append("장표 " + slide_txt)
    meta += view["notes"]
    meta.append("경험칙 등급이며 매수·매도 권유가 아니다")
    L.append(" · ".join(meta))
    L.append("")
    L.append("## 축별 등급")
    L += md_table(["축", "등급", "근거(굵은 것이 가장 약한 고리)", "그 지표 뜻", "풀이"],
                  [[a["name"], a["grade_name"], a["evidence_md"], a["gloss"], a["note"]] for a in view["axes"]])
    L.append("")
    L.append("## 핵심 숫자")
    L += md_table(["지표", "값", "상태", "뜻"],
                  [[r["name"], r["text"], r["status"] + (" · " + r["why"] if r["why"] else ""), r["gloss"]]
                   for r in view["key_numbers"]])
    L.append("")
    pv = view["peers"]
    L.append("## 동종 대비 위치")
    head = ["지표"] + ["%s %s · %s" % (c["ticker"], c["depth_name"], c["market_cap"]) for c in pv["columns"]] + [
        "%s 순위" % T, "subsector median"]
    L += md_table(head, [[r["name"]] + r["cells"] + [r["rank"] or "–", r["median"] or "–"] for r in pv["rows"]])
    others = [c for c in pv["columns"][1:]]
    note = (view.get("peer_note") or "")
    auto = ("경쟁 종목에는 등급을 매기지 않고 숫자만 둔다(깊이: %s)" % ", ".join(
        "%s %s" % (c["ticker"], c["depth_name"]) for c in others) + (
        ". 깊게 본 종목의 등급은 그 종목 보고서에 있다" if any(c["depth"] == "deep" for c in others) else "")) if others else "경쟁 종목 없음"
    diff = [c["ticker"] for c in others if not c["same_subsector"]]
    if diff:
        auto += ". %s의 subsector(%s)가 달라 P/FFO 순위에서 뺐다" % ("·".join(diff), "·".join(
            c["subsector"] or "모름" for c in others if not c["same_subsector"]))
    L.append((note + ". " if note and not note.endswith(".") else note + " " if note else "") + auto + ".")
    L.append("")
    tv = view["trend"]
    L.append("## 8분기 추세")
    if tv and tv["rows"]:
        L += md_table(["지표"] + tv["head"] + ["처음→끝"], [[r["name"]] + r["cells"] + [r["change_text"]] for r in tv["rows"]])
        tn = []
        if view["trend_note"]:
            tn.append(view["trend_note"])
        if tv["dropped"]:
            tn.append("값이 4분기보다 적어 뺀 지표: %s" % "·".join(tv["dropped"]))
        tn.append("SEC XBRL 분기 값(일부는 누적 차이로 푼 계산값)")
        L.append(". ".join(tn))
    else:
        L.append("추세 표가 없다(edgar.py trend를 돌리지 않았다)")
    L.append("")
    L.append("## 주요 사건")
    if view["events"]:
        L += ["- %s · %s · %s (%s)" % (e["date"], e["type_name"], e["title"], e["source"]) for e in view["events"]]
    else:
        L.append("- facts에 적힌 사건이 없다")
    L.append("")
    L.append("## 위험과 다시 볼 조건")
    L += md_table(["위험", "다시 볼 조건"], [[r["risk"], r["trigger"]] for r in view["risks"]])
    body = "\n".join(L)
    L.append("")
    L += glossary_md(body)
    return "\n".join(L) + "\n"


def render_audit_md(view, out_dir):
    a = view["audit"]
    T, P = view["ticker"], view["period"]
    L = ["# %s %s 부록: 계산 검증과 출처 (%s)" % (T, P, view["date"]), ""]
    L.append("읽는 보고서: [%s](%s) · metrics: %s · facts: %s · 공시: %s" % (
        os.path.basename(view["files"].get("report", "")), link(view["files"].get("report"), out_dir),
        view["sources"]["metrics"], view["sources"]["facts"], view["filing"]))
    L.append("")
    L += audit_sections(a)
    return "\n".join(L) + "\n"


def audit_sections(a, level="##"):
    L = ["%s FFO 재계산(Nareit, 분기)" % level, ""]
    L += md_table(["줄", "값", "단위", "상태", "출처 위치"], [[r["name"], r["value"], r["unit"], r["status"], r["locator"]]
                                                       for r in a["ffo"]["rows"]])
    f = a["ffo"]
    L.append("")
    L.append("재계산 %s · 회사 FFO %s · 차이 %s → %s%s" % (f["recomputed"], f["reported"], f["diff"],
                                                     {True: "맞음(0.5% 안)", False: "다름(0.5% 넘음)", None: "재지 않음"}[f["pass"]],
                                                     (" · " + f["note"]) if f["note"] else ""))
    L += ["", "%s AFFO bridge(회사 정의)" % level, ""]
    if a["bridge"]["rows"]:
        L += md_table(["줄", "종류", "값", "출처 위치"], [[r["name"], r["category"], r["value"], r["locator"]] for r in a["bridge"]["rows"]])
        b = a["bridge"]
        L += ["", "합 %s · 회사 AFFO %s · 차이 %s → %s" % (b["sum"], b["reported"], b["diff"], {
            True: "맞음", False: "다름", None: "재지 않음(%s)" % (b["note"] or "AFFO 없음")}[b["pass"]])]
    else:
        L.append("AFFO bridge가 없다(회사가 AFFO를 내지 않거나 가볍게 봄)")
    L += ["", "%s 만기 사다리(원금, 천 달러)" % level, ""]
    if a["ladder"]:
        L += md_table(["해", "원금", "비중"], [[r["period"], r["value"], r["pct"]] for r in a["ladder"]] + [
            ["합", a["ladder_total"], ""]])
    else:
        L.append("만기 사다리가 없다")
    L += ["", "%s covenant(회사 정의)" % level, ""]
    if a["covenants"]:
        L += md_table(["covenant", "방향", "한도", "실제", "headroom"],
                      [[r["name"], r["dir"], r["limit"], r["actual"], r["headroom"]] for r in a["covenants"]])
    else:
        L.append("covenant 표가 없다. " + (a["covenant_note"] or ""))
    L += ["", "%s cap rate 기준선과 economic EV 다리" % level, ""]
    L += md_table(["칸", "값", "상태", "메모"], [[r["name"], r["value"], r["status"], r["note"]] for r in a["cap"]])
    L += ["", "%s 등급 근거 전부" % level, ""]
    L += md_table(["축", "지표", "쓰임", "값", "등급", "기준", "메모"],
                  [[r["axis"], r["metric"], r["role"], r["value"], r["grade"], r["criterion"], r["note"]] for r in a["axes"]])
    L += ["", "%s 사건 원문" % level, ""]
    if a["events"]:
        L += md_table(["날짜", "사건", "원문", "출처", "위치"],
                      [[e["date"], e["title"], e["quote"], e["source"], e["url"] or e["locator"]] for e in a["events"]])
    else:
        L.append("facts에 적힌 사건이 없다")
    L += ["", "%s 데이터 구멍" % level, ""]
    L += md_table(["항목", "상태", "이유"], [[g["item"], g["status"], g["reason"]] for g in a["gaps"]]) if a["gaps"] else ["없음"]
    L += ["", "%s 모든 지표" % level, ""]
    L += md_table(["지표", "값", "상태", "식", "기준일"], [[r["name"], r["value"], r["status"], r["formula"], r["as_of"]] for r in a["all"]])
    L += ["", "%s facts 출처 위치" % level, ""]
    L += md_table(["칸", "값", "출처", "위치"], [[r["name"], r["value"], r["source"], r["locator"]] for r in a["sources"]])
    return L


# ---------------------------------------------------------------- build (단일)

def today(args_date):
    if args_date:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", args_date):
            raise InputError("--date는 YYYY-MM-DD")
        return args_date
    return datetime.date.today().isoformat()


def finish_md(md, what):
    """보고서 자체 검사: 줄 수와 칸 이름 누출. 문제 목록."""
    probs = []
    n = len(body_lines(md))
    if n > MAX_LINES:
        probs.append("%s가 용어 표를 빼고 %d줄이다(%d줄까지)" % (what, n, MAX_LINES))
    bad = leaks("\n".join(body_lines(md)))
    if bad:
        probs.append("%s 본문에 칸 이름·영문 status가 남았다: %s" % (what, ", ".join(sorted(set(bad))[:5])))
    return probs


def write_text(path, text):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def cmd_build(args):
    try:
        date = today(args.date)
        main_doc = read_doc(args.metrics, reit_calc.METRICS_SCHEMA)
        if main_doc.get("path") == "dividend":
            raise InputError("일반 배당주 metrics는 report.py가 아직 보고서를 만들지 않는다(SKILL.md의 일반 배당주 경로)")
        peers = [read_doc(p, reit_calc.METRICS_SCHEMA) for p in args.peers or []]
        trend = read_doc(args.trend, "reit-trend/1") if args.trend else None
        nar, why = load_narrative(args.narrative, "single")
    except InputError as e:
        log(str(e))
        return 1
    facts, facts_path = read_facts(main_doc)
    if nar is not None:
        probs = check_narrative(nar, "single", allowed_numbers([main_doc] + peers, trend_numbers(trend),
                                                               [facts] + [read_facts(p)[0] for p in peers]))
        if probs:
            log("풀이 글 검사에 걸려 아무 파일도 쓰지 않았다(%s):" % args.narrative)
            for p in probs:
                log("  - " + p)
            return 2
    view = build_single_view(main_doc, peers, trend, nar, date, facts, facts_path)
    if nar:
        view["peer_note"] = nar.get("peer_position", "")
    out = os.path.abspath(args.out_dir)
    stem = "%s-deep" % date
    view["files"] = {"report": os.path.join(out, stem + ".md"), "audit": os.path.join(out, stem + "-audit.md"),
                     "view": os.path.join(out, stem + ".view.json")}
    failed = write_slides(single_slides(view), out, "%s-%s" % (date, view["ticker"]), view)
    md = render_single_md(view, out)
    write_text(view["files"]["report"], md)
    write_text(view["files"]["audit"], render_audit_md(view, out))
    write_text(view["files"]["view"], json.dumps(view, ensure_ascii=False, indent=1) + "\n")
    probs = finish_md(md, "읽는 보고서")
    for p in probs:
        log(p)
    if why:
        log(why + ". 숫자로 만들 수 있는 부분만 채웠다")
    print(view["files"]["report"])
    return 2 if (why or failed or probs) else 0


def trend_numbers(trend):
    if not trend:
        return []
    tv = trend_view(trend)
    return [r["change_text"] for r in tv["rows"]]


# ---------------------------------------------------------------- earnings (B19)

def grade_rank(g):
    return GRADE_ORDER.index(g) if g in GRADE_ORDER else None


def build_earnings_view(diff, prev, curr, nar, date):
    T = curr.get("ticker")
    pa = {a["key"]: a for a in (prev.get("scorecard") or {}).get("axes") or []}
    worse, better, axes = [], [], []
    for a in (curr.get("scorecard") or {}).get("axes") or []:
        b = pa.get(a["key"]) or {}
        row = {"kind": "등급", "name": a["name"], "prev": b.get("grade_name") or "없음", "curr": a.get("grade_name"),
               "change": ""}
        axes.append(row)
        r0, r1 = grade_rank(b.get("grade")), grade_rank(a.get("grade"))
        if r0 is not None and r1 is not None and r0 != r1:
            row["change"] = "%d단계 %s" % (abs(r1 - r0), "나빠짐" if r1 > r0 else "좋아짐")
            (worse if r1 > r0 else better).append(dict(row, order=0, size=abs(r1 - r0)))
        elif a.get("key") == "valuation" and b.get("grade") != a.get("grade"):
            row["change"] = "위치 바뀜"
    for f in diff.get("flags") or []:
        if f.get("change") in ("worse", "better"):
            (worse if f["change"] == "worse" else better).append(
                {"kind": "검사", "name": FLAG_NAMES.get(f["flag"], f["flag"]), "prev": FLAG_LEVEL.get(f["prev"], f["prev"]),
                 "curr": FLAG_LEVEL.get(f["curr"], f["curr"]), "change": "나빠짐" if f["change"] == "worse" else "좋아짐",
                 "order": 1, "size": 1})
    for r in diff.get("rows") or []:
        k = r["metric"]
        meta = lab()["metrics"].get(k) or {}
        pv, cv = r["prev"].get("value"), r["curr"].get("value")
        unit = r.get("unit")
        item = {"kind": "지표", "name": mname(k), "prev": show({"value": pv, "status": r["prev"].get("status"), "unit": unit}, key=k),
                "curr": show({"value": cv, "status": r["curr"].get("status"), "unit": unit}, key=k), "order": 2}
        if r.get("status_change") == "worse" and k in lab()["metrics"]:
            worse.append(dict(item, change="상태 %s → %s" % (lab()["status"].get(r["prev"].get("status"), ""),
                                                             lab()["status"].get(r["curr"].get("status"), "")), size=0.5))
            continue
        if meta.get("bad") and isinstance(r.get("change"), (int, float)) and r["change"] != 0:
            up = r["change"] > 0
            bad = (up and meta["bad"] == "high") or (not up and meta["bad"] == "low")
            ch = fval(r["change"], "%p" if r.get("change_unit") == "%p" else unit, k)
            if r.get("change_unit") != "%p" and r.get("change_pct") is not None:
                ch = ("+" if r["change"] > 0 else "") + ch + " (%s%s%%)" % ("+" if r["change_pct"] > 0 else "", fnum(r["change_pct"]))
            size = abs(r["change_pct"]) / 100 if r.get("change_pct") is not None else abs(r["change"]) / 10
            (worse if bad else better).append(dict(item, change=ch, size=size))
    key = lambda x: (x["order"], -x["size"])
    worse.sort(key=key)
    better.sort(key=key)
    return {"schema": VIEW_SCHEMA, "kind": "earnings", "date": date, "ticker": T, "prev_period": diff.get("prev_period"),
            "curr_period": diff.get("curr_period"), "worse": worse[:10], "better": better[:6], "axes": axes,
            "conclusion": (nar or {}).get("conclusion") or "%s %s → %s: 나빠진 것 %d개, 좋아진 것 %d개(풀이 글 없음)" % (
                T, diff.get("prev_period"), diff.get("curr_period"), len(worse), len(better)),
            "revisit": (nar or {}).get("revisit") or [], "narrative_missing": nar is None,
            "summary": diff.get("summary") or {}}


FLAG_NAMES = {"affo_payout": "AFFO payout 검사", "maturity_concentration": "만기 집중 검사", "net_debt_to_ebitdare": "부채 배수 검사",
              "interest_coverage": "이자 보상 검사", "covenant_headroom": "covenant 여유 검사", "eps_payout": "EPS payout 검사",
              "fcf_payout": "FCF payout 검사", "chowder": "Chowder 검사", "yield_trap": "yield trap 검사"}
FLAG_LEVEL = {"ok": "통과", "watch": "주의", "alert": "경고", "review": "판정 못 함"}


def render_earnings_md(v):
    L = ["# %s %s → %s 실적 뒤 점검" % (v["ticker"], v["prev_period"], v["curr_period"]), ""]
    L.append("**결론** " + v["conclusion"])
    L += ["", "%s 작성 · 나빠진 것이 먼저 · 경험칙 등급·검사이며 매수·매도 권유가 아니다" % v["date"], ""]
    head = ["구분", "무엇", v["prev_period"], v["curr_period"], "변화"]
    L.append("## 나빠진 것")
    L += md_table(head, [[r["kind"], r["name"], r["prev"], r["curr"], r["change"]] for r in v["worse"]]) if v["worse"] else ["- 없음"]
    L += ["", "## 좋아진 것"]
    L += md_table(head, [[r["kind"], r["name"], r["prev"], r["curr"], r["change"]] for r in v["better"]]) if v["better"] else ["- 없음"]
    L += ["", "## 등급 전후"]
    L += md_table(["축", v["prev_period"], v["curr_period"], "변화"], [[r["name"], r["prev"], r["curr"], r["change"] or "같음"]
                                                                    for r in v["axes"]])
    L += ["", "## 다시 볼 조건"]
    L += ["- " + x for x in v["revisit"]]
    if not v["revisit"]:
        L.append("- 다음 실적 8-K가 나오면 다시 점검한다")
    body = "\n".join(L)
    L.append("")
    L += glossary_md(body)
    return "\n".join(L) + "\n"


def cmd_earnings(args):
    try:
        date = today(args.date)
        diff = read_doc(args.diff, reit_calc.DIFF_SCHEMA)
        files = diff.get("files") or {}
        prev = read_doc(files.get("prev") or "", reit_calc.METRICS_SCHEMA)
        curr = read_doc(files.get("curr") or "", reit_calc.METRICS_SCHEMA)
        nar, why = load_narrative(args.narrative, "earnings")
    except InputError as e:
        log(str(e))
        return 1
    if nar is not None:
        probs = check_narrative(nar, "earnings", allowed_numbers([prev, curr]))
        if probs:
            log("풀이 글 검사에 걸려 아무 파일도 쓰지 않았다:")
            for p in probs:
                log("  - " + p)
            return 2
    v = build_earnings_view(diff, prev, curr, nar, date)
    md = render_earnings_md(v)
    path = os.path.join(os.path.abspath(args.out_dir), "%s-earnings.md" % date)
    write_text(path, md)
    write_text(path[:-3] + ".view.json", json.dumps(v, ensure_ascii=False, indent=1) + "\n")
    probs = finish_md(md, "실적 뒤 점검")
    for p in probs:
        log(p)
    if why:
        log(why)
    print(path)
    return 2 if (why or probs) else 0


# ---------------------------------------------------------------- CLI

def main(argv):
    p = argparse.ArgumentParser(prog="report.py", description="metrics → 읽는 보고서·부록·장표")
    p.add_argument("--check", action="store_true", help="network 없이 fixture로 확인")
    sub = p.add_subparsers(dest="cmd")
    b = sub.add_parser("build", help="단일 보고서(종목 하나 + 경쟁 종목)")
    b.add_argument("--metrics", required=True)
    b.add_argument("--peers", nargs="*", default=[])
    b.add_argument("--trend")
    b.add_argument("--narrative")
    b.add_argument("--out-dir", required=True)
    b.add_argument("--date", help="보고서 날짜(기본 오늘)")
    e = sub.add_parser("earnings", help="실적 뒤 점검(reit_calc.py diff 출력)")
    e.add_argument("--diff", required=True)
    e.add_argument("--narrative")
    e.add_argument("--out-dir", required=True)
    e.add_argument("--date")
    try:
        args = p.parse_args(argv)
    except SystemExit as x:
        return 0 if x.code == 0 else 1
    if args.check:
        return run_check()
    if args.cmd == "build":
        return cmd_build(args)
    if args.cmd == "earnings":
        return cmd_earnings(args)
    p.print_help(sys.stderr)
    return 1


# ---------------------------------------------------------------- --check

def fixture_metrics(fx, work):
    """tests/fixtures/facts_o_2026q3.json을 ticker만 바꾼 가상 종목 AAA(deep)와 BBB·CCC(light). 경로 dict."""
    import copy
    rep = reit_calc.read_json(os.path.join(fx, "report", "fixture.json"))
    base = reit_calc.read_json(os.path.join(fx, "facts_o_2026q3.json"))
    th = dict(reit_calc.load_thresholds(), history_min_points=3)
    macro, _ = reit_calc.load_macro(os.path.join(fx, "macro_snapshot.json"))
    baa, _ = reit_calc.load_macro(os.path.join(fx, "macro_snapshot.json"), "BAA10Y")
    rw = reit_calc.load_reitwatch(os.path.join(fx, "reitwatch"), "O")
    trk = reit_calc.load_tracker(os.path.join(fx, "tracker"))
    tmap = {"Free Standing (가상)": {"name": "Free Standing", "parent": "Retail"}}
    paths = {}
    for t, spec in rep["tickers"].items():
        f = copy.deepcopy(base)
        f.update(ticker=t, company=dict(f["company"], name=spec["name"]), events=spec.get("events", []))
        if spec.get("depth"):
            f["depth"] = spec["depth"]
        doc, _ = reit_calc.compute(f, th, macro=macro, rw=rw, baa=baa, tracker=trk, tmap=tmap)
        for k, v in (spec.get("set") or {}).items():
            doc["metrics"][k] = dict(doc["metrics"].get(k) or {"metric": k, "unit": v.get("unit", "")}, **v)
        for k, g in (spec.get("grades") or {}).items():
            a = next(x for x in doc["scorecard"]["axes"] if x["key"] == k)
            a.update(grade=g, grade_name=grade_names()[g], base_grade=g)
        d = os.path.join(work, "data", "tickers", t)
        fp = os.path.join(d, "facts", "2026Q3.json")
        mp = os.path.join(d, "metrics", "2026Q3.json")
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        os.makedirs(os.path.dirname(mp), exist_ok=True)
        reit_calc.write_json(f, fp)
        doc["facts_file"] = fp
        reit_calc.write_json(doc, mp)
        paths[t] = mp
    return paths


def run_check():
    import tempfile
    import edgar
    root = reit_calc.skill_root()
    fx = os.path.join(root, "tests", "fixtures")
    exp = reit_calc.read_json(os.path.join(root, "tests", "expected.json"))["report"]
    fails = []

    def ok(cond, label):
        if not cond:
            fails.append(label)
        log(("PASS " if cond else "FAIL ") + label)

    work = tempfile.mkdtemp(prefix="report-check-")
    docs = fixture_metrics(fx, work)
    trend = os.path.join(work, "trend.json")
    write_text(trend, json.dumps(edgar.trend_doc(reit_calc.read_json(os.path.join(fx, "edgar", "companyfacts_trend.json")),
                                                 "AAA", "2026Q3"), ensure_ascii=False))
    nar = os.path.join(fx, "report", "narrative_single.json")

    def run(argv, env=None):
        old = {k: os.environ.get(k) for k in (env or {})}
        os.environ.update(env or {})
        out, sys.stdout = sys.stdout, open(os.devnull, "w")
        try:
            return main(argv)
        finally:
            sys.stdout.close()
            sys.stdout = out
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def read(path):
        if not os.path.isfile(path):
            return ""
        with open(path, encoding="utf-8") as f:
            return f.read()

    no_ds = {"DIAGRAM_SLIDES_DIR": os.path.join(work, "no-diagram-slides")}
    build = ["build", "--metrics", docs["AAA"], "--peers", docs["BBB"], docs["CCC"], "--trend", trend, "--date", "2026-11-19"]

    # B1·B4·B28: 단일 보고서
    out1 = os.path.join(work, "single")
    code = run(build + ["--narrative", nar, "--out-dir", out1], no_ds)
    md = read(os.path.join(out1, "2026-11-19-deep.md"))
    ok(code == 0 and md, "B1 build exit 0과 읽는 보고서 (got %s)" % code)
    body = "\n".join(body_lines(md))
    ok(0 < len(body_lines(md)) <= MAX_LINES, "B1 용어 표를 빼고 60줄 이하 (got %d)" % len(body_lines(md)))
    heads = re.findall(r"^## (.+)$", md, flags=re.M)
    ok(heads == exp["single_sections"], "B1 절 차례 %s" % heads)
    ok(not leaks(body), "B1 본문에 칸 이름·exit·영문 status 없음 %s" % leaks(body)[:5])
    for s in exp["single_has"]:
        ok(s in md, "B1·B4·B28 읽는 보고서에 %r" % s)
    for s in exp["single_not"]:
        ok(s not in body, "B28 읽는 보고서 본문에 %r 없음" % s)
    peer_sec = md.split("## 동종 대비 위치")[1].split("\n## ")[0] if "## 동종 대비 위치" in md else ""
    ok(peer_sec and not any(g in peer_sec for g in ("넉넉", "보통", "빠듯", "위험", "가볍게 봄")),
       "B28 경쟁 종목에는 등급을 매기지 않고 숫자만(동종 절에 등급 말 없음)")
    gl = md.split(GLOSSARY_HEAD)[1] if GLOSSARY_HEAD in md else ""
    for t in exp["glossary_has"]:
        ok("| %s |" % t in gl, "B4 용어 표에 %s" % t)

    # B2: 부록
    audit = read(os.path.join(out1, "2026-11-19-deep-audit.md"))
    for s in exp["audit_has"]:
        ok(s in audit, "B2 부록에 %r" % s)

    # B5: 장표 HTML 3장, diagram-slides 없으면 굽지 않고 한 줄
    sl = sorted(os.listdir(os.path.join(out1, "slides"))) if os.path.isdir(os.path.join(out1, "slides")) else []
    ok([s.split("-", 4)[-1] for s in sl] == exp["single_slides"], "B5 장표 HTML %s" % sl)
    one = read(os.path.join(out1, "slides", sl[0])) if sl else ""
    ok(all(x in one for x in ("<h1>", "class=\"diagram\"", "class=\"panel\"", "class=\"key\"")) and "<html" not in one,
       "B5 장표가 diagram-slides 견본 구조(body 안쪽만)")
    ok("diagram-slides 없음" in md and not any(s.endswith(".png") for s in sl), "B5 diagram-slides가 없으면 PNG 없이 보고서에 한 줄")

    # B5: diagram-slides와 Chrome이 있으면 PNG까지 굽는다(없으면 건너뛴다)
    if find_diagram_slides() and find_chrome():
        outp = os.path.join(work, "baked")
        run(build + ["--narrative", nar, "--out-dir", outp])
        pngs = [n for n in os.listdir(os.path.join(outp, "slides")) if n.endswith(".png")]
        ok(len(pngs) == 3, "B5 diagram-slides가 있으면 장표 3장을 PNG로 굽는다 (got %s)" % pngs)
    else:
        log("SKIP B5 굽기: diagram-slides나 Chrome이 없다")

    # B20: 풀이 글 검사
    for case, frag in exp["narrative_bad"].items():
        bad = json.loads(read(nar))
        apply_path(bad, frag["path"], frag["value"])
        p = os.path.join(work, "bad-%s.json" % case)
        write_text(p, json.dumps(bad, ensure_ascii=False))
        outb = os.path.join(work, "bad-" + case)
        err = capture(lambda: run(build + ["--narrative", p, "--out-dir", outb], no_ds))
        ok(err[0] == 2 and not os.path.exists(outb) and frag["message"] in err[1],
           "B20 %s → exit 2, 파일 없음, 메시지 %r (got %s, %s)" % (case, frag["message"], err[0], err[1].strip()[-120:]))
    good = json.loads(read(nar))
    apply_path(good, "conclusion", "(가상) 만기 위험이 사라지면 등급이 오른다")
    p = os.path.join(work, "ok-sarajida.json")
    write_text(p, json.dumps(good, ensure_ascii=False))
    ok(run(build + ["--narrative", p, "--out-dir", os.path.join(work, "ok-s")], no_ds) == 0, "B20 '사라지면'은 금지어 '사라'가 아님")

    # B1: 풀이 글이 없으면 숫자만으로 만들고 exit 2
    out0 = os.path.join(work, "no-narrative")
    code = run(build + ["--out-dir", out0], no_ds)
    md0 = read(os.path.join(out0, "2026-11-19-deep.md"))
    ok(code == 2 and "풀이 글 없음" in md0 and "| 만기·유동성 | 위험 |" in md0 and len(body_lines(md0)) <= MAX_LINES,
       "B1 풀이 글이 없으면 숫자만으로 만들고 '풀이 글 없음', exit 2 (got %s)" % code)
    ok(exp["auto_trigger"] in md0, "B1 풀이 글이 없으면 등급 경계로 다시 볼 조건 %r" % exp["auto_trigger"])

    # B19: earnings
    e_out = os.path.join(work, "earnings")
    prev = json.loads(read(docs["AAA"]))
    prev["period"] = "2026Q2"
    for a in prev["scorecard"]["axes"]:
        if a["key"] == "leverage":
            a.update(grade="ample", grade_name="넉넉", base_grade="ample")
    prev["metrics"]["net_debt_to_ebitdare"]["value"] = 4.8
    pp = os.path.join(work, "data", "tickers", "AAA", "metrics", "2026Q2.json")
    reit_calc.write_json(prev, pp)
    dd, _ = reit_calc.diff_metrics(reit_calc.read_json(pp), reit_calc.read_json(docs["AAA"]))
    dd["files"] = {"prev": pp, "curr": docs["AAA"]}
    dp = os.path.join(work, "earnings.json")
    reit_calc.write_json(dd, dp)
    code = run(["earnings", "--diff", dp, "--out-dir", e_out, "--date", "2026-11-19"])
    emd = read(os.path.join(e_out, "2026-11-19-earnings.md"))
    worse = emd.split("## 나빠진 것")[1].split("##")[0] if "## 나빠진 것" in emd else ""
    rows = [r for r in worse.split("\n") if r.startswith("| ") and "---" not in r][1:]
    ok(code == 2 and rows and rows[0].startswith(exp["earnings_first"]), "B19 나빠진 등급이 맨 위 %r (got %s)" % (
        exp["earnings_first"], rows[:1]))
    ok(any("net debt/EBITDAre" in r for r in rows), "B19 나빠진 지표(net debt/EBITDAre)가 나빠진 것에")
    ok(not leaks("\n".join(body_lines(emd))), "B19 칸 이름 누출 없음")

    log("report.py --check: %s" % ("실패 %d" % len(fails) if fails else "통과"))
    return 1 if fails else 0


def apply_path(doc, path, value):
    cur = doc
    keys = path.split(".")
    for k in keys[:-1]:
        cur = cur[int(k)] if isinstance(cur, list) else cur[k]
    last = keys[-1]
    if isinstance(cur, list):
        cur[int(last)] = value
    else:
        cur[last] = value


def capture(fn):
    import io
    buf, old = io.StringIO(), sys.stderr
    sys.stderr = buf
    try:
        code = fn()
    finally:
        sys.stderr = old
    return code, buf.getvalue()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

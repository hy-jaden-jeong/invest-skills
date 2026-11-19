#!/usr/bin/env python3
"""reit-sector-scan: Nareit REITWatch PDF를 받아 파싱하고, subsector valuation 표와 peer 표를 낸다.

명령
  fetch --month YYYY-MM|latest [--backfill]   REITWatch 호를 받아 parsed JSON을 쓴다(PDF는 지운다)
  sectors [--macro-snapshot P] [--out P]      subsector 표(median, percentile, z-score, spread, 상대 성과, 섹터 지표)
  peers (--subsector S | --ticker T) [--out P]  한 subsector의 종목 screening 표
  tracker fetch --quarter YYYYQn|latest [--url U]  Nareit REIT Industry Tracker XLSX를 받아 parsed JSON을 쓴다(XLSX는 지운다)
  --check                                     network 없이 fixture로 확인한다

exit: 0 = 다 채움, 2 = missing이나 check 실패가 있음, 1 = 입력·network 오류.
Python 3 stdlib만 쓰고(3.9 이상), 외부 도구는 pdftotext 하나다.
"""
import argparse
import csv
import datetime as dt
import glob
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tracker  # noqa: E402  같은 skill 폴더 안의 script

SCHEMA = "reitwatch-parsed/1"
MACRO_SCHEMA = "macro-snapshot/1"
URL = "https://www.reit.com/sites/default/files/reitwatch/RW%s.pdf"
BACKFILL_START = (2015, 1)       # backfill은 2015-01 호부터
QUARTER_MONTHS = (1, 4, 7, 10)   # 가격 기준일이 분기말(3·6·9·12월 말)인 호
RECENT_MONTHS = 12
MIN_HISTORY = 8                  # 표본(1·4·7·10월 호 + 최신 호)이 이보다 적으면 percentile·z-score는 missing
MIN_ROWS = 50                    # 종목이 이보다 적으면 파싱 실패(layout이 바뀐 것으로 본다)
SNAPSHOT_MAX_AGE = 7             # macro snapshot 날짜가 오늘보다 이 일수 넘게 이르면 DGS10은 stale
LATEST_LOOKBACK = 3              # --month latest는 이번 달부터 세 달 앞까지 거꾸로 본다
# PDF 종목 표의 숫자 column 23개. ffo_payout·debt_ebitda는 읽기만 하고 parsed에 넣지 않는다(값이 여러 달 같아 멈춰 있다).
COLS = ["price", "hi52", "lo52", "ffo_fy1", "ffo_fy2", "p_ffo_fy1", "p_ffo_fy2", "ffo_growth", "ffo_payout",
        "debt_ebitda", "tr_1m", "tr_qtd", "tr_ytd", "tr_1y", "tr_3y", "tr_5y", "div_yield", "mkt_cap",
        "implied_mkt_cap", "debt_ratio", "avg_share_vol", "avg_dollar_vol", "rel_liq"]
ROW_FIELDS = ["ticker", "name", "subsector", "price", "ffo_fy1", "ffo_fy2", "p_ffo_fy1", "p_ffo_fy2", "div_yield",
              "mkt_cap", "debt_ratio", "hi52", "lo52", "ffo_growth", "implied_mkt_cap", "tr_1y", "tr_3y", "tr_5y",
              "rating"]
NUM = re.compile(r"^-?[\d,]*\.?\d+$")
RATING = re.compile(r"^(AAA|AA|A|BBB|BB|B|CCC|CC|C|D)[+-]?$")
TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,6}$")
MONTHS = {m: i + 1 for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July",
                                          "August", "September", "October", "November", "December"])}
REVISION_MONTHS = 3              # consensus FFO 변화는 이 달 수만큼 앞선 호와 견준다
HISTORY_COLS = ["month", "as_of", "subsector", "n", "median_p_ffo_fy1", "median_div_yield",
                "median_tr_3y", "median_tr_5y"]


class FetchError(Exception):
    pass


class NotPublished(FetchError):
    pass


class ParseError(Exception):
    pass


def log(msg):
    print(msg, file=sys.stderr)


def data_dir():
    return os.path.expanduser(os.environ.get("INVEST_DATA_DIR") or "~/invest-data")


def rw_dir():
    return os.path.join(data_dir(), "reitwatch")


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(obj, path):
    text = json.dumps(obj, ensure_ascii=False, indent=1, allow_nan=False)  # NaN·Infinity 토큰을 쓰지 않는다
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    os.replace(tmp, path)


def emit(obj, out):
    if out:
        write_json(obj, out)
        print(out)
    else:
        print(json.dumps(obj, ensure_ascii=False, indent=1, allow_nan=False))


def parse_month(s):
    m = re.match(r"^(\d{4})-(\d{2})$", s or "")
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise ValueError("month는 YYYY-MM: %r" % s)
    return int(m.group(1)), int(m.group(2))


def fmt_month(y, m):
    return "%04d-%02d" % (y, m)


def shift_month(y, m, n):
    k = y * 12 + (m - 1) + n
    return k // 12, k % 12 + 1


# ---------------------------------------------------------------- 파싱

def _num(t):
    if t == "-" or t.startswith("#") or t == "NA":
        return None
    return float(t.replace(",", ""))


def _is_cell(t):
    return bool(NUM.match(t)) or t == "-" or t.startswith("#") or t == "NA"


def _looks_like_row(tok):
    """ticker 다음에 숫자 칸이 오고 숫자 칸이 10개 넘는 줄: 종목 row인데 column 수가 다른 것."""
    return any(TICKER.match(t) and i + 1 < len(tok) and _is_cell(tok[i + 1]) for i, t in enumerate(tok)) \
        and sum(1 for t in tok if _is_cell(t)) >= 10


def parse_text(text, month, source, min_rows=MIN_ROWS):
    """pdftotext -layout 출력 → parsed dict. 숫자 token 수로 row를 읽고, 수가 다른 줄은 unparsed에 남긴다."""
    lines = text.replace("\f", "\n").split("\n")
    as_of, fy = None, (None, None)
    for line in lines:
        m = re.search(r"Data as of ([A-Z][a-z]+) (\d{1,2}), (\d{4})", line)
        if m and m.group(1) in MONTHS:
            as_of = dt.date(int(m.group(3)), MONTHS[m.group(1)], int(m.group(2))).isoformat()
            break
    rows, unparsed = [], []
    in_tbl, sector, texts = False, None, []
    for line in lines:
        s = line.strip()
        if re.match(r"^Name\s+Ticker\b", s):
            in_tbl, texts = True, []
            f = re.search(r"\bLow\s+(\d{4})\s+(\d{4})\b", s)
            if f and fy == (None, None):
                fy = (int(f.group(1)), int(f.group(2)))
            continue
        if not in_tbl or not s:
            continue
        tok = s.split()
        if s.startswith("OVERALL AVERAGE") or s.isdigit():  # 표 끝, 또는 page 번호(page마다 머리줄이 다시 나온다)
            in_tbl = False
            continue
        if tok[0] == "AVERAGE":
            continue
        if not any(NUM.match(t) for t in tok):
            if len(s) <= 80:
                texts.append(s)
            continue
        rating = tok.pop() if RATING.match(tok[-1]) else None
        vals = tok[-len(COLS):]
        head = tok[:-len(COLS)]
        if len(tok) < len(COLS) + 1 or not all(_is_cell(t) for t in vals) or not head or not TICKER.match(head[-1]):
            if _looks_like_row(tok):
                unparsed.append(s[:100])
            continue
        ticker, name = head[-1], " ".join(head[:-1])
        if not name:  # 이름이 앞 줄로 꺾였다
            if not texts:
                unparsed.append(s[:100])
                continue
            name = texts.pop()
        if texts:
            sector = texts[-1]
        texts = []
        if sector is None:
            unparsed.append(s[:100])
            continue
        v = {c: _num(x) for c, x in zip(COLS, vals)}
        row = {"ticker": ticker, "name": name, "subsector": sector, "rating": rating}
        row.update({k: v[k] for k in COLS if k in ROW_FIELDS})
        rows.append({k: row.get(k) for k in ROW_FIELDS})
    if as_of is None:
        raise ParseError("'Data as of' 날짜를 찾지 못함")
    if len(rows) < min_rows:
        raise ParseError("종목 row가 %d개뿐(%d개 미만). layout이 다를 수 있다" % (len(rows), min_rows))
    return {"schema": SCHEMA, "month": month, "as_of": as_of, "source": source, "fy1": fy[0], "fy2": fy[1],
            "parsed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "unparsed": unparsed, "rows": rows}


# ---------------------------------------------------------------- 받기

def backfill_months(month):
    """2015-01부터 1·4·7·10월 호 + month까지 최근 12개월."""
    y, m = parse_month(month)
    end = y * 12 + m - 1
    out = set()
    k = BACKFILL_START[0] * 12 + BACKFILL_START[1] - 1
    while k <= end:
        if k % 12 + 1 in QUARTER_MONTHS:
            out.add(fmt_month(k // 12, k % 12 + 1))
        k += 1
    for n in range(RECENT_MONTHS):
        out.add(fmt_month(*shift_month(y, m, -n)))
    return sorted(out)


def network_text(month):
    """PDF를 받아 pdftotext -layout 결과를 돌려준다. PDF와 txt는 바로 지운다(Nareit 저작물이고 크다)."""
    if not shutil.which("pdftotext"):
        raise FetchError("pdftotext가 없음(poppler를 설치한다)")
    y, m = parse_month(month)
    url = URL % ("%02d%02d" % (y % 100, m))
    tmp = tempfile.mkdtemp(prefix="reitwatch-", dir=_ensure(os.path.join(rw_dir(), "tmp")))
    try:
        pdf = os.path.join(tmp, "rw.pdf")
        last = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=60) as r, open(pdf, "wb") as f:
                    shutil.copyfileobj(r, f)
                last = None
                break
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise NotPublished("HTTP 404 (%s)" % url)
                last = "HTTP %d (%s)" % (e.code, url)
            except (urllib.error.URLError, OSError) as e:
                last = "network 오류 %s (%s)" % (getattr(e, "reason", e), url)
            time.sleep(2 * (attempt + 1))
        if last:
            raise FetchError(last)
        with open(pdf, "rb") as f:
            if f.read(5) != b"%PDF-":
                raise FetchError("응답이 PDF가 아님 (%s)" % url)
        txt = os.path.join(tmp, "rw.txt")
        r = subprocess.run(["pdftotext", "-layout", pdf, txt], capture_output=True, timeout=300)
        if r.returncode != 0:
            raise FetchError("pdftotext 실패: %s" % r.stderr.decode("utf-8", "replace")[:200])
        with open(txt, encoding="utf-8", errors="replace") as f:
            return f.read(), url
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _ensure(d):
    os.makedirs(d, exist_ok=True)
    return d


def parsed_path(month, base=None):
    return os.path.join(base or os.path.join(rw_dir(), "parsed"), month + ".json")


def fetch_one(month, get_text, base=None, min_rows=MIN_ROWS):
    text, source = get_text(month)
    doc = parse_text(text, month, source, min_rows)
    write_json(doc, parsed_path(month, base))
    return doc


def cmd_fetch(args, get_text=network_text, today=None, base=None, min_rows=MIN_ROWS):
    today = today or dt.date.today()
    if args.month == "latest":
        found = None
        for n in range(LATEST_LOOKBACK + 1):
            mo = fmt_month(*shift_month(today.year, today.month, -n))
            try:
                doc = fetch_one(mo, get_text, base, min_rows)
            except NotPublished:
                log("%s: 아직 없음" % mo)
                continue
            except (FetchError, ParseError) as e:
                log("%s: %s" % (mo, e))
                return 1
            found = mo
            log("%s: as_of %s, 종목 %d개, unparsed %d" % (mo, doc["as_of"], len(doc["rows"]), len(doc["unparsed"])))
            break
        if not found:
            log("최근 %d개월 안에 올라온 호가 없음" % (LATEST_LOOKBACK + 1))
            return 1
        args.month = found
        if not args.backfill:
            write_history(base)
            print(parsed_path(found, base))
            return 0
    try:
        parse_month(args.month)
    except ValueError as e:
        log(str(e))
        return 1
    months = backfill_months(args.month) if args.backfill else [args.month]
    got, skipped, failed = [], [], []
    for mo in months:
        if args.backfill and os.path.exists(parsed_path(mo, base)):
            skipped.append(mo)
            continue
        try:
            doc = fetch_one(mo, get_text, base, min_rows)
            got.append(mo)
            log("%s: as_of %s, 종목 %d개, unparsed %d" % (mo, doc["as_of"], len(doc["rows"]), len(doc["unparsed"])))
        except (FetchError, ParseError) as e:
            failed.append(mo)
            log("%s: 실패 %s" % (mo, e))
        if args.backfill and get_text is network_text:
            time.sleep(1)
    write_history(base)
    log("받음 %d, 이미 있음 %d, 실패 %d%s" % (len(got), len(skipped), len(failed),
                                        (" (" + ", ".join(failed) + ")") if failed else ""))
    if not got and not skipped:
        return 1
    print(os.path.dirname(parsed_path(args.month, base)))
    return 2 if failed else 0


# ---------------------------------------------------------------- history

def load_parsed(base=None):
    base = base or os.path.join(rw_dir(), "parsed")
    docs = []
    for p in sorted(glob.glob(os.path.join(base, "*.json"))):
        try:
            d = read_json(p)
        except ValueError:
            log("읽지 못함: %s" % p)
            continue
        if d.get("schema") == SCHEMA:
            docs.append(d)
    return docs


def median(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 4) if xs else None


def aggregate(doc, aliases=None):
    """호 하나 → subsector별 median. aliases로 예전 이름을 지금 이름에 잇는다(Timber → Timberland 등)."""
    aliases = aliases or {}
    by = {}
    for r in doc["rows"]:
        by.setdefault(aliases.get(r["subsector"], r["subsector"]), []).append(r)
    out = {}
    for sub, rs in by.items():
        out[sub] = {"n": len(rs),
                    "p_ffo": median(r["p_ffo_fy1"] for r in rs),
                    "div_yield": median(r["div_yield"] for r in rs),
                    "tr_3y": median(r["tr_3y"] for r in rs),
                    "tr_5y": median(r["tr_5y"] for r in rs)}
    return out


def write_history(base=None):
    docs = load_parsed(base)
    path = os.path.join(_ensure(os.path.dirname(base) if base else rw_dir()), "history.csv")
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(HISTORY_COLS)
        for d in docs:
            for sub, a in sorted(aggregate(d).items()):
                w.writerow([d["month"], d["as_of"], sub, a["n"]] +
                           ["" if a[k] is None else a[k] for k in ("p_ffo", "div_yield", "tr_3y", "tr_5y")])
    return path


# ---------------------------------------------------------------- sectors

def val(value, unit, status, as_of, source, locator, note=""):
    if value is None and status != "missing":
        status = "missing"
    return {"value": value, "unit": unit, "status": status, "as_of": as_of, "source": source,
            "locator": locator, "note": note}


def is_mortgage(sub):
    return "Financing" in sub or "Mortgage" in sub


def hist_stats(points, latest):
    """points = 표본 값(1·4·7·10월 호 + 최신 호). percentile은 최신값 이하 비율, z는 (최신값 − 평균) / 표본 표준편차."""
    xs = [x for x in points if x is not None]
    if latest is None:
        return None, None, "최신값이 없음"
    if len(xs) < MIN_HISTORY:
        return None, None, "history 표본 %d개(%d개 미만)" % (len(xs), MIN_HISTORY)
    pct = round(100.0 * sum(1 for x in xs if x <= latest) / len(xs), 4)
    sd = statistics.stdev(xs)
    z = round((latest - statistics.mean(xs)) / sd, 4) if sd > 0 else None
    return pct, z, "" if z is not None else "표본 표준편차가 0"


def load_snapshot(path):
    """macro snapshot 경로를 정한다. 없으면 (None, None, 이유)."""
    if path is None:
        files = sorted(glob.glob(os.path.join(data_dir(), "macro", "snapshots", "*.json")))
        if not files:
            return None, None, "macro snapshot이 없음(macro-check fetch를 먼저 돌린다)"
        path = files[-1]
    if not os.path.exists(path):
        return None, path, "macro snapshot이 없음: %s" % path
    try:
        snap = read_json(path)
    except ValueError:
        return None, path, "macro snapshot을 읽지 못함: %s" % path
    if snap.get("schema") != MACRO_SCHEMA:
        return None, path, "macro snapshot schema가 %r" % snap.get("schema")
    return snap, path, ""


def _finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def macro_entry(snap, sid):
    """snapshot의 series 하나. 값·변화가 유한한 숫자가 아니면(NaN 등) 그 칸을 비워 돌려준다(표에 NaN을 쓰지 않는다)."""
    e = (snap.get("series") or {}).get(sid) or (snap.get("web") or {}).get(sid)
    if not e:
        return e
    e = dict(e)
    if e.get("value") is not None and not _finite(e["value"]):
        e.update(value=None, status="missing", note="snapshot의 %s 값(%r)이 유한한 숫자가 아님" % (sid, e["value"]))
    e["derived"] = {k: (v if v is None or _finite(v) else None) for k, v in (e.get("derived") or {}).items()}
    return e


def structural(snap, snap_note, macro_sector, smap, extra=None):
    """섹터 수요·공급 지표와 구조적 하락 신호(demand 절반 이상 나빠짐 + supply 하나라도 늘어남이면 watch).

    extra는 Tracker의 demand 지표(SS NOI, occupancy)다. supply가 macro snapshot에만 있어서 macro 쪽이 판단 못 하면
    extra도 판정에 세지 않고 표에만 놓는다."""
    extra = extra or []
    if macro_sector is None:
        return {"flag": "review", "note": "대응하는 macro-check 섹터가 없음", "indicators": extra}
    if snap is None:
        return {"flag": "review", "note": snap_note, "indicators": extra}
    if macro_sector not in (snap.get("sectors") or {}):
        return {"flag": "review", "indicators": extra,
                "note": "macro snapshot에 %s 섹터가 없음(macro.py fetch --sector %s)" % (macro_sector, macro_sector)}
    inds, demand_n, worse, supply_up, supply_n = [], 0, 0, 0, 0
    specs = smap["indicators"].get(macro_sector, [])
    for spec in specs:
        e = macro_entry(snap, spec["id"]) or {}
        d = e.get("derived") or {}
        c3, c12 = d.get("chg_3m"), d.get("chg_12m")
        judged = None
        if spec["role"] in ("demand", "supply") and c3 is not None and c12 is not None:
            if spec["role"] == "demand":
                demand_n += 1
                bad = (c3 < 0 and c12 < 0) if spec["good"] == "up" else (c3 > 0 and c12 > 0)
                judged = "나빠짐" if bad else "아님"
                worse += bad
            else:
                supply_n += 1
                up = c3 > 0 and c12 > 0
                judged = "늘어남" if up else "아님"
                supply_up += up
        inds.append({"id": spec["id"], "role": spec["role"], "good": spec.get("good"),
                     "value": e.get("value"), "unit": e.get("unit"), "as_of": e.get("as_of"),
                     "status": e.get("status", "missing"), "chg_3m": c3, "chg_12m": c12,
                     "yoy_pct": d.get("yoy_pct"), "judged": judged, "note": spec.get("note", "")})
    # supply가 macro에만 있어서 macro 쪽이 판단 못 하면 Tracker 지표는 표에만 두고 판정을 올리지 않는다
    macro_review = None
    if demand_n == 0:
        macro_review = "3·12개월 변화가 있는 macro demand 지표가 없음"
    elif any(sp["role"] == "supply" for sp in specs) and supply_n == 0:
        macro_review = "3·12개월 변화가 있는 supply 지표가 없음"
    if macro_review:
        return {"flag": "review", "indicators": inds + extra,
                "note": macro_review + ("(Tracker 지표는 표에만 두고 판정에 세지 않음)" if extra else "")}
    for e in extra:
        inds.append(e)
        if e["judged"] is not None:
            demand_n += 1
            worse += e["judged"] == "나빠짐"
    if worse * 2 >= demand_n and supply_up > 0:
        flag, note = "watch", "demand %d개 중 %d개 나빠짐, supply %d개 늘어남" % (demand_n, worse, supply_up)
    else:
        flag, note = "ok", "demand %d개 중 %d개 나빠짐, supply %d개 늘어남" % (demand_n, worse, supply_up)
    return {"flag": flag, "note": note, "indicators": inds}


def macro_val(snap, snap_path, snap_note, sid, today):
    """macro snapshot의 series 하나를 값의 형식으로. snapshot이 7일보다 오래됐으면 reported도 stale."""
    if snap is None:
        return val(None, "Percent", "missing", None, "", "", snap_note)
    e = macro_entry(snap, sid) or {}
    st = e.get("status", "missing")
    note = ""
    age = (today - dt.date(*[int(x) for x in snap["date"].split("-")])).days
    if st == "reported" and age > SNAPSHOT_MAX_AGE:
        st, note = "stale", "macro snapshot이 %d일 전(허용 %d일)" % (age, SNAPSHOT_MAX_AGE)
    return val(e.get("value"), "Percent", st, e.get("as_of"), "macro-snapshot:" + snap_path,
               "series." + sid, note or e.get("note", "") or ("" if e else "snapshot에 %s가 없음" % sid))


def baa_yield(snap, snap_path, snap_note, dgs, today):
    """Baa 회사채 수익률: snapshot에 DBAA가 있으면 그것, 없으면 BAA10Y(Baa − 10년물 spread) + DGS10."""
    d = macro_val(snap, snap_path, snap_note, "DBAA", today)
    if d["value"] is not None:
        return d
    b = macro_val(snap, snap_path, snap_note, "BAA10Y", today)
    if b["value"] is None or dgs["value"] is None:
        why = "; ".join("%s: %s" % (sid, e["note"] or "값 없음") for sid, e in (("BAA10Y", b), ("DGS10", dgs))
                        if e["value"] is None)
        return val(None, "Percent", "missing", None, "", "",
                   snap_note if snap is None else "snapshot에 DBAA 값이 없고 BAA10Y + DGS10을 낼 수 없음(%s)" % why)
    return val(round(b["value"] + dgs["value"], 4), "Percent", tracker.derive(b["status"], dgs["status"]),
               min(b["as_of"], dgs["as_of"]), "macro-snapshot:" + snap_path, "series.BAA10Y + series.DGS10",
               "; ".join(x for x in ("Baa = BAA10Y %s(%s) + DGS10 %s(%s)" % (b["value"], b["as_of"], dgs["value"],
                                                                         dgs["as_of"]), b["note"]) if x))


def find_month(docs, month):
    return next((d for d in docs if d["month"] == month), None)


def ffo_revision(new_doc, old_doc, ticker):
    """consensus FFO FY1의 변화(%): 새 호 FY1과 앞 호에서 같은 회계연도 column(FY1, FY 교체 뒤면 FY2)을 견준다."""
    if old_doc is None:
        return None, "앞 호가 없음"
    year = new_doc.get("fy1")
    if year is None:
        return None, "새 호의 FY1 연도를 모름"
    col = "ffo_fy1" if old_doc.get("fy1") == year else ("ffo_fy2" if old_doc.get("fy2") == year else None)
    if col is None:
        return None, "앞 호 %s에 %s년 column이 없음" % (old_doc["month"], year)
    new = next((r for r in new_doc["rows"] if r["ticker"] == ticker), None)
    old = next((r for r in old_doc["rows"] if r["ticker"] == ticker), None)
    if new is None or new.get("ffo_fy1") is None:
        return None, "새 호에 FFO FY1이 없음"
    if old is None or old.get(col) is None or old[col] <= 0:
        return None, "앞 호 %s에 %s 값이 없거나 0 이하" % (old_doc["month"], col)
    return round((new["ffo_fy1"] / old[col] - 1) * 100.0, 4), "%s %s %s 대비" % (old_doc["month"], col, old[col])


def ffo_growth(row):
    """consensus FFO FY2/FY1 − 1(%)."""
    a, b = row.get("ffo_fy1"), row.get("ffo_fy2")
    return round((b / a - 1) * 100.0, 4) if a is not None and b is not None and a > 0 else None


def build_sectors(docs, snap, snap_path, snap_note, smap, today, tdoc=None, tpath=None, tnote="Tracker parsed가 없음"):
    latest = docs[-1]
    month, as_of = latest["month"], latest["as_of"]
    src = "reitwatch:" + latest["source"]
    sample_docs = [d for d in docs[:-1] if int(d["month"][5:7]) in QUARTER_MONTHS] + [latest]
    aliases = {k: v for k, v in smap.get("aliases", {}).items() if not k.startswith("_")}
    aggs = [aggregate(d, aliases) for d in sample_docs]
    cur = aggs[-1]
    equity = [r for r in latest["rows"] if not is_mortgage(r["subsector"])]
    all3, all5 = median(r["tr_3y"] for r in equity), median(r["tr_5y"] for r in equity)

    # 10년물, Baa
    dgs = macro_val(snap, snap_path, snap_note, "DGS10", today)
    baa = baa_yield(snap, snap_path, snap_note, dgs, today)
    tmap = {k: v for k, v in smap.get("tracker", {}).items() if not k.startswith("_")}
    old_month = fmt_month(*shift_month(*parse_month(month), -REVISION_MONTHS))
    old_doc = find_month(docs, old_month)
    rev_rule = "새 호 FY1 / %s 호의 같은 회계연도 consensus FFO(FY 교체 뒤면 FY2) - 1" % old_month

    table, excluded, missing, t_missing = [], [], 0, 0
    for sub in sorted(cur):
        if is_mortgage(sub):
            excluded.append({"subsector": sub, "reason": "mortgage REIT이라 REITWatch에 FFO가 없어 P/FFO를 견줄 수 없음"})
            continue
        a = cur[sub]
        loc = "종목 표 %s %d개 row의 median" % (sub, a["n"])
        row = {"subsector": sub, "n": a["n"], "history_points": sum(1 for g in aggs if sub in g)}
        for key, unit in (("p_ffo", "x"), ("div_yield", "Percent")):
            row[key + "_median"] = val(a[key], unit, "derived", as_of, src, loc,
                                       "" if a[key] is not None else "row에 값이 없음")
            pct, z, why = hist_stats([g[sub][key] for g in aggs if sub in g], a[key])
            hloc = "같은 subsector의 1·4·7·10월 호와 최신 호 median"
            row[key + "_pctile"] = val(pct, "Percent", "derived", as_of, "reitwatch:parsed", hloc, why)
            row[key + "_z"] = val(z, "z", "derived", as_of, "reitwatch:parsed", hloc, why)
        # 배당수익률 − 10년물 spread(%p)
        if a["div_yield"] is None or dgs["value"] is None:
            row["spread_dgs10"] = val(None, "Percent points", "missing", as_of, "", "",
                                      dgs["note"] if dgs["value"] is None else "배당수익률 median이 없음")
        else:
            row["spread_dgs10"] = val(round(a["div_yield"] - dgs["value"], 4), "Percent points",
                                      "stale" if dgs["status"] == "stale" else "derived", as_of,
                                      "derived:" + src + " - DGS10", "div_yield_median - DGS10",
                                      "배당수익률 기준일 %s, DGS10 기준일 %s" % (as_of, dgs["as_of"]))
        # 3·5년 상대 성과: subsector median − equity 전체 median(%p)
        for key, base in (("tr_3y", all3), ("tr_5y", all5)):
            if a[key] is None or base is None:
                row["rel_" + key] = val(None, "Percent points", "missing", as_of, src, "", "3·5년 수익률이 있는 row가 없음")
            else:
                row["rel_" + key] = val(round(a[key] - base, 4), "Percent points", "derived", as_of, src,
                                        "%s median %s - equity 전체 median %s" % (key, a[key], base),
                                        "지금 상장된 종목만 든다(survivorship)")
        # consensus FFO: 3개월 변화와 FY2/FY1 성장(종목 값의 median)
        subrows = [r for r in latest["rows"] if aliases.get(r["subsector"], r["subsector"]) == sub]
        revs = [ffo_revision(latest, old_doc, r["ticker"])[0] for r in subrows]
        revs = [x for x in revs if x is not None]
        row["ffo_rev_3m_median"] = val(median(revs), "Percent", "derived", as_of, src,
                                       "%d종목 median. %s" % (len(revs), rev_rule),
                                       "" if revs else ("%s 호가 없음" % old_month if old_doc is None
                                                        else "두 호에 FFO가 모두 있는 종목이 없음"))
        grs = [g for g in (ffo_growth(r) for r in subrows) if g is not None]
        row["ffo_growth_fy2_median"] = val(median(grs), "Percent", "derived", as_of, src,
                                           "%d종목 median. FY2 / FY1 consensus FFO - 1" % len(grs),
                                           "" if grs else "FY1·FY2 FFO가 모두 있는 종목이 없음")
        # Tracker
        tcols, tinds, uncovered = tracker.columns(tdoc, tpath, tnote, tmap.get(sub), dgs, baa, today)
        row["tracker"] = dict({"name": (tmap.get(sub) or {}).get("name"),
                               "parent": (tmap.get(sub) or {}).get("parent")}, **tcols)
        # Tracker가 원래 내지 않는 칸만 exit에서 뺀다. 입력(macro·history)이 모자라 빈 칸은 missing으로 센다
        t_missing += sum(1 for k, v in tcols.items() if v["status"] == "missing" and k in uncovered)
        missing += sum(1 for k, v in tcols.items() if v["status"] == "missing" and k not in uncovered)
        ms = smap["subsectors"].get(sub)
        row["macro_sector"] = ms
        row["structural"] = structural(snap, snap_note, ms, smap, tinds)
        if sub not in smap["subsectors"]:
            row["structural"]["note"] = "sector_map.json에 없는 subsector 이름. " + row["structural"]["note"]
        missing += sum(1 for k, v in row.items() if isinstance(v, dict) and v.get("status") == "missing")
        table.append(row)
    need = sorted({r["macro_sector"] for r in table if r["macro_sector"]
                   and (snap is None or r["macro_sector"] not in (snap.get("sectors") or {}))})
    tr = None if tdoc is None else {"quarter": tdoc["quarter"], "as_of": tdoc["as_of"], "source": tdoc["source"],
                                    "path": tpath, "pctile_from": tracker.PCTILE_START}
    out = {"schema": "reit-sectors/1", "month": month, "as_of": as_of, "source": latest["source"],
           "history_months": [d["month"] for d in sample_docs],
           "macro_snapshot": snap_path if snap is not None else None, "dgs10": dgs, "baa": baa,
           "tracker": tr, "revision_vs_month": old_month if old_doc is not None else None,
           "subsectors": table, "excluded": excluded, "macro_sectors_needed": need,
           "summary": {"missing_cells": missing, "tracker_missing_cells": t_missing,
                       "tracker_note": "" if tdoc is not None else tnote,
                       "structural_watch": [r["subsector"] for r in table if r["structural"]["flag"] == "watch"]}}
    return out, (2 if missing or tdoc is None else 0)


def print_table(out):
    def f(v, w=7):
        x = v["value"] if isinstance(v, dict) else v
        return ("%*s" % (w, "-")) if x is None else ("%*.2f" % (w, x) if isinstance(x, float) else "%*s" % (w, x))
    log("REITWatch %s (as_of %s), DGS10 %s (%s)" % (out["month"], out["as_of"], out["dgs10"]["value"],
                                                   out["dgs10"]["status"]))
    log("%-22s %3s %7s %7s %7s %7s %7s %7s %7s %7s %7s %s" % (
        "subsector", "n", "P/FFO", "pctile", "z", "yield", "pctile", "z", "spread", "rel3y", "rel5y", "structural"))
    for r in out["subsectors"]:
        log("%-22s %3d %s %s %s %s %s %s %s %s %s %s" % (
            r["subsector"][:22], r["n"], f(r["p_ffo_median"]), f(r["p_ffo_pctile"]), f(r["p_ffo_z"]),
            f(r["div_yield_median"]), f(r["div_yield_pctile"]), f(r["div_yield_z"]), f(r["spread_dgs10"]),
            f(r["rel_tr_3y"]), f(r["rel_tr_5y"]), r["structural"]["flag"]))
    t = out.get("tracker")
    log("Tracker %s (as_of %s), FFO 변화는 %s 호 대비" % (t["quarter"] if t else "없음", t["as_of"] if t else "-",
                                                    out.get("revision_vs_month") or "없음"))
    log("%-22s %7s %7s %7s %7s %7s %7s %7s %7s %7s %7s" % (
        "subsector", "cap", "pctile", "-10y", "-Baa", "SSNOI", "avg4q", "occ", "occ4q", "dev%", "rev3m"))
    for r in out["subsectors"]:
        x = r["tracker"]
        log("%-22s %s %s %s %s %s %s %s %s %s %s" % (
            r["subsector"][:22], f(x["implied_cap_rate"]), f(x["implied_cap_rate_pctile"]), f(x["cap_spread_dgs10"]),
            f(x["cap_spread_baa"]), f(x["ss_noi_yoy"]), f(x["ss_noi_yoy_avg_4q"]), f(x["occupancy"]),
            f(x["occupancy_chg_4q"]), f(x["dev_pipeline_pct_assets"]), f(r["ffo_rev_3m_median"])))


def assets_path():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "sector_map.json")


def cmd_sectors(args, base=None, today=None):
    docs = load_parsed(base)
    if not docs:
        log("parsed 파일이 없음. 먼저 fetch --month latest --backfill을 돌린다")
        return 1
    write_history(base)
    snap, path, note = load_snapshot(args.macro_snapshot)
    if note:
        log(note)
    tdoc, tpath, tnote = tracker.load_latest(os.path.dirname(rw_dir()) if base is None else os.path.dirname(
        os.path.dirname(base)))
    if tnote:
        log(tnote)
    out, code = build_sectors(docs, snap, path, note, read_json(assets_path()), today or dt.date.today(),
                              tdoc, tpath, tnote or "Tracker parsed가 없음")
    print_table(out)
    if out["macro_sectors_needed"]:
        log("macro snapshot에 없는 섹터: %s" % ", ".join(out["macro_sectors_needed"]))
    emit(out, args.out)
    return code


# ---------------------------------------------------------------- peers

PEER_FIELDS = ["ticker", "name", "price", "p_ffo_fy1", "p_ffo_fy2", "div_yield", "mkt_cap", "debt_ratio",
               "ffo_growth", "tr_1y", "tr_3y", "tr_5y", "rating", "ffo_fy1_rev_3m", "ffo_growth_fy2"]


def build_peers(doc, subsector=None, ticker=None, old_doc=None):
    """old_doc은 REVISION_MONTHS 앞선 호. 종목마다 consensus FFO FY1 변화와 FY2/FY1 성장을 더한다."""
    if ticker:
        hit = [r for r in doc["rows"] if r["ticker"] == ticker.upper()]
        if not hit:
            return None, "%s 호에 %s가 없음" % (doc["month"], ticker)
        subsector = hit[0]["subsector"]
    rows = [r for r in doc["rows"] if r["subsector"] == subsector]
    if not rows:
        subs = sorted({r["subsector"] for r in doc["rows"]})
        return None, "subsector %r가 없음(있는 것: %s)" % (subsector, ", ".join(subs))
    rows.sort(key=lambda r: (r["p_ffo_fy1"] is None, r["p_ffo_fy1"] if r["p_ffo_fy1"] is not None else 0, r["ticker"]))
    rows = [dict(r, ffo_fy1_rev_3m=ffo_revision(doc, old_doc, r["ticker"])[0], ffo_growth_fy2=ffo_growth(r))
            for r in rows]
    med = {k: median(r[k] for r in rows) for k in PEER_FIELDS
           if k not in ("ticker", "name", "rating")}
    return {"schema": "reit-peers/1", "month": doc["month"], "as_of": doc["as_of"], "source": doc["source"],
            "status": "reported", "subsector": subsector, "ticker": ticker.upper() if ticker else None,
            "revision_vs_month": old_doc["month"] if old_doc else None,
            "revision_rule": "ffo_fy1_rev_3m = 새 호 FY1 / 앞 호의 같은 회계연도 consensus FFO(FY 교체 뒤면 FY2) - 1(%), "
                             "ffo_growth_fy2 = FY2 / FY1 - 1(%). 둘 다 derived",
            "rows": [{k: r.get(k) for k in PEER_FIELDS} for r in rows],
            "median": dict(med, status="derived")}, ""


def cmd_peers(args, base=None):
    if not args.subsector and not args.ticker:
        log("--subsector나 --ticker를 준다")
        return 1
    docs = load_parsed(base)
    if not docs:
        log("parsed 파일이 없음. 먼저 fetch --month latest를 돌린다")
        return 1
    latest = docs[-1]
    old = find_month(docs, fmt_month(*shift_month(*parse_month(latest["month"]), -REVISION_MONTHS)))
    if old is None:
        log("%d개월 앞 호가 없어 FFO 변화 칸은 비운다" % REVISION_MONTHS)
    out, why = build_peers(latest, args.subsector, args.ticker, old)
    if out is None:
        log(why)
        return 1
    log("%s (%s 호, as_of %s): %d종목" % (out["subsector"], out["month"], out["as_of"], len(out["rows"])))
    emit(out, args.out)
    return 0


# ---------------------------------------------------------------- CLI

def main(argv):
    p = argparse.ArgumentParser(prog="reitwatch.py", description="REITWatch subsector·peer 표")
    p.add_argument("--check", action="store_true", help="network 없이 fixture로 확인")
    sub = p.add_subparsers(dest="cmd")
    f = sub.add_parser("fetch", help="REITWatch 호를 받아 parsed JSON을 쓴다")
    f.add_argument("--month", required=True, help="YYYY-MM 또는 latest")
    f.add_argument("--backfill", action="store_true", help="2015-01부터 분기 호 + 최근 12개월")
    s = sub.add_parser("sectors", help="subsector 표")
    s.add_argument("--macro-snapshot")
    s.add_argument("--out")
    q = sub.add_parser("peers", help="한 subsector의 종목 표")
    g = q.add_mutually_exclusive_group()
    g.add_argument("--subsector")
    g.add_argument("--ticker")
    q.add_argument("--out")
    t = sub.add_parser("tracker", help="Nareit REIT Industry Tracker")
    tsub = t.add_subparsers(dest="tcmd")
    tf = tsub.add_parser("fetch", help="Tracker XLSX를 받아 parsed JSON을 쓴다")
    tf.add_argument("--quarter", required=True, help="YYYYQn 또는 latest")
    tf.add_argument("--url", help="후보 URL이 모두 404일 때 reit.com에서 찾은 XLSX URL")
    try:
        args = p.parse_args(argv)
    except SystemExit as e:
        return 0 if e.code == 0 else 1
    if args.check:
        return run_check()
    if args.cmd == "fetch":
        return cmd_fetch(args)
    if args.cmd == "sectors":
        return cmd_sectors(args)
    if args.cmd == "peers":
        return cmd_peers(args)
    if args.cmd == "tracker" and args.tcmd == "fetch":
        return tracker.cmd_fetch(args, data_dir(), dt.date.today(), write_json, log)
    p.print_help(sys.stderr)
    return 1


# ---------------------------------------------------------------- --check

def run_check():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fx = os.path.join(root, "tests", "fixtures")
    exp = read_json(os.path.join(root, "tests", "expected.json"))
    today = dt.date(*[int(x) for x in exp["today"].split("-")])
    fails = []

    def ok(cond, label):
        if not cond:
            fails.append(label)
        log(("PASS " if cond else "FAIL ") + label)

    # 1. assets
    smap = read_json(assets_path())
    bad = [(sec, i.get("id")) for sec, items in smap["indicators"].items() for i in items
           if i.get("role") not in ("demand", "supply", "context") or (i["role"] == "demand" and i.get("good") not in ("up", "down"))]
    ok(not bad and all(v is None or v in smap["indicators"] for v in smap["subsectors"].values()),
       "assets/sector_map.json 모양 %s" % (bad or ""))
    al = {k: v for k, v in smap["aliases"].items() if not k.startswith("_")}
    ok(all(v in smap["subsectors"] for v in al.values()) and not any(k in smap["subsectors"] for k in al),
       "aliases는 지금 이름으로만 잇는다")
    agg = aggregate({"rows": [{"subsector": "Timber", "p_ffo_fy1": 20.0, "div_yield": 3.0, "tr_3y": None, "tr_5y": None},
                              {"subsector": "Timberland", "p_ffo_fy1": 30.0, "div_yield": 4.0, "tr_3y": None, "tr_5y": None}]},
                    {"Timber": "Timberland"})
    ok(list(agg) == ["Timberland"] and agg["Timberland"]["p_ffo"] == 25.0, "예전 이름(Timber)을 지금 이름에 이음")

    # 2. 파싱
    with open(os.path.join(fx, "RW_sample.txt"), encoding="utf-8") as f:
        text = f.read()
    pe = exp["parse"]
    doc = parse_text(text, pe["month"], "fixture:RW_sample.txt", min_rows=1)
    by = {r["ticker"]: r for r in doc["rows"]}
    ok(list(doc.keys())[0] == "schema" and doc["schema"] == SCHEMA, "parsed 첫 칸이 schema")
    ok((doc["month"], doc["as_of"], doc["fy1"], doc["fy2"]) == (pe["month"], pe["as_of"], pe["fy1"], pe["fy2"]),
       "month·as_of·fy1·fy2 (got %s %s %s %s)" % (doc["month"], doc["as_of"], doc["fy1"], doc["fy2"]))
    ok([r["ticker"] for r in doc["rows"]] == pe["tickers"], "종목 %d개와 순서 (got %s)" % (
        pe["n_rows"], [r["ticker"] for r in doc["rows"]]))
    for t in ("FSB", "FSA", "OFB", "OFC", "FSE"):
        got = {k: by.get(t, {}).get(k) for k in pe[t]}
        ok(got == pe[t], "%s 칸 %s" % (t, "" if got == pe[t] else "got %r" % got))
    ok(all(set(r.keys()) == set(ROW_FIELDS) for r in doc["rows"]) and not any(k in ROW_FIELDS for k in pe["no_fields"]),
       "row 칸 이름이 계약대로, ffo_payout·debt_ebitda 없음")
    ok(any(pe["unparsed_has"] in u for u in doc["unparsed"]) and len(doc["unparsed"]) == 1,
       "column 수가 다른 줄만 unparsed (got %r)" % doc["unparsed"])
    try:
        parse_text(text, pe["month"], "https://example.org/RW (가상)")
        ok(False, "종목이 50개 미만이면 파싱 실패")
    except ParseError:
        ok(True, "종목이 50개 미만이면 파싱 실패")

    # 3. backfill 달 목록
    be = exp["backfill"]
    ms = backfill_months(be["month"])
    ok(len(ms) == be["count"] and ms[0] == be["first"] and ms[-1] == be["last"]
       and all(m in ms for m in be["has"]) and not any(m in ms for m in be["not_has"]),
       "backfill 달 목록 %d개 (got %d)" % (be["count"], len(ms)))

    with tempfile.TemporaryDirectory() as tmp:
        base = os.path.join(tmp, "reitwatch", "parsed")
        os.makedirs(base)
        for p in glob.glob(os.path.join(fx, "parsed", "*.json")):
            shutil.copy(p, base)
        calls = []

        def fixture_text(month):
            calls.append(month)
            if month == "2026-10":
                return text, "fixture:RW_sample.txt"
            raise NotPublished("HTTP 404 (가상)")

        # 4. fetch: latest는 404면 앞 달로, backfill은 있는 파일을 건너뛴다
        a = argparse.Namespace(month="latest", backfill=False)
        code = cmd_fetch(a, get_text=fixture_text, today=dt.date(2026, 11, 18), base=base, min_rows=1)
        ok(code == 0 and calls == ["2026-11", "2026-10"] and os.path.exists(parsed_path("2026-10", base)),
           "fetch --month latest는 404인 2026-11을 건너 2026-10을 받음 (got %s %s)" % (code, calls))
        calls.clear()
        code = cmd_fetch(argparse.Namespace(month="2026-10", backfill=True), get_text=fixture_text, base=base, min_rows=1)
        ok("2026-10" not in calls and "2026-07" not in calls and "2015-01" in calls and code == 2,
           "backfill은 이미 받은 호를 건너뛰고 못 받은 호가 있으면 exit 2")
        hist = os.path.join(tmp, "reitwatch", "history.csv")
        ok(os.path.exists(hist), "history.csv를 씀")

        # 5. sectors
        snap_p = os.path.join(fx, "macro_snapshot.json")
        snap = read_json(snap_p)
        docs = load_parsed(base)
        out, code = build_sectors(docs, snap, snap_p, "", smap, today)
        rows = {r["subsector"]: r for r in out["subsectors"]}
        for sub, want in exp["sectors"].items():
            r = rows.get(sub)
            if r is None:
                ok(False, "%s: 표에 없음" % sub)
                continue
            got = {"n": r["n"], "history_points": r["history_points"], "macro_sector": r["macro_sector"],
                   "structural": r["structural"]["flag"]}
            for k in ("p_ffo_median", "p_ffo_pctile", "p_ffo_z", "div_yield_median", "div_yield_pctile",
                      "div_yield_z", "spread_dgs10", "rel_tr_3y", "rel_tr_5y"):
                got[k] = r[k]["value"]
            diff = [k for k in want if not k.startswith("_") and not _same(got.get(k), want[k])]
            ok(not diff, "%s 값 %s" % (sub, "" if not diff else "다름: " + ", ".join(
                "%s got=%r want=%r" % (k, got.get(k), want[k]) for k in diff)))
            if want["p_ffo_pctile"] is None:
                ok(r["p_ffo_pctile"]["status"] == "missing" and "history" in r["p_ffo_pctile"]["note"],
                   "%s: history가 짧으면 percentile missing과 이유" % sub)
        ok([e["subsector"] for e in out["excluded"]] == exp["excluded"], "mortgage subsector는 표에서 뺌")
        ok(out["history_months"][-2:] == ["2026-07", "2026-10"], "2026-09(분기 아님) 호는 표본에서 뺌")
        fs = rows["Free Standing"]
        ok(fs["spread_dgs10"]["status"] == "derived" and "2026-11-16" in fs["spread_dgs10"]["note"],
           "spread는 derived, 두 기준일을 note에")
        ok(fs["p_ffo_median"]["source"].startswith("reitwatch:") and fs["p_ffo_median"]["locator"],
           "median에 source·locator")
        ok(out["macro_sectors_needed"] == ["industrial"], "snapshot에 없는 섹터를 알림 (got %s)" % out["macro_sectors_needed"])
        off = {i["id"]: i["judged"] for i in rows["Office"]["structural"]["indicators"]}
        ok(off == {"USPBS": "나빠짐", "USINFO": "아님", "USFIRE": None, "TLOFCONS": "늘어남"},
           "office 지표 판정 (got %s)" % off)
        ok(code == exp["exit_code"], "missing 칸이 있으면 exit 2")

        # 6. macro snapshot이 없으면 spread missing, 오래되면 stale
        snap2, p2, note2 = load_snapshot(os.path.join(tmp, "nope.json"))
        out2, code2 = build_sectors(docs, snap2, p2, note2, smap, today)
        r2 = {r["subsector"]: r for r in out2["subsectors"]}["Free Standing"]
        ok(snap2 is None and r2["spread_dgs10"]["value"] is None and r2["spread_dgs10"]["status"] == "missing"
           and "없음" in r2["spread_dgs10"]["note"] and r2["structural"]["flag"] == "review" and code2 == 2,
           "macro snapshot이 없으면 spread missing, 구조 신호 review")
        old = dict(snap, date="2026-11-01")
        out3, _ = build_sectors(docs, old, snap_p, "", smap, today)
        r3 = {r["subsector"]: r for r in out3["subsectors"]}["Free Standing"]
        ok(out3["dgs10"]["status"] == "stale" and r3["spread_dgs10"]["status"] == "stale"
           and r3["spread_dgs10"]["value"] == 0.75, "7일 넘은 snapshot이면 spread stale")

        # 7. peers
        latest = docs[-1]
        pr, _ = build_peers(latest, subsector="Free Standing")
        ok([r["ticker"] for r in pr["rows"]] == exp["peers_free_standing"] and pr["median"]["p_ffo_fy1"] == 14.0,
           "peers --subsector: P/FFO 오름차순, 값 없는 종목은 끝")
        pt, _ = build_peers(latest, ticker="fsb")
        ok(pt and pt["subsector"] == "Free Standing", "peers --ticker는 그 종목의 subsector")
        none, why = build_peers(latest, ticker="ZZZZ")
        ok(none is None and why, "없는 ticker는 거절")
        ok(main(["peers"]) == 1 and main(["fetch", "--month", "2026-13"]) == 1, "입력 오류는 exit 1")

        # 8. consensus FFO 변화와 FY2/FY1 성장
        rv = exp["revision"]
        old = find_month(docs, rv["vs_month"])
        pr2, _ = build_peers(latest, subsector="Free Standing", old_doc=old)
        got = {r["ticker"]: r["ffo_fy1_rev_3m"] for r in pr2["rows"]}
        ok(got == rv["peers"] and pr2["revision_vs_month"] == rv["vs_month"],
           "peers 종목별 FFO FY1 3개월 변화 (got %s)" % got)
        ok(_same(pr2["median"]["ffo_fy1_rev_3m"], rv["fs_rev_median"]) and
           _same(pr2["median"]["ffo_growth_fy2"], rv["fs_growth_median"]), "peers median 변화·FY2/FY1 성장")
        ok(out["revision_vs_month"] == rv["vs_month"] and _same(rows["Free Standing"]["ffo_rev_3m_median"]["value"],
                                                                rv["fs_rev_median"])
           and _same(rows["Free Standing"]["ffo_growth_fy2_median"]["value"], rv["fs_growth_median"])
           and rows["Office"]["ffo_rev_3m_median"]["value"] is None
           and rows["Office"]["ffo_rev_3m_median"]["status"] == "missing",
           "sectors 표의 FFO 변화·성장 median, 값이 없는 subsector는 missing")
        new = {"month": "2026-04", "fy1": 2026, "fy2": 2027, "rows": [{"ticker": "A", "ffo_fy1": 2.2}]}
        old1 = {"month": "2026-01", "fy1": 2025, "fy2": 2026, "rows": [{"ticker": "A", "ffo_fy1": 1.9, "ffo_fy2": 2.0}]}
        old2 = {"month": "2025-04", "fy1": 2024, "fy2": 2025, "rows": [{"ticker": "A", "ffo_fy1": 1.9, "ffo_fy2": 2.0}]}
        ok(ffo_revision(new, old1, "A")[0] == rv["fy_roll"] and ffo_revision(new, old2, "A")[0] is None
           and ffo_revision(new, None, "A")[0] is None,
           "FY 교체 뒤에는 앞 호의 FY2와 견주고, 같은 회계연도 column이 없으면 빈 값")
        no_old, _ = build_sectors([d for d in docs if d["month"] != rv["vs_month"]], snap, snap_p, "", smap, today)
        ok(no_old["revision_vs_month"] is None and all(r["ffo_rev_3m_median"]["status"] == "missing"
                                                       for r in no_old["subsectors"]), "3개월 앞 호가 없으면 FFO 변화 missing")

        # 9. Tracker 파싱
        tp = exp["tracker_parse"]
        xlsx = os.path.join(tmp, "tracker.xlsx")
        _zip_dir(os.path.join(fx, "tracker_xlsx"), xlsx)
        tdoc = tracker.parse_tracker(xlsx, "fixture:tracker_xlsx (가상)")
        trow = {r["name"]: r for r in tdoc["rows"]}
        ok(list(tdoc)[0] == "schema" and tdoc["schema"] == tracker.SCHEMA, "Tracker parsed 첫 칸이 schema")
        ok((tdoc["quarter"], tdoc["as_of"], tdoc["sheet"], len(tdoc["quarters"]), tdoc["quarters"][0]) ==
           (tp["quarter"], tp["as_of"], tp["sheet"], tp["n_quarters"], tp["first"]),
           "Tracker 분기·기준일·sheet·머리줄 (got %s %s %s %d)" % (tdoc["quarter"], tdoc["as_of"], tdoc["sheet"],
                                                              len(tdoc["quarters"])))
        ok(tdoc["missing_blocks"] == tp["missing_blocks"], "없는 block 목록 (got %s)" % tdoc["missing_blocks"])
        ok(sorted(trow) == sorted(tp["names"]), "읽은 row 이름: 모르는 block의 row는 넣지 않음 (got %s)" % sorted(trow))
        for nm in ("Office", "Industrial", "Free Standing", "Specialty", "Timberland"):
            got = {k: trow.get(nm, {}).get(k) for k in tp[nm]}
            ok(all(_same(got[k], tp[nm][k]) for k in tp[nm]), "Tracker %s 칸 (got %s)" % (nm, got))
        ok(trow["Office"]["cells"]["implied_cap_rate"] == tp["office_cap_cell"]
           and len(trow["Office"]["history"]["implied_cap_rate"]) == tp["office_cap_history_n"]
           and len(trow["Industrial"]["history"]["implied_cap_rate"]) == tp["industrial_cap_history_n"],
           "셀 위치와 history(0은 뺌, 다른 block의 같은 이름 row는 섞지 않음)")
        for bad, why in (("Data Q2 2026", "sheet 이름과 머리줄 분기가 다르면 실패"),):
            bdir = os.path.join(tmp, "bad_xlsx")
            shutil.copytree(os.path.join(fx, "tracker_xlsx"), bdir)
            wbp = os.path.join(bdir, "xl", "workbook.xml")
            with open(wbp, encoding="utf-8") as f:
                w = f.read().replace("Data Q3 2026", bad)
            with open(wbp, "w", encoding="utf-8") as f:
                f.write(w)
            _zip_dir(bdir, os.path.join(tmp, "bad.xlsx"))
            try:
                tracker.parse_tracker(os.path.join(tmp, "bad.xlsx"), "x")
                ok(False, why)
            except tracker.TrackerError:
                ok(True, why)
        try:
            tracker.parse_tracker(xlsx, "x", want="2026Q2")
            ok(False, "요청한 분기와 파일 분기가 다르면 실패")
        except tracker.TrackerError:
            ok(True, "요청한 분기와 파일 분기가 다르면 실패")

        # 10. Tracker 받기: 후보 폴더 달을 차례로, XLSX는 지운다
        dd = os.path.join(tmp, "data")
        tcalls = []

        def fake_get(url, dest):
            tcalls.append(url)
            if "/2026-10/" in url:
                raise tracker.NotPublished("HTTP 404 (가상)")
            shutil.copy(xlsx, dest)

        tc = tracker.cmd_fetch(argparse.Namespace(quarter="latest", url=None), dd, dt.date(2026, 11, 18),
                               write_json, log, get=fake_get)
        left = [f for _, _, fs in os.walk(os.path.join(dd, "tracker")) for f in fs if f.endswith(".xlsx")]
        ok(tc == 0 and [u.split("/")[-2] for u in tcalls] == ["2026-10", "2026-11"]
           and os.path.exists(tracker.parsed_path("2026Q3", dd)) and not left,
           "tracker fetch latest: 2026-10 폴더 404 뒤 2026-11, parsed만 남음 (got %s %s %s)" % (tc, tcalls, left))
        ok(tracker.cmd_fetch(argparse.Namespace(quarter="2026Q5", url=None), dd, today, write_json, log,
                             get=fake_get) == 1, "잘못된 분기는 exit 1")
        tdoc2, tpath2, _ = tracker.load_latest(dd)

        # 11. 섹터 표의 Tracker 칸과 구조 신호
        ts = exp["tracker_sectors"]
        out4, code4 = build_sectors(docs, snap, snap_p, "", smap, today, tdoc2, tpath2, "")
        r4 = {r["subsector"]: r for r in out4["subsectors"]}
        for sub in ("Office", "Industrial", "Free Standing"):
            got = {k: r4[sub]["tracker"][k]["value"] for k in ts[sub]}
            diff = [k for k in ts[sub] if not _same(got[k], ts[sub][k])]
            ok(not diff, "Tracker 칸 %s %s" % (sub, "" if not diff else "다름: " + ", ".join(
                "%s got=%r want=%r" % (k, got[k], ts[sub][k]) for k in diff)))
        sts = {k: r4[k.split(".")[0]]["tracker"][k.split(".")[1]]["status"] for k in ts["status"]}
        ok(sts == ts["status"], "Tracker 칸 status (got %s)" % sts)
        fs4 = r4["Free Standing"]["tracker"]
        ok("Retail" in fs4["ss_noi_yoy"]["note"] and "Retail" in fs4["ss_noi_yoy"]["locator"]
           and fs4["implied_cap_rate"]["locator"].startswith("Data Q3 2026!P46"),
           "parent 값은 note·locator에 Retail, 자기 값은 셀 위치")
        jd = {sub: {i["id"]: i["judged"] for i in r4[sub]["structural"]["indicators"] if i["id"].startswith("tracker:")}
              for sub in ts["judged"]}
        ok(jd == ts["judged"], "Tracker 지표 판정 (got %s)" % jd)
        fl = {sub: r4[sub]["structural"]["flag"] for sub in ts["structural"]}
        ok(fl == ts["structural"] and not any(i["id"].startswith("tracker:")
                                             for i in r4["Free Standing"]["structural"]["indicators"]),
           "구조 신호에 Tracker 지표를 넣고 parent 값은 넣지 않음 (got %s)" % fl)
        ok(out4["baa"]["value"] == 6.25 and out4["baa"]["locator"] == "series.BAA10Y + series.DGS10"
           and out4["tracker"]["quarter"] == "2026Q3" and code4 == 2,
           "Baa = BAA10Y + DGS10, 표 머리에 Tracker 분기")
        snap_d = json.loads(json.dumps(snap))
        snap_d["series"]["DBAA"] = dict(snap["series"]["BAA10Y"], value=6.0, source="fred:DBAA")
        out5, _ = build_sectors(docs, snap_d, snap_p, "", smap, today, tdoc2, tpath2, "")
        r5 = {r["subsector"]: r for r in out5["subsectors"]}["Free Standing"]["tracker"]["cap_spread_baa"]
        ok(_same(r5["value"], ts["dbaa_spread_fs"]) and "DBAA" in r5["locator"], "DBAA가 있으면 그것을 Baa로")
        late = dt.date(*[int(x) for x in ts["stale_today"].split("-")])
        c6, _, _ = tracker.columns(tdoc2, tpath2, "", smap["tracker"]["Office"], out4["dgs10"], out4["baa"], late)
        ok(c6["implied_cap_rate"]["status"] == "stale" and "150" in c6["implied_cap_rate"]["note"]
           and c6["implied_cap_rate_pctile"]["status"] == "stale",
           "Tracker 분기 끝날이 150일 넘게 전이면 stale")
        out7, code7 = build_sectors(docs, snap, snap_p, "", smap, today, None, None, "Tracker parsed가 없음(가상)")
        r7 = {r["subsector"]: r for r in out7["subsectors"]}
        ok(all(v["status"] == "missing" and "Tracker parsed" in v["note"] for v in
               (r7["Office"]["tracker"][k] for k in tracker.COLUMNS)) and code7 == 2
           and r7["Office"]["structural"]["flag"] == "watch" and out7["summary"]["tracker_note"],
           "Tracker가 없으면 칸 missing, 구조 신호는 macro만으로 계속, exit 2")
        full = docs[:-1] + [dict(latest, rows=[r for r in latest["rows"]
                                              if r["subsector"] == "Free Standing" and r["ffo_fy1"] is not None])]
        _, c8 = build_sectors(full, snap, snap_p, "", smap, today, tdoc2, tpath2, "")
        _, c9 = build_sectors(full, snap, snap_p, "", smap, today, None, None, "없음(가상)")
        ok((c8, c9) == (0, 2), "REITWatch 칸이 다 차면 exit 0, Tracker만 없어도 exit 2 (got %s %s)" % (c8, c9))
        ok(all(k in smap["tracker"] for k in smap["subsectors"]), "sector_map.json tracker 대응표가 subsector를 모두 덮음")

        # 12. 2차 검증 결함: macro가 판단 못 하면 Tracker로 판정을 올리지 않음, Tracker 칸의 입력 부족은 exit 2
        def snap_with(change):
            s2 = json.loads(json.dumps(snap))
            change(s2["series"])
            return s2

        def off_missing(series):
            for spec in smap["indicators"]["office"]:
                if spec["id"] in series:
                    series[spec["id"]].update(value=None, status="missing", note="받기 실패(가상)", derived={})
        o12, _ = build_sectors(docs, snap_with(off_missing), snap_p, "", smap, today, tdoc2, tpath2, "")
        st12 = {r["subsector"]: r for r in o12["subsectors"]}["Office"]["structural"]
        ok(st12["flag"] == "review" and any(i["id"].startswith("tracker:") for i in st12["indicators"])
           and "Tracker" in st12["note"],
           "F2 macro 섹터 지표가 모두 missing이면 Tracker 지표가 있어도 review(Tracker는 표에만) (got %s: %s)" % (
               st12["flag"], st12["note"]))
        o13, _ = build_sectors(docs, snap_with(lambda s: s["TLOFCONS"].update(derived={})), snap_p, "", smap, today,
                               tdoc2, tpath2, "")
        st13 = {r["subsector"]: r for r in o13["subsectors"]}["Office"]["structural"]
        ok(st13["flag"] == "review" and "supply" in st13["note"],
           "F2 supply 지표가 있는 섹터인데 supply 값이 없으면 macro 판정은 review (got %s)" % st13["flag"])
        _, c14 = build_sectors(full, snap_with(lambda s: s["BAA10Y"].update(value=None, status="missing",
                                                                            note="받기 실패(가상)")),
                               snap_p, "", smap, today, tdoc2, tpath2, "")
        o14, _ = build_sectors(docs, snap_with(lambda s: s["BAA10Y"].update(value=None, status="missing",
                                                                            note="받기 실패(가상)")),
                               snap_p, "", smap, today, tdoc2, tpath2, "")
        ok(c14 == 2 and "BAA10Y" in o14["baa"]["note"] and "DGS10도" not in o14["baa"]["note"],
           "F3 Tracker가 내는 칸(cap_spread_baa)이 BAA10Y가 없어 missing이면 exit 2, note는 실제 빠진 입력 (got %s, %s)" % (
               c14, o14["baa"]["note"]))
        short = json.loads(json.dumps(tdoc2))
        for r in short["rows"]:
            h = r["history"].get("implied_cap_rate", {})
            r["history"]["implied_cap_rate"] = {k: h[k] for k in sorted(h)[-5:]}
        _, c15 = build_sectors(full, snap, snap_p, "", smap, today, short, tpath2, "")
        ok(c15 == 2, "F3 cap rate history가 짧아 percentile이 missing이면 exit 2 (got %s)" % c15)
        noocc = json.loads(json.dumps(tdoc2))
        for r in noocc["rows"]:
            if r["name"] in ("Free Standing", "Retail"):
                r["occupancy"] = None
        o16, c16 = build_sectors(full, snap, snap_p, "", smap, today, noocc, tpath2, "")
        ok(c16 == 0 and o16["summary"]["tracker_missing_cells"] == 3,
           "Tracker가 그 subsector·칸을 내지 않으면(occupancy와 변화 3칸) exit에 세지 않고 tracker_missing_cells에 셈 (got %s %s)" % (
               c16, o16["summary"]["tracker_missing_cells"]))
        for sid in ("BAA10Y", "DGS10"):
            try:
                o17, c17 = build_sectors(full, snap_with(lambda s, sid=sid: s[sid].update(value=float("nan"))),
                                         snap_p, "", smap, today, tdoc2, tpath2, "")
                json.dumps(o17, allow_nan=False)
                good = c17 == 2 and o17["baa"]["value"] is None
            except ValueError:
                good = False
            ok(good, "macro snapshot %s가 NaN이면 그 값 missing, 표는 NaN 없이 계속(exit 2)" % sid)

    log("check: %d개 실패" % len(fails) if fails else "check: 모두 통과")
    return 2 if fails else 0


def _zip_dir(src, dest):
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for d, _, files in os.walk(src):
            for f in sorted(files):
                p = os.path.join(d, f)
                z.write(p, os.path.relpath(p, src))


def _same(a, b):
    if isinstance(a, float) or isinstance(b, float):
        return a is not None and b is not None and abs(a - b) < 1e-9
    return a == b


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

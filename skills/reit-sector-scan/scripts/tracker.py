"""reit-sector-scan: Nareit REIT Industry Tracker(분기 XLSX)를 받아 파싱하고, 섹터 표의 Tracker 칸을 계산한다.

reitwatch.py가 import한다(`reitwatch.py tracker fetch --quarter YYYYQn|latest`). Python 3 stdlib(zipfile, xml)만 쓴다.
XLSX는 Nareit 저작물(출처 S&P Capital IQ Pro)이라 파싱한 뒤 지우고 parsed JSON만 데이터 폴더에 남긴다.
"""
import datetime as dt
import glob
import json
import os
import re
import shutil
import statistics
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

SCHEMA = "nareit-tracker/1"
URL = "https://www.reit.com/sites/default/files/%s/REIT_Industry_Tracker_%s.xlsx"  # (올린 달 YYYY-MM, 분기 YYYYQn)
PUBLISH_LAG_MONTHS = (1, 2, 3)   # 분기 끝 달 뒤 몇 달째 폴더에 올라오나(2026Q1은 2026-05, 2026Q3은 2026-11)
PCTILE_START = "2015Q1"          # implied cap rate percentile 표본의 첫 분기(REITWatch history와 같은 기간)
MIN_HISTORY = 8                  # 표본이 이보다 적으면 percentile은 missing
MAX_AGE_DAYS = 150               # 분기 끝날이 오늘보다 이 일수 넘게 이르면 Tracker 값은 stale(한 분기를 놓친 것)
MAX_MEMBER_BYTES = 64 * 1024 * 1024
# 'Data Qn YYYY' sheet의 block 제목 -> (parsed 칸, 단위). 목록에 없는 block은 읽지 않는다.
BLOCKS = {
    "Implied Cap Rate": ("implied_cap_rate", "Percent"),
    "Rolling 4-quarter Cap Rate": ("cap_rate_4q", "Percent"),
    "Same Store NOI": ("ss_noi_yoy", "Percent"),
    "Occupancy Rates": ("occupancy", "Percent"),
    "Debt to EBITDA": ("debt_to_ebitda", "x"),
    "Debt/Market Assets": ("debt_to_market_assets", "Percent"),
    "Dividend Payout": ("payout", "Percent"),
    "Property Development Pipeline": ("dev_pipeline", "USD millions"),
    "Total Property Holdings, undepreciated book value": ("property_holdings", "USD millions"),
}
DERIVED = {"dev_pipeline_pct_assets": "Percent"}  # dev_pipeline / property_holdings * 100
REQUIRED = ("implied_cap_rate",)
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


class TrackerError(Exception):
    pass


class NotPublished(TrackerError):
    pass


# ---------------------------------------------------------------- 분기

def parse_quarter(s):
    m = re.match(r"^(\d{4})Q([1-4])$", s or "")
    if not m:
        raise ValueError("quarter는 YYYYQn: %r" % s)
    return int(m.group(1)), int(m.group(2))


def fmt_quarter(y, q):
    return "%04dQ%d" % (y, q)


def shift_quarter(y, q, n):
    k = y * 4 + (q - 1) + n
    return k // 4, k % 4 + 1


def quarter_end(y, q):
    m = q * 3
    nxt = dt.date(y + (m == 12), m % 12 + 1, 1)
    return nxt - dt.timedelta(days=1)


def latest_quarter(today):
    """오늘 이전에 끝난 마지막 분기."""
    return shift_quarter(today.year, (today.month - 1) // 3 + 1, -1)


def folder_months(y, q, today):
    """그 분기 XLSX가 올라올 수 있는 폴더 달(오늘 달까지)."""
    out = []
    for lag in PUBLISH_LAG_MONTHS:
        k = y * 12 + q * 3 - 1 + lag
        yy, mm = k // 12, k % 12 + 1
        if (yy, mm) <= (today.year, today.month):
            out.append("%04d-%02d" % (yy, mm))
    return out


# ---------------------------------------------------------------- XLSX 읽기

def _col_index(col):
    n = 0
    for ch in col:
        n = n * 26 + ord(ch) - 64
    return n


def _read_member(z, name):
    info = z.getinfo(name)
    if info.file_size > MAX_MEMBER_BYTES:
        raise TrackerError("XLSX 안 %s가 너무 큼(%d bytes)" % (name, info.file_size))
    return z.read(name)


def read_sheet(path, pattern):
    """이름이 pattern에 맞는 첫 worksheet → (sheet 이름, [(row 번호, {column: (type, text)})])."""
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise TrackerError("XLSX(zip)가 아님: %s" % path)
    with z:
        names = set(z.namelist())
        ss = []
        if "xl/sharedStrings.xml" in names:
            for si in ET.fromstring(_read_member(z, "xl/sharedStrings.xml")).findall(NS + "si"):
                ss.append("".join(t.text or "" for t in si.iter(NS + "t")))
        wb = ET.fromstring(_read_member(z, "xl/workbook.xml"))
        rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(_read_member(z, "xl/_rels/workbook.xml.rels"))}
        for s in wb.find(NS + "sheets"):
            name = s.get("name")
            if not re.match(pattern, name or ""):
                continue
            target = rels.get(s.get(RID), "")
            member = target.lstrip("/") if target.startswith("/") else "xl/" + target
            if "worksheets/" not in member or member not in names:
                continue
            sheet = ET.fromstring(_read_member(z, member))
            rows = []
            for row in sheet.find(NS + "sheetData"):
                cells = {}
                for c in row:
                    v = c.find(NS + "v")
                    ref = c.get("r") or ""
                    col = re.match(r"[A-Z]+", ref)
                    if col is None:
                        continue
                    t = c.get("t")
                    if t == "inlineStr":
                        text = "".join(x.text or "" for x in c.iter(NS + "t"))
                        cells[col.group(0)] = ("s", text)
                    elif v is not None and v.text is not None:
                        if t == "s":
                            cells[col.group(0)] = ("s", ss[int(v.text)])
                        elif t in (None, "n"):
                            cells[col.group(0)] = ("n", v.text)
                        else:
                            cells[col.group(0)] = ("s", v.text)
                rows.append((int(row.get("r")), cells))
            return name, rows
    raise TrackerError("'%s' 모양의 worksheet가 없음" % pattern)


def _header_quarter(cell):
    """머리줄 칸 2026.3 → '2026Q3'. 아니면 None."""
    if cell is None or cell[0] != "n":
        return None
    try:
        f = float(cell[1])
    except ValueError:
        return None
    y = int(f)
    q = int(round((f - y) * 10))
    if not 1 <= q <= 4 or abs(f - (y + q / 10.0)) > 1e-6:
        return None
    return fmt_quarter(y, q)


def _number(cell):
    if cell is None or cell[0] != "n":
        return None
    try:
        x = float(cell[1])
    except ValueError:
        return None
    return x if x == x and abs(x) != float("inf") else None


def parse_tracker(path, source, want=None):
    """Tracker XLSX → parsed dict. 머리줄(2000.1 … 2026.3)의 column으로 분기를 정하고, block 제목 아래 row를 읽는다.

    Tracker는 값이 없는 칸을 0으로 쓴다(2026Q3 Free Standing SS NOI는 PDF에 '-', XLSX에 0). 0과 빈 값을 가를 수 없어
    정확히 0인 칸은 값이 없는 것으로 본다."""
    sheet, rows = read_sheet(path, r"^Data Q[1-4] \d{4}$")
    m = re.match(r"^Data Q([1-4]) (\d{4})$", sheet)
    sheet_q = fmt_quarter(int(m.group(2)), int(m.group(1)))
    header, hrow = None, None
    for rn, cells in rows:
        qs = {col: _header_quarter(c) for col, c in cells.items() if col != "A"}
        qs = {col: q for col, q in qs.items() if q}
        if len(qs) >= 8:
            header, hrow = qs, rn
            break
    if header is None:
        raise TrackerError("분기 머리줄(2000.1 …)을 찾지 못함")
    cols = sorted(header, key=_col_index)
    quarters = [header[c] for c in cols]
    if quarters != sorted(quarters) or len(set(quarters)) != len(quarters):
        raise TrackerError("머리줄 분기가 차례대로가 아님")
    if quarters[-1] != sheet_q:
        raise TrackerError("머리줄 마지막 분기 %s가 sheet 이름 %s와 다름" % (quarters[-1], sheet))
    if want and want != sheet_q:
        raise TrackerError("받은 파일은 %s(%s), 요청은 %s" % (sheet_q, sheet, want))
    last_col = cols[-1]

    # 값이 하나라도 있는 row 이름(header 줄 제외) = sector 이름. 값이 없는 줄 중 이 이름이 아닌 것이 block 제목·단위 줄이다.
    def nums(cells):
        return {header[c]: _number(cells.get(c)) for c in cols if _number(cells.get(c)) is not None}

    sector_names = set()
    for rn, cells in rows:
        a = cells.get("A")
        if rn != hrow and a and a[0] == "s" and nums(cells):
            sector_names.add(a[1].strip())
    data, order, found = {}, [], []
    cur, started = None, False
    for rn, cells in rows:
        if rn <= hrow:
            continue
        a = cells.get("A")
        if not a or a[0] != "s" or not a[1].strip():
            continue
        name = a[1].strip()
        vals = nums(cells)
        if not vals and name not in sector_names:
            if name in BLOCKS:
                cur, started = BLOCKS[name][0], False
                found.append(cur)
            elif cur and started:
                cur = None
            continue
        if cur is None or not vals:
            continue
        started = True
        r = data.setdefault(name, {})
        if name not in order:
            order.append(name)
        if cur in r:
            continue  # 같은 block에 같은 이름이 또 나오면 첫 줄만 쓴다
        r[cur] = {"hist": {q: v for q, v in vals.items() if v != 0}, "row": rn}
    missing_req = [k for k in REQUIRED if k not in found]
    if missing_req:
        raise TrackerError("필수 block이 없음: %s. layout이 바뀌었을 수 있다" % ", ".join(missing_req))
    keys = [k for k, _ in BLOCKS.values()]
    out_rows = []
    for name in order:
        r = data[name]
        row = {"name": name}
        hist, cells_at = {}, {}
        for k in keys:
            h = r.get(k, {}).get("hist", {})
            row[k] = h.get(sheet_q)
            if h:
                hist[k] = h
            if k in r and sheet_q in h:
                cells_at[k] = "%s%d" % (last_col, r[k]["row"])
        p, a = r.get("dev_pipeline", {}).get("hist", {}), r.get("property_holdings", {}).get("hist", {})
        dp = {q: round(p[q] / a[q] * 100.0, 6) for q in quarters if q in p and q in a and a[q] > 0}
        row["dev_pipeline_pct_assets"] = dp.get(sheet_q)
        if dp:
            hist["dev_pipeline_pct_assets"] = dp
        if sheet_q in dp:
            cells_at["dev_pipeline_pct_assets"] = "%s / %s" % (cells_at["dev_pipeline"], cells_at["property_holdings"])
        row["cells"] = cells_at
        row["history"] = hist
        out_rows.append(row)
    y, q = parse_quarter(sheet_q)
    return {"schema": SCHEMA, "quarter": sheet_q, "as_of": quarter_end(y, q).isoformat(), "source": source,
            "sheet": sheet, "parsed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "units": dict({k: u for k, u in BLOCKS.values()}, **DERIVED),
            "missing_blocks": [k for k in keys if k not in found],
            "quarters": quarters, "rows": out_rows}


# ---------------------------------------------------------------- 받기

def network_get(url, dest):
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=60) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f)
            return
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotPublished("HTTP 404 (%s)" % url)
            last = "HTTP %d (%s)" % (e.code, url)
        except (urllib.error.URLError, OSError) as e:
            last = "network 오류 %s (%s)" % (getattr(e, "reason", e), url)
        time.sleep(2 * (attempt + 1))
    raise TrackerError(last)


def tracker_dir(data_dir):
    return os.path.join(data_dir, "tracker")


def parsed_path(quarter, data_dir):
    return os.path.join(tracker_dir(data_dir), "parsed", quarter + ".json")


def fetch(quarter, data_dir, today, write_json, log, url=None, get=network_get):
    """분기 하나를 받아 parsed를 쓴다. 성공하면 parsed 경로, 못 찾으면 NotPublished."""
    y, q = parse_quarter(quarter)
    urls = [url] if url else [URL % (mo, quarter) for mo in folder_months(y, q, today)]
    if not urls:
        raise NotPublished("%s는 아직 올라올 때가 아님" % quarter)
    tmp_root = os.path.join(tracker_dir(data_dir), "tmp")
    os.makedirs(tmp_root, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="tracker-", dir=tmp_root)
    try:
        dest = os.path.join(tmp, "tracker.xlsx")
        for u in urls:
            try:
                get(u, dest)
            except NotPublished:
                log("%s: 없음 (%s)" % (quarter, u))
                continue
            doc = parse_tracker(dest, u, want=quarter)
            path = parsed_path(quarter, data_dir)
            write_json(doc, path)
            return path, doc
        raise NotPublished("%s: 모든 후보 URL이 404 (%s)" % (quarter, ", ".join(urls)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_fetch(args, data_dir, today, write_json, log, get=network_get):
    if args.quarter == "latest":
        y, q = latest_quarter(today)
        tries = [fmt_quarter(y, q), fmt_quarter(*shift_quarter(y, q, -1))]
    else:
        try:
            parse_quarter(args.quarter)
        except ValueError as e:
            log(str(e))
            return 1
        tries = [args.quarter]
    for qq in tries:
        try:
            path, doc = fetch(qq, data_dir, today, write_json, log, url=args.url, get=get)
        except NotPublished as e:
            log(str(e))
            continue
        except TrackerError as e:
            log("%s: %s" % (qq, e))
            return 1
        log("%s: as_of %s, row %d개, 분기 %s~%s, 없는 block %s" % (
            qq, doc["as_of"], len(doc["rows"]), doc["quarters"][0], doc["quarters"][-1],
            ", ".join(doc["missing_blocks"]) or "없음"))
        print(path)
        return 0
    log("Tracker를 받지 못함. reit.com의 REIT Industry Tracker page에서 XLSX URL을 찾아 --url로 준다")
    return 1


# ---------------------------------------------------------------- 섹터 표의 Tracker 칸

def load_latest(data_dir):
    files = sorted(glob.glob(os.path.join(tracker_dir(data_dir), "parsed", "*.json")))
    for p in reversed(files):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except ValueError:
            continue
        if d.get("schema") == SCHEMA:
            return d, p, ""
    return None, None, "Tracker parsed가 없음(reitwatch.py tracker fetch --quarter latest를 먼저 돌린다)"


ORDER = ["reported", "derived", "stale", "estimated", "missing"]


def worst(*sts):
    """나쁜 순서 reported < derived < stale < estimated < missing. 모르는 status는 missing."""
    return max(sts, key=lambda s: ORDER.index(s) if s in ORDER else len(ORDER) - 1) if sts else "missing"


def derive(*sts):
    """계산값의 status: 입력 중 가장 나쁜 것, reported면 derived."""
    s = worst(*sts)
    return "derived" if s == "reported" else s


def _v(value, unit, status, as_of, source, locator, note=""):
    """섹터 표의 값은 소수 넷째 자리로 둔다(셀의 전체 값은 parsed 파일에 있다)."""
    if value is None:
        status = "missing"
    else:
        value = round(value, 4)
    return {"value": value, "unit": unit, "status": status, "as_of": as_of, "source": source,
            "locator": locator, "note": note}


def _prev(quarter, n):
    return fmt_quarter(*shift_quarter(*parse_quarter(quarter), -n))


COLUMNS = ["implied_cap_rate", "implied_cap_rate_pctile", "cap_spread_dgs10", "cap_spread_baa",
           "ss_noi_yoy", "ss_noi_yoy_avg_4q", "occupancy", "occupancy_chg_1q", "occupancy_chg_4q",
           "dev_pipeline_pct_assets"]


def columns(doc, doc_path, doc_note, entry, dgs, baa, today):
    """한 subsector의 Tracker 칸과 구조 신호에 넣을 지표. entry = sector_map.json tracker 항목({"name", "parent"}).

    (칸, 지표, uncovered). uncovered는 Tracker 파일은 있는데 Tracker가 그 subsector·칸을 내지 않아 빈 칸 이름이다
    (빈 칸이 정상이라 exit 2로 세지 않는다). 입력(macro, history)이 모자라 빈 칸은 uncovered가 아니다."""
    out, inds, uncovered = {}, [], set()
    units = {"implied_cap_rate": "Percent", "implied_cap_rate_pctile": "Percent",
             "cap_spread_dgs10": "Percent points", "cap_spread_baa": "Percent points", "ss_noi_yoy": "Percent",
             "ss_noi_yoy_avg_4q": "Percent", "occupancy": "Percent", "occupancy_chg_1q": "Percent points",
             "occupancy_chg_4q": "Percent points", "dev_pipeline_pct_assets": "Percent"}
    if doc is None or entry is None:
        note = doc_note if doc is None else "sector_map.json의 tracker 대응표에 없는 subsector"
        for k in COLUMNS:
            out[k] = _v(None, units[k], "missing", None, "", "", note)
        return out, inds, set() if doc is None else set(COLUMNS)
    qtr, as_of = doc["quarter"], doc["as_of"]
    src = "nareit-tracker:" + doc["source"]
    rows = {r["name"]: r for r in doc["rows"]}
    age = (today - dt.date(*[int(x) for x in as_of.split("-")])).days
    base_st, age_note = ("stale", "Tracker 분기 끝날이 %d일 전(허용 %d일)" % (age, MAX_AGE_DAYS)) \
        if age > MAX_AGE_DAYS else ("reported", "")

    def pick(key):
        """(row, status, note): 자기 row에 값이 있으면 그것, 없으면 parent row(estimated)."""
        own = rows.get(entry.get("name")) if entry.get("name") else None
        if own is not None and own.get(key) is not None:
            return own, base_st, age_note
        par = rows.get(entry.get("parent")) if entry.get("parent") else None
        if par is not None and par.get(key) is not None:
            why = "Tracker에 %s 값이 없어 %s 전체 값" % (entry.get("name") or "이 subsector", entry["parent"])
            return par, "estimated", "; ".join(x for x in (why, age_note) if x)
        return None, "missing", "Tracker %s가 %s 값을 내지 않음" % (qtr, entry.get("name") or "이 subsector")

    def loc(row, key):
        return "%s!%s (%s)" % (doc["sheet"], row["cells"].get(key, "?"), row["name"])

    # implied cap rate와 자기 history percentile
    r, st, note = pick("implied_cap_rate")
    if r is None:
        uncovered.update(("implied_cap_rate", "implied_cap_rate_pctile", "cap_spread_dgs10", "cap_spread_baa"))
    cap = r["implied_cap_rate"] if r else None
    out["implied_cap_rate"] = _v(cap, "Percent", st, as_of, src if r else "", loc(r, "implied_cap_rate") if r else "", note)
    if r:
        h = r["history"].get("implied_cap_rate", {})
        xs = [v for q, v in h.items() if PCTILE_START <= q <= qtr]
        if len(xs) < MIN_HISTORY:
            out["implied_cap_rate_pctile"] = _v(None, "Percent", "missing", as_of, "", "",
                                                "history 표본 %d개(%d개 미만)" % (len(xs), MIN_HISTORY))
        else:
            pct = round(100.0 * sum(1 for x in xs if x <= cap) / len(xs), 4)
            out["implied_cap_rate_pctile"] = _v(pct, "Percent", derive(st), as_of, src,
                                                "%s %s~%s %d개 분기 중 최신값 이하 비율" % (r["name"], PCTILE_START, qtr, len(xs)),
                                                note)
    else:
        out["implied_cap_rate_pctile"] = _v(None, "Percent", "missing", as_of, "", "", note)
    for k, m, label in (("cap_spread_dgs10", dgs, "DGS10"), ("cap_spread_baa", baa, "Baa")):
        if cap is None or m["value"] is None:
            out[k] = _v(None, "Percent points", "missing", as_of, "", "",
                        note if cap is None else m["note"])
        else:
            out[k] = _v(round(cap - m["value"], 4), "Percent points", derive(st, m["status"]), as_of,
                        "derived:" + src + " - " + label, "implied_cap_rate - %s (%s)" % (label, m["locator"]),
                        "; ".join(x for x in ("cap rate 기준일 %s, %s 기준일 %s" % (as_of, label, m["as_of"]),
                                               m.get("note", ""), note) if x))

    # SS NOI YoY: 최신값과 최근 4분기 평균
    r, st, note = pick("ss_noi_yoy")
    if r is None:
        uncovered.update(("ss_noi_yoy", "ss_noi_yoy_avg_4q"))
    out["ss_noi_yoy"] = _v(r["ss_noi_yoy"] if r else None, "Percent", st, as_of, src if r else "",
                           loc(r, "ss_noi_yoy") if r else "", note)
    avg = None
    if r:
        h = r["history"].get("ss_noi_yoy", {})
        last4 = [h.get(_prev(qtr, n)) for n in range(4)]
        avg = round(statistics.mean(last4), 4) if None not in last4 else None
        out["ss_noi_yoy_avg_4q"] = _v(avg, "Percent", derive(st), as_of, src,
                                      "%s %s~%s 4개 분기 평균" % (r["name"], _prev(qtr, 3), qtr),
                                      note if avg is not None else "최근 4개 분기 중 빈 분기가 있음")
    else:
        out["ss_noi_yoy_avg_4q"] = _v(None, "Percent", "missing", as_of, "", "", note)
    if r and st != "estimated":
        latest = r["ss_noi_yoy"]
        judged = None if avg is None else ("나빠짐" if latest < 0 and avg < 0 else "아님")
        inds.append({"id": "tracker:ss_noi_yoy", "role": "demand", "good": "up", "value": round(latest, 4), "unit": "Percent",
                     "as_of": as_of, "status": st, "avg_4q": avg, "judged": judged,
                     "note": "Tracker same-store NOI YoY. 최신값과 4분기 평균이 둘 다 음수면 나빠짐"})

    # occupancy: 최신값, 1·4분기 변화
    r, st, note = pick("occupancy")
    if r is None:
        uncovered.update(("occupancy", "occupancy_chg_1q", "occupancy_chg_4q"))
    out["occupancy"] = _v(r["occupancy"] if r else None, "Percent", st, as_of, src if r else "",
                          loc(r, "occupancy") if r else "", note)
    chg = {}
    for n in (1, 4):
        k = "occupancy_chg_%dq" % n
        if r:
            h = r["history"].get("occupancy", {})
            old = h.get(_prev(qtr, n))
            chg[n] = round(r["occupancy"] - old, 4) if old is not None else None
            out[k] = _v(chg[n], "Percent points", derive(st), as_of, src,
                        "%s %s - %s" % (r["name"], qtr, _prev(qtr, n)),
                        note if chg[n] is not None else "%s 값이 없음" % _prev(qtr, n))
        else:
            chg[n] = None
            out[k] = _v(None, "Percent points", "missing", as_of, "", "", note)
    if r and st != "estimated":
        judged = None if None in (chg[1], chg[4]) else ("나빠짐" if chg[1] < 0 and chg[4] < 0 else "아님")
        inds.append({"id": "tracker:occupancy", "role": "demand", "good": "up", "value": round(r["occupancy"], 4),
                     "unit": "Percent", "as_of": as_of, "status": st, "chg_1q": chg[1], "chg_4q": chg[4],
                     "judged": judged, "note": "Tracker occupancy. 1·4분기 변화가 둘 다 음수면 나빠짐"})

    # development pipeline / 보유 자산
    r, st, note = pick("dev_pipeline_pct_assets")
    if r is None:
        uncovered.add("dev_pipeline_pct_assets")
    out["dev_pipeline_pct_assets"] = _v(r["dev_pipeline_pct_assets"] if r else None, "Percent",
                                        derive(st) if r else "missing", as_of, src if r else "",
                                        loc(r, "dev_pipeline_pct_assets") if r else "", note)
    return out, inds, uncovered

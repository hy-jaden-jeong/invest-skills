#!/usr/bin/env python3
"""reit-stock-review: SEC EDGAR에서 회사·공시·exhibit·XBRL 값을 받는다.

명령
  resolve T                         ticker → CIK, 회사 이름, SIC(6798이면 REIT)
  filings T [--forms F] [--limit N] 최근 공시 목록. 실적 8-K(item 2.02)를 표시한다
  exhibits T --accession A          공시의 EX-99 문서를 받아 .txt로 바꾼다
  xbrl T --period P [--facts F]     XBRL에서 그 분기 값을 facts 초안에 채운다(비어 있는 칸만)
  dividends T [--facts F]           XBRL 연간 주당 배당 history
  --check                           network 없이 fixture로 확인한다

SEC는 User-Agent에 연락처를 요구한다. 환경 변수 SEC_USER_AGENT로만 받는다(예: "이름 email").
요청은 초당 10회 아래로 보낸다. 받은 파일은 $INVEST_DATA_DIR/tickers/<T>/filings/에 둔다.

exit: 0 = 다 채움, 2 = 일부 missing, 1 = 입력·network 오류.
Python 3 stdlib만 쓴다(3.9 이상).
"""
import argparse
import datetime as dt
import html.parser
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reit_calc  # noqa: E402  같은 skill 폴더 안의 script

MIN_INTERVAL = 0.15          # 초. SEC 한도(초당 10회)보다 넉넉히 느리게
CACHE_SECONDS = 24 * 3600    # submissions·companyfacts
TICKERS_CACHE_SECONDS = 7 * 24 * 3600
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK%s.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK%s.json"
ARCHIVES = "https://www.sec.gov/Archives/edgar/data/%d/%s/"
REIT_SIC = "6798"

# facts 칸 ← XBRL concept 후보(앞에서부터). 회사마다 고르는 concept이 달라 Ex.99 표와 맞춰 본다.
XBRL_MAP = {
    "net_income_common": ("duration", "USD", ["us-gaap:NetIncomeLossAvailableToCommonStockholdersBasic"]),
    "depreciation_amortization": ("duration", "USD", ["us-gaap:DepreciationDepletionAndAmortization",
                                                      "us-gaap:DepreciationAndAmortization",
                                                      "us-gaap:DepreciationAmortizationAndAccretionNet"]),
    "impairment_depreciable_re": ("duration", "USD", ["us-gaap:ImpairmentOfRealEstate"]),
    "gain_on_sale_re": ("duration", "USD", ["us-gaap:GainLossOnSaleOfProperties",
                                            "us-gaap:GainsLossesOnSalesOfInvestmentRealEstate",
                                            "us-gaap:GainLossOnSaleOfRealEstateInvestmentProperty"]),
    "interest_expense": ("duration", "USD", ["us-gaap:InterestExpense", "us-gaap:InterestExpenseOperating",
                                             "us-gaap:InterestExpenseNonoperating", "us-gaap:InterestExpenseDebt"]),
    "cash": ("instant", "USD", ["us-gaap:CashAndCashEquivalentsAtCarryingValue"]),
    "common_shares_outstanding": ("instant", "shares", ["us-gaap:CommonStockSharesOutstanding",
                                                        "dei:EntityCommonStockSharesOutstanding"]),
    "dividend_regular_latest": ("instant", "USD/shares", ["us-gaap:DividendsPayableAmountPerShare"]),
    "dividends_paid_per_share_quarter": ("duration", "USD/shares", ["us-gaap:CommonStockDividendsPerShareCashPaid",
                                                                    "us-gaap:CommonStockDividendsPerShareDeclared"]),
}
UNIT_NAME = {"USD": "USD", "shares": "shares", "USD/shares": "USD/share"}
DIVIDEND_CONCEPTS = ["us-gaap:CommonStockDividendsPerShareDeclared", "us-gaap:CommonStockDividendsPerShareCashPaid"]


class FetchError(Exception):
    pass


def log(msg):
    print(msg, file=sys.stderr)


data_dir = reit_calc.data_dir
read_json = reit_calc.read_json
write_json = reit_calc.write_json


def ticker_dir(t):
    return os.path.join(data_dir(), "tickers", t.upper())


def filings_dir(t):
    return os.path.join(ticker_dir(t), "filings")


def parse_date(s):
    return dt.date(*[int(x) for x in s[:10].split("-")])


# ---------------------------------------------------------------- network

_last = [0.0]


def throttle():
    wait = MIN_INTERVAL - (time.monotonic() - _last[0])
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.monotonic()


def user_agent():
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua:
        raise FetchError("SEC_USER_AGENT가 없음. SEC는 연락처가 든 User-Agent를 요구한다. "
                         "예: export SEC_USER_AGENT=\"이름 email@example.com\"")
    return ua


def http_get(url):
    ua = user_agent()
    last = None
    for attempt in range(3):
        throttle()
        req = urllib.request.Request(url, headers={"User-Agent": ua})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise FetchError("HTTP 404 (%s)" % url)
            if e.code == 403:
                raise FetchError("HTTP 403 (%s): SEC_USER_AGENT에 연락처가 있는지, 요청이 너무 잦지 않은지 본다" % url)
            last = "HTTP %d (%s)" % (e.code, url)
        except (urllib.error.URLError, OSError) as e:
            last = "network 오류 %s (%s)" % (getattr(e, "reason", e), url)
        time.sleep(2 * (attempt + 1))
    raise FetchError(last)


def cached(path, url, max_age, fetch=None):
    """path가 max_age 안이면 그대로, 아니면 받는다. 받기 실패면 오래된 cache라도 쓴다."""
    fetch = fetch or http_get
    if os.path.exists(path) and (max_age is None or time.time() - os.path.getmtime(path) < max_age):
        with open(path, "rb") as f:
            return f.read()
    try:
        body = fetch(url)
    except FetchError as e:
        if os.path.exists(path):
            log("받기 실패(%s), 전에 받은 cache를 씀: %s" % (e, path))
            with open(path, "rb") as f:
                return f.read()
        raise
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "wb") as f:
        f.write(body)
    os.replace(path + ".tmp", path)
    return body


# ---------------------------------------------------------------- 순수 파싱

def cik_for(tickers_json, ticker):
    for row in tickers_json.values():
        if str(row.get("ticker", "")).upper() == ticker.upper():
            return "%010d" % int(row["cik_str"])
    return None


def company_info(sub, ticker):
    return {"ticker": ticker.upper(), "cik": "%010d" % int(sub["cik"]), "name": sub.get("name"),
            "sic": str(sub.get("sic", "")), "sic_description": sub.get("sicDescription"),
            "is_reit": str(sub.get("sic", "")) == REIT_SIC, "exchanges": sub.get("exchanges", []),
            "fiscal_year_end": sub.get("fiscalYearEnd"),
            "source": "sec:submissions", "locator": SUBMISSIONS_URL % ("%010d" % int(sub["cik"]))}


def filing_list(sub, forms, limit):
    r = sub["filings"]["recent"]
    out = []
    for i in range(len(r["accessionNumber"])):
        if forms and r["form"][i] not in forms:
            continue
        items = [x.strip() for x in (r.get("items", [""] * (i + 1))[i] or "").split(",") if x.strip()]
        out.append({"form": r["form"][i], "accession": r["accessionNumber"][i], "filed": r["filingDate"][i],
                    "report_date": r["reportDate"][i] or None, "items": items,
                    "primary_document": r["primaryDocument"][i],
                    "earnings": r["form"][i] == "8-K" and "2.02" in items})
        if len(out) >= limit:
            break
    return out


class _Cells(html.parser.HTMLParser):
    """EDGAR filing index의 문서 표에서 row마다 cell 글자와 link를 모은다."""

    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell, self.href = [], None, None, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell, self.href = [], None
        elif tag == "a" and self.cell is not None:
            self.href = dict(attrs).get("href")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.row is not None and self.cell is not None:
            self.row.append(("".join(self.cell).strip(), self.href))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def index_documents(index_html):
    """[(seq, description, document, type, size)]: 'Document Format Files' 표의 row."""
    p = _Cells()
    p.feed(index_html)
    docs = []
    for row in p.rows:
        if len(row) >= 5 and row[0][0].isdigit():
            href = row[2][1] or ""
            name = href.rsplit("/", 1)[-1] if href else row[2][0].split()[0] if row[2][0] else ""
            if href.startswith("/ix?doc="):
                name = href.rsplit("/", 1)[-1]
            docs.append({"seq": int(row[0][0]), "description": row[1][0], "document": name,
                         "type": row[3][0], "size": int(row[4][0]) if row[4][0].isdigit() else None})
    return docs


class _Text(html.parser.HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "title"}
    SKIP = {"script", "style", "head", "ix:header"}

    def __init__(self):
        super().__init__()
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in ("td", "th"):
            self.out.append("\t")
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(re.sub(r"\s+", " ", data.replace("\xa0", " ")))


def html_to_text(s):
    """표는 cell을 tab, row를 줄로 둔다. agent가 줄 번호로 위치를 적을 수 있게 한다."""
    p = _Text()
    p.feed(s)
    lines = []
    for line in "".join(p.out).split("\n"):
        line = re.sub(r" *\t *", "\t", line).strip(" ")
        if line.strip("\t "):
            lines.append(line.rstrip("\t "))
    return "\n".join(lines) + "\n"


def quarter_end(period):
    m = re.fullmatch(r"(\d{4})Q([1-4])", period or "")
    if not m:
        return None
    y, q = int(m.group(1)), int(m.group(2))
    return dt.date(y, q * 3, [31, 30, 30, 31][q - 1])


def pick_fact(cf, concept, unit, kind, end):
    """그 분기 끝날의 값. duration은 80~100일(3개월)만. 같은 기간이 여러 번 나오면 가장 늦게 낸 값."""
    ns, name = concept.split(":")
    obs = (((cf.get("facts") or {}).get(ns) or {}).get(name) or {}).get("units", {}).get(unit) or []
    best = None
    for o in obs:
        if o.get("end") != end.isoformat():
            continue
        if kind == "instant":
            if o.get("start"):
                continue
        else:
            if not o.get("start"):
                continue
            days = (parse_date(o["end"]) - parse_date(o["start"])).days
            if not 80 <= days <= 100:
                continue
        if best is None or o.get("filed", "") > best.get("filed", ""):
            best = o
    return best


def xbrl_draft(cf, period):
    end = quarter_end(period)
    out = {}
    for key, (kind, unit, concepts) in XBRL_MAP.items():
        for c in concepts:
            o = pick_fact(cf, c, unit, kind, end)
            if o:
                span = ("%s..%s" % (o["start"], o["end"])) if o.get("start") else o["end"]
                out[key] = {"value": o["val"], "unit": UNIT_NAME[unit], "status": "reported", "as_of": o["end"],
                            "source": "sec:xbrl %s %s" % (o.get("form", ""), o.get("accn", "")),
                            "locator": "%s %s" % (c, span),
                            "note": "XBRL 초안: Ex.99 표의 같은 줄과 맞는지 확인한다"}
                break
    return out


def annual_dividends(cf):
    """연간(350~380일, 10-K) 주당 배당. Declared가 없으면 CashPaid. 해마다 가장 늦게 낸 값."""
    for c in DIVIDEND_CONCEPTS:
        ns, name = c.split(":")
        obs = (((cf.get("facts") or {}).get(ns) or {}).get(name) or {}).get("units", {}).get("USD/shares") or []
        years = {}
        for o in obs:
            if not o.get("start") or not str(o.get("form", "")).startswith("10-K"):
                continue
            days = (parse_date(o["end"]) - parse_date(o["start"])).days
            if not 350 <= days <= 380:
                continue
            y = o["end"][:4]
            if y not in years or o.get("filed", "") > years[y].get("filed", ""):
                years[y] = o
        if years:
            series = [{"period": y, "value": o["val"], "unit": "USD/share", "status": "reported", "as_of": o["end"],
                       "source": "sec:xbrl %s %s" % (o.get("form"), o.get("accn")),
                       "locator": "%s FY%s (%s..%s)" % (c, y, o["start"], o["end"]), "note": ""}
                      for y, o in sorted(years.items())]
            short = [o for o in obs if o.get("start") and
                     25 <= (parse_date(o["end"]) - parse_date(o["start"])).days <= 35]
            hint = "월별 지급 기록이 있음(12회일 수 있음)" if short else "월별 기록 없음(분기 4회일 수 있음)"
            return series, c, hint
    return [], None, "XBRL에 주당 배당 concept이 없음"


def skeleton(info, period, filing):
    facts = {k: {"value": None, "status": "missing", "note": "Ex.99에서 채울 것"} for k in reit_calc.FACT_KINDS}
    return {"schema": reit_calc.FACTS_SCHEMA, "ticker": info["ticker"], "period": period,
            "period_end": quarter_end(period).isoformat(),
            "company": {k: info[k] for k in ("cik", "name", "sic", "sic_description", "is_reit")},
            "filing": filing or {}, "facts": facts,
            "series": {k: [] for k in reit_calc.SERIES_KINDS},
            "affo_bridge": {"start": None, "items": []}, "sector_kpis": {}}


def merge_missing(doc, draft):
    """비어 있는(missing) 칸만 채운다. agent가 채운 값은 덮지 않는다."""
    filled = []
    for k, v in draft.items():
        cur = doc["facts"].get(k)
        if cur is None or cur.get("status") == "missing" or cur.get("value") is None:
            doc["facts"][k] = v
            filled.append(k)
    return filled


# ---------------------------------------------------------------- 명령

def load_company(t, fetch=None):
    tick = read_json_bytes(cached(os.path.join(data_dir(), "tickers", "_sec", "company_tickers.json"),
                                  TICKERS_URL, TICKERS_CACHE_SECONDS, fetch))
    cik = cik_for(tick, t)
    if not cik:
        raise FetchError("SEC ticker 목록에 %s가 없음" % t.upper())
    sub = read_json_bytes(cached(os.path.join(filings_dir(t), "submissions.json"),
                                 SUBMISSIONS_URL % cik, CACHE_SECONDS, fetch))
    return company_info(sub, t), sub


def read_json_bytes(b):
    return json.loads(b.decode("utf-8"))


def facts_path(t, period):
    return os.path.join(ticker_dir(t), "facts", "%s.json" % period)


def cmd_resolve(args):
    info, _ = load_company(args.ticker)
    write_json(info, os.path.join(filings_dir(args.ticker), "company.json"))
    print(json.dumps(info, ensure_ascii=False, indent=2))
    if not info["is_reit"]:
        log("SIC %s(%s): REIT(6798)가 아님. FFO 경로는 맞지 않는다" % (info["sic"], info["sic_description"]))
    return 0


def cmd_filings(args):
    info, sub = load_company(args.ticker)
    forms = [x.strip() for x in args.forms.split(",") if x.strip()]
    rows = filing_list(sub, forms, args.limit)
    latest = next((r for r in rows if r["earnings"]), None)
    print(json.dumps({"ticker": info["ticker"], "cik": info["cik"], "latest_earnings_8k": latest, "filings": rows},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_exhibits(args):
    info, _ = load_company(args.ticker)
    acc = args.accession.strip()
    if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", acc):
        log("--accession은 0000726728-26-000056 모양")
        return 1
    base = ARCHIVES % (int(info["cik"]), acc.replace("-", ""))
    adir = os.path.join(filings_dir(args.ticker), acc)
    idx = cached(os.path.join(adir, "index.htm"), base + acc + "-index.htm", None).decode("utf-8", "replace")
    docs = [d for d in index_documents(idx) if d["type"].upper().startswith("EX-99")]
    if not docs:
        log("이 공시에 EX-99 문서가 없음")
        return 2
    out = []
    for d in docs:
        raw = cached(os.path.join(adir, d["document"]), base + d["document"], None)
        tpath = os.path.join(adir, d["document"].rsplit(".", 1)[0] + ".txt")
        if d["document"].lower().endswith((".htm", ".html")):
            with open(tpath, "w", encoding="utf-8") as f:
                f.write(html_to_text(raw.decode("utf-8", "replace")))
        else:
            tpath = None
        out.append(dict(d, path=os.path.join(adir, d["document"]), text_path=tpath, url=base + d["document"]))
    print(json.dumps({"ticker": info["ticker"], "accession": acc, "exhibits": out}, ensure_ascii=False, indent=2))
    return 0


def load_companyfacts(t, cik):
    return read_json_bytes(cached(os.path.join(filings_dir(t), "companyfacts.json"),
                                  COMPANYFACTS_URL % cik, CACHE_SECONDS))


def cmd_xbrl(args):
    if not quarter_end(args.period):
        log("--period는 2026Q3 모양(달력 분기, 분기 끝날로 맞춘다)")
        return 1
    info, sub = load_company(args.ticker)
    cf = load_companyfacts(args.ticker, info["cik"])
    draft = xbrl_draft(cf, args.period)
    path = args.facts or facts_path(args.ticker, args.period)
    if os.path.exists(path):
        doc = read_json(path)
    else:
        latest = next((r for r in filing_list(sub, ["8-K"], 40) if r["earnings"]), None)
        doc = skeleton(info, args.period, latest and {k: latest[k] for k in ("form", "accession", "filed")})
    filled = merge_missing(doc, draft)
    write_json(doc, path)
    todo = [k for k, v in doc["facts"].items() if v.get("status") == "missing"]
    log("XBRL로 채운 칸 %d: %s" % (len(filled), ", ".join(filled) or "없음"))
    log("아직 빈 칸 %d: Ex.99·supplemental에서 locator와 함께 채운다" % len(todo))
    print(path)
    return 0 if not todo else 2


def cmd_dividends(args):
    info, _ = load_company(args.ticker)
    cf = load_companyfacts(args.ticker, info["cik"])
    series, concept, hint = annual_dividends(cf)
    print(json.dumps({"ticker": info["ticker"], "concept": concept, "frequency_hint": hint, "series": series},
                     ensure_ascii=False, indent=2))
    if args.facts:
        doc = read_json(args.facts)
        doc.setdefault("series", {})["dividend_per_share_annual"] = series
        write_json(doc, args.facts)
        log("facts에 연간 배당 %d년을 넣음: %s" % (len(series), args.facts))
    return 0 if series else 2


def main(argv):
    p = argparse.ArgumentParser(prog="edgar.py", description="SEC EDGAR 공시·XBRL")
    p.add_argument("--check", action="store_true", help="network 없이 fixture로 확인")
    sub = p.add_subparsers(dest="cmd")
    for name in ("resolve", "filings", "exhibits", "xbrl", "dividends"):
        s = sub.add_parser(name)
        s.add_argument("ticker")
        if name == "filings":
            s.add_argument("--forms", default="8-K,10-Q,10-K")
            s.add_argument("--limit", type=int, default=20)
        if name == "exhibits":
            s.add_argument("--accession", required=True)
        if name == "xbrl":
            s.add_argument("--period", required=True)
        if name in ("xbrl", "dividends"):
            s.add_argument("--facts")
    try:
        args = p.parse_args(argv)
    except SystemExit as e:
        return 0 if e.code == 0 else 1
    if args.check:
        return run_check()
    cmds = {"resolve": cmd_resolve, "filings": cmd_filings, "exhibits": cmd_exhibits,
            "xbrl": cmd_xbrl, "dividends": cmd_dividends}
    if args.cmd not in cmds:
        p.print_help(sys.stderr)
        return 1
    try:
        user_agent()  # cache가 있어도 SEC_USER_AGENT 없이는 돌지 않는다
        return cmds[args.cmd](args)
    except FetchError as e:
        log(str(e))
        return 1


# ---------------------------------------------------------------- --check

def run_check():
    root = reit_calc.skill_root()
    fx = os.path.join(root, "tests", "fixtures", "edgar")
    exp = read_json(os.path.join(root, "tests", "expected.json"))["edgar"]
    fails = []

    def ok(cond, label):
        if not cond:
            fails.append(label)
        log(("PASS " if cond else "FAIL ") + label)

    def fx_json(name):
        return read_json(os.path.join(fx, name))

    def fx_text(name):
        with open(os.path.join(fx, name), encoding="utf-8") as f:
            return f.read()

    # 1. resolve
    ok(cik_for(fx_json("company_tickers.json"), "test") == exp["cik"], "ticker → CIK (대소문자 무시)")
    ok(cik_for(fx_json("company_tickers.json"), "NOPE") is None, "없는 ticker는 None")
    sub = fx_json("submissions.json")
    info = company_info(sub, "test")
    ok(info["is_reit"] and info["cik"] == exp["cik"] and info["ticker"] == "TEST", "SIC 6798이면 REIT")
    ok(not company_info(dict(sub, sic="2834"), "test")["is_reit"], "SIC가 6798이 아니면 REIT 아님")

    # 2. filings
    rows = filing_list(sub, ["8-K", "10-Q"], 10)
    ok([r["accession"] for r in rows if r["earnings"]] == exp["earnings_accessions"], "실적 8-K는 item 2.02가 든 것")
    ok(all(r["form"] in ("8-K", "10-Q") for r in rows) and len(filing_list(sub, [], 2)) == 2, "--forms와 --limit")

    # 3. exhibits
    docs = index_documents(fx_text("index.htm"))
    ok([(d["type"], d["document"]) for d in docs if d["type"].startswith("EX-99")] == [tuple(x) for x in exp["ex99"]],
       "filing index에서 EX-99 문서 이름")
    text = html_to_text(fx_text("ex99.htm"))
    for line in exp["text_lines"]:
        ok(line in text.split("\n"), "HTML 표 → tab 줄: %r" % line)
    ok("hidden" not in text and "script" not in text, "script·ix:header 글자는 버림")

    # 4. xbrl 초안
    cf = fx_json("companyfacts.json")
    draft = xbrl_draft(cf, "2026Q3")
    for k, want in exp["xbrl"].items():
        got = draft.get(k, {})
        ok(got.get("value") == want["value"] and got.get("locator", "").startswith(want["locator"]),
           "xbrl %s = %s (%s)" % (k, want["value"], want["why"]))
    ok(all(v["status"] == "reported" and v["source"].startswith("sec:xbrl") for v in draft.values()),
       "XBRL 값은 reported, source=sec:xbrl")
    ok("gain_on_sale_re" not in draft, "없는 concept은 초안에 넣지 않음")
    ok(quarter_end("2026Q2") == dt.date(2026, 6, 30) and quarter_end("2026-3") is None, "period → 분기 끝날")

    doc = skeleton(info, "2026Q3", {"form": "8-K", "accession": exp["earnings_accessions"][0], "filed": "2026-11-02"})
    doc["facts"]["net_income_common"] = {"value": 1, "unit": "USD thousands", "status": "reported",
                                         "source": "agent (가상)", "locator": "x (가상)"}
    filled = merge_missing(doc, draft)
    ok(doc["facts"]["net_income_common"]["value"] == 1 and "net_income_common" not in filled
       and doc["facts"]["cash"]["value"] == exp["xbrl"]["cash"]["value"],
       "초안은 빈 칸만 채우고 agent 값은 덮지 않음")
    ok(set(doc["facts"]) == set(reit_calc.FACT_KINDS) and doc["schema"] == reit_calc.FACTS_SCHEMA,
       "facts 초안에 reit_calc의 칸이 모두 있음")

    # 5. 배당 history
    series, concept, hint = annual_dividends(cf)
    ok([(s["period"], s["value"]) for s in series] == [tuple(x) for x in exp["dividends"]]
       and concept.endswith("CashPaid") and "월별" in hint,
       "연간 배당: 10-K 연간 값만, 해마다 늦게 낸 값, Declared 없으면 CashPaid")

    # 6. network 약속
    old = os.environ.pop("SEC_USER_AGENT", None)
    old_dir = os.environ.get("INVEST_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["INVEST_DATA_DIR"] = tmp
            ok(main(["resolve", "TEST"]) == 1, "SEC_USER_AGENT가 없으면 exit 1")
    finally:
        if old is not None:
            os.environ["SEC_USER_AGENT"] = old
        if old_dir is None:
            os.environ.pop("INVEST_DATA_DIR", None)
        else:
            os.environ["INVEST_DATA_DIR"] = old_dir
    t0 = time.monotonic()
    for _ in range(4):
        throttle()
    ok(MIN_INTERVAL >= 0.1 and time.monotonic() - t0 >= 0.3 - 0.01,
       "요청 사이 간격이 0.1초 이상(초당 10회 아래, 지금 %.2f초)" % MIN_INTERVAL)
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "c.json")
        calls = []

        def fake(url):
            calls.append(url)
            return b"{}"

        def down(url):
            raise FetchError("network 오류 (가상)")
        cached(p, "u", 3600, fake)
        cached(p, "u", 3600, fake)
        ok(len(calls) == 1, "cache 안이면 다시 받지 않음")
        ok(cached(p, "u", 0, down) == b"{}", "받기 실패면 전에 받은 cache를 씀")

    log("check: %d개 실패" % len(fails) if fails else "check: 모두 통과")
    return 2 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

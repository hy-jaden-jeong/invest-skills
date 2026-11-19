#!/usr/bin/env python3
"""reit-stock-review: SEC EDGAR에서 회사·공시·exhibit·XBRL 값을 받는다.

명령
  resolve T                         ticker → CIK, 회사 이름, SIC(6798이면 REIT)
  filings T [--forms F] [--limit N] 최근 공시 목록. 실적 8-K(item 2.02)를 표시한다
  exhibits T --accession A          공시의 EX-99 문서를 받아 .txt로 바꾼다
  xbrl T --period P [--facts F]     XBRL에서 그 분기 값을 facts 초안에 채운다(비어 있는 칸만).
                                    REIT가 아니면 일반 배당주 칸(EPS, 현금흐름 YTD, 배당)
  dividends T [--facts F]           XBRL 연간 주당 배당 history
  config (--user-agent "이름 이메일" | --show)
                                    www.sec.gov용 연락처를 $INVEST_DATA_DIR/config.json에 저장·확인한다
  --check                           network 없이 fixture로 확인한다

User-Agent: data.sec.gov(submissions, companyfacts)와 efts.sec.gov(ticker → CIK)는 연락처가 없으면
기본값 "invest-skills/1.0"(사람·레포·이메일 없음)으로 받는다. www.sec.gov(ticker 목록, Archives의 exhibit)는
연락처가 든 User-Agent가 아니면 403을 줘서, SEC_USER_AGENT 환경 변수나 config.json의 sec_user_agent가
있어야 받는다. 둘 다 없으면 그 명령은 exit 1로 멈추고 저장 방법을 안내한다. 이것은 key가 아니라 SEC가
요구하는 연락처다. 가짜 연락처 기본값은 두지 않는다.
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
import urllib.parse
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
EFTS_URL = "https://efts.sec.gov/LATEST/search-index?keysTyped=%s"   # key 없는 회사 검색(ticker → CIK)
CONFIG_FILE = "config.json"   # $INVEST_DATA_DIR 아래. 레포 밖
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
    # 신용. 정기 원금 상환·unencumbered·covenant는 표준 concept이 없어 Ex.99에서 채운다
    "total_assets": ("instant", "USD", ["us-gaap:Assets"]),
    "accumulated_depreciation": ("instant", "USD", ["us-gaap:RealEstateInvestmentPropertyAccumulatedDepreciation"]),
    "secured_debt": ("instant", "USD", ["us-gaap:SecuredDebt"]),
    "capitalized_interest": ("duration", "USD", ["us-gaap:InterestCostsCapitalized"]),
    "preferred_dividends": ("duration", "USD", ["us-gaap:PreferredStockDividendsIncomeStatementImpact",
                                                "us-gaap:DividendsPreferredStock"]),
    # 주당 AFFO를 내지 않는 회사의 분모. AFFO 총액과 같은 기준(OP unit 포함 여부)인지 Ex.99와 맞춰 본다
    "diluted_shares_weighted": ("duration", "shares", ["us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding"]),
}
# 일반 배당주(SIC가 6798이 아님) 칸 ← concept 후보. ytd는 회계연도 시작부터 그 분기 끝날까지(현금흐름표 기간).
DIVIDEND_XBRL_MAP = {
    "net_income_common": ("duration", "USD", ["us-gaap:NetIncomeLossAvailableToCommonStockholdersBasic",
                                              "us-gaap:NetIncomeLoss"]),
    "eps_diluted_quarter": ("duration", "USD/shares", ["us-gaap:EarningsPerShareDiluted"]),
    "dividends_declared_per_share_quarter": ("duration", "USD/shares", ["us-gaap:CommonStockDividendsPerShareDeclared",
                                                                        "us-gaap:CommonStockDividendsPerShareCashPaid"]),
    "dividend_regular_latest": ("duration", "USD/shares", ["us-gaap:CommonStockDividendsPerShareDeclared",
                                                           "us-gaap:CommonStockDividendsPerShareCashPaid"]),
    "operating_cash_flow_ytd": ("ytd", "USD", ["us-gaap:NetCashProvidedByUsedInOperatingActivities",
                                               "us-gaap:NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]),
    "capex_ytd": ("ytd", "USD", ["us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
                                 "us-gaap:PaymentsToAcquireProductiveAssets"]),
    "dividends_paid_ytd": ("ytd", "USD", ["us-gaap:PaymentsOfDividendsCommonStock", "us-gaap:PaymentsOfOrdinaryDividends",
                                          "us-gaap:PaymentsOfDividends"]),
}
XBRL_NOTE = {"dividend_regular_latest": "XBRL 초안: 분기 배당 1회분(분기 배당이면 분기 값과 같다). 최근 배당 공지가 다르면 고친다"}
END_TOLERANCE_DAYS = 7  # 52·53주 회계연도는 분기 끝날이 달력 분기 끝날과 며칠 다르다
UNIT_NAME = {"USD": "USD", "shares": "shares", "USD/shares": "USD/share"}
DIVIDEND_CONCEPTS = ["us-gaap:CommonStockDividendsPerShareDeclared", "us-gaap:CommonStockDividendsPerShareCashPaid"]


class FetchError(Exception):
    pass


class NeedContact(FetchError):
    """www.sec.gov에 보낼 연락처 User-Agent가 설정돼 있지 않다."""


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


DEFAULT_UA = "invest-skills/1.0"  # 식별 정보(사람·레포·이메일)를 넣지 않는다
CONTACT_HELP = ("www.sec.gov에서 공시 원문을 받으려면 SEC가 요구하는 연락처(이름과 이메일)가 든 User-Agent가 필요하다. "
                "이것은 API key가 아니다. 사용자에게 이름과 이메일을 한 번 묻고 "
                "`python3 <skill 폴더>/scripts/edgar.py config --user-agent \"이름 이메일\"`로 저장한 뒤 다시 돈다"
                "(환경 변수 SEC_USER_AGENT로 줘도 된다)")
EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


def config_path():
    return os.path.join(data_dir(), CONFIG_FILE)


def read_config():
    path = config_path()
    if not os.path.exists(path):
        return {}
    try:
        doc = read_json(path)
    except (OSError, ValueError) as e:
        log("config 읽기 실패(%s): %s" % (e, path))
        return {}
    return doc if isinstance(doc, dict) else {}


def contact_ua():
    """(값, 어디서). SEC_USER_AGENT 환경 변수 > config.json sec_user_agent > (None, None)."""
    env = os.environ.get("SEC_USER_AGENT", "").strip()
    if env:
        return env, "env:SEC_USER_AGENT"
    cfg = str(read_config().get("sec_user_agent") or "").strip()
    if cfg:
        return cfg, "config:" + config_path()
    return None, None


def user_agent(url=""):
    """www.sec.gov는 연락처가 있어야 한다(없으면 NeedContact). 나머지는 연락처가 없으면 DEFAULT_UA."""
    ua, _ = contact_ua()
    if ua:
        return ua
    if "://www.sec.gov/" in url:
        raise NeedContact("%s 받기 멈춤. %s" % (url, CONTACT_HELP))
    return DEFAULT_UA


def mask_ua(ua):
    """이메일 local part를 첫 글자만 남기고 가린다(a***@example.com)."""
    return EMAIL_RE.sub(lambda m: m.group(0)[0] + "***@" + m.group(0).split("@", 1)[1], ua)


def http_get(url):
    ua = user_agent(url)
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
                hint = (CONTACT_HELP if ua == DEFAULT_UA else
                        "요청이 너무 잦지 않은지, User-Agent(%s)에 이름과 이메일이 있는지 본다" % mask_ua(ua))
                raise FetchError("HTTP 403 (%s): %s" % (url, hint))
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


def cik_from_efts(doc, ticker):
    """efts 회사 검색 응답에서 tickers 칸에 그 ticker가 정확히 있는 첫 항목의 CIK. 없으면 None."""
    want = ticker.upper()
    for h in (doc.get("hits") or {}).get("hits") or []:
        src = h.get("_source") or {}
        ticks = [x.strip().upper() for x in str(src.get("tickers") or "").split(",")]
        if want in ticks and str(h.get("_id", "")).isdigit():
            return "%010d" % int(h["_id"])
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
    """그 분기 끝날(±7일)의 값. duration은 80~100일(3개월)만, ytd는 380일 안에서 가장 긴 기간.
    끝날이 가장 가까운 것, 그다음 (ytd면 긴 기간,) 가장 늦게 낸 값."""
    ns, name = concept.split(":")
    obs = (((cf.get("facts") or {}).get(ns) or {}).get(name) or {}).get("units", {}).get(unit) or []
    best, best_key = None, None
    for o in obs:
        gap = abs((parse_date(o["end"]) - end).days)
        if gap > END_TOLERANCE_DAYS:
            continue
        days = 0
        if kind == "instant":
            if o.get("start"):
                continue
        else:
            if not o.get("start"):
                continue
            days = (parse_date(o["end"]) - parse_date(o["start"])).days
            if kind == "duration" and not 80 <= days <= 100:
                continue
            if kind == "ytd" and not 80 <= days <= 380:
                continue
        key = (-gap, days if kind == "ytd" else 0, o.get("filed", ""))
        if best is None or key > best_key:
            best, best_key = o, key
    return best


def xbrl_draft(cf, period, is_reit=True):
    end = quarter_end(period)
    out = {}
    for key, (kind, unit, concepts) in (XBRL_MAP if is_reit else DIVIDEND_XBRL_MAP).items():
        for c in concepts:
            o = pick_fact(cf, c, unit, kind, end)
            if o:
                span = ("%s..%s" % (o["start"], o["end"])) if o.get("start") else o["end"]
                out[key] = {"value": o["val"], "unit": UNIT_NAME[unit], "status": "reported", "as_of": o["end"],
                            "source": "sec:xbrl %s %s" % (o.get("form", ""), o.get("accn", "")),
                            "locator": "%s %s" % (c, span),
                            "note": XBRL_NOTE.get(key, "XBRL 초안: Ex.99 표의 같은 줄과 맞는지 확인한다")}
                break
    return out


def annual_dividends(cf):
    """연간(350~380일, 10-K) 주당 배당. 해마다 가장 늦게 낸 값. 해는 기간 가운데 날의 해.
    Declared를 먼저 쓰되, 마지막 해 값이 CashPaid의 절반도 안 되면 다른 뜻으로 단 tag로 보고 CashPaid를 쓴다."""
    found = []
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
            mid = parse_date(o["start"]) + (parse_date(o["end"]) - parse_date(o["start"])) / 2
            y = str(mid.year)  # 52·53주 회계연도는 끝날이 다음 해 1월 초일 수 있어 기간 가운데 날의 해로 부른다
            if y not in years or o.get("filed", "") > years[y].get("filed", ""):
                years[y] = o
        if years:
            found.append((c, obs, years))
    if not found:
        return [], None, "XBRL에 주당 배당 concept이 없음"
    c, obs, years = found[0]
    note = ""
    if len(found) > 1:
        last0 = found[0][2][max(found[0][2])]["val"]
        last1 = found[1][2][max(found[1][2])]["val"]
        if last0 < 0.5 * last1:
            note = "%s 마지막 해 값 %s가 %s %s의 절반도 안 되어 %s를 씀; " % (
                found[0][0], last0, found[1][0], last1, found[1][0])
            c, obs, years = found[1]
    series = [{"period": y, "value": o["val"], "unit": "USD/share", "status": "reported", "as_of": o["end"],
               "source": "sec:xbrl %s %s" % (o.get("form"), o.get("accn")),
               "locator": "%s FY%s (%s..%s)" % (c, y, o["start"], o["end"]), "note": ""}
              for y, o in sorted(years.items())]
    short = [o for o in obs if o.get("start") and
             25 <= (parse_date(o["end"]) - parse_date(o["start"])).days <= 35]
    hint = note + ("월별 지급 기록이 있음(12회일 수 있음)" if short else "월별 기록 없음(분기 4회일 수 있음)")
    return series, c, hint


def skeleton(info, period, filing):
    """facts 초안. REIT가 아니면 일반 배당주 칸(reit_calc.DIVIDEND_FACT_KINDS)으로 만든다."""
    reit = info["is_reit"]
    kinds = reit_calc.FACT_KINDS if reit else reit_calc.DIVIDEND_FACT_KINDS
    facts = {k: {"value": None, "status": "missing", "note": "Ex.99에서 채울 것"} for k in kinds}
    doc = {"schema": reit_calc.FACTS_SCHEMA, "ticker": info["ticker"], "period": period,
           "period_end": quarter_end(period).isoformat(),
           "company": {k: info[k] for k in ("cik", "name", "sic", "sic_description", "is_reit")},
           "filing": filing or {}, "facts": facts,
           "series": {k: [] for k in (reit_calc.SERIES_KINDS if reit else reit_calc.DIVIDEND_SERIES_KINDS)}}
    if reit:
        doc.update({"affo_bridge": {"start": None, "items": []}, "sector_kpis": {}})
    return doc


def earnings_8k_for(rows, period):
    """그 분기의 실적 8-K: 분기 끝날 뒤 120일 안에 낸 실적 8-K 중 가장 이른 것. 없으면 None."""
    end = quarter_end(period)
    cands = [r for r in rows if r["earnings"] and 0 < (parse_date(r["filed"]) - end).days <= 120]
    return min(cands, key=lambda r: r["filed"]) if cands else None


def add_new_fields(doc):
    """전에 만든 facts에 없는 칸(나중에 더한 칸)을 빈 칸으로 더한다. 더한 칸 이름 목록."""
    reit = reit_calc.is_reit_doc(doc)
    added = []
    for k in (reit_calc.FACT_KINDS if reit else reit_calc.DIVIDEND_FACT_KINDS):
        if k not in doc.setdefault("facts", {}):
            doc["facts"][k] = {"value": None, "status": "missing", "note": "Ex.99에서 채울 것"}
            added.append(k)
    for k in (reit_calc.SERIES_KINDS if reit else reit_calc.DIVIDEND_SERIES_KINDS):
        if k not in doc.setdefault("series", {}):
            doc["series"][k] = []
            added.append("series." + k)
    return added


def merge_missing(doc, draft):
    """비어 있는(missing) 칸만 채운다. agent가 채운 값은 덮지 않는다."""
    filled = []
    for k, v in draft.items():
        cur = doc["facts"].get(k)
        if cur is None or cur.get("status") == "missing" or cur.get("value") is None:
            doc["facts"][k] = v
            filled.append(k)
    return filled


def merge_series(have, new):
    """연간 series를 해(period)마다 합친다. 값과 locator가 있는 해는 덮지 않고, 비었거나 없는 해만 채운다.
    (합친 series, 새로 채운 해 목록)."""
    by = {str(e.get("period")): e for e in have}
    added = []
    for e in new:
        cur = by.get(str(e["period"]))
        if cur and cur.get("value") is not None and cur.get("status") != "missing" and str(cur.get("locator", "")).strip():
            continue
        by[str(e["period"])] = e
        added.append(str(e["period"]))
    return [by[k] for k in sorted(by)], added


# ---------------------------------------------------------------- 명령

def find_cik(t, fetch=None):
    """ticker 목록(www.sec.gov, cache 7일) → 전에 resolve한 company.json → efts 회사 검색(key 없음) 순서."""
    try:
        tick = read_json_bytes(cached(os.path.join(data_dir(), "tickers", "_sec", "company_tickers.json"),
                                      TICKERS_URL, TICKERS_CACHE_SECONDS, fetch))
        cik = cik_for(tick, t)
        if cik:
            return cik
        log("SEC ticker 목록에 %s가 없음, 전에 찾은 CIK나 efts 회사 검색으로 찾는다" % t.upper())
    except NeedContact:
        log("연락처가 없어 ticker 목록(www.sec.gov)을 받지 않고, 전에 찾은 CIK나 efts 회사 검색으로 찾는다")
    except (FetchError, ValueError) as e:
        log("ticker 목록을 받지 못함(%s), 전에 찾은 CIK나 efts 회사 검색으로 찾는다" % e)
    prev = os.path.join(filings_dir(t), "company.json")
    if os.path.exists(prev):
        try:
            cik = str(read_json(prev).get("cik") or "")
            if cik.isdigit():
                return "%010d" % int(cik)
        except (OSError, ValueError):
            pass
    url = EFTS_URL % urllib.parse.quote(t.upper())
    cik = cik_from_efts(read_json_bytes((fetch or http_get)(url)), t)
    if not cik:
        raise FetchError("efts 회사 검색에 ticker가 정확히 %s인 회사가 없음 (%s)" % (t.upper(), url))
    return cik


def load_company(t, fetch=None):
    cik = find_cik(t, fetch)
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
    print(json.dumps(info, ensure_ascii=False, indent=2, allow_nan=False))
    if not info["is_reit"]:
        log("SIC %s(%s): REIT(6798)가 아님. FFO 대신 일반 배당주 경로(EPS·FCF payout, Chowder)를 탄다" % (
            info["sic"], info["sic_description"]))
    return 0


def cmd_filings(args):
    info, sub = load_company(args.ticker)
    forms = [x.strip() for x in args.forms.split(",") if x.strip()]
    rows = filing_list(sub, forms, args.limit)
    latest = next((r for r in rows if r["earnings"]), None)
    print(json.dumps({"ticker": info["ticker"], "cik": info["cik"], "latest_earnings_8k": latest, "filings": rows},
                     ensure_ascii=False, indent=2, allow_nan=False))
    return 0


def cmd_config(args):
    if args.user_agent is not None:
        ua = " ".join(args.user_agent.split())
        if not ua or not EMAIL_RE.search(ua) or len(ua.split()) < 2:
            log("--user-agent는 \"이름 이메일\" 모양이어야 한다(SEC가 연락처를 요구한다)")
            return 1
        doc = read_config()
        doc["sec_user_agent"] = ua
        write_json(doc, config_path())
        try:
            os.chmod(config_path(), 0o600)
        except OSError:
            pass
        log("저장함: %s" % config_path())
    ua, where = contact_ua()
    print(json.dumps({"sec_user_agent": mask_ua(ua) if ua else None, "from": where,
                      "default_for_data_sec_gov": DEFAULT_UA, "config_file": config_path()},
                     ensure_ascii=False, indent=2))
    if not ua:
        log("연락처 설정 없음. " + CONTACT_HELP)
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
    print(json.dumps({"ticker": info["ticker"], "accession": acc, "exhibits": out}, ensure_ascii=False, indent=2, allow_nan=False))
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
    draft = xbrl_draft(cf, args.period, is_reit=info["is_reit"])
    if not info["is_reit"]:
        log("SIC %s: REIT가 아니라 일반 배당주 칸으로 초안을 만든다(EPS·FCF payout, Chowder)" % info["sic"])
    path = args.facts or facts_path(args.ticker, args.period)
    if os.path.exists(path):
        doc = read_json(path)
        new = add_new_fields(doc)
        if new:
            log("전에 만든 facts에 없던 칸 %d을 빈 칸으로 더함: %s" % (len(new), ", ".join(new)))
    else:
        f8k = earnings_8k_for(filing_list(sub, ["8-K"], 80), args.period)
        doc = skeleton(info, args.period, f8k and {k: f8k[k] for k in ("form", "accession", "filed")})
        ends = [v["as_of"] for v in draft.values()]
        if ends:  # 52·53주 회계연도면 실제 분기 끝날(XBRL)이 달력 끝날과 다르다
            doc["period_end"] = max(sorted(set(ends)), key=ends.count)
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
                     ensure_ascii=False, indent=2, allow_nan=False))
    if args.facts:
        doc = read_json(args.facts)
        ser = doc.setdefault("series", {})
        ser["dividend_per_share_annual"], added = merge_series(ser.get("dividend_per_share_annual") or [], series)
        write_json(doc, args.facts)
        log("facts에 연간 배당 %d년을 넣음(이미 값과 locator가 있는 해는 남김): %s" % (len(added), args.facts))
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
    c = sub.add_parser("config")
    g = c.add_mutually_exclusive_group(required=True)
    g.add_argument("--user-agent", help="\"이름 이메일\". $INVEST_DATA_DIR/config.json에 저장")
    g.add_argument("--show", action="store_true")
    try:
        args = p.parse_args(argv)
    except SystemExit as e:
        return 0 if e.code == 0 else 1
    if args.check:
        return run_check()
    if args.cmd == "config":
        return cmd_config(args)
    cmds = {"resolve": cmd_resolve, "filings": cmd_filings, "exhibits": cmd_exhibits,
            "xbrl": cmd_xbrl, "dividends": cmd_dividends}
    if args.cmd not in cmds:
        p.print_help(sys.stderr)
        return 1
    try:
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
    ok((earnings_8k_for(rows, "2026Q3") or {}).get("accession") == exp["earnings_accessions"][0]
       and (earnings_8k_for(rows, "2026Q2") or {}).get("accession") == exp["earnings_accessions"][1]
       and earnings_8k_for(rows, "2025Q3") is None, "분기마다 그 분기 실적 8-K(끝날 뒤 120일 안 가장 이른 것)")

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
    old = skeleton(info, "2026Q3", None)
    del old["facts"]["principal_amortization"]
    del old["series"]["covenants"]
    old["facts"]["cash"] = {"value": 7, "unit": "USD thousands", "status": "reported", "source": "agent (가상)",
                            "locator": "x (가상)"}
    added = add_new_fields(old)
    ok(added == ["principal_amortization", "series.covenants"] and old["facts"]["principal_amortization"]["status"] == "missing"
       and old["series"]["covenants"] == [] and old["facts"]["cash"]["value"] == 7,
       "전에 만든 facts에 없는 새 칸(%s)을 빈 칸으로 더하고 있는 값은 그대로" % added)

    # 4b. 일반 배당주 초안: 52·53주 회계연도(분기 끝날 2026-09-27), 현금흐름은 YTD
    cfd = fx_json("companyfacts_div.json")
    dd = xbrl_draft(cfd, "2026Q3", is_reit=False)
    for k, (val, loc) in exp["xbrl_div"]["values"].items():
        got = dd.get(k, {})
        ok(got.get("value") == val and got.get("locator") == loc, "xbrl(배당주) %s = %s %s (got %s %s)" % (
            k, val, loc, got.get("value"), got.get("locator")))
    ok(xbrl_draft(cfd, "2026Q2", is_reit=False).get("operating_cash_flow_ytd", {}).get("value") == 11000000000,
       "YTD는 그 분기 끝날의 가장 긴 기간(2분기는 6개월 값)")
    ok("depreciation_amortization" not in dd and "ffo_reported" not in skeleton(
        dict(info, is_reit=False), "2026Q3", None)["facts"], "배당주 초안·skeleton에는 FFO 칸이 없음")
    ok(set(skeleton(dict(info, is_reit=False), "2026Q3", None)["facts"]) == set(reit_calc.DIVIDEND_FACT_KINDS),
       "배당주 skeleton 칸 = reit_calc.DIVIDEND_FACT_KINDS")
    ok(xbrl_draft(cf, "2026Q4") == {} and pick_fact(cfd, "us-gaap:NetIncomeLoss", "USD", "duration",
                                                    dt.date(2026, 10, 15)) is None,
       "끝날이 7일 넘게 다르면 다른 분기로 보고 고르지 않음")

    # 5. 배당 history
    series, concept, hint = annual_dividends(cf)
    ok([(s["period"], s["value"]) for s in series] == [tuple(x) for x in exp["dividends"]]
       and concept.endswith("CashPaid") and "월별" in hint,
       "연간 배당: 10-K 연간 값만, 해마다 늦게 낸 값, Declared 없으면 CashPaid")
    odd = {"facts": {"us-gaap": {
        "CommonStockDividendsPerShareDeclared": {"units": {"USD/shares": [
            {"start": "2025-01-01", "end": "2025-12-31", "val": 0.03, "form": "10-K", "filed": "2026-02-20"}]}},
        "CommonStockDividendsPerShareCashPaid": {"units": {"USD/shares": [
            {"start": "2025-01-01", "end": "2025-12-31", "val": 4.04, "form": "10-K", "filed": "2026-02-20"}]}}}}}
    s2, c2, h2 = annual_dividends(odd)
    ok(c2.endswith("CashPaid") and s2[0]["value"] == 4.04 and "절반" in h2,
       "Declared 값이 CashPaid의 절반도 안 되면 CashPaid를 씀(가상: 0.03 vs 4.04)")
    odd["facts"]["us-gaap"]["CommonStockDividendsPerShareDeclared"]["units"]["USD/shares"][0]["val"] = 4.05
    ok(annual_dividends(odd)[1].endswith("Declared"), "Declared가 정상 값이면 Declared를 씀")
    have = [{"period": "2024", "value": 3.0, "unit": "USD/share", "status": "reported", "source": "sec:8-K (가상)",
             "locator": "agent가 Ex.99에서 고친 값 (가상)"},
            {"period": "2023", "value": None, "status": "missing", "note": "가상"}]
    new = [{"period": y, "value": v, "status": "reported", "source": "sec:xbrl (가상)", "locator": "x (가상)"}
           for y, v in (("2023", 2.9), ("2024", 3.1), ("2025", 3.2))]
    merged, added = merge_series(have, new)
    ok([(e["period"], e["value"]) for e in merged] == [("2023", 2.9), ("2024", 3.0), ("2025", 3.2)]
       and added == ["2023", "2025"], "dividends --facts는 locator가 있는 해를 덮지 않고 빈 해만 채움")

    # 6. network 약속
    old = os.environ.pop("SEC_USER_AGENT", None)
    old_dir = os.environ.get("INVEST_DATA_DIR")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["INVEST_DATA_DIR"] = tmp
            ok(user_agent("https://data.sec.gov/x") == DEFAULT_UA == "invest-skills/1.0" and "@" not in DEFAULT_UA,
               "연락처가 없으면 data.sec.gov에는 식별 정보 없는 기본 User-Agent")
            try:
                user_agent("https://www.sec.gov/Archives/x")
                need = None
            except NeedContact as e:
                need = str(e)
            ok(need is not None and "config --user-agent" in need and "key가 아니다" in need,
               "연락처가 없으면 www.sec.gov 요청 전에 멈추고 config 저장을 안내")
            import contextlib
            import io as _io
            with contextlib.redirect_stdout(_io.StringIO()), contextlib.redirect_stderr(_io.StringIO()):
                bad_cfg = main(["config", "--user-agent", "nomail"])
                saved = main(["config", "--user-agent", "Test User test@example.com (가상)"])
            ok(bad_cfg == 1 and saved == 0 and read_json(config_path())["sec_user_agent"] == "Test User test@example.com (가상)"
               and config_path().startswith(tmp), "config: 이메일 없는 값은 거절, 저장은 데이터 폴더의 config.json")
            ok(contact_ua()[1].startswith("config:") and user_agent("https://www.sec.gov/x").startswith("Test User"),
               "config.json의 sec_user_agent를 www.sec.gov에 씀")
            os.environ["SEC_USER_AGENT"] = "Env User env@example.com (가상)"
            ok(contact_ua() == ("Env User env@example.com (가상)", "env:SEC_USER_AGENT"), "순서: 환경 변수 > config")
            os.environ.pop("SEC_USER_AGENT", None)
            ok(mask_ua("Test User test@example.com") == "Test User t***@example.com", "config 출력은 이메일 일부를 가림")
            os.remove(config_path())
            ok(contact_ua() == (None, None), "둘 다 없으면 연락처 없음")

            # efts 회사 검색(가상 응답): tickers 칸에 정확히 그 ticker가 있는 항목만
            efts = read_json(os.path.join(fx, "efts_TEST.json"))
            ok(cik_from_efts(efts, "TEST") == "0000000001" and cik_from_efts(efts, "TES") is None
               and cik_from_efts(efts, "TST.B") == "0000000003" and cik_from_efts({}, "TEST") is None,
               "efts 응답: 정확히 일치하는 ticker의 CIK만(앞부분만 같으면 None)")

            # ticker 목록 cache 없이, 연락처 없이 resolve: efts로 CIK를 찾고 data.sec.gov로 submissions (가짜 http_get)
            global http_get
            real_get, urls = http_get, []
            with open(os.path.join(fx, "submissions.json"), "rb") as f:
                sub_bytes = f.read()
            with open(os.path.join(fx, "efts_TEST.json"), "rb") as f:
                efts_bytes = f.read()

            def fake_get(url):
                user_agent(url)  # 진짜와 같이 www.sec.gov면 NeedContact
                urls.append(url)
                if url.startswith("https://efts.sec.gov/"):
                    return efts_bytes
                if url.startswith("https://data.sec.gov/submissions/"):
                    return sub_bytes
                raise FetchError("HTTP 404 (%s)" % url)
            try:
                http_get = fake_get
                with contextlib.redirect_stdout(_io.StringIO()), contextlib.redirect_stderr(_io.StringIO()):
                    r1 = main(["resolve", "TEST"])
                    r2 = main(["resolve", "NOPE"])
                    r3 = main(["exhibits", "TEST", "--accession", "0000000001-26-000001"])
                    err = _io.StringIO()
                with contextlib.redirect_stdout(_io.StringIO()), contextlib.redirect_stderr(err):
                    main(["exhibits", "TEST", "--accession", "0000000001-26-000001"])
            finally:
                http_get = real_get
            ok(r1 == 0 and urls[0].startswith(EFTS_URL % "TEST") and urls[1] == SUBMISSIONS_URL % "0000000001"
               and not any("www.sec.gov" in u for u in urls),
               "연락처·ticker 목록 없이 resolve: efts → CIK → data.sec.gov submissions, exit 0")
            ok(r2 == 1, "efts에 정확히 일치하는 ticker가 없으면 exit 1")
            ok(r3 == 1 and "config --user-agent" in err.getvalue() and not any("Archives" in u for u in urls),
               "연락처 없이 exhibits는 www.sec.gov에 요청하지 않고 exit 1, stderr에 config 안내")
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

#!/usr/bin/env python3
"""reit-stock-review: SEC EDGAR에서 회사·공시·exhibit·XBRL 값을 받는다.

명령
  resolve T                         ticker → CIK, 회사 이름, SIC(6798이면 REIT)
  filings T [--forms F] [--limit N] 최근 공시 목록. 실적 8-K(item 2.02)를 표시한다
  exhibits T --accession A [--type 10-Q] [--images]
                                    공시의 EX-99 문서(--type이면 그 type 문서)를 받아 .txt로 바꾼다.
                                    --images면 문서가 참조하는 그림(jpg·png·gif)도 같은 폴더에 받는다(supplemental이 그림인 회사)
  xbrl T --period P [--facts F]     XBRL에서 그 분기 값을 facts 초안에 채운다(비어 있는 칸만).
                                    REIT가 아니면 일반 배당주 칸(EPS, 현금흐름 YTD, 배당)
  dividends T [--facts F]           XBRL 연간 주당 배당 history(대조용. split·특별배당을 가르지 못해 quote.py dividends가 먼저)
  trend T [--quarters 8] [--period P]
                                    companyfacts로 최근 8분기 매출·이자비용·부채·희석 주식 수·주당 배당 표.
                                    $INVEST_DATA_DIR/tickers/<T>/trend/<P>.json과 stdout. 4분기보다 적은 지표는 빼고 알린다
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
    # 부채 시가: 10-Q·10-K fair value 표의 fair value와 같은 표의 carrying value. 원금을 DebtInstrumentCarryingAmount로
    # 다는 회사가 있어(PLD) carrying은 LongTermDebt를 먼저 본다. 10-Q 표와 맞춰 본다
    "debt_fair_value": ("instant", "USD", ["us-gaap:DebtInstrumentFairValue", "us-gaap:LongTermDebtFairValue"]),
    "debt_carrying_value": ("instant", "USD", ["us-gaap:LongTermDebt", "us-gaap:DebtInstrumentCarryingAmount"]),
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
DIVIDEND_CONCEPTS = ["us-gaap:CommonStockDividendsPerShareDeclared", "us-gaap:CommonStockDividendsPerShareCashPaid"]
FFO_BASIS_NOTE = "XBRL 초안: 회사 FFO 표와 기준이 다를 수 있다(%s). Ex.99 줄과 맞춰 볼 것"
XBRL_NOTE = {"dividend_regular_latest": "XBRL 초안: 분기 배당 1회분(분기 배당이면 분기 값과 같다). 최근 배당 공지가 다르면 고친다",
             "depreciation_amortization": FFO_BASIS_NOTE % "총 D&A라 FFO 표의 부동산 D&A보다 클 수 있음",
             "accumulated_depreciation": FFO_BASIS_NOTE % "lease intangible 상각이 빠질 수 있음",
             "diluted_shares_weighted": FFO_BASIS_NOTE % "OP unit이 빠질 수 있음",
             "net_income_common": FFO_BASIS_NOTE % "FFO 표의 시작 줄과 NCI·우선주 처리가 다를 수 있음",
             "debt_carrying_value": FFO_BASIS_NOTE % "발행비용을 뺀 carrying value라 원금과 다를 수 있음"}
DIV_XBRL_NOTE = ("XBRL 연간 합은 split·특별배당을 가르지 못한다(reverse split 전후 값이 섞이고 특별·주식 배당이 든 해 다음 해가 컷으로 보인다). "
                 "quote.py dividends가 먼저이고 이 값은 대조용이다")
# 이자비용 줄을 순이자(이자비용이 음수) concept으로 다는 회사가 있다(DEA). 다른 후보가 없을 때만 부호를 바꿔 쓴다
NEGATE = {"us-gaap:InterestIncomeExpenseNonoperatingNet": "순이자 concept(이자비용이 음수, 이자수익 차감)이라 부호를 바꿈"}
TREND_SCHEMA = "reit-trend/1"
TREND_MIN = 4   # 값이 이보다 적은 지표는 표에서 뺀다
# 부채: 합계 concept(xbrl 초안 debt_carrying_value의 후보 + 둘). 없으면 구성 concept을 무리마다 하나씩 골라 더한다(겹치지 않게)
DEBT_TOTALS = XBRL_MAP["debt_carrying_value"][2] + ["us-gaap:DebtLongtermAndShorttermCombinedAmount",
                                                   "us-gaap:LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities"]
DEBT_PART_GROUPS = [["us-gaap:SeniorNotes", "us-gaap:NotesPayable", "us-gaap:UnsecuredDebt"],
                    ["us-gaap:SecuredDebt", "us-gaap:SecuredLongTermDebt"], ["us-gaap:LoansPayable"],
                    ["us-gaap:LineOfCredit", "us-gaap:LongTermLineOfCredit"], ["us-gaap:CommercialPaper"]]
# (칸, 이름, 성격, unit, concept 후보, note). flow는 분기 값이 없으면 누적(YTD) 차이로 푼다. average(가중평균)는 풀지 않는다
TREND_METRICS = [
    ("revenue", "매출", "flow", "USD", ["us-gaap:Revenues", "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                                       "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
                                       "us-gaap:OperatingLeaseLeaseIncome"],
     "총매출(Revenues 계열). 보도자료 표의 total revenues와 맞춰 본다"),
    ("interest_expense", "이자비용", "flow", "USD", XBRL_MAP["interest_expense"][2] + ["us-gaap:InterestIncomeExpenseNonoperatingNet"],
     "손익계산서 이자비용"),
    ("debt", "부채", "debt", "USD", DEBT_TOTALS,
     "carrying value라 회사 total debt(원금)와 다를 수 있다. 구성 concept 합이면 빠진 부채(revolver 등)가 있을 수 있다"),
    ("diluted_shares", "희석 주식 수(가중평균)", "average", "shares",
     ["us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding"],
     "OP unit이 빠질 수 있다. 가중평균이라 4분기(연간 − 9개월)는 풀지 않는다"),
    ("dividend_per_share", "주당 배당(선언)", "flow", "USD/shares", DIVIDEND_CONCEPTS,
     "Declared가 없으면 CashPaid. split 전 값이 섞이지 않았는지 quote.py dividends와 맞춰 본다"),
]
END_TOLERANCE_DAYS = 7  # 52·53주 회계연도는 분기 끝날이 달력 분기 끝날과 며칠 다르다
UNIT_NAME = {"USD": "USD", "shares": "shares", "USD/shares": "USD/share"}


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
CONTACT_HELP = ("www.sec.gov는 SEC가 요구하는 연락처(이름과 이메일)가 든 User-Agent에만 원문을 준다(API key가 아니다). "
                "연락처가 없으면 사용자에게 묻지 않고 agent의 web fetch 도구로 같은 URL을 읽는다. "
                "있으면 `python3 <skill 폴더>/scripts/edgar.py config --user-agent \"이름 이메일\"`로 저장해 script가 직접 받게 할 수 있다"
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
    """(값, 어디서). SEC_USER_AGENT 환경 변수 > config.json sec_user_agent > (None, None).
    ASCII가 아닌 값은 쓰지 않는다(HTTP header에 한글을 넣을 수 없다)."""
    env = os.environ.get("SEC_USER_AGENT", "").strip()
    if env and env.isascii():
        return env, "env:SEC_USER_AGENT"
    if env:
        log("SEC_USER_AGENT에 영문·숫자·기호(ASCII)가 아닌 글자가 있어 쓰지 않음: 이름과 이메일을 영문으로 다시 준다")
    cfg = str(read_config().get("sec_user_agent") or "").strip()
    if cfg and cfg.isascii():
        return cfg, "config:" + config_path()
    if cfg:
        log("config.json sec_user_agent가 ASCII가 아니라 쓰지 않음: edgar.py config --user-agent로 영문으로 다시 저장한다")
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


IMG_RE = re.compile(r"""<img\b[^>]*?\bsrc\s*=\s*(["']?)([^"'\s>]+)\1""", re.I)
SAFE_IMG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.(?:jpe?g|png|gif)$", re.I)


def image_refs(html_text):
    """문서가 참조하는 그림 중 같은 폴더의 jpg·png·gif 이름(나온 차례, 겹치면 한 번). 다른 host·상위 경로는 버린다."""
    out = []
    for m in IMG_RE.finditer(html_text):
        name = m.group(2)
        if SAFE_IMG.match(name) and name not in out:
            out.append(name)
    return out


def wanted_documents(docs, doc_type):
    """filing index 문서 중 type이 doc_type(EX-99, 10-Q 등)으로 시작하는 것."""
    return [d for d in docs if d["type"].upper().startswith(doc_type.upper())]


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


def xbrl_dividends_doc(info, cf):
    series, concept, hint = annual_dividends(cf)
    return {"ticker": info["ticker"], "concept": concept, "frequency_hint": hint, "series": series, "note": DIV_XBRL_NOTE}


# ---------------------------------------------------------------- 8분기 추세

def prev_quarter(q):
    y, n = int(q[:4]), int(q[5])
    return "%dQ%d" % ((y - 1, 4) if n == 1 else (y, n - 1))


def _obs(cf, concept, unit):
    ns, name = concept.split(":")
    return (((cf.get("facts") or {}).get(ns) or {}).get(name) or {}).get("units", {}).get(unit) or []


def _days(o):
    return (parse_date(o["end"]) - parse_date(o["start"])).days


def _num(v):
    v = round(v, 6)
    return int(v) if float(v).is_integer() else v


def ytd_diff(cf, concept, unit, end):
    """분기 값 = 그 분기 끝날의 누적(YTD) − 같은 시작일로 한 분기 짧은 누적(연간 − 9개월, 6개월 − 3개월). (A, B) 또는 None."""
    obs = [o for o in _obs(cf, concept, unit) if o.get("start")]
    cands = [o for o in obs if abs((parse_date(o["end"]) - end).days) <= END_TOLERANCE_DAYS and 100 < _days(o) <= 380]
    for a in sorted(cands, key=lambda o: (_days(o), o.get("filed", "")), reverse=True):
        bs = [o for o in obs if abs((parse_date(o["start"]) - parse_date(a["start"])).days) <= 3
              and 80 <= _days(a) - _days(o) <= 100]
        if bs:
            return a, max(bs, key=lambda o: o.get("filed", ""))
    return None


def trend_cell(cf, concept, unit, kind, end):
    """분기 하나의 값 envelope. 풀 수 없으면 missing."""
    o = pick_fact(cf, concept, unit, "instant" if kind == "instant" else "duration", end)
    if o:
        span = ("%s..%s" % (o["start"], o["end"])) if o.get("start") else o["end"]
        return {"value": o["val"], "unit": UNIT_NAME[unit], "status": "reported", "as_of": o["end"],
                "source": "sec:xbrl %s %s" % (o.get("form", ""), o.get("accn", "")), "locator": "%s %s" % (concept, span),
                "note": ""}
    if kind == "average":
        return reit_calc.missing("3개월 값이 없음. 가중평균이라 누적 차이로 풀지 않는다")
    if kind == "flow":
        ab = ytd_diff(cf, concept, unit, end)
        if ab:
            a, b = ab
            return {"value": _num(a["val"] - b["val"]), "unit": UNIT_NAME[unit], "status": "derived", "as_of": a["end"],
                    "source": "sec:xbrl %s %s − %s %s" % (a.get("form", ""), a.get("accn", ""), b.get("form", ""), b.get("accn", "")),
                    "locator": "%s %s..%s %s − %s..%s %s" % (concept, a["start"], a["end"], a["val"], b["start"], b["end"], b["val"]),
                    "note": "분기 값이 없어 누적(YTD) 차이로 풀었다"}
    return reit_calc.missing("그 분기 값이 companyfacts에 없음")


def debt_parts_cells(cf, quarters):
    """합계 concept이 없을 때: 마지막 분기에 값이 있는 구성 concept을 무리마다 하나씩 골라 분기마다 더한다.
    고른 concept 중 하나라도 없는 분기는 missing. (concept 이름, {분기: envelope})."""
    last = quarter_end(quarters[-1])
    parts = []
    for group in DEBT_PART_GROUPS:
        c = next((c for c in group if pick_fact(cf, c, "USD", "instant", last)), None)
        if c:
            parts.append(c)
    cells = {}
    for q in quarters:
        got = [(c, pick_fact(cf, c, "USD", "instant", quarter_end(q))) for c in parts]
        if not parts or any(o is None for _, o in got):
            cells[q] = reit_calc.missing("구성 concept 중 그 분기 값이 없는 것: %s" % ", ".join(c for c, o in got if o is None)
                                         if parts else "부채 concept이 없음")
            continue
        cells[q] = {"value": sum(o["val"] for _, o in got), "unit": "USD", "status": "derived" if len(got) > 1 else "reported",
                    "as_of": max(o["end"] for _, o in got),
                    "source": "sec:xbrl " + ", ".join(sorted({"%s %s" % (o.get("form", ""), o.get("accn", "")) for _, o in got})),
                    "locator": " + ".join("%s %s" % (c, o["val"]) for c, o in got) + " (%s)" % got[0][1]["end"],
                    "note": "합계 concept이 없어 구성 concept을 더함"}
    return " + ".join(parts), cells


def trend_doc(cf, ticker, period, n=8):
    """companyfacts → 최근 n분기 표. 지표마다 분기 값이 가장 많은 concept 하나를 쓴다(분기마다 concept을 섞지 않는다)."""
    quarters = [period]
    while len(quarters) < n:
        quarters.insert(0, prev_quarter(quarters[0]))
    metrics, dropped = {}, []
    for key, label, kind, unit, cands, note in TREND_METRICS:
        best = None
        for c in cands:
            cells = {q: trend_cell(cf, c, unit, "instant" if kind == "debt" else kind, quarter_end(q)) for q in quarters}
            if c in NEGATE:
                cells = {q: dict(v, value=-v["value"], note="; ".join(x for x in (v["note"], NEGATE[c]) if x))
                         if v["value"] is not None else v for q, v in cells.items()}
            cnt = sum(1 for v in cells.values() if v["value"] is not None)
            if cnt and (best is None or cnt > best[2]):
                best = (c, cells, cnt)
        if kind == "debt" and (best is None or best[2] < len(quarters)):
            c, cells = debt_parts_cells(cf, quarters)
            cnt = sum(1 for v in cells.values() if v["value"] is not None)
            if cnt and (best is None or cnt > best[2]):
                best = (c, cells, cnt)
        if best is None or best[2] < min(TREND_MIN, n):
            dropped.append({"metric": key, "label": label, "count": best[2] if best else 0,
                            "concept": best[0] if best else None,
                            "reason": "값이 %d분기뿐이라 표에서 뺌(%d분기 이상만 싣는다)" % (best[2] if best else 0, min(TREND_MIN, n))})
            continue
        metrics[key] = {"label": label, "concept": best[0], "unit": UNIT_NAME[unit], "kind": kind, "count": best[2],
                        "values": best[1], "note": "; ".join(x for x in (note, NEGATE.get(best[0])) if x)}
    return {"schema": TREND_SCHEMA, "ticker": ticker.upper(), "period": period, "quarters": quarters,
            "source": "sec:companyfacts", "metrics": metrics, "dropped": dropped,
            "note": "분기는 달력 분기(끝날 ±%d일). 값은 companyfacts에서 늦게 낸 것, 3개월 값이 없으면 누적 차이(derived)" % END_TOLERANCE_DAYS}


def latest_quarter(cf):
    """추세 concept 중 가장 늦은 분기 끝날(달력 분기 끝날 ±7일)의 분기."""
    best = None
    concepts = [(c, u) for _, _, _, u, cs, _ in TREND_METRICS for c in cs] + [
        (c, "USD") for g in DEBT_PART_GROUPS for c in g]
    for c, u in concepts:
        for o in _obs(cf, c, u):
            e = parse_date(o["end"])
            for q in ("%dQ%d" % (e.year + dy, k) for dy in (-1, 0) for k in (1, 2, 3, 4)):
                if abs((quarter_end(q) - e).days) <= END_TOLERANCE_DAYS and (best is None or q > best):
                    best = q
    return best


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
    """비어 있는(missing) 칸만 채운다. agent가 채운 값(여러 줄 합인 parts 포함)은 덮지 않는다."""
    filled = []
    for k, v in draft.items():
        cur = doc["facts"].get(k)
        if cur is None or ("parts" not in cur and (cur.get("status") == "missing" or cur.get("value") is None)):
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
        if not ua.isascii():
            log("--user-agent는 영문·숫자·기호(ASCII)로만 쓴다. HTTP header에 한글을 넣을 수 없다")
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


AGENT_FETCH_HOW = ("SEC 연락처가 없어 script가 www.sec.gov를 받지 않았다. 사용자에게 묻지 않는다. "
                   "agent의 web fetch 도구로 index_url을 열어 type이 %s로 시작하는 문서의 URL을 찾고, "
                   "문서마다 열어 본문(표는 cell을 tab, row를 줄로)을 save_dir/<문서 이름에서 확장자를 뺀 것>.txt에 쓴다. "
                   "facts의 source는 `sec:<문서 URL>`, locator는 그 .txt 줄 번호다. 원문이 같아 status는 그대로 reported다. "
                   "사용자가 연락처를 주면 `edgar.py config --user-agent \"이름 이메일\"`로 저장해 다음부터 script가 직접 받게 할 수 있다(선택)")


def agent_fetch_fallback(info, acc, index_url, adir, doc_type):
    """연락처가 없을 때 멈추지 않고, agent가 web fetch 도구로 같은 원문을 읽도록 URL과 저장 위치를 낸다(exit 2)."""
    os.makedirs(adir, exist_ok=True)
    log(AGENT_FETCH_HOW % doc_type)
    print(json.dumps({"ticker": info["ticker"], "accession": acc, "fetch_by_agent": True, "type": doc_type,
                      "index_url": index_url, "save_dir": adir, "exhibits": []},
                     ensure_ascii=False, indent=2, allow_nan=False))
    return 2


def cmd_exhibits(args):
    info, _ = load_company(args.ticker)
    acc = args.accession.strip()
    if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", acc):
        log("--accession은 0000726728-26-000056 모양")
        return 1
    base = ARCHIVES % (int(info["cik"]), acc.replace("-", ""))
    adir = os.path.join(filings_dir(args.ticker), acc)
    index_url = base + acc + "-index.htm"
    try:
        idx = cached(os.path.join(adir, "index.htm"), index_url, None).decode("utf-8", "replace")
    except NeedContact:
        return agent_fetch_fallback(info, acc, index_url, adir, args.type)
    docs = wanted_documents(index_documents(idx), args.type)
    if not docs:
        log("이 공시에 %s 문서가 없음" % args.type)
        return 2
    out = []
    for d in docs:
        try:
            raw = cached(os.path.join(adir, d["document"]), base + d["document"], None)
        except NeedContact:
            return agent_fetch_fallback(info, acc, index_url, adir, args.type)
        tpath = os.path.join(adir, d["document"].rsplit(".", 1)[0] + ".txt")
        images = []
        if d["document"].lower().endswith((".htm", ".html")):
            text = raw.decode("utf-8", "replace")
            with open(tpath, "w", encoding="utf-8") as f:
                f.write(html_to_text(text))
            for name in (image_refs(text) if args.images else []):
                try:
                    cached(os.path.join(adir, name), base + name, None)
                except NeedContact:
                    return agent_fetch_fallback(info, acc, index_url, adir, args.type)
                images.append({"name": name, "path": os.path.join(adir, name), "url": base + name})
        else:
            tpath = None
        out.append(dict(d, path=os.path.join(adir, d["document"]), text_path=tpath, url=base + d["document"], images=images))
    if args.images:
        log("그림 %d개를 받음: %s" % (sum(len(x["images"]) for x in out), adir))
    print(json.dumps({"ticker": info["ticker"], "accession": acc, "images_requested": bool(args.images), "exhibits": out},
                     ensure_ascii=False, indent=2, allow_nan=False))
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
    doc = xbrl_dividends_doc(info, cf)
    series = doc["series"]
    log(DIV_XBRL_NOTE)
    print(json.dumps(doc, ensure_ascii=False, indent=2, allow_nan=False))
    if args.facts:
        doc = read_json(args.facts)
        ser = doc.setdefault("series", {})
        ser["dividend_per_share_annual"], added = merge_series(ser.get("dividend_per_share_annual") or [], series)
        write_json(doc, args.facts)
        log("facts에 연간 배당 %d년을 넣음(이미 값과 locator가 있는 해는 남김): %s" % (len(added), args.facts))
    return 0 if series else 2


def cmd_trend(args):
    if args.period and not quarter_end(args.period):
        log("--period는 2026Q3 모양(달력 분기)")
        return 1
    if args.quarters < 1:
        log("--quarters는 1 이상")
        return 1
    info, _ = load_company(args.ticker)
    cf = load_companyfacts(args.ticker, info["cik"])
    period = args.period or latest_quarter(cf)
    if not period:
        log("companyfacts에 추세 concept의 분기 값이 없음")
        return 1
    doc = trend_doc(cf, info["ticker"], period, args.quarters)
    doc["url"] = COMPANYFACTS_URL % info["cik"]
    path = os.path.join(ticker_dir(args.ticker), "trend", "%s.json" % period)
    write_json(doc, path)
    for k, m in doc["metrics"].items():
        empty = [q for q, v in m["values"].items() if v["value"] is None]
        log("%s(%s): %d/%d분기%s" % (m["label"], m["concept"], m["count"], len(doc["quarters"]),
                                   ", 없음 " + " ".join(empty) if empty else ""))
    for d in doc["dropped"]:
        log("표에서 뺌: %s(%s) %s" % (d["label"], d["metric"], d["reason"]))
    log("추세 표: %s" % path)
    print(json.dumps(doc, ensure_ascii=False, indent=2, allow_nan=False))
    return 2 if doc["dropped"] else 0


def main(argv):
    p = argparse.ArgumentParser(prog="edgar.py", description="SEC EDGAR 공시·XBRL")
    p.add_argument("--check", action="store_true", help="network 없이 fixture로 확인")
    sub = p.add_subparsers(dest="cmd")
    for name in ("resolve", "filings", "exhibits", "xbrl", "dividends", "trend"):
        s = sub.add_parser(name)
        s.add_argument("ticker")
        if name == "filings":
            s.add_argument("--forms", default="8-K,10-Q,10-K")
            s.add_argument("--limit", type=int, default=20)
        if name == "exhibits":
            s.add_argument("--accession", required=True)
            s.add_argument("--type", default="EX-99", help="받을 문서 type(기본 EX-99. 10-Q 본문의 fair value 주석은 10-Q)")
            s.add_argument("--images", action="store_true", help="문서가 참조하는 그림(jpg·png·gif)도 받는다")
        if name == "trend":
            s.add_argument("--quarters", type=int, default=8)
            s.add_argument("--period", help="마지막 분기(2026Q3). 없으면 companyfacts의 가장 늦은 분기")
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
            "xbrl": cmd_xbrl, "dividends": cmd_dividends, "trend": cmd_trend}
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
    ok([d["type"] for d in wanted_documents(docs, "EX-99")] == ["EX-99.1", "EX-99.2"]
       and [d["type"] for d in wanted_documents(docs, "8-k")] == ["8-K"],
       "exhibits --type: 기본 EX-99, 10-Q·8-K 본문도 고를 수 있음(대소문자 무시)")
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
    pdoc = skeleton(info, "2026Q3", None)
    pdoc["facts"]["debt_fair_value"] = {"parts": [{"value": 1, "unit": "USD thousands", "status": "reported",
                                                   "source": "agent (가상)", "locator": "x (가상)"}], "unit": "USD thousands"}
    pfilled = merge_missing(pdoc, draft)
    ok("debt_fair_value" not in pfilled and "parts" in pdoc["facts"]["debt_fair_value"]
       and "debt_carrying_value" in pfilled, "agent가 parts(여러 줄)로 적은 칸은 초안이 덮지 않음")
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
            ok(need is not None and "web fetch" in need and "key가 아니다" in need,
               "연락처가 없으면 www.sec.gov 요청 전에 멈추고 agent web fetch 경로를 안내")
            import contextlib
            import io as _io
            # 아래 연락처는 모두 가상이다(HTTP header라 ASCII만 되어 값에 "(가상)"을 붙이지 못한다)
            with contextlib.redirect_stdout(_io.StringIO()), contextlib.redirect_stderr(_io.StringIO()):
                bad_cfg = main(["config", "--user-agent", "nomail"])
                bad_ascii = main(["config", "--user-agent", "테스트 test@example.com"])
                saved = main(["config", "--user-agent", "Test User test@example.com"])
            ok(bad_cfg == 1 and bad_ascii == 1 and saved == 0 and read_json(config_path())["sec_user_agent"] == "Test User test@example.com"
               and config_path().startswith(tmp), "config: 이메일 없는 값은 거절, 저장은 데이터 폴더의 config.json")
            ok(contact_ua()[1].startswith("config:") and user_agent("https://www.sec.gov/x").startswith("Test User"),
               "config.json의 sec_user_agent를 www.sec.gov에 씀")
            os.environ["SEC_USER_AGENT"] = "Env User env@example.com"
            ok(contact_ua() == ("Env User env@example.com", "env:SEC_USER_AGENT"), "순서: 환경 변수 > config")
            os.environ["SEC_USER_AGENT"] = "테스트 env@example.com"
            with contextlib.redirect_stderr(_io.StringIO()) as err:
                got_ua = contact_ua()
            ok(got_ua[1].startswith("config:") and "ASCII" in err.getvalue(),
               "SEC_USER_AGENT가 ASCII가 아니면 쓰지 않고(config로) 영문으로 다시 주라고 알림 (got %s)" % (got_ua[1],))
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
                    err, out3 = _io.StringIO(), _io.StringIO()
                with contextlib.redirect_stdout(out3), contextlib.redirect_stderr(err):
                    r3 = main(["exhibits", "TEST", "--accession", "0000000001-26-000001"])
            finally:
                http_get = real_get
            ok(r1 == 0 and urls[0].startswith(EFTS_URL % "TEST") and urls[1] == SUBMISSIONS_URL % "0000000001"
               and not any("www.sec.gov" in u for u in urls),
               "연락처·ticker 목록 없이 resolve: efts → CIK → data.sec.gov submissions, exit 0")
            ok(r2 == 1, "efts에 정확히 일치하는 ticker가 없으면 exit 1")
            fb = json.loads(out3.getvalue() or "{}")
            ok(r3 == 2 and fb.get("fetch_by_agent") is True
               and fb.get("index_url", "").endswith("/000000000126000001/0000000001-26-000001-index.htm")
               and fb.get("save_dir", "").endswith(os.path.join("TEST", "filings", "0000000001-26-000001"))
               and "묻지 않는다" in err.getvalue() and "web fetch" in err.getvalue()
               and not any("Archives" in u for u in urls),
               "연락처 없이 exhibits는 www.sec.gov에 요청하지 않고 exit 2, 사용자에게 묻지 말고 agent web fetch로 읽을 index_url·save_dir을 냄")
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

    check_s3(ok, fx, cf, info)

    log("check: %d개 실패" % len(fails) if fails else "check: 모두 통과")
    return 2 if fails else 0


def check_s3(ok, fx, cf, info):
    """trend, dividends note, XBRL 초안 note, exhibits --images. 기대값은 fixture를 만들 때 손으로 셈(가상 회사)."""
    import contextlib
    import io as _io
    # XBRL 초안 중 회사 FFO 표와 기준이 다른 칸(findings 15): 값은 그대로, note에 맞춰 볼 곳
    draft = xbrl_draft(cf, "2026Q3")
    for k in ("depreciation_amortization", "accumulated_depreciation", "diluted_shares_weighted", "net_income_common",
              "debt_carrying_value"):
        ok("회사 FFO 표와 기준이 다를 수 있다" in draft[k]["note"] and "Ex.99" in draft[k]["note"],
           "XBRL 초안 %s note: 회사 FFO 표와 기준이 다를 수 있다, Ex.99 줄과 맞춰 볼 것" % k)
    # dividends: 대조용으로 남기고 note
    dd = xbrl_dividends_doc(info, cf)
    ok("quote.py dividends" in dd["note"] and "split" in dd["note"] and "특별" in dd["note"] and dd["series"],
       "edgar.py dividends 출력 note: split·특별배당을 가르지 못한다, quote.py dividends가 먼저")

    # trend: 가상 회사 8분기
    ct = read_json(os.path.join(fx, "companyfacts_trend.json"))
    td = trend_doc(ct, "TEST", "2026Q3", 8)
    qs = ["2024Q4", "2025Q1", "2025Q2", "2025Q3", "2025Q4", "2026Q1", "2026Q2", "2026Q3"]
    ok(td["quarters"] == qs and td["schema"] == TREND_SCHEMA and td["period"] == "2026Q3", "trend: 8분기(오래된 것부터)")
    M = td["metrics"]

    def vals(m):
        return [(M[m]["values"][q]["value"], M[m]["values"][q]["status"]) for q in qs]
    ok(vals("revenue") == [(1050, "derived"), (1060, "reported"), (1090, "reported"), (1105, "reported"), (1150, "derived"),
                           (1200, "reported"), (1210, "reported"), (1230, "reported")]
       and M["revenue"]["concept"] == "us-gaap:Revenues",
       "매출: 4분기는 연간 − 9개월(4000 − 2950, 4400 − 3250), 다시 낸 값(1105)은 늦게 낸 것, 분기 값이 적은 concept은 고르지 않음")
    ok(M["revenue"]["values"]["2025Q4"]["locator"].startswith("us-gaap:Revenues 2025-01-01..2025-12-31 4400 − 2025-01-01..2025-09-30 3250")
       and M["revenue"]["values"]["2025Q3"]["source"] == "sec:xbrl 10-Q 0000000001-26-000030"
       and M["revenue"]["values"]["2026Q3"]["as_of"] == "2026-09-30" and M["revenue"]["values"]["2026Q3"]["unit"] == "USD",
       "값마다 source·locator(concept, 기간, 원래 값)·as_of·unit")
    ok(vals("interest_expense") == [(90, "derived"), (92, "reported"), (94, "derived"), (96, "reported"), (98, "derived"),
                                    (100, "reported"), (102, "reported"), (104, "reported")]
       and M["interest_expense"]["concept"] == "us-gaap:InterestExpenseNonoperating",
       "이자비용: 3개월 값이 없는 분기는 YTD 차이(6개월 186 − 1분기 92 = 94), 연간 값 하나뿐인 concept은 고르지 않음")
    ok([M["debt"]["values"][q]["value"] for q in qs] == [5400, None, 5630, 5470, 5710, 5850, 5840, 5950]
       and M["debt"]["values"]["2025Q1"]["status"] == "missing" and "NotesPayable" in M["debt"]["concept"]
       and "LineOfCredit" in M["debt"]["concept"] and M["debt"]["values"]["2026Q3"]["status"] == "derived",
       "부채: 합계 concept이 없으면 최근 분기에 있는 구성 concept(notes, secured, line of credit)의 합, 하나라도 없는 분기는 없음")
    ok([M["diluted_shares"]["values"][q]["value"] for q in qs] == [None, 100, 101, 102, None, 104, 105, 106]
       and "가중평균" in M["diluted_shares"]["values"]["2025Q4"]["note"],
       "희석 주식 수: 가중평균이라 4분기(연간 − 9개월)를 풀지 않음")
    ok("dividend_per_share" not in M and [x["metric"] for x in td["dropped"]] == ["dividend_per_share"]
       and td["dropped"][0]["count"] == 3, "값이 4분기보다 적은 지표(주당 배당 3분기)는 표에서 빼고 dropped에 알림")
    t4 = trend_doc(ct, "TEST", "2026Q3", 4)
    ok(t4["quarters"] == qs[4:] and sorted(x["metric"] for x in t4["dropped"]) == ["diluted_shares", "dividend_per_share"],
       "--quarters 4: 4분기 표, 희석 주식 수 3분기는 뺌")
    ok(latest_quarter(ct) == "2026Q3", "--period가 없으면 companyfacts의 가장 늦은 분기")
    net = {"facts": {"us-gaap": {"InterestIncomeExpenseNonoperatingNet": {"units": {"USD": [
        {"start": a, "end": b, "val": v, "form": "10-Q", "accn": "0000000001-26-000030", "filed": "2026-11-03"}
        for a, b, v in (("2025-10-01", "2025-12-31", -48), ("2026-01-01", "2026-03-31", -50),
                        ("2026-04-01", "2026-06-30", -51), ("2026-07-01", "2026-09-30", -52))]}}}}}
    tn = trend_doc(net, "TEST", "2026Q3", 4)["metrics"].get("interest_expense") or {}
    ok([v["value"] for v in (tn.get("values") or {}).values()] == [48, 50, 51, 52] and "부호" in tn.get("note", ""),
       "이자비용 concept이 순이자(InterestIncomeExpenseNonoperatingNet, 음수)뿐이면 부호를 바꿔 쓰고 note에 적음")

    # CLI(가짜 http_get): trend 파일, exhibits --images
    global http_get
    real_get, urls = http_get, []
    old_ua, old_dir = os.environ.get("SEC_USER_AGENT"), os.environ.get("INVEST_DATA_DIR")
    files = {"company_tickers.json": "company_tickers.json", "/submissions/": "submissions.json",
             "/companyfacts/": "companyfacts_trend.json", "-index.htm": "index.htm", "ex991.htm": "ex99_img.htm",
             "supp.htm": "ex99.htm"}

    def fake_get(url):
        urls.append(url)
        for key, name in files.items():
            if key in url:
                with open(os.path.join(fx, name), "rb") as f:
                    return f.read()
        if url.endswith((".jpg", ".png", ".gif")):
            return b"IMG " + url.rsplit("/", 1)[-1].encode()
        raise FetchError("HTTP 404 (%s)" % url)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["INVEST_DATA_DIR"] = tmp
            os.environ["SEC_USER_AGENT"] = "Test User test@example.com"   # 가상 연락처
            http_get = fake_get
            out = _io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(_io.StringIO()) as err:
                rc = main(["trend", "TEST"])
            tp = os.path.join(tmp, "tickers", "TEST", "trend", "2026Q3.json")
            ok(rc == 2 and os.path.exists(tp) and read_json(tp)["metrics"]["revenue"]["values"]["2025Q4"]["value"] == 1150
               and json.loads(out.getvalue())["period"] == "2026Q3" and "dividend_per_share" in err.getvalue(),
               "trend CLI: tickers/<T>/trend/<P>.json과 stdout, 뺀 지표가 있으면 exit 2와 stderr 알림")
            adir = os.path.join(tmp, "tickers", "TEST", "filings", "0000000001-26-000008")
            with contextlib.redirect_stdout(_io.StringIO()), contextlib.redirect_stderr(_io.StringIO()):
                r0 = main(["exhibits", "TEST", "--accession", "0000000001-26-000008"])
            ok(r0 == 0 and not os.path.exists(os.path.join(adir, "g1.jpg")), "exhibits: 기본은 그림을 받지 않음")
            out = _io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(_io.StringIO()):
                r1 = main(["exhibits", "TEST", "--accession", "0000000001-26-000008", "--images"])
            ex = json.loads(out.getvalue())["exhibits"]
            ok(r1 == 0 and [i["name"] for i in ex[0]["images"]] == ["g1.jpg", "logo.jpg"] and ex[1]["images"] == []
               and open(os.path.join(adir, "g1.jpg"), "rb").read() == b"IMG g1.jpg"
               and not any("example.com" in u or "x.gif" in u or "chart.svg" in u for u in urls),
               "exhibits --images: EX-99가 참조하는 jpg·png·gif를 같은 폴더에 받음(다른 host·상위 경로·다른 형식은 받지 않음, 겹치면 한 번)")
    finally:
        http_get = real_get
        for k, v in (("SEC_USER_AGENT", old_ua), ("INVEST_DATA_DIR", old_dir)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

---
name: reit-stock-review
description: 미국 REIT 종목을 공시로 분석한다. deep mode는 최신 실적 8-K Ex.99(보도자료·supplemental)와 XBRL을 SEC EDGAR에서 받고, agent가 표의 값을 위치(locator)와 함께 facts에 적으면, script가 Nareit FFO 재계산(회사 값과 0.5% 넘게 다르면 실패), AFFO bridge, forward yield, AFFO·FFO payout, 배당 CAGR, net debt/EBITDAre, coverage, 만기 집중도, 신용 지표(gross assets 대비 부채, fixed-charge coverage, covenant headroom), 리파이낸싱·유동성, 임차인 집중도, P/FFO·P/AFFO, implied cap rate(명목, economic, 부채 시가 반영)와 Tracker·회사 매입·매각 cap rate 비교 줄, consensus FFO 3개월 변화를 계산해 보고서를 쓴다. compare mode는 같은 섹터 여러 종목을 한 표에 놓고 subsector가 다르면 경고하며, earnings mode는 새 분기 metrics를 직전 분기와 diff해 나빠진 status·flag를 보고한다. REIT가 아닌 배당주(SIC 6798이 아님)는 EPS·FCF payout, 배당 CAGR, Chowder rule, yield trap 신호로 본다. API key 없이 돈다(주가는 Yahoo chart 종가, 실패하면 출처를 단 웹 채우기). "O 분석해줘", "이 리츠 배당 괜찮아?", "PLD, REXR, EGP 비교해줘", "O 실적 나왔는데 봐줘", "JNJ 배당 괜찮아?" 요청에 쓴다. 매크로 지표는 macro-check, 섹터 valuation과 종목 screening 표는 reit-sector-scan이 맡고 이 skill은 그 snapshot을 읽기만 한다. 매수·매도 판단은 내리지 않는다.
---

# 종목 공시 분석 (deep·compare·earnings)

종목 하나의 최신 분기 공시를 읽어 facts를 만들고, script가 지표를 계산하고, agent가 보고서를 쓴다. 이것이 deep mode이고, compare mode(여러 종목)와 earnings mode(두 분기)는 deep mode의 1~9단계로 만든 metrics 파일을 script로 견준다. 숫자는 script만 계산한다. agent는 공시에서 값을 읽어 위치와 함께 적고 해석한다. 같은 입력이면 같은 숫자가 나와야 하고, agent가 손으로 셈한 숫자는 틀려도 가릴 방법이 없기 때문이다.

`<skill 폴더>`는 이 SKILL.md가 있는 폴더다. 데이터 폴더는 `$INVEST_DATA_DIR`(없으면 `~/invest-data`)이고 레포 밖에 있다. 아래에서 `T`는 ticker, `P`는 달력 분기(`2026Q3`)다.

## 준비

- API key 없이 돈다. resolve·filings·xbrl·dividends(data.sec.gov, efts.sec.gov)는 설정 없이 기본 User-Agent `invest-skills/1.0`으로 받는다.
- 공시 원문(exhibits, www.sec.gov)은 SEC가 요구하는 연락처(이름과 이메일)가 든 User-Agent가 있으면 script가 직접 받는다. `SEC_USER_AGENT` 환경 변수, 없으면 `$INVEST_DATA_DIR/config.json`의 `sec_user_agent`를 쓴다. 둘 다 영문·숫자·기호(ASCII)로만 쓴다(HTTP header에 한글을 넣을 수 없다). 연락처가 없어도 멈추지 않는다. 3단계의 web fetch 경로로 같은 원문을 읽는다. `edgar.py config --show`로 지금 값(이메일 일부 가림)을 본다.
- script는 SEC에 초당 10회 아래로 요청한다. 받은 파일은 `$INVEST_DATA_DIR/tickers/<T>/filings/`에 둔다.

## 차례

1. **종목 확인**: `python3 <skill 폴더>/scripts/edgar.py resolve T`. `is_reit`가 false(SIC가 6798이 아님)면 아래 "일반 배당주 경로"를 탄다. FFO가 REIT에만 맞는 지표이기 때문이다.
2. **실적 공시 찾기**: `edgar.py filings T`. `latest_earnings_8k`(item 2.02가 든 8-K)의 accession과 `report_date`로 분기 `P`를 정한다. 사용자가 분기를 말했으면 그 분기의 실적 8-K를 고른다. `P`는 달력 분기 이름이다. 52·53주 회계연도 회사(분기 끝날이 9월 27일 같은 날)도 가장 가까운 달력 분기로 부른다.
3. **exhibit 받기**: `edgar.py exhibits T --accession A`. EX-99 문서마다 `.txt`가 생긴다. 표는 cell이 tab, row가 줄이다. 보도자료(EX-99.1)와 supplemental(EX-99.2)이 따로 있으면 둘 다 읽는다. supplemental이 8-K에 없으면 회사 IR의 supplemental PDF를 웹에서 찾아 `pdftotext -layout`으로 읽고 `source`를 `web:<URL>`로 쓴다.
   - 연락처가 없으면 `exhibits`가 www.sec.gov에 요청하지 않고 exit 2와 `fetch_by_agent: true`, `index_url`, `save_dir`을 낸다. 사용자에게 연락처를 묻지 않는다. agent의 web fetch 도구로 `index_url`을 열어 EX-99 문서 URL을 찾고, 문서마다 열어 표 숫자를 원문 그대로(표는 cell을 tab, row를 줄로) `save_dir/<문서 이름>.txt`에 쓴 뒤 그 줄 번호를 locator로 쓴다. `source`는 `sec:<문서 URL>`이다. web fetch는 페이지를 요약해 돌려줄 수 있으니 큰 표는 나눠 읽고, 숫자는 "그대로 옮겨 달라"고 요청한다. FFO tie-out(0.5%)이 옮긴 숫자를 검산한다.
   - 이 경로는 script가 직접 받을 때보다 느리고 토큰을 더 쓰고 숫자를 옮기다 틀릴 수 있다. 보고서 끝 "데이터 구멍"에 "SEC 연락처를 `edgar.py config --user-agent \"이름 이메일\"`로 저장하면 script가 원문을 직접 받아 더 정확하고 싸다(선택)"를 한 줄 적는다. 사용자가 연락처를 주면 그 명령으로 저장한다. 값은 `$INVEST_DATA_DIR/config.json`에만 두고 레포나 보고서에 적지 않는다.
   - 연락처가 있는데 HTTP 403이면 요청이 너무 잦은지 보고, 같은 web fetch 경로로 넘어간다.
4. **facts 초안**: `edgar.py xbrl T --period P`가 `$INVEST_DATA_DIR/tickers/<T>/facts/<P>.json`을 만들고 XBRL에 있는 값을 채운다. 이미 있는 파일이면 빈 칸만 채우고, 그 파일을 만든 뒤에 더한 칸(신용·리파이낸싱 칸 등)은 빈 칸으로 더한다. exit 2는 아직 빈 칸이 있다는 뜻이다. `filing`은 그 분기 끝날 뒤 120일 안의 첫 실적 8-K다. SEC companyfacts에 그 분기 10-Q가 아직 없으면 초안이 0칸일 수 있다. 그러면 모든 칸을 Ex.99에서 채운다.
5. **값 읽어 적기**: facts 파일을 열어 빈 칸을 `.txt`에서 읽은 값으로 채운다. 형식과 칸의 뜻은 `<skill 폴더>/references/facts-schema.md`에 있다.
   - 값마다 `source`와 `locator`(text 파일 이름, 줄 번호, 표 이름, 원문 숫자)를 쓴다. 없으면 script가 그 값을 버린다.
   - 회사가 원래 내지 않는 값(표에 그 칸이 없음)은 `not_disclosed: true`와 그 표의 `source`·`locator`(그 칸이 없는 표의 위치)를 적는다. script는 그 값으로 셈하는 지표를 missing으로 두되 exit 2로 세지 않고 데이터 구멍에 "공시 없음"으로 올린다. 못 찾은 값(missing)과 내지 않는 값을 가르기 위해서다. 근거가 없으면 보통 missing으로 센다.
   - 돈 칸이 회사 표의 consolidated 열인지 pro-rata(지분 몫) 열인지 알면 `basis`(`consolidated`·`pro_rata`)를 적는다. 한 식의 입력끼리 basis가 다르면 script가 그 지표를 estimated로 내리고 note에 섞인 칸을 쓴다.
   - 분기 값은 three months 열에서 읽는다. 단위(`USD thousands` 등)는 표 머리에 적힌 대로 쓴다.
   - XBRL로 채운 값은 Ex.99 표의 같은 줄과 맞춰 보고, 맞으면 `note`에 그 줄을 적는다. 다르면 Ex.99 값을 쓰고 이유를 `note`에 적는다.
   - FFO 표의 줄은 하나도 빼지 않는다. 회사 표에 그 줄이 없으면 0과 "회사 표에 이 줄이 없음", 못 찾았으면 missing과 이유다.
   - FFO 표가 보통주 귀속이 아닌 연결 순이익에서 시작하면(NCI·우선주를 FFO 아래에서 뺌) `net_income_common`에 그 시작 값을, `ffo_reported`에 같은 기준의 FFO를 적고 `note`에 기준을 쓴다. 기준을 맞추려고 agent가 더하거나 빼지 않는다. 숫자는 script만 계산하기 때문이다.
   - 회사 net debt 표가 이미 현금을 뺀 값만 내면 `total_debt`에 그 값, `cash`에 0과 "이미 빠짐"을 적는다. 회사가 연환산 EBITDAre를 따로 내지 않으면 `ebitdare_annualized`를 missing으로 두면 script가 분기×4(estimated)로 가정한다.
   - 만기 표가 날짜별 줄이면 줄마다 `period`(연도)를 붙여 옮긴다. script가 해마다 더한다. `period`가 연도(YYYY)나 `thereafter`가 아닌 줄이 있으면 만기 사다리·24개월 만기를 내지 않는다.
   - AFFO reconciliation은 `affo_bridge`에 줄마다 옮긴다. 회사 정의를 그대로 따른다. 줄마다 `category`(straight_line, recurring_capex, leasing_cost, non_cash, other)를 단다(`references/facts-schema.md`). recurring_capex·leasing_cost 줄은 AFFO에서 빼는 줄이라 0 이하다. 양수이거나 다섯 가지가 아닌 category가 하나라도 있으면 script가 category 지표와 capex reserve 대리값을 내지 않는다.
   - 주당 AFFO를 내지 않는 회사는 AFFO 총액과 같은 기준의 분기 가중평균 희석 주식 수를 `diluted_shares_weighted`에 적는다. 같은 기준이 없으면 가장 가까운 값을 `estimated`로 두고 기준 차이를 note에 쓴다. 주식 수를 더하거나 빼서 맞추지 않는다. 기말 보통주와 10배 넘게 다르면 script가 단위 실수로 보고 쓰지 않는다.
   - 만기 사다리(`series.debt_maturities`), 상위 임차인(`series.top_tenants`), 섹터 지표(`sector_kpis`, `references/sector-kpis.md`)도 옮긴다.
   - valuation 칸: 건설 중 자산(CIP), 개발용 토지, NOI에 수익이 들어 있지 않은 그 밖의 자산, `total_debt`에 들어 있지 않은 미연결 JV 부채의 지분 몫, 회사가 낸 capex reserve(없으면 missing으로 두면 script가 AFFO bridge 줄로 대신한다), 그 분기 매입·매각 cap rate(회사 정의를 note에). 모두 `noi_annualized`와 같은 기준(pro-rata·consolidated)을 고른다. 기준이 다른 줄밖에 없으면 그 차이를 note에 쓴다. 비운영 자산과 NOI가 같은 기준이어야 economic cap rate가 운영 부동산의 cap rate가 되기 때문이다.
   - 부채 fair value와 carrying value는 10-Q·10-K의 fair value 주석 표에서 읽는다. XBRL 초안이 채웠으면 그 표와 맞춰 본다. XBRL에 없으면(companyfacts에 그 분기 10-Q가 아직 없거나 회사가 총액 concept을 달지 않음) `edgar.py filings T --forms 10-Q`로 그 분기 10-Q의 accession을 찾고 `edgar.py exhibits T --accession A --type 10-Q`로 본문을 `.txt`로 받아 줄 번호를 locator로 쓴다.
   - 회사 표에 합계 줄이 없어 여러 줄을 더해야 하는 값(JV마다 부채, fair value 표의 부채 종류마다)은 `value` 대신 `parts`에 줄마다 적는다(`references/facts-schema.md`). agent가 더하지 않는다.
   - 리파이낸싱·유동성 칸: debt summary의 WAM·가중평균 금리·변동금리 원금(swap 반영), liquidity 표의 현금·revolver 잔여(commercial paper를 이미 뺐는지 note에), 개발 표의 남은 개발비. 만기 사다리와 같은 기준(consolidated·pro-rata)을 고른다.
   - 신용 칸: balance sheet의 총자산·감가상각 누계·담보부 부채, 회사 fixed charge 표나 손익계산서·개발 표 주석의 자본화 이자·우선주 배당·정기 원금 상환, unencumbered 자산(또는 NOI). 회사 debt covenant 표는 줄마다 `series.covenants`에 한도·실제·방향과 함께 옮긴다(`references/facts-schema.md`). covenant 표가 없으면 빈 목록으로 둔다. 손익계산서에 우선주 배당 줄이 없으면 0과 "회사 표에 이 줄이 없음", 정기 원금 상환처럼 회사가 내지 않는 값은 `not_disclosed`와 그 표의 위치다.
6. **배당 history**: `edgar.py dividends T --facts <facts 파일>`. XBRL 10-K 연간 주당 배당을 넣는다. `frequency_hint`는 참고만 하고, `dividend_frequency`는 보도자료 문장(예: "monthly dividends")에서 적는다.
7. **가격**: `python3 <skill 폴더>/scripts/quote.py price T --period P`. key 없는 Yahoo chart endpoint에서 마지막으로 끝난 거래일의 종가를 넣는다(`source: yahoo:chart`, `locator`에 URL과 기준일, `status: reported`, 5일 넘으면 `stale`). 문서에 없는 endpoint라 HTTP 오류·형식 변화·빈 값이면 price가 missing으로 남고 exit 2다. 그러면 아래 "웹 채우기"로 넣는다.
8. **위 단계 snapshot 확인**: 아래 "읽는 데이터"대로 macro snapshot과 REITWatch parsed가 있는지 보고, 없거나 오래됐으면 갱신한다.
9. **계산**: `python3 <skill 폴더>/scripts/reit_calc.py metrics --facts <facts 파일>`. `$INVEST_DATA_DIR/tickers/<T>/metrics/<P>.json`을 쓰고 경로를 찍는다. stderr에 check, flag, 데이터 구멍 수가 나온다.
   - exit 0: 다 채웠다(회사가 내지 않는 값 때문에 빈 지표는 `summary.metrics_not_disclosed`에만 센다). exit 2: missing이 있거나 check가 실패했다(이유는 `data_gaps`). exit 1: facts 형식이 틀렸다.
   - `ffo_tie_out`이 실패하면 먼저 facts를 의심한다. 열(3개월/누적), 부호, 단위, 빠진 줄을 Ex.99와 다시 맞춘다. 그래도 다르면 회사 정의가 Nareit과 다른 것이니 그 차이를 보고서 "데이터 구멍"에 쓴다. 맞추려고 값을 고치지 않는다.
10. **보고서**: 아래 "보고서" 모양으로 `$INVEST_DATA_DIR/tickers/<T>/reports/<오늘 날짜>-deep.md`를 쓰고 사용자에게 요약을 말한다.

## 웹 채우기 (가격)

1. 1차 자료를 먼저 찾는다. 거래소(NYSE·Nasdaq) quote page나 회사 IR의 주가 page다.
2. 1차 자료가 JavaScript로만 그려져 읽을 수 없으면 일별 시세 표가 있는 시세 사이트를 쓰고 `--secondary`를 붙인다. 가능하면 다른 사이트 하나와 값이 같은지 본다.
3. 원문에서 값이 든 문장이나 표의 row를 그대로 옮긴다. 기준일은 그 가격의 거래일이다.

```bash
python3 <skill 폴더>/scripts/quote.py web O --period 2026Q3 --value 58.04 --as-of 2026-11-17 \
  --url https://... --quote "| Nov 17, 2026 | 58.00 | 58.52 | 57.72 | 58.04 | ..." --secondary
python3 <skill 폴더>/scripts/quote.py web O --period 2026Q3 --missing "시세 page를 읽지 못함"
```

- URL이나 원문 문장이 없으면 script가 거절한다. 출처 없는 숫자가 들어가지 않게 하려는 것이다.
- 1차 자료는 `reported`, `--secondary`는 `estimated`다. 기준일이 5일(`assets/thresholds.json`의 `price_max_age_days`)을 넘으면 1차 자료는 `stale`이 되고, 2차 자료는 더 나쁜 `estimated`를 지키고 나이를 `note`에 쓴다.
- `--value`가 0 이하·NaN·무한대이거나 `--as-of`가 오늘보다 늦으면 script가 거절한다(exit 1).
- 못 찾으면 `--missing "이유"`로 둔다(exit 2). 앞서 넣은 가격이 있으면 지우지 않는다. 기억이나 추측으로 가격을 넣지 않는다.

## 읽는 데이터 (다른 skill의 snapshot)

이 skill은 아래 파일을 읽기만 한다. 고치지 않는다.

| 데이터 | 경로 | 쓰는 곳 | 없을 때 |
| --- | --- | --- | --- |
| macro snapshot | `$INVEST_DATA_DIR/macro/snapshots/<날짜>.json`(가장 최근) | DGS10과 yield spread, 시장 차입금리 추정(DBAA가 있으면 DBAA, 없으면 DGS10 + BAA10Y. reit-sector-scan과 같은 차례) | macro-check가 설치돼 있으면 `python3 <skills 폴더>/macro-check/scripts/macro.py fetch`. 아니면 spread가 missing |
| REITWatch parsed | `$INVEST_DATA_DIR/reitwatch/parsed/<YYYY-MM>.json` | subsector, consensus FFO와 3개월 변화·FY2/FY1 성장, 자기 P/FFO history, peer median | reit-sector-scan이 설치돼 있으면 그 skill의 fetch를 돌린다. 아니면 그 칸이 missing |
| Tracker parsed | `$INVEST_DATA_DIR/tracker/parsed/<YYYYQn>.json`(가격 기준일 전에 끝난 분기 중 가장 늦은 것. 그 뒤 분기 파일은 그날 아직 없던 자료라 고르지 않는다) | subsector implied cap rate(비교 줄) | reit-sector-scan이 설치돼 있으면 `reitwatch.py tracker fetch --quarter latest`. 아니면 Tracker 비교 줄만 missing |

- macro snapshot이 가격 기준일보다 7일 넘게 앞서면 DGS10·BAA10Y가 stale로 표시된다. 오늘 snapshot이 없으면 갱신한다.
- REITWatch는 월간이라 최신 달이 지난달이면 그대로 쓴다. consensus를 회사 연간 FFO 대신 쓰면 가정이라 `estimated`다. consensus 3개월 변화와 FY2/FY1 성장은 REITWatch 값의 계산이라 `derived`다(reit-sector-scan과 같다, 호가 오래되면 `stale`).
- 최신 호의 가격 기준일이 facts 가격 기준일보다 75일(`assets/thresholds.json`의 `reitwatch_max_age_days`) 넘게 앞서면 그 호에서 온 값의 `note`와 데이터 구멍(`reitwatch`)에 나이가 붙는다. 그러면 reit-sector-scan의 `fetch --month latest`로 갱신하고 다시 계산한다.
- Tracker 분기 끝날이 가격 기준일보다 150일(`assets/thresholds.json`의 `tracker_max_age_days`) 넘게 앞서면 Tracker 값이 stale로 표시된다. 그러면 새 분기 Tracker를 받는다. REITWatch subsector → Tracker row 대응은 `assets/tracker_map.json`(reit-sector-scan의 표를 복사한 것)이다.
- 갱신이 실패해도(exit 1) 그 칸을 missing으로 두고 계속한다.

## 보고서

`$INVEST_DATA_DIR/tickers/<T>/reports/<날짜>-deep.md`. 숫자는 metrics JSON에서 그대로 옮긴다. 다시 계산하거나 반올림을 바꾸지 않는다(표에 줄일 때는 소수 둘째 자리까지 내림 없이 반올림만 한다). 값마다 status를 붙이고, `estimated`·`stale`은 문장에도 적는다.

```md
# <T> <회사 이름> <P> 깊게 보기 (<날짜>)

facts: <경로>, metrics: <경로>, 공시: <form> <accession> (<제출일>). 판단 재료일 뿐 매수·매도 권유가 아니다.

## 요약
<3~5줄. FFO tie-out 결과, flag, 눈에 띄는 변화와 데이터 구멍>

## FFO → AFFO bridge
<Nareit 재계산 표(입력 줄, 재계산, 회사 값, 차이 %), AFFO bridge(시작 값, 줄과 category, 합, 회사 AFFO), 주당 FFO·AFFO(총액 / 희석 주식 수로 냈으면 그렇다고), 연간 AFFO(guidance), recurring capex/NOI, straight-line rent/FFO>

## 배당 지속성
<forward dividend, forward yield, AFFO payout(forward, 분기), FFO payout(참고, flag 없음), 배당 CAGR 3·5년, 컷 이력, flag와 기준(경험칙)>

## 재무
<net debt, net debt/EBITDAre, interest coverage, 고정금리 비중, 만기 사다리 표(해마다 금액·비중), 가장 큰 만기 해, flag>

## 신용·자본구조
<gross assets(회사 GAV와 맞는지), debt(+우선주)/gross assets, secured debt/gross assets, unencumbered 비율(자산·NOI 중 무엇인지), fixed-charge coverage(S&P식, 회사 covenant의 coverage와 식이 다름)와 S&P 구간, covenant 표(줄마다 한도·실제·headroom), 최소 headroom과 flag. 신용평가사 등급이 공시에 있으면 출처와 함께 적는다>

## 리파이낸싱·유동성
<WAM, 가중평균 금리, 24개월 안 만기(셋째 해를 나눴는지), 시장 차입금리 추정(DBAA 또는 DGS10 + BAA10Y, 기준일), refi 이자 증가와 AFFO 대비, 변동금리 100bp 충격/AFFO, liquidity sources(현금·revolver 잔여·유보 현금흐름)와 uses(만기·남은 개발비), 24개월 liquidity coverage. 모두 가정이 든 estimated라고 문장에 적는다>

## 운영
<occupancy, WALE, 상위 1·5·10 임차인 비중, 투자등급 임차인 비중, same store 성장률(무슨 지표인지), 섹터 지표>

## valuation
<P/FFO, P/AFFO, AFFO yield, yield − 10년물 spread, REITWatch P/FFO의 자기 history percentile, peer median과 비율. FFO FY가 무엇(guidance·consensus·분기×4)인지 적는다. consensus FFO FY1 3개월 변화와 FY2/FY1 성장(REITWatch 두 호의 계산, derived)>

### cap rate 기준선
<한 표에 나란히: 명목 implied cap rate, economic implied cap rate, 부채 시가 반영 economic cap rate, Tracker subsector implied cap rate(분기, cell 위치), 명목 − Tracker(%p), 회사 매입 cap rate, 회사 매각 cap rate(회사 정의). 그 아래 economic EV 다리: 시총, net debt, 우선주, 미연결 JV 부채, 빼는 비운영 자산(CIP·토지·기타), 연환산 NOI, capex reserve(회사 값인지 AFFO bridge 대리인지), 부채 fair value·carrying value·조정. NOI와 각 칸의 기준(pro-rata·consolidated)이 다르면 적는다(metrics `basis`가 `mixed`인 줄은 estimated다). capex reserve가 분기×4 대리면 estimated라고 문장에 적는다>

## 다시 볼 조건
<다음에 이 종목을 다시 볼 사실 조건. 예: "AFFO payout이 85%를 넘으면", "2029년 만기 비중이 15%를 넘으면", "다음 실적 8-K가 나오면". 사라·팔아라가 아니라 다시 계산할 때를 적는다>

## 데이터 구멍
<metrics의 data_gaps 전부: missing·estimated·stale 항목과 이유, check 실패, 쓰지 않은 facts, 웹 채우기 출처와 status. 공시 없음(not_disclosed) 항목은 따로 묶어 "회사가 내지 않음"과 근거 위치를 쓴다>
```

## 해석할 때

- 사라·팔아라, 비중을 늘려라·줄여라, 목표가, 매매 시점을 쓰지 않는다. 결과는 판단 재료이고 결정은 사용자가 한다.
- flag 기준은 경험칙이다. "AFFO payout 73%는 기준(85%) 아래다"처럼 사실로 쓰고, "안전하다"고 단정하지 않는다.
- P/FFO·P/AFFO는 같은 섹터 안에서만 견준다. AFFO는 회사마다 정의가 달라 peer와 바로 견주지 않는다.
- 회사가 낸 non-GAAP 값(AFFO, EBITDAre, NOI)은 회사 정의라고 적는다.
- consensus·2차 자료 가격처럼 `estimated`인 값으로 한 해석에는 그 status를 문장에 붙인다.
- cap rate 기준선은 지금 가격이 운영 부동산에 매긴 수익률을 여러 잣대로 놓은 것이다. NAV/share, "적정 cap rate라면 주가는 얼마"라는 계산, 목표가를 쓰지 않는다. 그런 표는 사실상 적정 주가로 읽히기 때문이다. 차이는 metrics의 차 줄을 옮겨 "명목 implied cap rate는 Tracker subsector 값보다 0.10%p 낮다(가상)"처럼 사실로 쓰고, 싸다·비싸다로 단정하지 않는다.
- Tracker 값은 Nareit이 시총과 부채로 셈한 명목 값이라 명목 implied cap rate와만 차를 본다. 회사 매입·매각 cap rate는 회사 정의(cash yield, stabilized cap rate)가 달라 다른 회사 값과 바로 견주지 않는다.

## compare mode (같은 섹터 종목 비교)

"PLD, REXR, EGP 비교해줘"처럼 여러 종목을 견주는 요청이다.

1. 종목마다 최신 분기 metrics가 있는지 본다(`$INVEST_DATA_DIR/tickers/<T>/metrics/<P>.json`). 없거나 그 뒤 실적 8-K가 나왔으면 deep mode 1~9단계로 만든다. 종목마다 facts 채우기는 서로 독립이라 subagent가 있으면 종목마다 나눠 맡긴다(Claude Code는 Agent 도구, Codex는 subagent). 지시문에는 이 SKILL.md 경로, ticker, 분기만 준다. subagent가 없으면 종목마다 차례로 한다. subagent의 보고는 증명이 아니다. metrics를 받으면 `ffo_tie_out` check와 데이터 구멍을 직접 연다.
2. **표**: `python3 <skill 폴더>/scripts/reit_calc.py compare --metrics <M1> <M2> ... --out $INVEST_DATA_DIR/compare/<날짜>-<T1>-<T2>-....json`. 같은 지표를 종목마다 한 줄에 놓고 이 종목들끼리의 median을 낸다.
   - REITWatch subsector가 다수와 다른 종목은 `columns[].warning`에 경고가 붙고 stderr에 "경고:"가 나온다. REIT와 일반 배당주를 섞거나 분기가 다르면 `warnings`에 오른다. 경고가 있으면 보고서 요약 첫 줄에 쓴다. 다른 섹터의 P/FFO·cap rate는 같은 기준이 아니기 때문이다.
   - exit 0: 경고가 없고 모든 종목 metrics가 다 찼다. exit 2: 경고가 있거나 어떤 종목 metrics에 missing·check 실패가 있다(`summary.incomplete`. 공시 없음만 있는 종목은 들지 않는다). exit 1: 종목이 하나이거나 같은 종목이 두 번이거나 metrics 형식이 틀렸다.
3. **보고서**: 아래 모양으로 `$INVEST_DATA_DIR/compare/<날짜>-<T1>-<T2>-....md`를 쓴다.

```md
# <T1>·<T2>·... <P> 비교 (<subsector>, <날짜>)

compare: <JSON 경로> (exit), metrics: <경로들>, 공시: <종목마다 form accession (제출일)>. 판단 재료일 뿐 매수·매도 권유가 아니다.

## 요약
<경고, FFO tie-out 결과, flag 차이, 가격 status, 눈에 띄는 차이 3~5줄>

## FFO tie-out
<종목마다 재계산, 회사 값, 차이, 회사 표의 기준(보통주 귀속인지, NCI 차감 전인지). AFFO bridge 결과>

## 비교 표
<지표 줄 × 종목 칸 + median. P/FFO(분모가 무엇인지), REITWatch P/FFO·history·peer, yield·spread, 배당 CAGR·컷, leverage·coverage·고정금리·만기 최대, 리파이낸싱(refi 영향/AFFO, 변동금리 충격/AFFO, liquidity coverage), 신용(debt+우선주/gross assets, secured/gross assets, unencumbered 비율, fixed-charge coverage, 최소 covenant headroom), occupancy·same store·임차인 집중도, implied cap rate(명목·economic·부채 시가 반영)·Tracker subsector cap rate·회사 매입·매각 cap rate, consensus FFO 3개월 변화·FY2/FY1 성장, AFFO payout(주당 값이 총액에서 나왔는지)·FFO payout(참고), recurring capex/NOI, straight-line rent/FFO. flag 표. 섹터 지표는 회사마다 정의가 달라 종목별 목록으로. covenant 줄은 회사마다 정의가 달라 최소 headroom만 한 줄에 놓는다>

## 읽을 때
<분모·기준이 다른 지표, 같은 출처끼리 견줄 줄, 사실로 쓴 차이>

## 다시 볼 조건

## 데이터 구멍
<종목마다 metrics의 data_gaps, 웹 채우기 출처와 status, 기준을 맞추지 못한 칸>
```

## earnings mode (실적 뒤 점검)

"O 실적 나왔는데 봐줘"처럼 새 분기가 나온 뒤의 요청이다.

1. 새 분기 `P2`를 deep mode 1~9단계로 만든다. 직전 분기 `P1` metrics가 없으면 그 분기도 만든다(`edgar.py xbrl T --period P1`이 그 분기의 실적 8-K를 고른다). 직전 분기를 나중에 만들 때는 두 분기에 같은 가격(오늘 가격)을 넣고 보고서에 적는다. 그래야 가격이 든 지표의 변화가 배당·FFO·NOI 변화만 담는다.
2. **diff**: `python3 <skill 폴더>/scripts/reit_calc.py diff --prev <P1 metrics> --curr <P2 metrics> --out $INVEST_DATA_DIR/tickers/<T>/reports/<날짜>-earnings.json`. 지표마다 전후 값, 변화(`%` 지표는 %p, 나머지는 같은 단위와 상대 변화 %), status 변화, flag 변화, check 전후를 낸다. `worse.status`·`worse.flags`가 나빠진 것이다.
   - exit 0: 두 분기 값이 다 있다(공시 없음이나 "갚을 것 없음"처럼 값이 없는 것이 정상인 줄은 세지 않는다). exit 2: 한쪽이 missing인 지표가 있다. exit 1: ticker가 다르거나 prev가 curr보다 앞선 분기가 아니다.
3. **보고서**: `$INVEST_DATA_DIR/tickers/<T>/reports/<날짜>-earnings.md`.

```md
# <T> <회사 이름> <P2> 실적 뒤 점검 (<P1> → <P2>, <날짜>)

diff: <JSON 경로> (exit), metrics: <두 경로>, 공시: <두 8-K>. 판단 재료일 뿐 매수·매도 권유가 아니다.

## 요약
<바뀐 지표 수, 나빠진 status·flag(없으면 없다고), tie-out, 주당 FFO·AFFO·guidance·payout 변화>

## 바뀐 지표
<changed가 true인 줄: 지표, P1, P2, 변화, 비고. 만기 사다리는 해마다 비중>

## flag와 check
<flag 전후와 change, check 전후>

## 다시 볼 조건

## 데이터 구멍
<두 분기의 가격 출처, 한쪽이 missing인 지표, estimated 항목>
```

## 일반 배당주 경로 (SIC가 6798이 아님)

"JNJ 배당 괜찮아?"처럼 REIT가 아닌 종목이다. deep mode 차례를 따르되 칸과 지표가 다르다.

- `edgar.py xbrl`이 일반 배당주 칸으로 초안을 만든다: 분기 순이익·diluted EPS·분기 주당 배당, 회계연도 누적(YTD) 영업현금흐름·설비투자·보통주 배당 지급. 10-Q 현금흐름표는 누적 값만 내기 때문에 FCF payout도 누적끼리 본다.
- agent가 채울 칸: `eps_guidance_low/high`(연간 EPS guidance. adjusted면 `note`에 회사 정의라고 쓴다), `dividend_regular_latest`·`dividend_frequency`(최근 배당 공지 문장), `price`. 배당 공지가 8-K에 없으면 회사 IR 보도자료를 웹에서 찾아 `source: web:<URL>`, `locator`에 원문 문장을 쓴다.
- `reit_calc.py metrics`가 경로를 스스로 고른다(`path: dividend`). 지표: `eps_fy`(guidance 가운데 → 분기×4 estimated), forward dividend·yield, `eps_payout_forward`·`eps_payout_quarter`, `fcf_ytd`·`fcf_payout_ytd`, 배당 CAGR 3·5년·컷, `chowder_number`(yield + 5년 배당 CAGR)와 `chowder_threshold`(utility SIC 49xx 8, yield 3% 미만 15, 그 외 12), `p_e`, `yield_trap_signals`.
- flag: `eps_payout`(75% watch·100% alert), `fcf_payout`(80%·100%), `chowder`(기준 미만 watch), `yield_trap`(신호 1개 watch, 2개 이상 alert). 기준은 `assets/thresholds.json`의 경험칙이다. 분기 GAAP EPS가 일회성 비용으로 작아 분기 payout이 100%를 넘으면 flag note에 남는다. 보고서에 그 비용을 적는다.
- 보고서는 deep 보고서 모양에서 "FFO → AFFO bridge"를 "배당과 payout", "재무·운영"을 "Chowder rule"과 "yield trap 신호"로 바꿔 `tickers/<T>/reports/<날짜>-deep.md`에 쓴다.

## 고친 뒤 확인

script나 `assets/thresholds.json`을 고치면 돌린다. network 없이 `tests/fixtures`로 돌고, 기대값은 `tests/expected.json`에 회사 발표값이나 손으로 셈한 값이다. diff·compare는 `tests/fixtures/metrics/`의 가상 metrics, 일반 배당주는 `tests/fixtures/facts_div_2026q3.json`(가상)으로 확인한다.

```bash
python3 <skill 폴더>/scripts/reit_calc.py --check   # exit 0이면 통과
python3 <skill 폴더>/scripts/edgar.py --check
python3 <skill 폴더>/scripts/quote.py --check
```

지표를 더하거나 식을 바꾸면 `references/metrics.md`도 같이 고친다. 기준값은 `assets/thresholds.json`에만 둔다.

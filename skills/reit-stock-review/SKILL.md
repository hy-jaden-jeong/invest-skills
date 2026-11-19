---
name: reit-stock-review
description: 미국 REIT 종목을 공시로 분석해 읽는 보고서와 장표를 낸다. 종목 하나를 물으면 단일 보고서다. 최신 실적 8-K Ex.99와 XBRL을 SEC EDGAR에서 받아 agent가 값을 위치와 함께 facts에 적으면, script가 Nareit FFO 재계산, AFFO bridge, payout, 배당 이력(split·특별배당 가름), 부채·만기·신용·covenant, 리파이낸싱, 임차인 집중, P/FFO·implied cap rate를 계산해 축 5개에 등급을 매기고, 같은 subsector 경쟁 종목 2개(재사용 또는 가볍게 보기), 8분기 추세, 공시·웹 사건(신용등급, 임차인 신용, 배당, guidance)을 곁들인다. 종목 2~6개를 지정하면 비교 보고서(2개는 맞대기, 3~6개는 등급 matrix), 새 분기가 나오면 실적 뒤 점검을 낸다. "PDF로", "레포트 발간해줘"면 A4 PDF를 굽는다. REIT가 아닌 배당주는 EPS·FCF payout, Chowder rule, yield trap 신호로 본다. API key 없이 돈다. "O 분석해줘", "이 리츠 배당 괜찮아?", "PLD, REXR, EGP 비교해줘", "O 실적 나왔는데 봐줘", "VICI 레포트 PDF로 발간해줘", "JNJ 배당 괜찮아?" 요청에 쓴다. 매크로 지표는 macro-check, 섹터 valuation과 7개 이상 종목 screening은 reit-sector-scan이 맡고 이 skill은 그 snapshot을 읽기만 한다. 매수·매도 판단과 목표가는 내지 않는다.
---

# 종목 공시 분석 (단일·비교·실적 뒤 점검)

종목의 최신 분기 공시를 읽어 facts를 만들고, script가 지표와 등급을 계산하고, script(`report.py`)가 보고서·부록·장표·PDF를 만든다. agent는 공시에서 값을 읽어 위치와 함께 적고, 풀이 글(`narrative.json`)만 쓴다. 같은 입력이면 같은 숫자가 나와야 하고, agent가 손으로 셈하거나 표를 옮긴 숫자는 틀려도 가릴 방법이 없기 때문이다.

`<skill 폴더>`는 이 SKILL.md가 있는 폴더다. 데이터 폴더는 `$INVEST_DATA_DIR`(없으면 `~/invest-data`)이고 레포 밖에 있다. 아래에서 `T`는 ticker, `P`는 달력 분기(`2026Q3`)다.

## 어느 보고서인가

사용자가 종목 이름을 몇 개 말했는지로 가른다. 질문이 다르기 때문이다. 단일은 "이 종목이 어떤 상태이고 동종 안에서 어디쯤인가", 비교는 "이 종목들이 어디서 갈리고 왜 갈리나"다.

| 요청 | 보고서 | 종목마다 깊이 | 차례 |
| --- | --- | --- | --- |
| 종목 하나("O 분석해줘", "O 경쟁사랑 같이 봐줘", "O랑 비슷한 거") | 단일 | 그 종목 deep, 경쟁 종목 2개는 `reit_calc.py peers`가 고른다(같은 분기 metrics가 있으면 재사용, 없으면 light). 사용자가 경쟁 종목을 말하면 그 종목, "경쟁사도 깊게"면 모두 deep | 아래 "단일 보고서" |
| 종목 2~6개("O, ADC 비교", "PLD, REXR, EGP 비교해줘") | 비교 | 지정한 종목 모두 deep(재사용 우선). 경쟁 종목을 더 고르지 않는다 | 아래 "비교 보고서" |
| 종목 7개 이상 | 만들지 않는다 | | reit-sector-scan의 screening 표로 본다. 한 표·한 장표에 들어가지 않는다 |
| 새 분기가 나온 종목("O 실적 나왔는데 봐줘") | 실적 뒤 점검 | 두 분기 모두 deep | 아래 "실적 뒤 점검" |
| REIT가 아닌 배당주(SIC 6798이 아님) | 일반 배당주 보고서 | deep 절차를 따르되 칸이 다르다 | 아래 "일반 배당주 경로" |

"PDF로", "레포트 발간해줘"가 붙으면 위 보고서를 만든 뒤 `report.py pdf`를 돌린다.

재사용: `$INVEST_DATA_DIR/tickers/<T>/metrics/<P>.json`이 있고 그 뒤에 새 실적 8-K가 없으면 다시 만들지 않는다. light로 만든 metrics는 비교 보고서에서 재사용하지 않고 deep으로 올린다(칸만 더 채우고 `depth`를 지운다). light와 deep을 한 matrix에 두면 light 쪽이 "판정 못 함"으로 가득해 비교가 되지 않는다.

## 준비

- API key 없이 돈다. resolve·filings·xbrl·trend(data.sec.gov, efts.sec.gov)는 설정 없이 기본 User-Agent `invest-skills/1.0`으로 받는다.
- 공시 원문(exhibits, www.sec.gov)은 SEC가 요구하는 연락처(이름과 이메일)가 든 User-Agent가 있으면 script가 직접 받는다. `SEC_USER_AGENT` 환경 변수, 없으면 `$INVEST_DATA_DIR/config.json`의 `sec_user_agent`를 쓴다. 둘 다 영문·숫자·기호(ASCII)로만 쓴다(HTTP header에 한글을 넣을 수 없다). 연락처가 없어도 멈추지 않는다. deep 절차 3단계의 web fetch 경로로 같은 원문을 읽는다. `edgar.py config --show`로 지금 값(이메일 일부 가림)을 본다.
- script는 SEC에 초당 10회 아래로 요청한다. 받은 파일은 `$INVEST_DATA_DIR/tickers/<T>/filings/`에 둔다.
- 장표 PNG는 diagram-slides skill이 있으면 굽는다(`report-format.md`의 "장표 굽기"). PDF는 Chrome이 있으면 굽는다.

## deep 절차 (종목 하나를 깊게 보기)

1. **종목 확인**: `python3 <skill 폴더>/scripts/edgar.py resolve T`. `is_reit`가 false(SIC가 6798이 아님)면 아래 "일반 배당주 경로"를 탄다. FFO가 REIT에만 맞는 지표이기 때문이다.
2. **실적 공시 찾기**: `edgar.py filings T`. `latest_earnings_8k`(item 2.02가 든 8-K)의 accession과 `report_date`로 분기 `P`를 정한다. 사용자가 분기를 말했으면 그 분기의 실적 8-K를 고른다. `P`는 달력 분기 이름이다. 52·53주 회계연도 회사(분기 끝날이 9월 27일 같은 날)도 가장 가까운 달력 분기로 부른다.
3. **exhibit 받기**: `edgar.py exhibits T --accession A`. EX-99 문서마다 `.txt`가 생긴다. 대개 표는 cell이 tab, row가 줄이지만, cell 하나가 한 줄로 풀린 문서도 있다(표가 세로로 늘어선다). 그러면 표 이름과 열 머리 줄을 먼저 찾아 몇째 줄이 어느 열인지 세고, locator에 줄 번호 범위(`L932-1056`)를 쓴다. 보도자료(EX-99.1)와 supplemental(EX-99.2)이 따로 있으면 둘 다 읽는다. supplemental 표가 그림으로만 들어 있으면 `--images`로 그림도 받아 연다. supplemental이 8-K에 없으면 회사 IR의 supplemental PDF를 웹에서 찾아 `pdftotext -layout`으로 읽고 `source`를 `web:<URL>`로 쓴다.
   - 연락처가 없으면 `exhibits`가 www.sec.gov에 요청하지 않고 exit 2와 `fetch_by_agent: true`, `index_url`, `save_dir`을 낸다. 사용자에게 연락처를 묻지 않는다. agent의 web fetch 도구로 `index_url`을 열어 EX-99 문서 URL을 찾고, 문서마다 열어 표 숫자를 원문 그대로(표는 cell을 tab, row를 줄로) `save_dir/<문서 이름>.txt`에 쓴 뒤 그 줄 번호를 locator로 쓴다. `source`는 `sec:<문서 URL>`이다. web fetch는 페이지를 요약해 돌려줄 수 있으니 큰 표는 나눠 읽고, 숫자는 "그대로 옮겨 달라"고 요청한다. FFO tie-out(0.5%)이 옮긴 숫자를 검산한다.
   - 이 경로는 script가 직접 받을 때보다 느리고 토큰을 더 쓰고 숫자를 옮기다 틀릴 수 있다. 사용자 요약 끝에 "SEC 연락처를 `edgar.py config --user-agent \"이름 이메일\"`로 저장하면 script가 원문을 직접 받아 더 정확하고 싸다(선택)"를 한 줄 적는다. 사용자가 연락처를 주면 그 명령으로 저장한다. 값은 `$INVEST_DATA_DIR/config.json`에만 두고 레포나 보고서에 적지 않는다.
   - 연락처가 있는데 HTTP 403이면 요청이 너무 잦은지 보고, 같은 web fetch 경로로 넘어간다.
4. **facts 초안**: `edgar.py xbrl T --period P`가 `$INVEST_DATA_DIR/tickers/<T>/facts/<P>.json`을 만들고 XBRL에 있는 값을 채운다. 이미 있는 파일이면 빈 칸만 채우고, 그 파일을 만든 뒤에 더한 칸(신용·리파이낸싱 칸 등)은 빈 칸으로 더한다. exit 2는 아직 빈 칸이 있다는 뜻이다. `filing`은 그 분기 끝날 뒤 120일 안의 첫 실적 8-K다. SEC companyfacts에 그 분기 10-Q가 아직 없으면 초안이 0칸일 수 있다. 그러면 모든 칸을 Ex.99에서 채운다.
5. **값 읽어 적기**: facts 파일을 열어 빈 칸을 `.txt`에서 읽은 값으로 채운다. 형식과 칸의 뜻은 `<skill 폴더>/references/facts-schema.md`에 있다.
   - 값마다 `source`와 `locator`(text 파일 이름, 줄 번호, 표 이름, 원문 숫자)를 쓴다. 없으면 script가 그 값을 버린다.
   - 회사가 원래 내지 않는 값(표에 그 칸이 없음)은 `not_disclosed: true`와 그 표의 `source`·`locator`(그 칸이 없는 표의 위치)를 적는다. script는 그 값으로 셈하는 지표를 missing으로 두되 exit 2로 세지 않고 데이터 구멍에 "공시 없음"으로 올린다. 못 찾은 값(missing)과 내지 않는 값을 가르기 위해서다. 근거가 없으면 보통 missing으로 센다.
   - covenant 한도·실제 표나 임차인 비중 표를 통째로(또는 일부 줄을) 내지 않는 회사는 칸 하나하나가 아니라 `series_not_disclosed.covenants`·`.top_tenants`에 근거 위치를 적는다(예: "in compliance with all covenants" 문장만 있는 줄, 임차인을 5곳까지만 낸 표). 그 축은 "판정 못 함"이 되고 exit 2로 세지 않는다.
   - 돈 칸이 회사 표의 consolidated 열인지 pro-rata(지분 몫) 열인지 알면 `basis`(`consolidated`·`pro_rata`)를 적는다. 한 식의 입력끼리 basis가 다르면 script가 그 지표를 estimated로 내리고 note에 섞인 칸을 쓴다.
   - 분기 값은 three months 열에서 읽는다. 단위(`USD thousands` 등)는 표 머리에 적힌 대로 쓴다.
   - XBRL로 채운 값은 Ex.99 표의 같은 줄과 맞춰 보고, 맞으면 `note`에 그 줄을 적는다. 다르면 Ex.99 값을 쓰고 이유를 `note`에 적는다. 특히 D&A(총 D&A와 부동산 D&A), 감가상각 누계(lease intangible 상각), 희석 주식 수(OP unit 포함 여부), `net_income_common`, 부채 장부가(발행비용 차감)는 XBRL 기준이 회사 표와 자주 다르다.
   - FFO 표의 줄은 하나도 빼지 않는다. 회사 표에 그 줄이 없으면 0과 "회사 표에 이 줄이 없음", 못 찾았으면 missing과 이유다.
   - FFO 표가 보통주 귀속이 아닌 연결 순이익에서 시작하면(NCI·우선주를 FFO 아래에서 뺌) `net_income_common`에 그 시작 값을, `ffo_reported`에 같은 기준의 FFO를 적고 `note`에 기준을 쓴다. 기준을 맞추려고 agent가 더하거나 빼지 않는다. 숫자는 script만 계산하기 때문이다.
   - 회사 net debt 표가 이미 현금을 뺀 값만 내면 `total_debt`에 그 값, `cash`에 0과 "이미 빠짐"을 적는다. 회사가 연환산 EBITDAre를 따로 내지 않으면 `ebitdare_annualized`를 missing으로 두면 script가 분기×4(estimated)로 가정한다.
   - 만기 표가 날짜별 줄이면 줄마다 `period`(연도)를 붙여 옮긴다. script가 해마다 더한다. `period`가 연도(YYYY)나 `thereafter`가 아닌 줄이 있으면 만기 사다리·24개월 만기를 내지 않는다.
   - AFFO reconciliation은 `affo_bridge`에 줄마다 옮긴다. 회사 정의를 그대로 따른다. 줄마다 `category`(straight_line, recurring_capex, leasing_cost, non_cash, other)를 단다(`references/facts-schema.md`). recurring_capex·leasing_cost 줄은 AFFO에서 빼는 줄이라 0 이하다. 양수이거나 다섯 가지가 아닌 category가 하나라도 있으면 script가 category 지표와 capex reserve 대리값을 내지 않는다.
   - **CAD·FAD·AFAD**: 회사가 AFFO 대신 CAD(cash available for distribution), FAD(funds available for distribution), AFAD 같은 이름으로 배당 재원을 내면 그 값을 `affo_reported`(주당 값이 있으면 `affo_per_share_diluted`)에 적고, 그 표를 `affo_bridge`에 옮긴다. `note`에 회사가 쓴 이름, 시작 값(FFO·Core FFO·Normalized FFO), AFFO와 다른 차감 줄(정기 원금 상환까지 빼는지 등)을 쓴다. 보고서에는 AFFO 자리에 나오므로 이름이 달라도 배당 여력 축이 매겨진다. 정의가 회사마다 다르니 다른 회사 AFFO와 바로 견주지 않는다. 어느 이름으로도 내지 않으면 `affo_reported`를 `not_disclosed`로 두고 straight-line rent만 따로 내면 `straight_line_rent`에 적는다(script가 FFO payout을 한 단계 보수적으로 쓴다).
   - 주당 AFFO를 내지 않는 회사는 AFFO 총액과 같은 기준의 분기 가중평균 희석 주식 수를 `diluted_shares_weighted`에 적는다. 같은 기준이 없으면 가장 가까운 값을 `estimated`로 두고 기준 차이를 note에 쓴다. 주식 수를 더하거나 빼서 맞추지 않는다. 기말 보통주와 10배 넘게 다르면 script가 단위 실수로 보고 쓰지 않는다.
   - 만기 사다리(`series.debt_maturities`), 상위 임차인(`series.top_tenants`, 비중 기준이 ABR이 아니면 항목마다 `measure`), 섹터 지표(`sector_kpis`, `references/sector-kpis.md`)도 옮긴다.
   - **NOI**: 회사가 연환산 NOI를 내면 `noi_annualized`, 분기 NOI만 내면 `noi_quarter`에 적는다(script가 분기×4를 추정으로 쓴다. agent가 곱하지 않는다). NOI를 어느 쪽으로도 내지 않는 회사(triple-net, gaming·healthcare lease, lender 성격)는 연환산 계약 임대료(ABR: 기말 월 현금 base rent × 12, 또는 회사가 낸 annualized contractual rent·interest)를 `noi_annualized`에 `status: estimated`로 대리로 적는다. `note`에 "NOI 대리값"과 그 값에 빠진 것(회사가 부담하는 부동산 비용, percentage rent, 대출 이자를 넣었는지, cash-basis·미납 임차인이 계약 기준으로 들어 있는지)을 쓴다. 대리값으로 낸 cap rate는 추정으로 읽힌다.
   - valuation 칸: 건설 중 자산(CIP), 개발용 토지, NOI에 수익이 들어 있지 않은 그 밖의 자산, `total_debt`에 들어 있지 않은 미연결 JV 부채의 지분 몫, 회사가 낸 capex reserve(없으면 missing으로 두면 script가 AFFO bridge 줄로 대신한다), 그 분기 매입·매각 cap rate(회사 정의를 note에, 그 분기 거래가 없으면 `none_in_period`). 모두 `noi_annualized`와 같은 기준(pro-rata·consolidated)을 고른다. 기준이 다른 줄밖에 없으면 그 차이를 note에 쓴다. 비운영 자산과 NOI가 같은 기준이어야 economic cap rate가 운영 부동산의 cap rate가 되기 때문이다.
   - 부채 fair value와 carrying value는 10-Q·10-K의 fair value 주석 표에서 읽는다. XBRL 초안이 채웠으면 그 표와 맞춰 본다. XBRL에 없으면(companyfacts에 그 분기 10-Q가 아직 없거나 회사가 총액 concept을 달지 않음) `edgar.py filings T --forms 10-Q`로 그 분기 10-Q의 accession을 찾고 `edgar.py exhibits T --accession A --type 10-Q`로 본문을 `.txt`로 받아 줄 번호를 locator로 쓴다.
   - 회사 표에 합계 줄이 없어 여러 줄을 더해야 하는 값(JV마다 부채, fair value 표의 부채 종류마다)은 `value` 대신 `parts`에 줄마다 적는다(`references/facts-schema.md`). agent가 더하지 않는다.
   - 리파이낸싱·유동성 칸: debt summary의 WAM·가중평균 금리·변동금리 원금(swap 반영), liquidity 표의 현금·revolver 잔여(commercial paper를 이미 뺐는지 note에), 개발 표의 남은 개발비, lender 성격 REIT의 대출 funding commitment(`other_committed_uses`). 만기 사다리와 같은 기준(consolidated·pro-rata)을 고른다.
   - 차환 금리 재료: 최근 12개월 안에 회사가 채권을 발행했으면 그 쿠폰을 `recent_issuance_rate_pct`에 발행일(`issue_date`)과 함께 적는다(담보부면 note에). 신용등급은 `series.credit_ratings`에 기관마다 적는다(공시에 없으면 신용평가사 발표나 기사 URL과 원문 문장, 기사면 `estimated`). script는 최근 발행 금리 > 등급별 시장 금리 > Baa(추정) 차례로 차환 금리를 고른다. 투자등급 아래 회사가 Baa로 셈하면 차환 비용이 줄어드는 것처럼 나오기 때문이다.
   - 신용 칸: balance sheet의 총자산·감가상각 누계·담보부 부채, 회사 fixed charge 표나 손익계산서·개발 표 주석의 자본화 이자·우선주 배당·정기 원금 상환, unencumbered 자산(또는 NOI). 회사 debt covenant 표는 줄마다 `series.covenants`에 한도·실제·방향과 함께 옮긴다(`references/facts-schema.md`). 손익계산서에 우선주 배당 줄이 없으면 0과 "회사 표에 이 줄이 없음"이다.
   - **정기 원금 상환**: 회사가 분기 정기 원금 상환(scheduled amortization)을 내면 그 값을 적는다. 부채가 모두 만기 일시 상환(bullet: 무담보 채권, term loan, revolver)이면 `principal_amortization`에 `0`을 적고 근거 위치(만기 해 말고는 상환이 없는 만기표·부채 표의 줄)를 locator에 쓴다. 공시 없음이 아니다. 그래야 fixed-charge coverage가 나온다. 상환 일정이 있는데 분기 값을 내지 않으면 그때만 `not_disclosed`와 그 표의 위치다.
6. **사건(events)**: 숫자로 잡히지 않는 사건을 facts 맨 위 `events`에 적는다(`references/facts-schema.md`의 "events"). 직전 실적 발표 뒤부터 오늘까지를 본다.
   - 공시 본문: 그 분기 10-Q·실적 8-K의 임차인 신용 사건(미납, 파산, going concern, rent deferral), 소송, 결산 후 사건(subsequent events), guidance 변화.
   - 웹 네 가지: 신용등급(신용평가사 발표, 등급·전망 변경), 임차인 신용 사건(주요 임차인의 파산·등급 하락·going concern), 배당 발표(회사 IR 보도자료), guidance 변경(실적 발표 밖의 수정). 회사 IR, 신용평가사, 거래소 공시를 먼저 찾고 기사는 그다음이다.
   - 항목마다 `type`, `date`, `title`(한국어 한 줄), `source`(공시는 `sec:<form> <accession>`, 웹은 `web:<URL>`), `locator`(공시면 text 파일과 줄 번호), `quote`(원문 문장 그대로)를 적는다. 원문 문장이나 출처가 없으면 script가 버린다. 기억이나 요약으로 사건을 쓰지 않는다.
   - 신용등급을 찾았으면 `series.credit_ratings`에도 적는다(5단계의 차환 금리 재료).
   - 찾아봤는데 없는 종류는 적지 않는다. 보고서에 최신순 5개까지 실린다.
7. **배당 이력**: `python3 <skill 폴더>/scripts/quote.py dividends T --facts <facts 파일>`을 먼저 돌린다. key 없는 Yahoo chart의 split 조정된 지급 내역에서 정기·특별을 가르고 해마다 정기 주당 배당(`series.dividend_per_share_annual`)과 특별배당(`series.special_dividends`)을 넣는다. Yahoo가 실패하면 `edgar.py dividends T --facts <facts 파일>`(XBRL 10-K 연간 값)로 대신한다. XBRL 연간 합은 reverse split 전후 값이 섞이고 특별·주식 배당이 든 해가 커져 그다음 해가 컷으로 보이기 때문에 대조용이다. `dividend_frequency`는 보도자료 문장(예: "monthly dividends")에서 적는다.
8. **가격**: `python3 <skill 폴더>/scripts/quote.py price T --period P`. key 없는 Yahoo chart endpoint에서 마지막으로 끝난 거래일의 종가를 넣는다(`source: yahoo:chart`, `locator`에 URL과 기준일, `status: reported`, 5일 넘으면 `stale`). 문서에 없는 endpoint라 HTTP 오류·형식 변화·빈 값이면 price가 missing으로 남고 exit 2다. 그러면 아래 "웹 채우기"로 넣는다.
9. **위 단계 snapshot 확인**: 아래 "읽는 데이터"대로 macro snapshot과 REITWatch parsed가 있는지 보고, 없거나 오래됐으면 갱신한다.
10. **계산**: `python3 <skill 폴더>/scripts/reit_calc.py metrics --facts <facts 파일>`. `$INVEST_DATA_DIR/tickers/<T>/metrics/<P>.json`을 쓰고 경로를 찍는다. stderr에 check, flag, 데이터 구멍 수가 나온다. metrics에는 축 5개의 등급(근거 지표·값·기준)과 검사된 사건이 들어간다.
    - exit 0: 다 채웠다(회사가 내지 않는 값 때문에 빈 지표는 `summary.metrics_not_disclosed`에만 센다). exit 2: missing이 있거나 check가 실패했다(이유는 `data_gaps`). exit 1: facts 형식이 틀렸다.
    - `ffo_tie_out`이 실패하면 먼저 facts를 의심한다. 열(3개월/누적), 부호, 단위, 빠진 줄을 Ex.99와 다시 맞춘다. 그래도 다르면 회사 정의가 Nareit과 다른 것이니 풀이 글에 그 차이를 쓴다(부록 데이터 구멍에도 실린다). 맞추려고 값을 고치지 않는다.
    - 버려진 사건(출처·원문 문장 없음)은 데이터 구멍 ignored에 나온다. 고쳐 다시 돌린다.
11. **8분기 추세**: `python3 <skill 폴더>/scripts/edgar.py trend T --period P`가 XBRL로 최근 8분기의 매출, 이자비용, 부채, 주식 수, 주당 배당 표를 `$INVEST_DATA_DIR/tickers/<T>/trend/<P>.json`에 쓴다. 빈 분기가 있어도 멈추지 않는다.

## light 절차 (경쟁 종목 가볍게 보기)

단일 보고서의 경쟁 종목을 핵심 칸만으로 본다. deep 하나는 토큰을 많이 쓰니 경쟁 종목까지 늘 deep으로 보지 않는다. 같은 script와 같은 식이라 나중에 deep으로 올릴 때는 칸만 더 채운다.

1. deep 절차 1~4단계(resolve, filings, exhibits, xbrl). exhibit은 보도자료 첫 표 몇 개면 대개 충분하고, 임차인 표가 supplemental에만 있으면 그것도 연다.
2. facts 맨 위에 `"depth": "light"`를 적고 핵심 칸 15개(`references/facts-schema.md`의 "가볍게 보기")만 채운다. 값마다 source·locator를 쓰는 규칙, CAD·FAD, 정기 원금 상환, NOI 대리값 규칙은 deep과 같다. 회사가 내지 않는 칸은 `not_disclosed`와 근거다.
3. `quote.py price T --period P`. 사건·배당 이력·추세는 만들지 않는다(단일 보고서에서 경쟁 종목은 숫자만 둔다).
4. `reit_calc.py metrics --facts <facts 파일>`. exit 0이어야 한다. exit 2면 `summary.light_core_missing`의 칸을 채운다. 내지 않은 지표는 보고서에 "가볍게 봄"으로 나온다.

## 단일 보고서

1. 그 종목을 deep 절차로 만든다(같은 분기 deep metrics가 있으면 재사용).
2. **경쟁 종목**: `python3 <skill 폴더>/scripts/reit_calc.py peers T --period P`. REITWatch 같은 subsector에서 시총이 가장 가까운 2개를 고르고 종목마다 `action`을 낸다. `reuse`면 그 metrics를 쓰고, `make_light`면 light 절차로 만든다. 사용자가 경쟁 종목을 말했으면 `--with A,B`다.
   - subsector에 종목이 1개뿐이면(예: Gaming) `peers`가 1개만 낸다. 그러면 lease 구조·임차인 업종이 가장 가까운 다른 subsector 종목 하나를 `--with`로 더하고(이미 metrics가 있는 종목을 먼저), 풀이 글 `peer_position`에 왜 그 종목인지 쓴다. report.py는 subsector가 다른 경쟁 종목을 P/FFO·cap rate 순위에서 뺀다.
3. **풀이 글**: `narrative.json`을 쓴다. 칸, 한도, 검사, 잘 쓰는 법은 `<skill 폴더>/references/report-format.md`의 "풀이 글"이다. 공시 본문(사건, lease 구조, 부채 구조)과 metrics의 등급 근거를 읽고 쓴다. 지표를 다시 읽어 주지 않고 "왜 그런지"를 쓴다. 파일은 `$INVEST_DATA_DIR/tickers/<T>/reports/<날짜>-narrative.json`에 둔다.
4. **만들기**:

   ```bash
   python3 <skill 폴더>/scripts/report.py build --metrics <T metrics> --peers <경쟁1 metrics> <경쟁2 metrics> \
     --trend $INVEST_DATA_DIR/tickers/<T>/trend/<P>.json --narrative <narrative.json> \
     --out-dir $INVEST_DATA_DIR/tickers/<T>/reports
   ```

   읽는 보고서 `<날짜>-deep.md`, 부록 `<날짜>-deep-audit.md`, view model, 장표 3장(등급표, 만기 일정, 배당 여력)이 나온다. 종료 코드마다 할 일은 `report-format.md`의 표다. exit 2(풀이 글 검사)면 아무 파일도 쓰지 않았으니 stderr대로 풀이 글을 고쳐 다시 돌린다.
5. **직접 열어 보기**: 읽는 보고서를 처음부터 끝까지 읽고, 장표 PNG 3장을 연다(이미지를 볼 수 있으면 Claude Code는 Read 도구, Codex는 이미지 보기. 없으면 HTML을 연다). 숫자가 metrics와 다르거나, 글자가 겹치거나, 칸 이름이 새면 script 결함이니 고치지 않고 사용자에게 알린다. 풀이 글이 보고서 숫자와 맞지 않으면 풀이 글을 고쳐 다시 만든다.
6. **사용자에게 요약**: 결론 한 줄, 축별 등급, 위험 3개와 다시 볼 조건, 주요 사건, 파일 경로(보고서, 부록, 장표). 데이터 구멍 중 판단에 걸리는 것(판정 못 함 축, 추정으로 셈한 줄)을 한두 줄 적는다.
7. **PDF**: "PDF로", "레포트 발간해줘"면 `python3 <skill 폴더>/scripts/report.py pdf --report-dir $INVEST_DATA_DIR/tickers/<T>/reports`. 본문 + 차트 + 부록 + 용어 표의 A4 PDF가 나온다. 쪽마다 열어 본 뒤 경로를 알린다. Chrome이 없으면 exit 1이고 인쇄용 HTML이 남는다(브라우저에서 A4로 인쇄해도 된다고 알린다).

## 비교 보고서

사용자가 종목 2~6개를 직접 지정했을 때다. 지정한 종목은 모두 같은 무게다.

1. 종목마다 같은 분기 deep metrics가 있는지 본다. 없거나, light이거나, 그 뒤 실적 8-K가 나왔으면 deep 절차로 만든다(사건과 추세까지). 종목마다 facts 채우기는 서로 독립이라 subagent가 있으면 종목마다 나눠 맡긴다(Claude Code는 Agent 도구, Codex는 subagent). 지시문에는 이 SKILL.md 경로, ticker, 분기만 준다. subagent가 없으면 종목마다 차례로 한다. subagent의 보고는 증명이 아니다. metrics를 받으면 `ffo_tie_out` check와 데이터 구멍을 직접 연다.
2. **풀이 글**: 비교용 `narrative.json`(차이 한 줄 결론, 축마다 차이를 만든 원인, 금리 민감도 견줌, 종목마다 강점·약점 하나, 장표 제목. `report-format.md`의 "비교")을 쓴다. 어느 종목을 고르라는 말은 쓰지 않는다.
3. **만들기**:

   ```bash
   python3 <skill 폴더>/scripts/report.py compare --metrics <M1> <M2> ... --narrative <narrative.json> \
     --out-dir $INVEST_DATA_DIR/compare
   ```

   2개는 맞대기(두 종목과 차이 column), 3~6개는 등급 matrix에 축마다 순위다. 7개 이상이면 exit 2로 아무 파일도 쓰지 않는다(reit-sector-scan screening으로). subsector가 섞이면 valuation은 각자 자기 subsector 기준과의 거리로 견주고 첫 줄에 섞였다고 쓴다. `reit_calc.py compare`와 같은 JSON(지표 표, 경고)도 함께 나온다.
4. **직접 열어 보기와 요약**: 단일 보고서 5~6단계와 같다. 요약은 어디서 갈리고 왜 갈리는지, 금리 민감도 차이, 종목별 보고서 링크다.
5. **PDF**: `report.py pdf --report-dir $INVEST_DATA_DIR/compare --report <날짜>-<T1>-<T2>-...`. 비교 본문 + 종목마다 한 쪽 요약 + 종목별 부록 + 용어 표다.

표만 필요하면 `reit_calc.py compare --metrics <M1> <M2> ... --out <JSON>`이 지표 표 JSON만 낸다(exit 0: 경고 없고 다 참, 2: 경고나 빈 metrics, 1: 종목이 하나이거나 겹침).

## 실적 뒤 점검

"O 실적 나왔는데 봐줘"처럼 새 분기가 나온 뒤의 요청이다.

1. 새 분기 `P2`를 deep 절차로 만든다. 직전 분기 `P1` metrics가 없으면 그 분기도 만든다(`edgar.py xbrl T --period P1`이 그 분기의 실적 8-K를 고른다). 직전 분기를 나중에 만들 때는 두 분기에 같은 가격(오늘 가격)을 넣고 풀이 글에 적는다. 그래야 가격이 든 지표의 변화가 배당·FFO·NOI 변화만 담는다.
2. **diff**: `python3 <skill 폴더>/scripts/reit_calc.py diff --prev <P1 metrics> --curr <P2 metrics> --out $INVEST_DATA_DIR/tickers/<T>/reports/<날짜>-earnings.json`. 지표마다 전후 값, 변화(`%` 지표는 %p, 나머지는 같은 단위와 상대 변화 %), status·flag 변화, check 전후를 낸다. exit 0: 두 분기 값이 다 있다(공시 없음처럼 값이 없는 것이 정상인 줄은 세지 않는다). exit 2: 한쪽이 missing인 지표가 있다. exit 1: ticker가 다르거나 prev가 curr보다 앞선 분기가 아니다.
3. **풀이 글**: 실적 뒤 점검용 `narrative.json`(무엇이 나빠지고 좋아졌나, 다시 볼 조건 3개까지)을 쓴다.
4. **만들기**: `python3 <skill 폴더>/scripts/report.py earnings --diff <earnings.json> --narrative <narrative.json> --out-dir $INVEST_DATA_DIR/tickers/<T>/reports`. 나빠진 등급·지표가 먼저 나오는 `<날짜>-earnings.md`가 나온다.
5. 단일 보고서 5~6단계처럼 직접 읽고 요약한다. 새 분기의 단일 보고서도 원하면 "단일 보고서" 2단계부터 돈다.

## 보고서 모양

보고서·부록·장표·PDF의 차례, 나오는 파일, 종료 코드, 풀이 글 칸과 검사는 `<skill 폴더>/references/report-format.md`가 기준이다. agent는 md를 손으로 쓰지 않는다. report.py가 metrics·trend JSON의 숫자를 그대로 옮기고, status를 사람 말(공시값, 계산값, 추정, 오래됨, 없음, 공시 없음, 가볍게 봄)로 쓰고, 처음 나오는 지표에 한 줄 풀이를 붙이고, 끝에 용어 표를 단다.

- 읽는 보고서(단일): 결론 한 줄, 축별 등급, 핵심 숫자, 동종 대비 위치(경쟁 종목은 등급 없이 숫자만, 깊이 표시, 순위), 8분기 추세, 주요 사건, 위험과 다시 볼 조건, 용어. 용어 표를 빼고 60줄 이하다.
- 부록: FFO 재계산, AFFO bridge, 만기 사다리, covenant, cap rate 기준선과 economic EV 다리, 등급 근거, 사건 원문, 데이터 구멍과 출처 위치. 숫자를 검증하는 사람이 읽는다.
- 장표 3장: 단일은 등급표·만기 일정·배당 여력, 비교는 등급 matrix·2x2(2개면 맞대기 막대)·최대 임차인 막대.

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
| macro snapshot | `$INVEST_DATA_DIR/macro/snapshots/<날짜>.json`(가장 최근) | DGS10과 yield spread, 시장 차입금리 추정(DBAA가 있으면 DBAA, 없으면 DGS10 + BAA10Y. reit-sector-scan과 같은 차례), 투자등급 아래 회사의 차환 금리(DGS10 + HY OAS) | macro-check가 설치돼 있으면 `python3 <skills 폴더>/macro-check/scripts/macro.py fetch`. 아니면 spread가 missing |
| REITWatch parsed | `$INVEST_DATA_DIR/reitwatch/parsed/<YYYY-MM>.json` | subsector, consensus FFO와 3개월 변화·FY2/FY1 성장, 자기 P/FFO history, peer median, 경쟁 종목 고르기(시총) | reit-sector-scan이 설치돼 있으면 그 skill의 fetch를 돌린다. 아니면 그 칸이 missing이고 `peers`가 exit 2로 멈춘다(경쟁 종목을 사용자에게 묻지 않고 같은 subsector로 알려진 종목을 골라 `report.py build --peers`에 바로 넘기고 풀이 글에 그렇게 적는다) |
| Tracker parsed | `$INVEST_DATA_DIR/tracker/parsed/<YYYYQn>.json`(가격 기준일 전에 끝난 분기 중 가장 늦은 것. 그 뒤 분기 파일은 그날 아직 없던 자료라 고르지 않는다) | subsector implied cap rate(비교 줄) | reit-sector-scan이 설치돼 있으면 `reitwatch.py tracker fetch --quarter latest`. 아니면 Tracker 비교 줄만 missing |

- macro snapshot이 가격 기준일보다 7일 넘게 앞서면 DGS10·BAA10Y가 stale로 표시된다. 오늘 snapshot이 없으면 갱신한다.
- REITWatch는 월간이라 최신 달이 지난달이면 그대로 쓴다. consensus를 회사 연간 FFO 대신 쓰면 가정이라 `estimated`다. consensus 3개월 변화와 FY2/FY1 성장은 REITWatch 값의 계산이라 `derived`다(reit-sector-scan과 같다, 호가 오래되면 `stale`).
- 최신 호의 가격 기준일이 facts 가격 기준일보다 75일(`assets/thresholds.json`의 `reitwatch_max_age_days`) 넘게 앞서면 그 호에서 온 값의 `note`와 데이터 구멍(`reitwatch`)에 나이가 붙는다. 그러면 reit-sector-scan의 `fetch --month latest`로 갱신하고 다시 계산한다.
- Tracker 분기 끝날이 가격 기준일보다 150일(`assets/thresholds.json`의 `tracker_max_age_days`) 넘게 앞서면 Tracker 값이 stale로 표시된다. 그러면 새 분기 Tracker를 받는다. REITWatch subsector → Tracker row 대응은 `assets/tracker_map.json`(reit-sector-scan의 표를 복사한 것)이다. 회계 기준이 달라 견줄 수 없는 subsector(Gaming)는 차이 줄을 내지 않고 이유를 적는다.
- ticker가 바뀐 종목(MPW → MPT)은 facts의 `former_tickers`와 `assets/ticker_aliases.json`으로 예전 호의 history를 잇는다.
- 갱신이 실패해도(exit 1) 그 칸을 missing으로 두고 계속한다.

## 해석할 때

풀이 글과 사용자 요약에 똑같이 적용한다.

- 사라·팔아라, 비중을 늘려라·줄여라, 목표가, 매매 시점을 쓰지 않는다. 결과는 판단 재료이고 결정은 사용자가 한다. report.py가 풀이 글의 금지어를 검사한다.
- 등급과 flag 기준은 경험칙이다(`assets/scorecard.json`, `assets/thresholds.json`). "AFFO payout 73%는 기준(85%) 아래다"처럼 사실로 쓰고, "안전하다"고 단정하지 않는다. valuation 축은 좋고 나쁨이 아니라 위치(낮은 편·중간·높은 편)다.
- P/FFO·P/AFFO는 같은 섹터 안에서만 견준다. AFFO(CAD·FAD 포함)는 회사마다 정의가 달라 peer와 바로 견주지 않는다.
- 회사가 낸 non-GAAP 값(AFFO, EBITDAre, NOI)은 회사 정의라고 적는다.
- consensus·2차 자료 가격·NOI 대리값·Baa 차환 금리처럼 추정인 값으로 한 해석에는 추정이라고 문장에 붙인다.
- cap rate 기준선은 지금 가격이 운영 부동산에 매긴 수익률을 여러 잣대로 놓은 것이다. NAV/share, "적정 cap rate라면 주가는 얼마"라는 계산, 목표가를 쓰지 않는다. 그런 표는 사실상 적정 주가로 읽히기 때문이다. 차이는 "명목 implied cap rate는 Tracker subsector 값보다 0.10%p 낮다(가상)"처럼 사실로 쓰고, 싸다·비싸다로 단정하지 않는다.
- Tracker 값은 Nareit이 시총과 부채로 셈한 명목 값이라 명목 implied cap rate와만 차를 본다. 회사 매입·매각 cap rate는 회사 정의(cash yield, stabilized cap rate)가 달라 다른 회사 값과 바로 견주지 않는다.
- 가장 중요한 정보가 지표가 아니라 공시 본문 문장일 때가 있다(임차인 going concern, 임대료 미납). 그런 사건은 결론 한 줄이나 위험 3개에 올린다.

## 일반 배당주 경로 (SIC가 6798이 아님)

"JNJ 배당 괜찮아?"처럼 REIT가 아닌 종목이다. deep 절차 1~10단계를 따르되 칸과 지표가 다르다.

- `edgar.py xbrl`이 일반 배당주 칸으로 초안을 만든다: 분기 순이익·diluted EPS·분기 주당 배당, 회계연도 누적(YTD) 영업현금흐름·설비투자·보통주 배당 지급. 10-Q 현금흐름표는 누적 값만 내기 때문에 FCF payout도 누적끼리 본다.
- agent가 채울 칸: `eps_guidance_low/high`(연간 EPS guidance. adjusted면 `note`에 회사 정의라고 쓴다), `dividend_regular_latest`·`dividend_frequency`(최근 배당 공지 문장), `price`. 배당 공지가 8-K에 없으면 회사 IR 보도자료를 웹에서 찾아 `source: web:<URL>`, `locator`에 원문 문장을 쓴다.
- `reit_calc.py metrics`가 경로를 스스로 고른다(`path: dividend`). 지표: `eps_fy`(guidance 가운데 → 분기×4 estimated), forward dividend·yield, `eps_payout_forward`·`eps_payout_quarter`, `fcf_ytd`·`fcf_payout_ytd`, 배당 CAGR 3·5년·컷, `chowder_number`(yield + 5년 배당 CAGR)와 `chowder_threshold`(utility SIC 49xx 8, yield 3% 미만 15, 그 외 12), `p_e`, `yield_trap_signals`.
- flag: `eps_payout`(75% watch·100% alert), `fcf_payout`(80%·100%), `chowder`(기준 미만 watch), `yield_trap`(신호 1개 watch, 2개 이상 alert). 기준은 `assets/thresholds.json`의 경험칙이다. 분기 GAAP EPS가 일회성 비용으로 작아 분기 payout이 100%를 넘으면 flag note에 남는다. 보고서에 그 비용을 적는다.
- **보고서는 report.py를 쓰지 않는다.** `report.py build`는 일반 배당주 metrics를 받으면 exit 1이다(등급·경쟁 종목·장표가 REIT 지표로 짜여 있다). 지금처럼 agent가 metrics의 숫자를 그대로 옮겨(다시 계산하거나 반올림을 바꾸지 않는다) `$INVEST_DATA_DIR/tickers/<T>/reports/<날짜>-deep.md`를 아래 모양으로 쓴다. 값마다 status를 붙이고, `estimated`·`stale`은 문장에도 적는다.

```md
# <T> <회사 이름> <P> 배당 점검 (<날짜>)

facts: <경로>, metrics: <경로>, 공시: <form> <accession> (<제출일>). 판단 재료일 뿐 매수·매도 권유가 아니다.

## 요약
<3~5줄. payout, Chowder, yield trap 신호, 눈에 띄는 변화와 데이터 구멍>

## 배당과 payout
<forward dividend·yield, EPS payout(forward, 분기), FCF payout(누적), 배당 CAGR 3·5년, 컷 이력, flag와 기준(경험칙)>

## Chowder rule
<chowder_number, 기준, 기준을 고른 이유(utility·저수익)>

## yield trap 신호
<신호마다 사실과 값>

## valuation
<P/E, EPS FY가 무엇(guidance·분기×4)인지>

## 다시 볼 조건
<사라·팔아라가 아니라 다시 계산할 때를 적는다>

## 데이터 구멍
<metrics의 data_gaps 전부, 웹 채우기 출처와 status>
```

## 고친 뒤 확인

script나 `assets/`의 기준 파일을 고치면 돌린다. network 없이 `tests/fixtures`로 돌고, 기대값은 `tests/expected.json`에 회사 발표값이나 손으로 셈한 값이다. diff·compare는 `tests/fixtures/metrics/`의 가상 metrics, 일반 배당주는 `tests/fixtures/facts_div_2026q3.json`(가상), report.py는 `tests/fixtures/report/`(가상)로 확인한다.

```bash
python3 <skill 폴더>/scripts/reit_calc.py --check   # exit 0이면 통과
python3 <skill 폴더>/scripts/edgar.py --check
python3 <skill 폴더>/scripts/quote.py --check
python3 <skill 폴더>/scripts/report.py --check
```

지표를 더하거나 식을 바꾸면 `references/metrics.md`도 같이 고친다. 기준값은 `assets/thresholds.json`·`assets/scorecard.json`에만 둔다. 보고서 모양을 바꾸면 `references/report-format.md`도 같이 고친다.

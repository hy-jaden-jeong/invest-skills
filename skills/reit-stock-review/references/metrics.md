# 지표 정의

`scripts/reit_calc.py`가 계산하는 지표의 뜻과 식이다. 식은 script와 이 파일이 같아야 한다. 하나를 고치면 다른 쪽도 고친다.

> 이 파일의 절차(FFO → AFFO → leverage → 배당 지속성 순서, 만기 집중도 표시, FFO 표를 줄마다 맞추는 품질 확인, 회사 정의 지표에 출처 표시)는 CaseMark의 `analyzing-reit-financials` skill(https://github.com/CaseMark/skills, `skills/finance/analyzing-reit-financials/SKILL.md`)에서 가져왔다.
> 원본 저작권: Copyright 2025 CaseMark, Inc. Apache License, Version 2.0으로 배포된다. License 전문은 같은 폴더의 `LICENSE-Apache-2.0.txt`에 있다(https://www.apache.org/licenses/LICENSE-2.0).
> 바꾼 점: NAV 추정과 투자 thesis·bull/bear 절은 뺐다. FFO 식을 Nareit 2018 정의로 적고 재계산이 회사 값과 0.5% 넘게 다르면 실패로 했다. 기준값은 `assets/thresholds.json` 한 곳으로 옮겼다. 결과에서 매수·매도 판단을 뺐다. 한국어로 다시 썼다.

## status

계산값은 입력 status를 물려받는다. 나쁜 순서는 `reported` < `derived` < `stale` < `estimated` < `missing`이다. 입력이 모두 reported면 계산값은 `derived`다. 입력이 하나라도 missing이면 계산값도 missing이고, 0이나 다른 값으로 채우지 않는다. 가정이 든 계산(분기×4 같은 run-rate)은 `estimated`로 두고 가정을 `note`에 쓴다.

- 한 값이 두 상태에 걸리면(2차 자료이면서 오래됨, REITWatch 추정값이면서 오래됨) 더 나쁜 쪽을 status로 두고 나머지 사정은 `note`에 쓴다. 그래서 오래된 2차 자료는 `estimated`다. stale로 바꾸면 오래될수록 좋은 status가 되기 때문이다.
- facts 값은 status와 상관없이 `source`·`locator`가 있어야 하고, `value`는 유한한 숫자여야 한다(NaN·무한대는 아님). 어기면 missing으로 내리고 데이터 구멍에 올린다. 모르는 status도 missing이다.
- 회사 표의 여러 줄 합인 facts 값은 `parts`에 줄마다 적고, script가 단위를 맞춰 더한다. status는 줄의 가장 나쁜 것이고 모두 reported면 derived다. 한 줄이라도 규칙을 어기거나 missing이면 그 칸은 missing이다. agent가 더하지 않는다.
- 결과 JSON에는 NaN·Infinity를 쓰지 않는다. 회사 FFO·AFFO가 0이면 tie-out은 실패이고 `diff_pct`는 null이다. covenant 줄의 `limit`·`actual`이 유한한 숫자가 아니면 그 줄은 missing이고 출력에는 null을 쓴다. macro snapshot의 DGS10·BAA10Y·DBAA가 유한한 숫자가 아니면 그 값은 missing이다.
- `parts`와 `value`를 함께 적으면 그 칸은 missing과 데이터 구멍이다(어느 쪽이 맞는지 고르지 않는다).
- facts 값에 `basis`(`consolidated`·`pro_rata`)를 적으면 계산값이 물려받는다. 한 식의 입력끼리 basis가 다르면 값은 그대로 내되 status를 `estimated`로 내리고 note에 섞인 칸을 쓴다(`basis: mixed`). 다른 basis 값은 그 칸이 missing이다.
- facts 값이 `not_disclosed: true`이고 근거(`source`·`locator`, 그 칸이 없는 표의 위치)가 있으면, 그 값으로 셈하는 지표는 missing이고 `not_disclosed: true`가 붙는다. 이 지표는 exit 2와 `summary.metrics_missing`에 세지 않고 `summary.metrics_not_disclosed`에 센다. 데이터 구멍에는 status `not_disclosed`("공시 없음")로 오른다. 빠진 입력 중 하나라도 보통 missing이면 그 지표는 보통 missing이다. 근거가 없거나 값과 함께 적으면 보통 missing이다.
- 값이 없는 것이 정상인 지표는 status missing에 표시가 붙고 exit 2와 `summary.metrics_missing`에 세지 않는다. `not_disclosed`(공시 없음, `summary.metrics_not_disclosed`), `none_in_period`(그 분기 거래 없음, `summary.metrics_none_in_period`. 매입·매각 cap rate만), `not_comparable`(견줄 수 없음, `summary.metrics_not_comparable`. 기준이 다른 Tracker subsector의 차이 줄)이다. 데이터 구멍에는 그 표시 이름이 status로 오른다. diff도 이 줄을 missing으로 세지 않는다.
- series 단위 공시 없음(`series_not_disclosed.covenants`·`top_tenants`, `references/facts-schema.md`)이 있으면 그 표로 셈하는 지표가 공시 없음이다. 공시 없음 때문에 값이 없는 flag는 review이고 note에 "공시 없음"이라고 적는다.
- 회사가 capex reserve를 공시 없음으로 적고 AFFO bridge에도 recurring_capex·leasing_cost 줄이 없으면(triple-net처럼 AFFO에서 유지 capex를 빼지 않는 회사) 유지 비용은 못 찾은 값이 아니라 공시 없음이다. `recurring_capex_to_noi_pct`, `capex_reserve`, `noi_economic`, economic cap rate 둘이 공시 없음이 된다.
- 가격이 든 지표(`forward_yield`, `market_cap`, `p_ffo`, `p_affo`, `affo_yield`, implied·economic cap rate, `economic_ev`, yield spread, `implied_cap_rate_vs_tracker`, 일반 배당주의 `p_e`·`chowder_number`)의 `as_of`는 가격 기준일이다.
- 회사가 AFFO를 내지 않아(`affo_reported`가 공시 없음) bridge가 없으면 check `affo_bridge`는 `pass: null`이다(실패로 세지 않는다). stderr에는 "공시 없음 affo_bridge"로 찍힌다.
- 0 이상이어야 하는 칸(`dev_remaining_spend`, `capex_reserve`)이 음수면 그 칸은 missing과 데이터 구멍이다. 회사 capex reserve를 거절하면 AFFO bridge 대리값으로 대신하지 않는다.

## FFO와 AFFO

| 지표 | 식 | 메모 |
| --- | --- | --- |
| `ffo_nareit` | 보통주 귀속 순이익 + (D&A − 비부동산 감가) + 감가 부동산 impairment − 부동산 매각 이익 + 미연결 JV 조정 + 비지배지분(NCI) 조정 + 기타 Nareit 조정 | Nareit FFO White Paper(2018) 정의. 매각 손실이면 `gain_on_sale_re`가 음수다 |
| check `ffo_tie_out` | (재계산 − 회사 FFO) / 회사 FFO | 0.5%(`ffo_tie_out_tolerance_pct`)를 넘으면 실패, exit 2, 데이터 구멍. facts를 잘못 옮겼거나 회사 정의가 Nareit과 다르다는 뜻이다 |
| AFFO | 회사 정의를 따른다 | 표준이 없다. FFO에서 maintenance capex·leasing cost·straight-line rent·비현금 항목을 빼고 더하는 방식이 회사마다 다르다. 회사 reconciliation 줄을 그대로 `affo_bridge`에 옮긴다 |
| check `affo_bridge` | 시작 값(FFO 또는 Normalized FFO) + bridge 줄 합 vs 회사 AFFO | 0.5%를 넘으면 실패. 빠진 줄이나 부호 실수를 잡는다 |
| `affo_per_share_diluted` | 회사 발표값, 없으면 AFFO 총액 / 분기 가중평균 희석 주식 수(derived) | PLD·REXR처럼 총액만 내는 회사도 payout을 낸다. 주식 수가 AFFO 총액과 다른 기준이면 facts에서 estimated로 내려온다. 희석 주식 수가 0 이하이거나 기말 보통주와 10배 넘게 다르면(단위 실수) 내지 않는다 |
| `affo_fy`, `ffo_fy` | 연간 주당 값 | AFFO: guidance 가운데 → 분기×4(estimated). FFO: guidance 가운데 → consensus FFO FY1(REITWatch, estimated) → 분기×4(estimated). consensus를 쓸 때 그 값이 회사 분기 FFO/share × 4와 10%(`CONSENSUS_GAP_PCT`) 넘게 다르거나 AFFO guidance 가운데와 1% 안이면 note에 "consensus 정의 점검"을 적는다. REITWatch consensus가 회사 Nareit FFO가 아니라 AFFO·NFFO 기준일 수 있어서다(VICI 2.46 = AFFO guidance, 회사 Nareit FFO는 CECL이 들어 분기 0.31) |
| `recurring_capex_to_noi_pct` | −(bridge의 recurring_capex + leasing_cost 줄, 분기) × 4 / 연환산 NOI × 100 | 부동산을 유지하는 데 드는 돈(maintenance capex + TI·LC)이 NOI의 몇 %인지(Green Street의 capex reserve 방식). 그 category 줄이 없으면 missing. recurring_capex·leasing_cost 줄이 양수(부호 뒤집힘)이거나 bridge에 다섯 가지가 아닌 category가 하나라도 있으면 missing(일부 줄의 합으로 내지 않는다). `straight_line_to_ffo_pct`도 모르는 category 규칙이 같다 |
| `straight_line_to_ffo_pct` | −(bridge의 straight_line 줄, 분기) / 분기 FFO × 100 | FFO 중 현금이 아닌 임대료 몫. bridge에 straight_line 줄이 없고 facts `straight_line_rent`가 있으면 그 값 / 분기 FFO × 100(AFFO를 내지 않는 회사) |

## 배당 지속성

| 지표 | 식 | 메모 |
| --- | --- | --- |
| `forward_dividend` | 최근 정기 배당 × 연 지급 횟수 | 특별배당은 넣지 않는다. TTM 합으로 대신하지 않는다(kanchi-dividend-sop의 regular forward yield) |
| `forward_yield` | forward dividend / price × 100 | |
| `affo_payout_forward` | forward dividend / 연간 AFFO per share × 100 | flag `affo_payout`의 기준. 연간 AFFO가 0 이하면 값을 내지 않는다(flag review) |
| `affo_payout_quarter` | 분기 지급 배당 / 분기 AFFO per share × 100 | 회사가 보고서에 내는 payout과 맞춰 볼 수 있다. 분기 AFFO가 0 이하면 내지 않는다 |
| `ffo_payout_forward` | forward dividend / 연간 FFO per share × 100 | 참고 줄. flag와 기준값이 없다. AFFO를 내지 않는 회사(EGP)도 값이 나온다. `ffo_fy`가 Core FFO guidance면 회사 정의다. 연간 FFO가 0 이하면 내지 않는다 |
| `dividend_cagr_3y`, `_5y` | (마지막 해 / n년 전)^(1/n) − 1 | XBRL 10-K 연간 주당 배당 |
| `dividend_cuts` | 연간 배당이 전년보다 줄어든 해 | 연간 값이라 한 해 안의 컷과 회복은 놓칠 수 있다 |

## 재무

| 지표 | 식 | 메모 |
| --- | --- | --- |
| `net_debt` | total debt − cash | 회사가 net debt를 정의한 표의 숫자를 쓴다 |
| `net_debt_to_ebitdare` | net debt / 연환산 EBITDAre | 회사의 annualized (pro forma) adjusted EBITDAre. 회사 발표 배수와 맞춰 본다 |
| `interest_coverage` | 분기 EBITDAre / 분기 interest expense | covenant의 fixed charge coverage와 식이 다르다 |
| `fixed_rate_debt_pct` | fixed rate debt / debt principal × 100 | swap으로 고정한 부분은 회사 표를 따른다 |
| `maturity_ladder`, `maturity_max_year_pct` | 해마다 만기 원금 / 만기 원금 합 | 연도 칸 중 최대값이 flag `maturity_concentration`의 기준. "thereafter"는 비중만 낸다 |

## 신용·자본구조

신용평가사가 REIT를 볼 때 쓰는 축이다. S&P "Key Credit Factors For The Real Estate Industry"(2018)는 debt/EBITDA, EBITDA/interest, fixed-charge coverage를 core ratio로 두고 secured debt가 감가상각 전 자산의 35%를 넘으면 무담보 채권 등급을 한 단계 내린다고 적는다. Moody's REIT scorecard(2021 방법론)는 leverage·coverage 항목에 total debt + 우선주 / gross assets, net debt/EBITDA, secured debt / gross assets, fixed-charge coverage를, 유동성 항목에 unencumbered assets / gross assets를 둔다. 아래 식은 그 정의를 공시 값으로 옮긴 것이고 신용평가사의 조정(리스 부채, hybrid 등)은 하지 않는다.

| 지표 | 식 | 메모 |
| --- | --- | --- |
| `gross_assets` | total assets + 부동산 감가상각 누계 | 감가상각 전 장부가. 회사가 GAV를 내면 맞춰 본다(O 87,758,545천 = Total GAV $87,759M). 감가상각 누계가 0이면(sales-type·financing lease 회계로 부동산을 감가상각하지 않는 회사) gross assets = total assets라고 note에 적는다 |
| `debt_pref_to_gross_assets` | (debt principal + 우선주) / gross assets × 100 | debt는 `debt_principal_total`(원금 합)이다. `total_debt`는 회사가 현금을 뺀 net debt일 수 있다. 가까운 S&P 보조 비율 debt/(debt + equity, 감가상각 전 장부가)의 구간(같은 식은 아니다): 30% 미만 minimal, 40~55% intermediate, 70% 넘으면 highly leveraged |
| `secured_debt_to_gross_assets` | secured debt / gross assets × 100 | S&P는 35% 넘으면 무담보 채권 notch down |
| `unencumbered_pct` | unencumbered assets / gross assets × 100, 없으면 unencumbered NOI / 연환산 NOI × 100 | 앞쪽은 Moody's, 뒤쪽은 S&P가 보는 비율. formula에 어느 쪽인지 적힌다 |
| `fixed_charge_coverage` | 분기 EBITDAre / (이자비용 + 자본화 이자 + 우선주 배당 + 정기 원금 상환) | S&P 정의. 구간: 3.7x 넘으면 minimal, 2.1~3.1x intermediate, 1.3~1.7x aggressive, 1.3x 미만 highly leveraged. 회사 covenant의 fixed charge coverage(대개 trailing 12개월, pro forma)와 식이 다르다. 부채가 모두 만기 일시 상환이면 facts에 정기 원금 상환 0과 근거를 적고, note에 0으로 셈했다고 근거와 함께 적는다 |
| `covenant_headroom` | 줄마다 max: (1 − actual / limit) × 100, min: (1 − limit / actual) × 100 | 한도에 닿기까지 좋은 쪽 값(max는 분모의 자산, min은 분자의 이익·자산)이 몇 % 줄 수 있는지. 한도를 넘었으면 음수다. 회사 covenant 정의를 그대로 따른다 |
| `covenant_headroom_min_pct` | 줄 headroom의 최소 | flag `covenant_headroom`의 기준. note에 그 줄 이름. headroom을 못 낸 줄이 하나라도 있으면 missing. covenant 줄이 없고 `series_not_disclosed.covenants`(근거)가 있으면 공시 없음 |

## 리파이낸싱·유동성

앞으로 24개월 안에 갚을 부채를 지금 금리로 다시 빌리면 이자가 얼마 늘고, 그 동안 쓸 돈을 손에 쥔 돈으로 덮는지 본다. Moody's credit opinion의 liquidity 분석(현금 + 남은 revolver + 배당 뒤 남는 영업 현금흐름 대 만기·개발비)을 공시 값으로 옮겼다. 모두 가정이 들어 estimated다.

| 지표 | 식 | 메모 |
| --- | --- | --- |
| `wam_years`, `weighted_avg_rate_pct` | 회사 발표값 그대로 | 가중평균 잔여 만기, 가중평균 금리 |
| `market_borrowing_rate_est` | 차환 금리 추정. 차례: ① 회사의 최근 12개월 안 발행 금리(`recent_issuance_rate_pct`, 발행일이 가격 기준일 전 365일 안), ② 신용등급별: 투자등급(BBB−·Baa3 이상)은 Baa 금리(DBAA, 없으면 DGS10 + BAA10Y), BB(BB+~BB−·Ba1~Ba3)는 DGS10 + ICE BofA BB OAS(BAMLH0A1HYBB. snapshot에 없으면 HY OAS BAMLH0A0HYM2로 대신하고 note에 적는다), 그 아래는 DGS10 + HY OAS(BAMLH0A0HYM2), ③ 등급을 모르면 Baa 금리(note에 "신용등급을 몰라 Baa 금리로 가정") | 모두 estimated. 기관마다 등급이 다르면 가장 낮은 것을 쓴다. 쓴 차례는 `rate_source`(`issuance`·`rating`·`unknown_rating`), 등급 구간은 `rating_band`(`investment_grade`·`bb`·`below_bb`), 읽은 등급은 `ratings_used`에 적힌다. 등급에 맞는 series가 snapshot에 없으면 Baa로 대신하지 않고 이 줄이 missing이다(투자등급 아래 회사를 Baa로 보면 차환 비용이 줄어드는 것처럼 나온다). Baa 금리는 reit-sector-scan의 Baa 금리와 같은 차례이고, 투자등급 하단 기준이라 A등급 회사에는 보수적이며 지수 만기(20년 안팎)가 신규 발행(5~10년)보다 길다. HY OAS는 HY 지수 평균 spread라 B 이하 회사에는 낮게 잡힐 수 있다 |
| `maturities_24m` | 분기 끝날 해와 다음 해 만기 + 그다음 해 만기 × (분기 끝 달 / 12) | 사다리가 해마다라 셋째 해는 12달에 고르게 나눈다고 가정한다(12월 끝날이면 나누지 않음). `period`가 연도·`thereafter`가 아닌 줄이 있으면 이 줄과 만기 사다리·`maturity_max_year_pct`가 missing이다(0으로 채우지 않는다) |
| `affo_annualized` | 분기 AFFO 총액 × 4 | |
| `refi_cost_24m` | 24개월 만기 × (시장 차입금리 추정 − 기존 가중평균 금리) / 100 | 다시 빌린 뒤 1년에 늘어나는 이자(천 달러). 기존 금리는 만기 부채가 아니라 전체 평균이다. 음수면 이자가 준다. 차환 금리 추정이 지금 가중평균 금리보다 낮으면 그 사실을 note 맨 앞에 적는다 |
| `refi_impact_affo_pct` | `refi_cost_24m` / 연간 AFFO 총액 × 100 | |
| `floating_rate_shock_affo_pct` | 변동금리 원금 × 1%p / 연간 AFFO 총액 × 100 | 금리가 1%p 오를 때 1년 이자 증가 |
| `retained_cash_flow_24m` | 2 × (연간 AFFO 총액 − forward dividend × (보통주 + OP unit)) | 배당 뒤 남는 AFFO가 24개월 이어진다고 가정 |
| `ffo_annualized`, `refi_impact_ffo_pct`, `floating_rate_shock_ffo_pct` | 분기 Normalized FFO(없으면 FFO) 총액 × 4, refi 이자 증가 / 그 값 × 100, 변동금리 원금 × 1%p / 그 값 × 100 | 회사가 AFFO를 내지 않을 때(`affo_reported`가 공시 없음)만 나오는 참고 줄, estimated. FFO는 유지 capex를 빼지 않아 AFFO보다 커서 비율이 작게 나온다. AFFO를 그냥 못 찾은 것(보통 missing)이면 내지 않는다 |
| `liquidity_sources_24m`, `liquidity_uses_24m` | 현금 + revolver 잔여 + 유보 현금흐름, 24개월 만기 + 남은 개발비(+ 그 밖의 약정) | 현금은 `liquidity_cash`. revolver 잔여가 commercial paper를 이미 뺀 값이면 CP를 만기에서 한 번 더 세어 보수적이다. 회사가 AFFO를 내지 않으면 유보 현금흐름은 `ffo_annualized`로 셈한다(estimated, note). facts `other_committed_uses`(대출 funding commitment 등)를 적으면 uses에 더하고 그 기간에 모두 쓴다고 가정한다 |
| `liquidity_coverage_24m` | sources / uses | 1x 아래면 24개월 동안 자본시장(채권·주식 발행, 매각)에 기대야 한다는 뜻. 기준값과 flag는 두지 않는다. uses가 0이면(24개월 안 만기와 남은 개발비가 없음) 값은 null, status는 uses의 것, note "갚을 것 없음"이고 missing으로 세지 않는다 |

## 운영

| 지표 | 식 | 메모 |
| --- | --- | --- |
| `occupancy_pct`, `wale_years`, `investment_grade_share_pct` | 회사 발표값 그대로 | |
| `same_store_growth_pct` | 이번 기간 / 전년 같은 기간 − 1 | same store NOI가 없으면 회사가 내는 same store 지표(예: rental revenue)를 쓰고 `note`에 적는다 |
| `top1/5/10_tenant_pct` | 임대료(ABR) 비중 상위 임차인 합 | facts `top_tenants`의 `measure`(income, revenue 등)가 있으면 formula에 그 기준을 쓴다. 기준이 섞이면 estimated. 공시된 임차인이 n곳보다 적고 `series_not_disclosed.top_tenants`가 있으면 공시 없음 |
| `sector_kpis` | 회사 발표값 그대로 | 섹터별 지표는 `sector-kpis.md` |

## valuation

| 지표 | 식 | 메모 |
| --- | --- | --- |
| `market_cap` | price × (보통주 + OP unit) | OP unit은 보통주로 바꿀 수 있어 넣는다 |
| `p_ffo`, `p_affo` | price / 연간 주당 FFO·AFFO | 같은 섹터 안에서만 견준다. AFFO는 회사 정의가 달라 peer끼리 바로 견주지 않는다 |
| `affo_yield` | 연간 AFFO per share / price × 100 | |
| `implied_cap_rate` | 연환산 NOI / (시총 + net debt + 우선주) × 100 | NOI와 debt가 같은 기준(pro-rata 또는 consolidated)인지 `note`에 적는다. 연환산 NOI가 없고 facts `noi_quarter`가 있으면 분기 NOI × 4(estimated). NOI를 쓰는 다른 줄(recurring capex/NOI, unencumbered NOI 비율, economic cap rate)도 같다 |
| `yield_spread_vs_10y`, `affo_yield_spread_vs_10y` | yield − DGS10 (%p) | DGS10은 macro-check snapshot. snapshot이 가격 기준일보다 7일 넘게 앞서면 stale |
| `p_ffo_reitwatch`, `p_ffo_history_pctile` | REITWatch P/FFO FY1, 자기 history 중 최신값 이하 비율 | history 표본은 1·4·7·10월 호(가격 기준일이 분기말)와 최신 호다(reit-sector-scan과 같은 표본). 8개(`history_min_points`)보다 적으면 missing. 같은 출처(REITWatch)끼리 견준다 |
| `peer_median_p_ffo`, `p_ffo_vs_peer` | 같은 subsector peer의 REITWatch P/FFO median, 비율 | reit-sector-scan의 parsed 파일을 읽기만 한다 |
| `ffo_fy1_rev_3m` | 새 호 consensus FFO FY1 / 3개월 앞 호의 같은 회계연도 consensus FFO − 1(%) | REITWatch parsed. 앞 호 FY1이 같은 해면 FY1, FY가 바뀐 뒤면(2월 호부터) 앞 호 FY2와 견준다. 앞 호가 없거나 값이 0 이하면 missing. reit-sector-scan `ffo_fy1_rev_3m`과 같은 규칙이고 값과 status도 같다. REITWatch 원문 값의 계산이라 derived(호가 오래되면 stale) |
| `ffo_growth_fy2` | consensus FFO FY2 / FY1 − 1(%) | REITWatch FFO growth column이 아니라 두 추정치로 셈한다(반올림 차이). derived(호가 오래되면 stale) |

REITWatch 최신 호의 가격 기준일이 가격 기준일(없으면 facts `period_end`)보다 75일(`reitwatch_max_age_days`) 넘게 앞서면, 그 호에서 온 값(consensus FFO, `p_ffo_reitwatch`, `peer_median_p_ffo`, `p_ffo_history_pctile`, `ffo_fy1_rev_3m`, `ffo_growth_fy2`)의 `note`에 나이를 쓰고 데이터 구멍에 `reitwatch`(stale)를 올린다. estimated인 값은 estimated 그대로이고(estimated가 stale보다 나쁘다), derived인 consensus 변화·성장은 stale이 된다. REITWatch 호는 그 달 중순이 지나서 올라오기도 해 최신 호의 기준일이 평소에도 30~60일 앞서므로 그보다 넉넉히 둔다.

### valuation 기준선 (economic implied cap rate)

명목 implied cap rate(`implied_cap_rate`)는 회사 NOI를 시총과 부채 전체로 나눈다. 그런데 EV에는 아직 NOI를 내지 않는 자산(건설 중 자산, 개발용 토지)이 들어 있고, 미연결 JV의 NOI 몫은 들어 있는데 그 부채는 빠져 있고, NOI에는 건물을 유지하는 데 해마다 드는 돈이 빠져 있지 않다. Green Street의 NAV 모델 설명("REIT Valuation: The NAV-based Pricing Model", Version 3.0 발췌, Green Street Advisors, 2014)은 이 셋을 이렇게 다룬다(우리 말로 옮김):

- 운영 부동산만 forward cash NOI와 cap rate로 값을 매기고, 건설 중 자산(CIP)과 토지는 따로 본다. straight-line rent 같은 현금 아닌 임대료는 빼고 cash NOI로 본다.
- JV는 회계가 지분법이라 보이지 않는 부채가 숨는다. 지분만큼 자산과 부채를 나눠(pro-rata) 넣어야 leverage가 보인다.
- capex는 사람들이 가장 적게 세는 비용이고 섹터·자산 품질마다 크게 다르다. NOI에서 capex reserve를 뺀 "economic" cap rate로 견줘야 하고, 빼지 않은 명목 cap rate로 견주면 capex가 큰 portfolio가 싸 보인다.
- 자산만 시가로 바꾸고 부채를 장부가로 두면 틀린다. 시장금리가 기존 금리와 크게 다르면 부채도 시가(mark-to-market)로 바꾼다.

이 skill은 NAV를 만들지 않는다(목표가로 읽히기 때문이다). 위 조정을 시장이 매긴 EV 쪽에 거꾸로 넣어, 지금 가격이 운영 부동산에 매긴 cap rate만 본다. 사적 시장 cap rate나 NAV/share는 내지 않는다.

| 지표 | 식 | 메모 |
| --- | --- | --- |
| `capex_reserve` | 회사가 낸 연간 capex reserve → 없으면 −(AFFO bridge의 recurring_capex + leasing_cost 줄, 분기) × 4 | 뒤쪽은 가정이라 estimated. `recurring_capex_to_noi_pct`와 같은 줄이다. 둘 다 없으면 missing(0으로 두지 않는다) |
| `noi_economic` | 연환산 NOI − capex reserve | |
| `non_operating_assets` | CIP + 개발용 토지 + 그 밖의 비운영 자산 | NOI에 그 수익이 들어 있지 않은 자산만 넣는다. NOI가 대출·JV 수익을 넣었으면 그 자산은 빼지 않는다 |
| `jv_debt_pro_rata` | 미연결 JV 부채의 지분 몫 | `total_debt`에 들어 있지 않은 것만. 회사 net debt가 이미 pro-rata(PLD의 Prologis Share)면 0 |
| `economic_ev` | 시총 + net debt + 우선주 + 미연결 JV 부채 − 비운영 자산 | 새 칸은 NOI와 같은 기준(pro-rata·consolidated)으로 적는다 |
| `economic_implied_cap_rate` | `noi_economic` / `economic_ev` × 100 | EV가 0 이하면 내지 않는다. flag 없음 |
| `debt_fair_value`, `debt_carrying_value` | 10-Q·10-K fair value 표의 fair value와 같은 표의 carrying value | 표에 합계가 없으면 줄마다 `parts`로 적고 script가 더한다 |
| `debt_mtm_adjustment` | fair value − carrying value | 음수면 시장금리가 기존 금리보다 높아 부채 시가가 장부가보다 낮다 |
| `economic_implied_cap_rate_debt_mtm` | `noi_economic` / (`economic_ev` + `debt_mtm_adjustment`) × 100 | fair value 표에 없는 부채(JV 부채, 표가 빼 둔 revolver·CP)는 장부가 그대로 |
| `acquisitions_cap_rate`, `dispositions_cap_rate` | 회사 발표 최근 매입·매각 cap rate | 회사 정의 그대로(initial cash yield, stabilized cap rate 등이 회사마다 다르다). 사적 시장 거래 가격의 단서다 |
| `tracker_subsector_cap_rate` | Nareit REIT Industry Tracker의 그 subsector implied cap rate(넷째 자리) | reit-sector-scan의 Tracker parsed 중 가격 기준일(없으면 `period_end`) 전에 끝난 분기의 가장 늦은 파일(그 뒤 분기는 그날 없던 자료다). 분기 끝날(`as_of`)이 날짜가 아니면 missing. subsector 대응은 `assets/tracker_map.json`, 자기 row에 값이 없으면 parent 값(estimated). Tracker 분기 끝날이 가격 기준일보다 150일(`tracker_max_age_days`) 넘게 앞서면 stale. Tracker parsed가 없으면 missing |
| `implied_cap_rate_vs_tracker` | 명목 implied cap rate − Tracker subsector implied cap rate(%p) | Tracker 값도 시총과 부채로 셈한 명목 값이라 명목끼리만 차를 낸다. `assets/tracker_map.json`에서 `cap_rate_comparable: false`인 subsector(Gaming: 임대가 sales-type·financing lease라 Tracker NOI에서 임대 수익 대부분이 빠진다)는 값을 내지 않고 `not_comparable`과 이유(`why`)를 적는다. Tracker 값 줄에도 같은 이유를 note에 적는다 |

## flag

`ok`, `watch`, `alert`, `review`. 데이터가 없으면 `review`이고 통과로 치지 않는다(kanchi-dividend-sop의 "silent PASS 금지"). 기준값은 `assets/thresholds.json`에 있고 모두 경험칙이다.

| flag | 지표 | watch | alert |
| --- | --- | --- | --- |
| `affo_payout` | `affo_payout_forward`(없으면 `affo_payout_quarter`) | 85% 이상 | 100% 이상 |
| `maturity_concentration` | `maturity_max_year_pct` | 15% 이상 | 20% 이상 |
| `net_debt_to_ebitdare` | `net_debt_to_ebitdare` | 6.0x 이상 | 7.0x 이상 |
| `interest_coverage` | `interest_coverage` | 3.0x 미만 | 2.0x 미만(S&P EBITDA/interest 1.8x 아래가 aggressive) |
| `covenant_headroom` | `covenant_headroom_min_pct` | 15% 미만 | 5% 미만 |

## 일반 배당주 경로 (SIC가 6798이 아님)

`compute()`가 facts의 `company`로 경로를 고르고 metrics에 `path: "dividend"`를 쓴다(REIT는 `"reit"`).

| 지표 | 식 | 메모 |
| --- | --- | --- |
| `eps_fy` | EPS guidance 가운데 → 분기 diluted EPS × 4(estimated) | adjusted guidance면 회사 정의 |
| `forward_dividend`, `forward_yield` | REIT와 같다 | |
| `eps_payout_forward` | forward dividend / `eps_fy` × 100 | flag `eps_payout`의 기준. EPS가 0 이하면 값을 내지 않는다 |
| `eps_payout_quarter` | 분기 주당 배당 / 분기 GAAP diluted EPS × 100 | 일회성 비용이 크면 100%를 넘는다. 그러면 flag note에 남긴다 |
| `fcf_ytd` | 영업현금흐름(YTD) − 설비투자(YTD) | |
| `fcf_payout_ytd` | 보통주 배당 지급(YTD) / `fcf_ytd` × 100 | FCF가 0 이하면 값을 내지 않고 `fcf_shortfall` 신호가 참 |
| `dividend_cagr_3y`, `_5y`, `dividend_cuts` | REIT와 같다 | n년 전 연도 값으로 본다. 그 해 값이 없으면 missing |
| `chowder_number` | forward yield + 5년 배당 CAGR | |
| `chowder_threshold` | utility(SIC 4900~4999) 8, yield 3% 미만 15, 그 외 12 | Chowder rule |
| `p_e` | price / `eps_fy` | |
| `yield_spread_vs_10y` | forward yield − DGS10 | |
| `yield_trap_signals` | `high_yield`(yield 8% 이상), `payout_over_100`, `fcf_shortfall`(FCF payout 100% 이상이나 FCF 0 이하), `recent_cut`(최근 5년 안 컷), `no_growth`(5년 CAGR 0 이하) | 값이 없어 판단 못 하면 None이고 status는 missing. 다 판단했으면 쓴 입력의 가장 나쁜 status(reported면 derived). flag `yield_trap`에도 그 status를 붙인다 |

| flag | 지표 | watch | alert |
| --- | --- | --- | --- |
| `eps_payout` | `eps_payout_forward`(없으면 `_quarter`) | 75% 이상 | 100% 이상 |
| `fcf_payout` | `fcf_payout_ytd` | 80% 이상 | 100% 이상 |
| `chowder` | `chowder_number` vs `chowder_threshold` | 기준 미만 | 없음 |
| `yield_trap` | 참인 신호 수 | 1개 | 2개 이상. 참이 없고 판단 못 한 신호가 있으면 review |

## diff (earnings mode)

`reit_calc.py diff --prev M1 --curr M2`. 같은 ticker, prev 분기 < curr 분기여야 한다(아니면 exit 1).

- 줄마다 `prev`·`curr`(값, status), `change` = curr − prev(단위가 `%`·`%p`면 %p), `change_pct` = 변화 / |prev| × 100(`%` 단위와 prev 0은 null), `status_change`(나쁜 순서 reported < derived < stale < estimated < missing으로 worse·better·same), `changed`(값이나 status가 다름).
- flag는 ok < watch < alert 순서로 worse·better, 한쪽이 review면 `review`. `worse.status`·`worse.flags`에 나빠진 것을 모은다.
- 한쪽이라도 값이 없는 줄이 있거나, prev·curr metrics에 check 실패가 있으면 exit 2. `summary`에 `checks_failed_prev`·`checks_failed_curr`.

## compare (compare mode)

`reit_calc.py compare --metrics M1 M2 ...`. 종목 둘 이상, ticker가 겹치지 않아야 한다(아니면 exit 1).

- 줄마다 종목별 값·status, `median`(값이 있는 종목이 둘 이상일 때 median, status는 가장 나쁜 것, reported면 derived). 이 표에 놓인 종목끼리의 median이고 REITWatch peer median과 다르다.
- 경고: REITWatch subsector가 다수(가장 많은 subsector가 하나이고 둘 이상)와 다른 종목, subsector를 모르는 종목, 다수가 없는데 subsector가 갈리는 경우, REIT와 일반 배당주를 섞은 경우, 분기가 다른 경우.
- 경고가 있거나 어떤 종목 metrics에 missing·check 실패가 있으면 exit 2.
- metrics 파일에 모르는 status가 있으면 missing으로 본다.

## REIT 경로에 더한 규칙 (S4)

- 연환산 EBITDAre가 missing이면 net debt/EBITDAre의 분모를 분기 EBITDAre × 4로 가정한다(estimated).
- 만기 사다리는 같은 `period` 줄을 더해 해마다 비중을 낸다.

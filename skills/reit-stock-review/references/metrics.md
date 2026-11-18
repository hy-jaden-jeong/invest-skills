# 지표 정의

`scripts/reit_calc.py`가 계산하는 지표의 뜻과 식이다. 식은 script와 이 파일이 같아야 한다. 하나를 고치면 다른 쪽도 고친다.

> 이 파일의 절차(FFO → AFFO → leverage → 배당 지속성 순서, 만기 집중도 표시, FFO 표를 줄마다 맞추는 품질 확인, 회사 정의 지표에 출처 표시)는 CaseMark의 `analyzing-reit-financials` skill(https://github.com/CaseMark/skills, Apache License 2.0)에서 가져왔다.
> 바꾼 점: NAV 추정과 투자 thesis·bull/bear 절은 뺐다. FFO 식을 Nareit 2018 정의로 적고 재계산이 회사 값과 0.5% 넘게 다르면 실패로 했다. 기준값은 `assets/thresholds.json` 한 곳으로 옮겼다. 결과에서 매수·매도 판단을 뺐다. 한국어로 다시 썼다.

## status

계산값은 입력 status를 물려받는다. 나쁜 순서는 `reported` < `derived` < `stale` < `estimated` < `missing`이다. 입력이 모두 reported면 계산값은 `derived`다. 입력이 하나라도 missing이면 계산값도 missing이고, 0이나 다른 값으로 채우지 않는다. 가정이 든 계산(분기×4 같은 run-rate)은 `estimated`로 두고 가정을 `note`에 쓴다.

## FFO와 AFFO

| 지표 | 식 | 메모 |
| --- | --- | --- |
| `ffo_nareit` | 보통주 귀속 순이익 + (D&A − 비부동산 감가) + 감가 부동산 impairment − 부동산 매각 이익 + 미연결 JV 조정 + 비지배지분(NCI) 조정 + 기타 Nareit 조정 | Nareit FFO White Paper(2018) 정의. 매각 손실이면 `gain_on_sale_re`가 음수다 |
| check `ffo_tie_out` | (재계산 − 회사 FFO) / 회사 FFO | 0.5%(`ffo_tie_out_tolerance_pct`)를 넘으면 실패, exit 2, 데이터 구멍. facts를 잘못 옮겼거나 회사 정의가 Nareit과 다르다는 뜻이다 |
| AFFO | 회사 정의를 따른다 | 표준이 없다. FFO에서 maintenance capex·leasing cost·straight-line rent·비현금 항목을 빼고 더하는 방식이 회사마다 다르다. 회사 reconciliation 줄을 그대로 `affo_bridge`에 옮긴다 |
| check `affo_bridge` | 시작 값(FFO 또는 Normalized FFO) + bridge 줄 합 vs 회사 AFFO | 0.5%를 넘으면 실패. 빠진 줄이나 부호 실수를 잡는다 |
| `affo_fy`, `ffo_fy` | 연간 주당 값 | AFFO: guidance 가운데 → 분기×4(estimated). FFO: guidance 가운데 → consensus FFO FY1(REITWatch, estimated) → 분기×4(estimated) |

## 배당 지속성

| 지표 | 식 | 메모 |
| --- | --- | --- |
| `forward_dividend` | 최근 정기 배당 × 연 지급 횟수 | 특별배당은 넣지 않는다. TTM 합으로 대신하지 않는다(kanchi-dividend-sop의 regular forward yield) |
| `forward_yield` | forward dividend / price × 100 | |
| `affo_payout_forward` | forward dividend / 연간 AFFO per share × 100 | flag `affo_payout`의 기준 |
| `affo_payout_quarter` | 분기 지급 배당 / 분기 AFFO per share × 100 | 회사가 보고서에 내는 payout과 맞춰 볼 수 있다 |
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

## 운영

| 지표 | 식 | 메모 |
| --- | --- | --- |
| `occupancy_pct`, `wale_years`, `investment_grade_share_pct` | 회사 발표값 그대로 | |
| `same_store_growth_pct` | 이번 기간 / 전년 같은 기간 − 1 | same store NOI가 없으면 회사가 내는 same store 지표(예: rental revenue)를 쓰고 `note`에 적는다 |
| `top1/5/10_tenant_pct` | 임대료(ABR) 비중 상위 임차인 합 | |
| `sector_kpis` | 회사 발표값 그대로 | 섹터별 지표는 `sector-kpis.md` |

## valuation

| 지표 | 식 | 메모 |
| --- | --- | --- |
| `market_cap` | price × (보통주 + OP unit) | OP unit은 보통주로 바꿀 수 있어 넣는다 |
| `p_ffo`, `p_affo` | price / 연간 주당 FFO·AFFO | 같은 섹터 안에서만 견준다. AFFO는 회사 정의가 달라 peer끼리 바로 견주지 않는다 |
| `affo_yield` | 연간 AFFO per share / price × 100 | |
| `implied_cap_rate` | 연환산 NOI / (시총 + net debt + 우선주) × 100 | NOI와 debt가 같은 기준(pro-rata 또는 consolidated)인지 `note`에 적는다 |
| `yield_spread_vs_10y`, `affo_yield_spread_vs_10y` | yield − DGS10 (%p) | DGS10은 macro-check snapshot. snapshot이 가격 기준일보다 7일 넘게 앞서면 stale |
| `p_ffo_reitwatch`, `p_ffo_history_pctile` | REITWatch P/FFO FY1, 자기 history 중 최신값 이하 비율 | 12달보다 짧으면 null. 같은 출처(REITWatch)끼리 견준다 |
| `peer_median_p_ffo`, `p_ffo_vs_peer` | 같은 subsector peer의 REITWatch P/FFO median, 비율 | reit-sector-scan의 parsed 파일을 읽기만 한다 |

## flag

`ok`, `watch`, `alert`, `review`. 데이터가 없으면 `review`이고 통과로 치지 않는다(kanchi-dividend-sop의 "silent PASS 금지"). 기준값은 `assets/thresholds.json`에 있고 모두 경험칙이다.

| flag | 지표 | watch | alert |
| --- | --- | --- | --- |
| `affo_payout` | `affo_payout_forward`(없으면 `affo_payout_quarter`) | 85% 이상 | 100% 이상 |
| `maturity_concentration` | `maturity_max_year_pct` | 15% 이상 | 20% 이상 |
| `net_debt_to_ebitdare` | `net_debt_to_ebitdare` | 6.0x 이상 | 7.0x 이상 |
| `interest_coverage` | `interest_coverage` | 3.0x 미만 | 1.5x 미만 |

# facts 형식

`$INVEST_DATA_DIR/tickers/<T>/facts/<분기>.json` 하나가 종목 한 분기의 입력이다. `edgar.py xbrl`이 초안(칸 목록 + XBRL 값)을 만들고, agent가 Ex.99에서 읽은 값을 채우고, `quote.py`가 `price`를 넣는다. `reit_calc.py metrics`는 이 파일만 읽어 계산한다.

```json
{
  "schema": "reit-facts/1",
  "ticker": "O", "period": "2026Q3", "period_end": "2026-09-30",
  "company": {"cik": "0000726728", "name": "...", "sic": "6798", "is_reit": true},
  "filing": {"form": "8-K", "accession": "0000726728-26-000056", "filed": "2026-11-02"},
  "facts": {
    "ffo_reported": {"value": 1041026, "unit": "USD thousands", "status": "reported", "as_of": "2026-09-30",
                     "source": "sec:8-K 0000726728-26-000056 ex99.1",
                     "locator": "o-991q32026.txt L306-307 FFO available to common stockholders $1,041,026", "note": ""}
  },
  "series": {"dividend_per_share_annual": [], "debt_maturities": [], "top_tenants": []},
  "affo_bridge": {"start": "normalized_ffo_reported", "items": [{"label": "...", "value": 21656, "unit": "USD thousands", "status": "reported", "source": "...", "locator": "..."}]},
  "sector_kpis": {"rent_recapture_rate_pct": {"value": 103.5, "unit": "%", "status": "reported", "source": "...", "locator": "..."}}
}
```

## 값 하나

- `value`: 유한한 숫자(NaN·Infinity는 쓰지 않는다). 공시 표의 부호를 그대로 옮기지 않고 아래 칸의 뜻에 맞춘다. 예: 표에 `(113,865)`로 빼는 매각 이익은 `gain_on_sale_re: 113865`(이익이 양수), `(20,705)`인 NCI 조정은 `nci_ffo_adjustments: -20705`(더하는 값의 부호).
- `unit`: 돈은 `USD`, `USD thousands`, `USD millions`, `USD billions`. 주식 수는 `shares`, `shares thousands`, `shares millions`. 주당 값 `USD/share`, 비율 `%`, 배수 `x`, 연수 `years`, 연 지급 횟수 `per year`. script가 돈은 thousands로, 주식 수는 thousands로 맞춘다. 맞지 않는 unit은 missing으로 내린다.
- `status`: `reported`(공시 원문), `estimated`(2차 자료나 가정), `stale`(기준일이 오래됨), `missing`. facts에 `derived`를 직접 쓰지 않는다. 계산은 script가 한다.
- `source`: `sec:8-K <accession> ex99.1`, `sec:xbrl <form> <accession>`, `web:<URL>`, `yahoo:chart`(quote.py price가 넣는 시세. `locator`는 URL과 기준일·종가).
- `locator`: 원문 위치. exhibit text 파일 이름과 줄 번호, 표 이름, 원문 숫자를 그대로 쓴다. status와 상관없이 값이 있는데 `source`나 `locator`가 비어 있으면 script가 쓰지 않고(missing) 데이터 구멍에 올린다.
- 회사 표에 그 줄이 없으면 `value: 0`, `status: reported`, `locator`에 표 이름, `note: "회사 표에 이 줄이 없음"`으로 둔다. 값을 못 찾은 것(`missing`)과 줄이 없는 것(0)은 다르다.
- 못 찾았으면 `{"value": null, "status": "missing", "note": "이유"}`로 둔다. 추측으로 채우지 않는다.
- 회사가 그 값을 원래 내지 않으면(그 칸이 있을 표에 줄이 없음) `not_disclosed: true`와 그 표의 `source`·`locator`를 함께 적는다. 그 값으로 셈하는 지표는 missing이지만 exit 2로 세지 않고 데이터 구멍에 "공시 없음"으로 오른다. 근거(`source`·`locator`)가 없거나 `value`와 함께 적으면 보통 missing으로 센다.

```json
"principal_amortization": {"value": null, "status": "missing", "not_disclosed": true,
  "source": "sec:8-K ... ex99.2", "locator": "...supple.txt L34 Debt Summary: 정기 원금 상환 줄 없음",
  "note": "회사가 정기 원금 상환을 내지 않음"}
```

- `basis`(선택): 돈 칸이 회사 표의 consolidated(연결) 열이면 `consolidated`, 지분 몫(pro-rata, Prologis Share 등) 열이면 `pro_rata`. 한 식의 입력끼리 basis가 다르면 script가 그 지표를 estimated로 내리고 note에 섞인 칸을 쓴다. 다른 값은 그 칸이 missing이다. 회사 표에서 기준을 알 수 있는 칸(부채 원금, 총자산, NOI, CIP, JV 부채, 부채 fair value, EBITDAre·이자비용 등)에 적는다.
- 돈·주식 수 칸이 회사 표의 여러 줄 합이고 회사가 합계 줄을 내지 않으면, `value` 대신 `parts`에 줄마다 값을 적는다. 줄 하나는 위 값 하나와 같은 규칙(status, 유한한 숫자, source·locator)이다. script가 단위를 맞춰 더한다. agent가 더하지 않는 것은 숫자를 script만 계산하기 때문이다. 빼는 줄은 음수로 적는다.

```json
"jv_debt_pro_rata": {"unit": "USD thousands", "note": "미연결 JV 부채의 지분 몫",
  "parts": [
    {"value": 233829, "unit": "USD thousands", "status": "reported", "as_of": "2026-09-30", "source": "sec:8-K ... ex99.2",
     "locator": "realtyincomeq32026supple.txt L24 (p.23) Cloud Capital Joint Venture 233,829"},
    {"value": 659190, "unit": "USD thousands", "status": "reported", "as_of": "2026-09-30", "source": "sec:8-K ... ex99.2",
     "locator": "realtyincomeq32026supple.txt L24 (p.23) Bellagio Las Vegas Joint Venture 659,190"}]}
```

  - 한 줄이라도 규칙을 어기면 그 칸은 missing과 데이터 구멍이고, 한 줄이 missing이면 그 칸도 missing이다. `parts`와 `value`를 함께 적어도 missing이다. `basis`는 칸에 한 번 적는다. `edgar.py xbrl`은 parts가 있는 칸을 덮지 않는다.

## 칸

분기 값은 그 분기 3개월(three months ended) 열에서 읽는다. 연간·누적 열을 쓰지 않는다.

| 칸 | 뜻 | 대개 어디에 |
| --- | --- | --- |
| `net_income_common` | 보통주 귀속 순이익 | FFO reconciliation 첫 줄, XBRL `NetIncomeLossAvailableToCommonStockholdersBasic` |
| `depreciation_amortization` | FFO 표의 D&A(부동산 D&A가 따로 있으면 그 값) | FFO 표, XBRL `DepreciationDepletionAndAmortization` |
| `non_real_estate_depreciation` | D&A 중 비부동산(FF&E, corporate) 감가. 양수로 적는다 | FFO 표 "Depreciation of furniture, fixtures and equipment" |
| `impairment_depreciable_re` | 감가 부동산 impairment | FFO 표 |
| `gain_on_sale_re` | 부동산 매각 이익(손실은 음수) | FFO 표 |
| `unconsolidated_jv_ffo_adjustments` | 미연결 JV 지분만큼의 FFO 조정(더하는 값의 부호) | FFO 표 |
| `nci_ffo_adjustments` | 비지배지분 몫의 FFO 조정(더하는 값의 부호) | FFO 표 |
| `other_nareit_adjustments` | 위에 없는 Nareit 조정(change of control 손익 등) | FFO 표 |
| `ffo_reported`, `normalized_ffo_reported`, `affo_reported` | 회사 발표 FFO·Normalized(Core) FFO·AFFO, 보통주 귀속 | FFO·AFFO 표 |
| `ffo_per_share_diluted`, `affo_per_share_diluted` | 분기 희석 주당 값 | 같은 표 |
| `diluted_shares_weighted` | 분기 가중평균 희석 주식 수. AFFO 총액과 같은 기준(OP unit 포함 여부)을 고른다. 같은 기준이 없으면 가장 가까운 값을 `estimated`와 기준 차이 note로 | per share 계산 표, XBRL `WeightedAverageNumberOfDilutedSharesOutstanding` |
| `ffo_guidance_low/high`, `affo_guidance_low/high` | 연간 guidance 범위 | Guidance 표. 내지 않으면 missing |
| `ffo_consensus_fy1` | consensus FFO FY1 | 비워 두면 REITWatch parsed에서 읽는다 |
| `dividend_regular_latest` | 최근 정기 배당 1회분 | 배당 공지, XBRL `DividendsPayableAmountPerShare` |
| `dividend_frequency` | 연 지급 횟수(월배당 12, 분기 4) | 보도자료 본문 |
| `dividends_paid_per_share_quarter` | 분기 동안 지급한 주당 배당 | 손익계산서 아래 줄 |
| `total_debt`, `cash` | 회사의 net debt 표에 쓰인 값 | supplemental net debt reconciliation |
| `preferred_equity` | 우선주 액면(없으면 0) | capitalization 표 |
| `fixed_rate_debt`, `debt_principal_total` | 고정금리 원금, 원금 합(같은 기준) | debt summary |
| `ebitdare_quarter`, `ebitdare_annualized` | 분기 adjusted EBITDAre, 연환산(pro forma) | EBITDAre 표 |
| `interest_expense` | 분기 이자비용 | 손익계산서 |
| `total_assets`, `accumulated_depreciation` | 기말 총자산, 부동산 감가상각 누계(양수). 합이 gross assets(감가상각 전 장부가)다 | balance sheet, XBRL `Assets`·`RealEstateInvestmentPropertyAccumulatedDepreciation` |
| `secured_debt` | 기말 담보부 부채(mortgage 등). gross assets와 같은 기준(대개 consolidated) | balance sheet "Mortgages payable", XBRL `SecuredDebt` |
| `unencumbered_assets`, `unencumbered_noi` | 담보로 잡히지 않은 자산(감가상각 전), 그 자산의 연환산 NOI(`noi_annualized`와 같은 기준). 회사가 내는 쪽만 채운다 | supplemental debt metrics·covenant 주석 |
| `capitalized_interest` | 분기 자본화 이자(개발 중 자산에 얹은 이자) | 개발 표 주석, 현금흐름표 주석, XBRL `InterestCostsCapitalized` |
| `preferred_dividends` | 분기 우선주 배당 | 손익계산서, XBRL `PreferredStockDividendsIncomeStatementImpact` |
| `principal_amortization` | 분기 정기 원금 상환(scheduled amortization. 만기 일시 상환은 넣지 않는다) | 회사 fixed charge coverage 표 |
| `wam_years`, `weighted_avg_rate_pct` | 부채 가중평균 잔여 만기, 가중평균 금리(swap 반영). 만기 사다리와 같은 기준 | debt summary |
| `floating_rate_debt` | 변동금리 원금(swap으로 고정한 부분은 뺀 회사 값). 비율만 내면 missing | debt summary |
| `revolver_available` | credit facility 남은 한도. commercial paper를 이미 뺀 값이면 note에 적는다 | liquidity 표 |
| `dev_remaining_spend` | 개발 중 자산에 앞으로 들어갈 돈(cost to complete, remaining investment). 0 이상 | 개발 표 |
| `liquidity_cash` | 유동성 계산용 현금·현금성 자산. net debt 표 때문에 `cash`가 0인 회사(D47)도 실제 현금을 적는다 | liquidity 표, balance sheet |
| `occupancy_pct`, `wale_years`, `investment_grade_share_pct` | 점유율, 가중평균 잔여 임대기간, 투자등급 임차인 비중 | 보도자료, supplemental |
| `same_store_current`, `same_store_prior` | same store NOI(없으면 회사의 same store 지표), 이번·전년 같은 기간 | supplemental |
| `noi_annualized` | 연환산 (cash) NOI | supplemental NOI 표 |
| `price` | 주가 | `quote.py`가 넣는다 |
| `common_shares_outstanding`, `op_units_outstanding` | 기말 보통주, OP unit | capitalization 표 |
| `construction_in_progress` | 건설 중 자산(CIP, 개발 portfolio 투자 잔액). NOI와 같은 기준(pro-rata면 지분 몫) | supplemental 개발 표, NAV components |
| `land_held` | 개발용 토지(land bank) 장부가. 운영 부동산의 토지(balance sheet Land)가 아니다. 없으면 0과 "회사 표에 이 줄이 없음" | land portfolio 표, NAV components |
| `other_non_operating_assets` | NOI에 그 수익이 들어 있지 않은 그 밖의 부동산·투자(매각 예정 자산, 다른 JV 투자 등). NOI가 그 수익(대출 이자, JV 몫)을 넣었으면 넣지 않는다. 현금은 net debt에서 빼므로 넣지 않는다 | NAV components, balance sheet |
| `jv_debt_pro_rata` | 미연결 JV 부채의 지분 몫 중 `total_debt`에 들어 있지 않은 것. 회사 net debt가 이미 pro-rata면 0과 note | JV 표, pro-rata debt 표 |
| `capex_reserve` | 회사가 낸 연간 capex reserve(유지 capex + TI·LC), 0 이상. 내지 않으면 missing으로 두면 script가 AFFO bridge 줄로 대신한다 | NAV components, 보도자료 |
| `debt_fair_value`, `debt_carrying_value` | 부채 fair value와 같은 표의 carrying value(대개 consolidated). 표에 없는 부채(revolver·CP)는 넣지 않는다 | 10-Q·10-K fair value 주석, XBRL `DebtInstrumentFairValue`·`LongTermDebt`. companyfacts에 그 분기가 없으면 10-Q 원문 |
| `acquisitions_cap_rate`, `dispositions_cap_rate` | 그 분기 매입·매각 cap rate(%). 회사 정의(initial cash yield, stabilized cap rate)를 note에 쓴다 | supplemental 투자·매각 표 |

## series

- `dividend_per_share_annual`: 해마다 정기 주당 배당 합(split 조정). `quote.py dividends T --facts F`가 먼저 넣는다(`source: yahoo:dividends`, `locator`는 URL과 그 해 정기 지급의 ex-date·금액). Yahoo가 실패하면 `edgar.py dividends --facts F`(XBRL 연간 값)로 대신한다. XBRL 연간 합은 reverse split 전후 값이 섞이고 특별·주식 배당이 든 해가 커져 그다음 해가 컷으로 보이므로 대조용이다. 항목마다 `period`(연도)가 있다.
  - 끝난 해만 넣는다(올해는 부분 합이라 CAGR·컷에 쓰지 않는다). Yahoo 값은 최근 끝난 15년만 넣는다(오래된 기록의 빈틈이 컷으로 읽힌다).
  - 다시 돌리면 해마다 합친다. 빈 해는 채우고, XBRL 값(`sec:xbrl`)과 전에 넣은 Yahoo 값은 Yahoo 값으로 바꾼다(다르면 `note`에 XBRL 값). Yahoo 값이 불확실한 해(아래 3)는 XBRL 값을 남기고 `note`에 Yahoo 값을 적는다. 그 밖의 출처(agent가 공시에서 고친 해)는 덮지 않는다. `edgar.py dividends`는 값과 `locator`가 있는 해를 덮지 않는다.
  - 정기·특별을 가르는 규칙(`quote.py`의 `classify`). 지급의 자리는 ex-date에 15일을 더한 날(지급일에 가깝다)의 달력 해와 월(월 배당)·분기(분기 배당)이고, 해도 그것으로 정한다. 주기는 최근 지급 간격 12개의 중앙값이다(45일 이하 월, 135일 이하 분기, 270일 이하 반기, 그 위 연).
    1. 금액이 앞뒤 지급보다 모두 20% 넘게 크거나 작고 다른 지급과 한 자리에 있으면 통째로 특별이다(정기 사이에 끼운 지급).
    2. 제 자리에 혼자 있고 앞뒤보다 20% 넘게 작으면 정기로 두되 불확실이다. 일부를 주식으로 준 배당은 Yahoo에 현금 부분만 보인다(GLPI 2020 0.60 중 현금 0.12).
    3. 제 자리에 혼자 있고 1·2를 건너뛴 앞뒤 정기보다 20% 넘게 크면 정기(앞뒤 중 큰 값)와 특별(나머지)로 나눈다. Yahoo가 정기와 특별을 한 건으로 준 것이다(GLPI 2023-03 0.97 = 0.72 + 0.25). 그 해는 `estimated`다.
    4. 이웃이 하나인 끝 지급은 1만 본다(혼자면 큰 인상인지 특별인지 모른다).
    5. ex-date + 15일로 정한 해가 한 해 주기−1회, 다음 해 주기+1회면 경계의 지급 하나를 ex-date의 해로 되돌린다(MPT 2008-12-19).
    6. 끝난 해의 정기 횟수가 주기와 다르면(상장·배당 시작 해, 중단) 그 해는 `estimated`이고 `note`에 횟수를 쓴다. 지급이 없는 끝난 해는 0이다. 상장한 해(첫 거래일이 1월 15일 뒤)는 넣지 않는다.
  - split: Yahoo 금액은 이미 split 조정돼 있다. 비율을 기약분수로 줄여 한쪽이 1이거나 둘 다 10 이하면(4:10, 2:1) 진짜 split이라 그대로 쓰고, 아니면(O 2021-11 1032:1000, spin-off) 가격 조정으로 보고 그 날 전 금액의 조정을 되돌린다(회사 공시 기준).
  - Yahoo 금액은 소수 셋째 자리로 반올림된 것이 있어(O 0.2685 → 0.269) 연간 합이 공시와 0.1%쯤 다를 수 있다.
- `special_dividends`: `quote.py dividends --facts`가 Yahoo 값으로 통째로 바꿔 넣는 특별배당 목록이다. 항목마다 `period`(해), `ex_date`, `value`(주당), `locator`(URL, ex-date, Yahoo 금액), `note`(왜 특별인지). `reit_calc.py`는 읽지 않는다(배당 CAGR·컷에 넣지 않는다).
- `debt_maturities`: 만기 사다리. `period`는 연도(`"2027"`)나 `thereafter`. 다른 글(날짜 등)이 하나라도 있으면 만기 사다리·24개월 만기가 missing이다. 같은 기준(pro-rata 또는 consolidated)의 원금 합 열을 쓴다.
- `top_tenants`: 임대료(ABR) 비중 상위 임차인. 항목마다 `label`(이름)과 `%` 값.
- `covenants`: 회사가 낸 debt covenant 표의 줄. `value` 대신 `limit`(한도)과 `actual`(실제)을 둔다. 한도가 여러 문서(indenture, credit facility)에 따로 있으면 줄마다 `name`에 어느 쪽인지 쓴다.

```json
{"name": "Limitation on incurrence of total debt", "limit": 60, "actual": 41.5, "direction": "max", "unit": "%",
 "status": "reported", "as_of": "2026-09-30", "source": "sec:8-K 0000726728-26-000056 ex99.2",
 "locator": "realtyincomeq32026supple.txt L36 (p.35) ... ≤ 60% of adjusted assets 41.5%", "note": ""}
```

  - `direction`: 실제가 한도 아래여야 하면(부채/자산 ≤ 60%) `max`, 위여야 하면(coverage ≥ 1.5x, unencumbered ≥ 150%) `min`.
  - `unit`: `%` 또는 `x`. `limit`은 양수, `actual`은 0 이상인 유한한 숫자다. 어기면 그 줄만 missing과 데이터 구멍이고 출력에는 null을 쓴다.
  - 회사가 covenant 표를 내지 않으면 빈 목록으로 둔다. 그러면 headroom은 missing, flag는 review다.

## affo_bridge

회사 AFFO reconciliation을 줄마다 옮긴다. `start`는 `ffo_reported`나 `normalized_ffo_reported`(표가 시작하는 값), `items`는 그 아래 줄들이다. 부호는 더하는 값 기준이다. 표의 소계 줄(예: "Cumulative adjustments")은 넣지 않는다.

줄마다 `category`를 단다. 회사 정의가 달라도 줄의 성격으로 견주기 위해서다.

| category | 줄 | 예 |
| --- | --- | --- |
| `straight_line` | straight-line rent(현금이 아닌 임대료) | Straight-line rent and expenses, net |
| `recurring_capex` | 유지 설비투자(maintenance capex) | Recurring capital expenditures, Property improvements |
| `leasing_cost` | 2nd generation TI·LC, turnover cost | Leasing costs and commissions, Turnover costs |
| `non_cash` | 그 밖의 비현금 항목 | 주식 보상, 부채 할인 상각, above/below-market lease 상각 |
| `other` | 위에 없는 것 | 일회성 비용, NCI·JV 조정, 우선주 배당 |

다섯 가지가 아닌 category가 하나라도 있으면 데이터 구멍에 오르고, category 지표(recurring capex/NOI, straight-line/FFO)와 capex reserve 대리값을 내지 않는다(그 줄을 빼고 나머지로 셈하면 틀린 값이 나온다). `recurring_capex`·`leasing_cost` 줄은 AFFO에서 빼는 줄이라 0 이하로 적는다. 양수면 부호를 뒤집어 옮긴 것으로 보고 같은 지표를 내지 않는다. category가 없는 줄은 bridge 합에만 들어간다.

## 일반 배당주 칸 (SIC가 6798이 아님)

`company.is_reit`가 false면(없으면 `company.sic`가 6798이 아니면) 칸 목록이 다르다. 목록은 `reit_calc.py`의 `DIVIDEND_FACT_KINDS` 한 곳에 있고 `edgar.py xbrl`이 그것으로 초안을 만든다. `affo_bridge`·`sector_kpis`는 없고 series는 `dividend_per_share_annual`만 쓴다.

| 칸 | 뜻 | 대개 어디에 |
| --- | --- | --- |
| `net_income_common` | 분기 순이익(보통주 귀속, 없으면 순이익) | XBRL `NetIncomeLossAvailableToCommonStockholdersBasic` → `NetIncomeLoss` |
| `eps_diluted_quarter` | 분기 GAAP diluted EPS | XBRL `EarningsPerShareDiluted` |
| `eps_guidance_low`, `eps_guidance_high` | 연간 EPS guidance. adjusted(non-GAAP)면 `note`에 쓴다 | 보도자료 guidance 표 |
| `dividend_regular_latest`, `dividend_frequency` | 최근 정기 배당 1회분, 연 지급 횟수 | 배당 공지(회사 IR). XBRL 초안은 분기 주당 배당 |
| `dividends_declared_per_share_quarter` | 분기 선언(없으면 지급) 주당 배당 | XBRL `CommonStockDividendsPerShareDeclared` → `CashPaid` |
| `operating_cash_flow_ytd` | 영업활동 현금흐름, 회계연도 누적 | XBRL `NetCashProvidedByUsedInOperatingActivities` |
| `capex_ytd` | 설비투자, 회계연도 누적(양수) | XBRL `PaymentsToAcquirePropertyPlantAndEquipment` |
| `dividends_paid_ytd` | 보통주 배당 지급, 회계연도 누적(양수) | XBRL `PaymentsOfDividendsCommonStock` → `PaymentsOfOrdinaryDividends` → `PaymentsOfDividends` |
| `price` | 주가 | `quote.py`가 넣는다 |

- XBRL 분기 값은 끝날이 달력 분기 끝날에서 7일 안인 관측을 고른다(52·53주 회계연도). 누적(`_ytd`) 칸은 그 끝날의 380일 안 기간 중 가장 긴 것이다. 새 초안의 `period_end`는 XBRL 관측의 끝날이다.
- 연간 배당 history는 REIT와 같이 `quote.py dividends`가 먼저다. `edgar.py dividends`(대조용)의 해는 회계연도 기간 가운데 날의 해다(끝날이 다음 해 1월 초인 53주 해 때문). `Declared` 마지막 해 값이 `CashPaid`의 절반도 안 되면 `CashPaid`를 쓴다(다른 뜻으로 단 tag).

## facts 밖 출력 파일

facts가 아니라 보고서(`report.py`)가 읽는 파일이다. 값 하나는 위 "값 하나"와 같은 `{value, unit, status, as_of, source, locator, note}`다(`status`에 script가 푼 값이면 `derived`).

- `$INVEST_DATA_DIR/tickers/<T>/dividends.json`(`quote.py dividends`, schema `reit-dividends/1`): `frequency`(`name` monthly·quarterly·semiannual·annual, `per_year`, `median_gap_days`), `splits`(`date`, `ratio`, `kind` split·adjustment), `payments`(지급마다 `ex_date`, `year`, `amount`, `yahoo_amount`, `kind` regular·special, `uncertain`, `note`. 정기 + 특별로 나눈 지급은 같은 ex-date로 두 줄), `annual`(상장 뒤 모든 해: `period`, `regular`, `special`, `regular_count`, `complete`, `uncertain`, `status`, `note`), `series`(facts에 넣는 두 series).
- `$INVEST_DATA_DIR/tickers/<T>/trend/<P>.json`(`edgar.py trend`, schema `reit-trend/1`): `quarters`(오래된 분기부터 8개), `metrics`(칸마다 `label`, `concept`, `unit`, `kind`, `count`, `values`(분기마다 값 하나, 없으면 missing), `note`), `dropped`(값이 4분기보다 적어 뺀 지표와 이유). 칸은 `revenue`, `interest_expense`, `debt`, `diluted_shares`, `dividend_per_share`다.
  - 지표마다 분기 값이 가장 많은 concept 하나를 쓴다(분기마다 concept을 섞지 않는다). 같은 기간 값이 여럿이면 늦게 낸 값이다.
  - 3개월 값이 없는 손익 항목(10-K의 4분기 등)은 같은 시작일의 누적 차이로 푼다(연간 − 9개월, 6개월 − 3개월, `derived`). 희석 주식 수는 가중평균이라 풀지 않는다.
  - 부채는 합계 concept(`LongTermDebt`, `DebtInstrumentCarryingAmount` 등)이 없으면 마지막 분기에 값이 있는 구성 concept(notes, secured, term loan, line of credit, commercial paper)을 무리마다 하나씩 더한다. 하나라도 없는 분기는 missing이다. carrying value라 Ex.99 total debt(원금)와 다를 수 있다.
  - 이자비용을 순이자 concept(`InterestIncomeExpenseNonoperatingNet`, 음수)으로만 단 회사는 부호를 바꿔 쓰고 `note`에 적는다.

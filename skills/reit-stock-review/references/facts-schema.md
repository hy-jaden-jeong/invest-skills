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

- `value`: 숫자. 공시 표의 부호를 그대로 옮기지 않고 아래 칸의 뜻에 맞춘다. 예: 표에 `(113,865)`로 빼는 매각 이익은 `gain_on_sale_re: 113865`(이익이 양수), `(20,705)`인 NCI 조정은 `nci_ffo_adjustments: -20705`(더하는 값의 부호).
- `unit`: 돈은 `USD`, `USD thousands`, `USD millions`, `USD billions`. 주식 수는 `shares`, `shares thousands`, `shares millions`. 주당 값 `USD/share`, 비율 `%`, 배수 `x`, 연수 `years`, 연 지급 횟수 `per year`. script가 돈은 thousands로, 주식 수는 thousands로 맞춘다. 맞지 않는 unit은 missing으로 내린다.
- `status`: `reported`(공시 원문), `estimated`(2차 자료나 가정), `stale`(기준일이 오래됨), `missing`. facts에 `derived`를 직접 쓰지 않는다. 계산은 script가 한다.
- `source`: `sec:8-K <accession> ex99.1`, `sec:xbrl <form> <accession>`, `web:<URL>`, `finnhub:/quote`.
- `locator`: 원문 위치. exhibit text 파일 이름과 줄 번호, 표 이름, 원문 숫자를 그대로 쓴다. `reported`·`estimated`·`stale`인데 `source`나 `locator`가 비어 있으면 script가 쓰지 않고(missing) 데이터 구멍에 올린다.
- 회사 표에 그 줄이 없으면 `value: 0`, `status: reported`, `locator`에 표 이름, `note: "회사 표에 이 줄이 없음"`으로 둔다. 값을 못 찾은 것(`missing`)과 줄이 없는 것(0)은 다르다.
- 못 찾았으면 `{"value": null, "status": "missing", "note": "이유"}`로 둔다. 추측으로 채우지 않는다.

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
| `occupancy_pct`, `wale_years`, `investment_grade_share_pct` | 점유율, 가중평균 잔여 임대기간, 투자등급 임차인 비중 | 보도자료, supplemental |
| `same_store_current`, `same_store_prior` | same store NOI(없으면 회사의 same store 지표), 이번·전년 같은 기간 | supplemental |
| `noi_annualized` | 연환산 (cash) NOI | supplemental NOI 표 |
| `price` | 주가 | `quote.py`가 넣는다 |
| `common_shares_outstanding`, `op_units_outstanding` | 기말 보통주, OP unit | capitalization 표 |

## series

- `dividend_per_share_annual`: `edgar.py dividends --facts F`가 넣는다. 항목마다 `period`(연도)가 있다.
- `debt_maturities`: 만기 사다리. `period`는 연도나 `thereafter`. 같은 기준(pro-rata 또는 consolidated)의 원금 합 열을 쓴다.
- `top_tenants`: 임대료(ABR) 비중 상위 임차인. 항목마다 `label`(이름)과 `%` 값.

## affo_bridge

회사 AFFO reconciliation을 줄마다 옮긴다. `start`는 `ffo_reported`나 `normalized_ffo_reported`(표가 시작하는 값), `items`는 그 아래 줄들이다. 부호는 더하는 값 기준이다. 표의 소계 줄(예: "Cumulative adjustments")은 넣지 않는다.

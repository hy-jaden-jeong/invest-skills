# 섹터별 운영지표

facts의 `sector_kpis`에 넣을 값이다. 회사가 supplemental에 내는 이름과 정의가 조금씩 달라, 이름은 아래 key로 맞추고 회사 정의는 `note`에 적는다. 값은 회사 발표값 그대로 두고 script는 다시 계산하지 않는다.

| 섹터 | key 예 | 무엇을 보나 |
| --- | --- | --- |
| industrial(물류) | `re_leasing_spread_pct`, `same_store_noi_growth_pct` | 재계약 임대료 상승폭. 공급이 늘면 먼저 줄어든다 |
| office | `occupancy_pct`, `leased_pct`, `ti_per_sf` | 공실과 재계약 때 TI(임차인 공사비) 부담 |
| retail | `tenant_sales_per_sf`, `occupancy_cost_ratio_pct` | 임차인 매출 대비 임대료 부담 |
| residential(주거) | `blended_lease_rate_growth_pct`, `turnover_pct` | 신규·갱신 임대료 변화, 이사 나가는 비율 |
| data_center | `mw_leased`, `backlog_usd` | 계약한 전력 용량, 아직 매출이 안 된 계약 |
| healthcare | `ebitdarm_coverage_x`, `shop_occupancy_pct` | 운영사가 임대료를 낼 여력(NNN), 직영(SHOP) 점유율 |
| net_lease | `rent_recapture_rate_pct`, `investment_cash_yield_pct`, `ig_tenant_pct` | 재임대 때 옛 임대료 회수율, 새 투자 cap rate(자본비용과의 spread를 본다), 투자등급 임차인 비중 |
| lodging(호텔) | `revpar_growth_pct` | 객실당 매출 |
| self_storage | `same_store_occupancy_pct`, `realized_rent_per_sf_growth_pct` | 점유율, 실제 받은 임대료 변화 |

출처: 공개 skill 조사(2026-11-18)와 Nareit·회사 supplemental의 관행. 섹터 대응은 경험적 판단이라 보고서에 "대리 지표"로 쓴다.

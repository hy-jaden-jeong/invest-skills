---
name: reit-sector-scan
description: Nareit REITWatch(매달 나오는 PDF)를 받아 미국 REIT subsector마다 median P/FFO·배당수익률과 그 자기 history 대비 percentile·z-score, 배당수익률−10년물 spread, 3·5년 상대 성과를 표로 내고, macro-check snapshot의 섹터 수요·공급 지표를 함께 놓아 구조적 하락 신호가 있는 섹터에 "싸 보이는 이유" 메모를 쓴다. 섹터를 고르면 그 섹터 종목의 screening 표(P/FFO, 배당수익률, 시총, debt ratio)를 낸다. "어느 섹터가 싸", "저평가 섹터 찾아줘", "리츠 섹터 valuation 봐줘", "net lease 종목 비교 표 줘" 요청에 쓴다. 매크로 지표 수집은 macro-check, 종목 하나의 공시 분석은 reit-stock-review가 맡는다. 매수·매도 판단은 내리지 않는다.
---

# REIT 섹터 스캔

REITWatch의 종목 row로 subsector valuation을 다시 세고, 자기 history와 금리, 섹터 매크로 지표에 비춰 본다. 숫자는 script가 계산하고 agent는 읽고 해석만 한다. 같은 입력이면 같은 숫자가 나와야 하기 때문이다.

`<skill 폴더>`는 이 SKILL.md가 있는 폴더다. 데이터 폴더는 `$INVEST_DATA_DIR`(없으면 `~/invest-data`)이고 레포 밖에 있다. REITWatch는 Nareit 저작물이라 PDF와 parsed JSON을 레포에 넣지 않는다.

외부 도구는 `pdftotext`(poppler) 하나다. 없으면 사용자에게 설치를 부탁한다(macOS는 `brew install poppler`).

## 차례

1. **받기**: 최신 호를 받는다. `$INVEST_DATA_DIR/reitwatch/parsed/`에 파일이 8개 미만이면 처음 도는 것이니 `--backfill`을 붙인다.
   ```bash
   python3 <skill 폴더>/scripts/reitwatch.py fetch --month latest            # 최신 호만
   python3 <skill 폴더>/scripts/reitwatch.py fetch --month latest --backfill # 처음: 2015-01부터
   ```
   - backfill은 2015-01부터 1·4·7·10월 호(가격 기준일이 분기말)와 최근 12개월을 받는다. 이미 받은 호는 건너뛴다. 50개 남짓이라 몇 분 걸린다. 시작 전에 사용자에게 알린다.
   - PDF는 파싱한 뒤 바로 지운다. 남는 것은 parsed JSON과 `reitwatch/history.csv`다.
   - 10월 호의 가격은 9월 말 값이다. 보고서에 호(`month`)와 가격 기준일(`as_of`)을 함께 쓴다.
2. **macro snapshot 확인**: `sectors`가 읽을 macro-check snapshot이 오늘 것인지, 보고 싶은 섹터가 들어 있는지 본다(아래 "macro snapshot"). 모자라면 갱신한다.
3. **섹터 표**: `sectors`를 돌린다. stdout(또는 `--out` 파일)에 JSON이, stderr에 요약 표가 나온다.
   ```bash
   python3 <skill 폴더>/scripts/reitwatch.py sectors --out "$INVEST_DATA_DIR/sectors/<날짜>.json"
   ```
4. **구조적 하락 메모**: `structural.flag`가 `watch`인 subsector마다 아래 "구조적 하락 신호"대로 메모를 쓴다.
5. **종목 표**: 사용자가 섹터를 고르거나 종목 하나의 peer를 물으면 `peers`를 돌린다.
   ```bash
   python3 <skill 폴더>/scripts/reitwatch.py peers --subsector "Free Standing"
   python3 <skill 폴더>/scripts/reitwatch.py peers --ticker O
   ```
6. **보고서**: 아래 "보고서"의 모양으로 `$INVEST_DATA_DIR/sectors/reports/<날짜>.md`를 쓰고 사용자에게 요약을 말한다.

사용자의 말을 subsector 이름으로 바꾼다. 트리플넷·net lease는 `Free Standing`, 물류·창고는 `Industrial`, 아파트는 `Apartments`, 쇼핑몰은 `Regional Malls`, 호텔은 `Lodging/Resorts`, 데이터센터는 `Data Centers`, 헬스케어는 `Health Care`다. 이름은 `peers`가 틀린 이름에 내는 목록에서 고른다.

exit code: 0이면 표의 칸이 다 찼다. 2면 계산은 했지만 missing 칸이 있다(history가 짧음, macro snapshot 없음, backfill 중 못 받은 호). 1이면 입력이 틀렸거나 받은 것이 없다. 2여도 표를 쓰고, 빈 칸과 이유(`note`)를 "데이터 구멍"에 적는다.

## 섹터 표의 값

`sectors`는 가장 최근 parsed 호로 표를 만든다. 칸마다 값의 형식(`value`, `unit`, `status`, `as_of`, `source`, `locator`, `note`)이 붙는다.

- `p_ffo_median`·`div_yield_median`: 그 subsector 종목 row의 P/FFO(올해 consensus FFO 기준)와 배당수익률의 median. PDF의 AVERAGE row는 쓰지 않는다. outlier에 약하기 때문이다(2018-12 Free Standing 평균 27.46, median 18.37).
- `*_pctile`: history 표본(같은 subsector의 1·4·7·10월 호 + 최신 호) 중 최신값 이하 비율(%). P/FFO는 낮을수록, 배당수익률은 높을수록 과거보다 싸다는 쪽이다. 최근 12개월 호는 표본에 넣지 않는다. 넣으면 최근 1년의 무게가 네 배가 된다.
- `*_z`: (최신값 − 표본 평균) / 표본 표준편차. 표본이 8개 미만이면 percentile과 z-score는 missing이다.
- `spread_dgs10`: median 배당수익률 − macro snapshot의 DGS10(%p). REITWatch 가격 기준일과 DGS10 기준일이 다르고, 두 날짜가 `note`에 있다. snapshot이 7일보다 오래됐으면 `stale`이다.
- `rel_tr_3y`·`rel_tr_5y`: subsector 종목의 3·5년 total return median − 같은 호 equity 종목 전체 median(%p). 지금 상장된 종목만 들어 망한 종목이 빠진다(survivorship).
- `history_points`: 표본 수. `history_months`에 표본 호 목록이 있다.
- mortgage subsector(Home Financing, Commercial Financing)는 REITWatch에 FFO가 없어 표에서 빼고 `excluded`에 둔다. `peers`로는 볼 수 있다.
- subsector 이름은 호마다 바뀔 수 있다(2015년 `Timber`·`Mixed`). history는 같은 이름끼리만 잇는다. 이름이 바뀐 subsector는 `history_points`가 작다.

## macro snapshot

`sectors`는 macro-check의 snapshot을 읽기만 한다. 기본은 `$INVEST_DATA_DIR/macro/snapshots/`의 가장 최근 파일이고, `--macro-snapshot P`로 바꿀 수 있다.

1. 표의 `macro_sectors_needed`나 stderr의 "macro snapshot에 없는 섹터"를 본다. snapshot이 없거나 오늘 것이 아니어도 갱신 대상이다.
2. macro-check가 같은 skills 폴더에 있으면 섹터마다 받는다. 같은 날 snapshot 하나에 합쳐진다.
   ```bash
   python3 <skills 폴더>/macro-check/scripts/macro.py fetch --sector office
   ```
3. macro-check가 없거나 exit 1이면 그 칸을 missing으로 두고 계속한다. spread는 missing, 구조 신호는 `review`가 된다.
4. 갱신했으면 `sectors`를 다시 돌린다.

REITWatch subsector와 macro-check 섹터의 대응, 섹터 지표마다의 demand·supply 구분과 좋은 방향은 `<skill 폴더>/assets/sector_map.json`에 있다. 대응은 경험적 판단이라 보고서에 "대리 지표"라고 쓴다.

## 구조적 하락 신호

싸 보이는 섹터가 실제로는 수요가 줄고 공급이 느는 중일 수 있다. script가 `structural`에 판정을 내고, agent가 메모를 쓴다.

- script의 판정(`structural.indicators`의 `judged`): demand 지표는 3개월·12개월 변화가 둘 다 나쁜 방향이면 "나빠짐", supply 지표는 둘 다 양수면 "늘어남"이다. 값이 있는 demand 지표의 절반 이상이 나빠지고 supply가 하나라도 늘면 `watch`, 지표가 없으면 `review`, 아니면 `ok`다. `context` 지표는 표에만 놓는다.
- `watch`인 subsector마다 메모를 쓴다.
  1. 나빠진 demand 지표와 늘어난 supply 지표를 값·3·12개월 변화·기준일과 함께 옮긴다. snapshot 숫자를 그대로 쓴다.
  2. valuation 칸과 나란히 놓는다. 예: "P/FFO median이 history 하위 20%지만, 같은 기간 수요 지표 둘이 3·12개월 모두 줄었고 건설 지출은 12개월에 +33% 늘었다."
  3. "싸 보이는 이유"를 가능성으로 쓴다. 수요 감소가 일시적인지 구조적인지(예: 원격 근무, 온라인 쇼핑 비중)를 가르는 근거를 하나 이상 찾고, 웹에서 찾았으면 출처 URL과 원문 문장을 단다. 못 찾았으면 "근거를 찾지 못함"이라고 쓴다.
  4. 다시 볼 조건을 한 줄 쓴다. 예: "office 고용 3개월 변화가 플러스로 돌아서면".
- `ok`여도 percentile이 극단(10 이하나 90 이상)인 subsector에는 지표 표를 짧게 붙인다. `review`는 왜 판단 못 했는지(`note`)를 "데이터 구멍"에 쓴다.

## 종목 표 (peers)

`peers`는 가장 최근 호에서 한 subsector의 종목을 P/FFO 오름차순으로 낸다(값이 없는 종목은 끝). 칸: price, P/FFO(올해·내년), 배당수익률, 시총(백만 달러), debt ratio(%), FFO 성장률, 1·3·5년 total return, 신용등급. `median`에 subsector median이 있다. `--ticker`면 그 종목의 subsector를 낸다.

screening 표는 깊은 분석의 후보를 고르는 재료다. 종목 하나를 깊게 보려면 reit-stock-review로 넘긴다.

## 보고서

`$INVEST_DATA_DIR/sectors/reports/<날짜>.md`에 쓴다. 숫자는 JSON에서 그대로 옮긴다. 다시 계산하거나 반올림을 바꾸지 않는다.

```md
# REIT 섹터 스캔 <날짜>

REITWatch <month> 호(가격 기준일 <as_of>, 출처 <source>), macro snapshot <경로 또는 없음>. 판단 재료일 뿐 매수·매도 권유가 아니다.

## 요약
<3~5줄. 자기 history 대비 가장 싼·비싼 subsector, 10년물 대비 spread, 구조 신호 watch>

## subsector 표
| subsector | 종목 수 | P/FFO median | percentile | z | 배당수익률 median | percentile | z | spread vs 10년물 | 3년 상대 | 5년 상대 | 구조 신호 |

## 구조적 하락 메모
<watch인 subsector마다 위 절차의 메모>

## 종목 표: <subsector>
<요청했을 때만. peers 표>

## 데이터 구멍
<missing·stale 칸과 이유, history가 짧은 subsector, 이름이 바뀐 subsector, 못 받은 호, excluded, 웹에서 찾은 근거의 출처와 status>
```

## 해석할 때

- percentile이 낮다는 것은 과거보다 싸다는 뜻이지 오른다는 뜻이 아니다. 싼 데는 이유가 있을 수 있어 구조 신호와 함께 읽는다.
- consensus FFO는 애널리스트 추정이다. 추정이 내려가면 P/FFO가 낮아도 싸지 않을 수 있다.
- 사라·팔아라, 비중을 늘려라·줄여라, 목표가, 매매 시점을 쓰지 않는다. 결과는 판단 재료이고 결정은 사용자가 한다.
- stale·missing·estimated 값으로 한 해석에는 그 status를 문장에 붙인다.

## 다른 skill이 읽을 때

reit-stock-review 등은 parsed 파일을 읽기만 한다. 고치지 않는다.

- 가장 최근 파일: `ls "${INVEST_DATA_DIR:-$HOME/invest-data}"/reitwatch/parsed/*.json | tail -1`
- 첫 칸이 `"schema": "reitwatch-parsed/1"`인지 본다. `month`(호), `as_of`(가격 기준일), `source`(PDF URL), `fy1`·`fy2`(FFO 추정 연도), `rows`가 있다.
- row 칸: `ticker`, `name`, `subsector`, `price`, `ffo_fy1`, `ffo_fy2`, `p_ffo_fy1`, `p_ffo_fy2`, `div_yield`, `mkt_cap`, `debt_ratio`, `hi52`, `lo52`, `ffo_growth`, `implied_mkt_cap`, `tr_1y`, `tr_3y`, `tr_5y`, `rating`. PDF에 값이 없으면 null이다. 단위: 가격·FFO는 달러(주당), P/FFO는 배수, 배당수익률·debt ratio·성장률·수익률은 %, 시총은 백만 달러.
- ticker로 subsector·consensus FFO·peer를 찾는다. 파일이 없거나 오래됐으면(최신 호가 두 달 넘게 전) 이 skill이 설치돼 있을 때 `fetch --month latest`를 돌리고, 아니면 그 칸을 missing으로 둔다.

## 고친 뒤 확인

script나 `assets/sector_map.json`을 고치면 돌린다. network 없이 `tests/fixtures`(지어낸 값으로 REITWatch layout을 흉내 낸 텍스트, 지어낸 history, 줄인 macro snapshot)로 돌고, 기대값은 `tests/expected.json`에 손으로 셈한 값이다.

```bash
python3 <skill 폴더>/scripts/reitwatch.py --check   # exit 0이면 통과
```

REITWatch layout이 바뀌면 `fetch`가 "종목 row가 N개뿐" 파싱 실패(exit 1)를 내거나 `unparsed`가 늘어난다. 그때는 받은 PDF를 `pdftotext -layout`으로 풀어 종목 표 머리줄과 row 모양을 보고 `parse_text`를 고친다. fixture에는 실제 row를 옮기지 않고 지어낸 값만 쓴다.

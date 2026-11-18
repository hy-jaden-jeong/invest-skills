---
name: macro-check
description: 미국 매크로 지표(금리, credit spread, 물가, 고용, 성장, 주택, 건설)를 FRED에서 받아 지표마다 최신값, 1·3·12개월 변화, YoY, 10년 percentile을 담은 snapshot JSON을 쓰고, 그것을 읽어 매크로 보고서를 쓴다. 섹터 이름(office, industrial, retail, residential, healthcare, data_center, lodging, self_storage, net_lease)을 주면 그 섹터의 수요·공급 지표를 함께 받는다. FRED에 없는 값(ISM PMI, FedWatch)은 출처 URL과 원문 문장을 달아 웹에서 채운다. "매크로 점검해줘", "금리·고용 요즘 어때", "오피스 섹터 매크로 봐줘" 요청에 쓴다. 다른 skill(reit-sector-scan 등)은 이 skill의 snapshot을 읽기만 한다. 섹터 밸류에이션과 종목 분석은 하지 않는다. 매수·매도 판단은 내리지 않는다.
---

# 매크로 점검

미국 매크로 지표를 받아 snapshot을 쓰고, snapshot을 읽어 보고서를 쓴다. 숫자는 script가 계산하고 agent는 읽고 해석만 한다. 같은 입력이면 같은 숫자가 나와야 하고, agent가 손으로 셈한 숫자는 틀려도 가릴 방법이 없기 때문이다.

`<skill 폴더>`는 이 SKILL.md가 있는 폴더다. 데이터 폴더는 `$INVEST_DATA_DIR`(없으면 `~/invest-data`)이고 레포 밖에 있다.

## 차례

1. **받기**: 요청에 맞게 `fetch`를 돌린다.
   - 매크로 전체: `python3 <skill 폴더>/scripts/macro.py fetch`
   - 섹터를 말했으면: `python3 <skill 폴더>/scripts/macro.py fetch --sector office`
   - 한 group만 물었으면(예: "금리만"): `--group rates`. group은 `rates`, `credit`, `inflation`, `labor`, `growth`, `housing`, `construction`이다.
   - 사용자의 말을 섹터 이름으로 바꾼다. 물류·창고는 `industrial`, 아파트·주거는 `residential`, 호텔은 `lodging`, 트리플넷은 `net_lease`다. 목록에 없는 섹터면 사용자에게 가까운 것을 묻는다.
2. **snapshot 읽기**: stdout에 찍힌 경로(`$INVEST_DATA_DIR/macro/snapshots/<날짜>.json`)를 읽는다. stderr에 missing·stale 목록이 나온다.
3. **웹 채우기**: snapshot의 `web` 항목(`status: missing`, `note: 웹 채우기 전`)을 아래 "웹 채우기"대로 채운다.
4. **보고서 쓰기**: 아래 "보고서"의 모양으로 `$INVEST_DATA_DIR/macro/reports/<날짜>.md`를 쓰고, 사용자에게 요약을 말한다.

exit code: 0이면 FRED series를 다 받았다. 2면 일부 series가 missing이다(받기 실패, 이유는 `note`). 1이면 입력이 틀렸거나 network가 끊겨 아무것도 받지 못했다. 2여도 snapshot은 쓰였으니 보고서를 쓰고 빠진 것을 "데이터 구멍"에 적는다. 웹 채우기 전 항목은 exit code에 들어가지 않는다.

`FRED_API_KEY` 환경 변수가 있으면 FRED 공식 API로, 없으면 key가 필요 없는 `fredgraph.csv`로 받는다. 받은 CSV는 `$INVEST_DATA_DIR/macro/cache/<ID>.csv`에 두고 하루 안이면 다시 받지 않는다. network가 실패하면 하루 넘은 cache라도 쓰고 `note`에 그렇게 적는다.

## snapshot

```json
{
  "schema": "macro-snapshot/1",
  "date": "2026-11-18",
  "series": {
    "DGS10": {
      "name": "Market Yield on U.S. Treasury Securities at 10-Year Constant Maturity, ...",
      "group": "rates", "sectors": ["net_lease"], "freq": "daily",
      "value": 5.12, "unit": "Percent", "status": "reported", "as_of": "2026-11-16",
      "source": "fred:DGS10", "locator": "https://fred.stlouisfed.org/series/DGS10 observation_date=2026-11-16", "note": "",
      "derived": {"status": "derived", "chg_1m": 0.03, "chg_3m": 0.44, "chg_12m": 0.98, "yoy_pct": null,
                  "pctile_10y": 99.3195, "ref": {"1m": "2026-10-16", "3m": "2026-08-14", "12m": "2025-11-14"},
                  "note": "yoy_pct: 단위가 Percent라 계산하지 않음(chg_12m을 %p로 본다)"}
    }
  },
  "web": {"ISM_MFG_PMI": {"value": 55.5, "status": "reported", "as_of": "2026-10-01", "source": "web:https://...", "locator": "원문 문장", "note": "1차 자료"}},
  "sectors": {"office": ["USPBS", "USINFO", "USFIRE", "TLOFCONS"]},
  "summary": {"missing": [], "stale": []}
}
```

- `status`: `reported`(FRED 원문 값), `stale`(최신 관측이 `max_lag_days`를 넘음), `missing`(값 없음, `value`는 null, 이유는 `note`). 웹 값은 `estimated`도 있다.
- `as_of`는 FRED의 관측일이다. 월간 자료는 그 달 1일, 분기 자료는 분기 첫날이다. 2026-10-01이면 10월 값이다.
- `derived`는 script가 계산한 값이다. `status`는 원래 값의 status를 물려받는다(reported면 derived, stale이면 stale, missing이면 missing).
  - `chg_1m`·`chg_3m`·`chg_12m`: 최신값 − 기준 시점 값, 단위는 원래 단위(Percent면 %p). 기준일은 `ref`에 있다. 일·주 자료는 목표일 앞 7일 안의 마지막 관측, 월·분기 자료는 같은 달 관측만 쓴다. 기준 달이 빠졌으면(2025-10 CPI처럼) null이다. 옆 달로 메우면 틀린 변화가 나오기 때문이다. 분기 자료는 `chg_1m`이 없다.
  - `yoy_pct`: (최신값 / 12개월 전 값 − 1) × 100. 단위가 Percent인 series(금리, spread, 실업률)는 계산하지 않고 `chg_12m`을 본다.
  - `pctile_10y`: 최신일 10년 전 초과부터 최신일까지 관측 중 최신값 이하인 비율(%). 100이면 10년 중 가장 높다. history가 10년보다 짧으면 null이다.
- `sectors`의 id는 `series`나 `web` 중 한쪽에 있다.
- 같은 날 다시 받으면 새로 받은 series만 바뀌고 다른 섹터와 웹 값은 남는다. 그래서 하루에 snapshot 파일은 하나다.

series 목록과 섹터 대응은 `<skill 폴더>/assets/series.json`에 있다. 섹터 대응은 경험적 판단이라 보고서에 "대리 지표"라고 쓴다.

## 웹 채우기

`source: web` 항목(ISM Manufacturing PMI, ISM Services PMI, CME FedWatch)은 script가 받지 못한다. 웹 검색 도구가 있으면 쓰고, 없으면 항목마다 missing으로 둔다.

1. 1차 자료를 먼저 찾는다. 발표 기관 자신의 page나 PDF(ISM의 ismworld.org Report On Business, 그 기관 이름으로 나간 보도자료, CME FedWatch page)다.
2. 원문에서 값이 든 문장을 그대로 옮긴다. 요약하거나 고쳐 쓰지 않는다.
3. 1차 자료를 찾지 못했을 때만 기사·블로그 같은 2차 자료를 쓰고 `--secondary`를 붙인다.
4. `web` 명령으로 넣는다. URL이나 원문 문장이 없으면 script가 거절한다. 출처 없는 숫자가 들어가지 않게 하려는 것이다.

```bash
python3 <skill 폴더>/scripts/macro.py web ISM_MFG_PMI --value 55.5 --as-of 2026-10 \
  --url https://www.ismworld.org/... --quote "The Manufacturing PMI® registered 55.5 percent."
python3 <skill 폴더>/scripts/macro.py web FEDWATCH_NEXT_FOMC --missing "CME page가 응답하지 않음"
```

- 1차 자료는 `reported`, `--secondary`는 `estimated`가 된다. 기준일이 `max_lag_days`를 넘으면 `stale`이 된다.
- FedWatch는 다음 FOMC 회의에서 가장 확률이 높은 target range의 확률(%)을 넣고, 회의 날짜와 range를 `--quote`의 원문에 담는다.
- 못 찾으면 `--missing "이유"`로 둔다. 기억이나 추측으로 숫자를 넣지 않는다.

## 보고서

`$INVEST_DATA_DIR/macro/reports/<날짜>.md`에 쓴다. 숫자는 snapshot에서 그대로 옮긴다. 다시 계산하거나 반올림을 바꾸지 않는다.

```md
# 매크로 점검 <날짜>

snapshot: <경로>. 판단 재료일 뿐 매수·매도 권유가 아니다.

## 요약
<3~5줄. 무엇이 10년 범위의 끝에 있고, 무엇이 방향을 바꿨나>

## 금리
| 지표 | 최신 | 기준일 | 1개월 | 3개월 | 12개월 | YoY | 10년 percentile | status |
<rates group. 해석 2~3줄>

## credit
## 물가
## 고용
## 성장
## 주택
## 건설
<group마다 같은 표와 해석>

## 섹터: <섹터 이름>
<요청한 섹터마다. snapshot의 sectors 목록으로 표를 만들고, 수요 지표(고용, 판매)와 공급 지표(건설 지출, 착공)를 나눠 해석한다. 대리 지표라고 적는다>

## 데이터 구멍
<missing·stale 항목과 이유, null인 derived 값과 이유(예: 2025-10 CPI가 없어 CPI YoY 없음), 웹 채우기에 쓴 출처와 status>
```

- 웹 채우기 값은 표에 status(`reported`·`estimated`)와 출처 URL을 함께 쓴다.
- 섹터를 요청하지 않았으면 섹터 절을 뺀다.

## 해석할 때

- 단정하지 않는다. "금리가 오르면 REIT가 떨어진다" 같은 문장을 쓰지 않는다. 금리 상승기에도 REIT 수익이 플러스였던 해가 많고 경기 성장이 더 크게 작용했다는 연구가 있어, 한 지표로 결과를 말할 수 없다. "10년물이 10년 중 상위 1%다" 같은 사실과 "섹터에 부담이 될 수 있다" 같은 가능성으로 쓴다.
- percentile이 높다는 것은 과거보다 높다는 뜻이지 내려온다는 뜻이 아니다.
- 사라·팔아라, 비중을 늘려라·줄여라, 목표가, 매매 시점을 쓰지 않는다. 결과는 판단 재료이고 결정은 사용자가 한다.
- stale·missing·estimated 값으로 한 해석에는 그 status를 문장에 붙인다.

## 다른 skill이 읽을 때

다른 skill은 이 snapshot을 읽기만 한다. 고치지 않는다.

- 가장 최근 파일: `ls "${INVEST_DATA_DIR:-$HOME/invest-data}"/macro/snapshots/*.json | tail -1`
- 읽기 전에 첫 칸이 `"schema": "macro-snapshot/1"`인지 본다.

### 갱신

snapshot이 없거나 오늘 것이 아니거나 필요한 섹터가 `sectors`에 없으면, 이 skill이 설치돼 있을 때 다음을 돌린다. 같은 날 snapshot에 합쳐진다.

```bash
python3 <skills 폴더>/macro-check/scripts/macro.py fetch --sector <섹터>
```

exit 1이면 그 값을 `missing`으로 두고 하던 일을 계속한다.

## 고친 뒤 확인

script나 `assets/series.json`을 고치면 돌린다. network 없이 `tests/fixtures`로 돌고, 기대값은 `tests/expected.json`에 손으로 셈한 값이다.

```bash
python3 <skill 폴더>/scripts/macro.py --check   # exit 0이면 통과
```

series를 더하면 FRED series page에서 이름(영어 그대로), 단위, 주기를 옮기고 `max_lag_days`는 주기 + 발표 지연 + 여유로 둔다(일 10, 주 14, 월 80~110, 분기 250).

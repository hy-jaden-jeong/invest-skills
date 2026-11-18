---
name: dividend-portfolio-check
description: 내 배당 포트폴리오를 점검한다. holdings CSV(ticker, 주식 수)를 읽고, 종목마다 reit-stock-review의 최신 metrics를 읽어 비중, subsector·섹터 쏠림(HHI), 예상 연 배당과 월별 배당 일정, 종목별 flag, 금리 민감도 노출을 snapshot과 보고서로 낸다. metrics가 없거나 새 실적 8-K가 나왔는데 metrics가 그 전 분기인 종목은 몇 종목인지 알린 뒤 reit-stock-review deep으로 다시 분석하고 다시 점검한다. "내 포트폴리오 점검해줘", "배당 언제 얼마 들어와", "섹터 쏠림 봐줘", "보유 종목 금리 민감도" 요청에 쓴다. 종목 하나의 공시 분석은 reit-stock-review, 섹터 valuation은 reit-sector-scan이 맡고 이 skill은 그 결과를 읽기만 한다. 사라·팔아라, 비중을 늘려라·줄여라는 말하지 않는다.
---

# 배당 포트폴리오 점검

holdings와 종목 metrics를 읽어 script가 비중·배당·쏠림을 계산하고, agent가 보고서를 쓴다. 숫자는 script만 계산한다. 종목 지표는 reit-stock-review가 만든 metrics를 그대로 쓰고 다시 계산하지 않는다. 같은 입력이면 같은 숫자가 나와야 하기 때문이다.

`<skill 폴더>`는 이 SKILL.md가 있는 폴더, `<skills 폴더>`는 그 위 폴더다. 데이터 폴더는 `$INVEST_DATA_DIR`(없으면 `~/invest-data`)이고 레포 밖에 있다. 오늘 날짜를 `<날짜>`(YYYY-MM-DD)로 쓴다.

## holdings

보유 정보는 개인 정보라 레포에 두지 않는다. script는 아래 순서로 찾는다.

1. `--holdings P`
2. 환경 변수 `INVEST_HOLDINGS_FILE`
3. `$INVEST_DATA_DIR/holdings.csv`

셋 다 없으면 `check`가 exit 1이다. 그러면 사용자에게 holdings 파일 경로를 묻는다. 파일이 아직 없으면 `<skill 폴더>/assets/holdings.example.csv`(가상 예시)를 `$INVEST_DATA_DIR/holdings.csv`로 복사해 사용자가 채우게 하고, 사용자가 말로 준 보유 내역은 그 파일에 적는다. 레포 안에는 쓰지 않는다.

형식: `#`로 시작하는 줄은 주석. 칸은 `ticker`, `shares`(양수), `cost_basis`(선택, 주당 평균 매입가 USD), `account`·`note`(선택, 자유 글). 같은 ticker를 계좌마다 여러 줄로 적으면 주식 수를 더한다.

## 차례

1. **holdings 확인**: 위 순서로 파일을 정하고 ticker 목록을 읽는다.
2. **실적 공시 목록 저장**: 오래된 종목을 가리려면 종목마다 최신 실적 8-K가 필요하다. reit-stock-review가 같은 skills 폴더에 있고 `SEC_USER_AGENT`가 있으면 종목마다 돌린다.

   ```bash
   mkdir -p "$INVEST_DATA_DIR/portfolio/filings"
   python3 <skills 폴더>/reit-stock-review/scripts/edgar.py filings T --forms 8-K --limit 40 \
     > "$INVEST_DATA_DIR/portfolio/filings/T.json"
   ```

   `portfolio.py`는 EDGAR를 직접 부르지 않고 이 파일만 읽는다. skill끼리는 데이터 폴더의 파일로만 주고받기 때문이다. exit가 0이 아니면 그 파일을 지운다. 파일이 없는 종목은 freshness가 `unknown`이 된다. `SEC_USER_AGENT`가 없으면 사용자에게 "이름 email" 모양의 값을 export해 달라고 하고, 그 값을 파일에 적지 않는다.
3. **위 단계 데이터 확인**: `$INVEST_DATA_DIR/reitwatch/parsed/`가 비어 있으면 reit-sector-scan이 설치돼 있을 때 그 skill의 `fetch --month latest`를 돌린다. 없으면 그대로 계속한다(metrics 없는 종목의 screening 값이 missing이 된다).
4. **점검**: `python3 <skill 폴더>/scripts/portfolio.py check`. `$INVEST_DATA_DIR/portfolio/snapshots/<날짜>.json`을 쓰고 경로를 찍는다. stderr에 평가액, 연 배당, needs_review가 나온다.
   - exit 0: 다 채웠다. exit 2: missing, needs_review, freshness unknown, 읽은 metrics의 check 실패 중 하나가 있다. exit 1: holdings가 없거나 형식이 틀렸다(줄 번호가 나온다. 숫자 칸은 유한한 양수만 받는다).
5. **다시 분석 (needs_review)**: snapshot의 `needs_review`가 비어 있지 않으면 아래 "다시 분석"대로 한다.
6. **다시 점검**: 4를 다시 돌린다. 같은 날 snapshot을 덮어쓴다. 다시 분석한 종목이 `basis: metrics`로 표에 들어갔는지 본다.
7. **보고서**: 아래 "보고서" 모양으로 `$INVEST_DATA_DIR/portfolio/reports/<날짜>.md`를 쓰고 사용자에게 요약을 말한다.

## 다시 분석

`needs_review`의 `reason`은 둘이다. `no_metrics`(metrics가 없음)와 `stale`(새 실적 8-K가 낸 분기가 metrics 분기보다 늦음)이다. 어느 쪽이든 reit-stock-review deep으로 다시 분석한다.

1. 시작하기 전에 몇 종목인지, 어떤 종목인지, 종목마다 공시를 읽어 시간과 token이 많이 든다는 것을 사용자에게 알린다. 예(가상): "3종목 중 2종목(PLD: metrics 없음, VICI: metrics 없음)을 reit-stock-review deep으로 분석한다." 알린 뒤 바로 시작한다(사용자가 멈추라고 하면 멈춘다).
2. 종목마다 `<skills 폴더>/reit-stock-review/SKILL.md`의 "차례"를 1부터 9(계산)까지 따른다. 보고서(10)는 사용자가 원할 때 쓴다.
   - subagent를 쓸 수 있으면(Claude Code는 Agent 도구, Codex는 subagent) 종목마다 하나씩 나눠 맡긴다. 지시문에는 ticker, reit-stock-review SKILL.md 경로, 분기를 주고, facts와 metrics 경로를 보고받는다. 보고는 증거가 아니다. metrics 파일이 생겼고 `summary`가 무엇인지 직접 연다.
   - subagent가 없으면 한 종목씩 차례로 한다.
   - `edgar.py resolve`가 `is_reit: false`(SIC가 6798이 아님)면 reit-stock-review가 일반 배당주 경로(EPS·FCF payout, Chowder rule, yield trap 신호)로 분석한다. 그 metrics에는 REITWatch subsector가 없어 이 skill의 금리 민감도는 `review`로 남는다. 보고서에 그렇게 적는다.
   - reit-stock-review가 설치돼 있지 않으면 다시 분석하지 않는다. screening 값만 쓰고 그렇게 적는다.
3. 배당 빈도가 12(월배당)가 아니면 최근 배당 지급일을 넣는다. 보도자료(.txt)의 배당 선언 문장(예: "payable on October 15")에서 읽는다. 없으면 회사 IR의 배당 page를 웹에서 찾는다.

   ```bash
   # (가상) 종목과 날짜는 예시다. 실제 값은 보도자료 원문에서 읽는다
   python3 <skill 폴더>/scripts/portfolio.py paydate VICI --date 2026-10-09 \
     --source "sec:8-K <accession> ex99.1" --locator "<파일> L<줄>: <원문 문장>"
   ```

   - 원문 문장(`--locator`)과 출처(`--source`)가 없으면 script가 거절한다. 날짜가 오늘보다 늦어도 거절한다. 기사·블로그 같은 2차 자료면 `--secondary`(estimated)를 붙인다.
   - 못 찾으면 넣지 않는다. 그 종목은 월별 일정에서 빠지고 `not_covered`에 남는다. 기억이나 추측으로 날짜를 넣지 않는다.

## snapshot

`portfolio.py check`가 쓴다. 값은 macro-check·reit-stock-review와 같은 형식(`value`, `unit`, `status`, `as_of`, `source`, `locator`, `note`)이다.

- `tickers[]`: 종목마다 `basis`(`metrics`·`reitwatch`·`none`), `period`, `price`, `forward_dividend`, `dividend_frequency`, `value`(가격×주식 수), `weight`, `annual_dividend`, `yield_on_cost`, `subsector`, `sector`, `rate_sensitivity`, `schedule`(월별 지급액), `flags`(metrics의 flag 그대로), `checks_failed`, `from_metrics`(forward yield, AFFO payout, net debt/EBITDAre, 고정금리 비중, 10년물 spread, P/FFO, P/AFFO), `freshness`.
- 가격: metrics가 가리키는 facts의 `price`. 기준일이 `--date`보다 5일(`assets/sectors.json`의 `price_max_age_days`) 넘게 이르면 reported도 `stale`이다. metrics가 없으면 REITWatch 최신 호의 가격을 `estimated`로 쓴다. 그 호의 가격 기준일이 `--date`보다 75일(`reitwatch_max_age_days`) 넘게 앞서면 `note`와 데이터 구멍(`reitwatch`)에 나이가 붙는다.
- 비중·평가액 합·연 배당 합은 종목 하나라도 missing이면 missing이다. 가격 하나가 estimated면 모든 비중이 estimated다(합이 그 가격에 걸리기 때문이다).
- `portfolio.monthly_schedule`: 일정을 아는 종목만 더한 부분 합이다. `covered`, `not_covered`, `coverage_pct`(연 배당 중 일정을 아는 몫)를 함께 본다. 배당 빈도가 1·2·4·12가 아닌 종목도 `not_covered`에 이유와 함께 남는다.
- `portfolio.concentration`: subsector·섹터별 비중 합(macro-check 섹터가 없는 subsector는 `none`), HHI(종목 비중 %의 제곱 합, 0~10000), `top`(비중 1위 ticker, 비중이 하나라도 missing이면 null). 기준값이나 flag는 없다.
- `portfolio.rate_exposure`: 금리 민감도 등급(high·medium·low)별 비중 합. 등급은 `assets/sectors.json`의 경험적 판단이라 `estimated`이고, 근거가 엇갈린다는 note가 붙는다. subsector를 모르면 `review`(status missing, pct null)다.
- `needs_review`, `freshness_unknown`, `data_gaps`.

## 보고서

`$INVEST_DATA_DIR/portfolio/reports/<날짜>.md`. 숫자는 snapshot에서 그대로 옮긴다. 다시 계산하지 않는다(표에 줄일 때는 소수 둘째 자리까지 반올림만 한다). 값마다 status를 붙이고, `estimated`·`stale`은 문장에도 적는다. 보고서는 보유 정보를 담으므로 데이터 폴더에만 둔다.

```md
# 배당 포트폴리오 점검 (<날짜>)

holdings: <경로>, snapshot: <경로>. 판단 재료일 뿐 매수·매도 권유가 아니다.

## 요약
<3~5줄. 종목 수, 평가액 합, 연 배당 합과 배당수익률, 가장 큰 subsector와 비중, flag가 watch·alert인 종목, 다시 분석한 종목과 남은 데이터 구멍>

## 보유와 비중
<표: ticker, 주식 수, 가격(기준일·status), 평가액, 비중, forward dividend, 연 배당, yield on cost, basis(metrics 분기 또는 REITWatch)>

## 쏠림
<subsector·섹터별 비중 표, HHI. 기준값 없이 사실만 쓴다>

## 배당
<연 배당 합, 포트폴리오 배당수익률, 월별 일정 표(1~12월), coverage와 일정을 모르는 종목과 이유>

## 종목별 flag
<표: ticker, flag, level, 값, 기준(경험칙). metrics 없는 종목은 review와 이유>

## 금리 민감도
<등급별 비중 표와 종목, 등급 이유, "경험적 판단이고 근거가 엇갈림"을 적는다. 종목별 고정금리 비중·10년물 spread는 metrics 값으로>

## 다시 분석한 종목
<needs_review였던 종목, 이유(no_metrics·stale), 분석한 분기와 metrics 경로, 남은 needs_review>

## 데이터 구멍
<snapshot의 data_gaps 전부, freshness unknown, 다시 분석하지 못한 종목과 이유, paydate 출처와 status>
```

## 해석할 때

- 사라·팔아라, 비중을 늘려라·줄여라, 리밸런싱하라, 목표가, 매매 시점을 쓰지 않는다. 결과는 판단 재료이고 결정은 사용자가 한다.
- 쏠림은 "Free Standing이 38.46%다"처럼 사실로 쓴다. "너무 많다", "분산이 필요하다"고 쓰지 않는다.
- flag 기준은 reit-stock-review의 경험칙이다. 그 skill의 metrics에 적힌 기준을 그대로 옮긴다.
- 금리 민감도 등급은 경험적 판단이다. "금리가 오르면 떨어진다"고 단정하지 않는다.
- REITWatch screening 값(metrics 없는 종목)은 월간 consensus·가격이라 `estimated`라고 문장에 붙인다.

## 고친 뒤 확인

script나 `assets/sectors.json`을 고치면 돌린다. network 없이 `tests/fixtures`(모두 가상)로 돌고, 기대값은 `tests/expected.json`에 손으로 셈한 값이다.

```bash
python3 <skill 폴더>/scripts/portfolio.py --check   # exit 0이면 통과
```

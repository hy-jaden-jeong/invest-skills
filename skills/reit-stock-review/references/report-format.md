# 보고서 형식 (report.py)

`scripts/report.py`가 metrics·facts·추세·경쟁 종목 metrics와 agent의 풀이 글(`narrative.json`)로 view model 하나를 만들고, 거기서 읽는 보고서(md), 부록(md), 장표(HTML, PNG)를 낸다. 숫자는 metrics·trend JSON에서 그대로 옮긴다. report.py가 새로 내는 숫자는 순위(몇째), 추세 표의 처음→끝 변화, 장표 막대의 묶음 합뿐이다. agent는 표를 옮기지 않고 풀이 글만 쓴다. 같은 view model에서 md·장표가 나오므로 같은 숫자가 서로 어긋나지 않는다.

## 명령

```bash
# 단일 보고서: 종목 하나 + 경쟁 종목(reit_calc.py peers가 고른 2개)
python3 <skill 폴더>/scripts/report.py build --metrics <T metrics> --peers <경쟁1 metrics> <경쟁2 metrics> \
  --trend $INVEST_DATA_DIR/tickers/<T>/trend/<P>.json --narrative <narrative.json> \
  --out-dir $INVEST_DATA_DIR/tickers/<T>/reports

# 비교 보고서: 사용자가 지정한 2~6개(모두 깊게 본 metrics)
python3 <skill 폴더>/scripts/report.py compare --metrics <M1> <M2> ... --narrative <narrative.json> \
  --out-dir $INVEST_DATA_DIR/compare

# 실적 뒤 점검: reit_calc.py diff 출력(--out으로 쓴 JSON)
python3 <skill 폴더>/scripts/report.py earnings --diff <earnings.json> --narrative <narrative.json> \
  --out-dir $INVEST_DATA_DIR/tickers/<T>/reports
```

`--date YYYY-MM-DD`로 파일 이름의 날짜를 정한다(기본 오늘).

| 종료 코드 | 뜻 | 할 일 |
| --- | --- | --- |
| 0 | 다 만들었다 | md를 처음부터 끝까지 읽고 장표 PNG를 연다 |
| 1 | 입력 오류(파일 없음, schema가 다름, 일반 배당주 metrics) | 메시지대로 고친다 |
| 2 | 풀이 글이 없다 | 숫자만으로 만든 보고서에 "풀이 글 없음"이 적혀 있다. 풀이 글을 써서 다시 돌린다 |
| 2 | 풀이 글 검사에 걸렸다 | 아무 파일도 쓰지 않았다. stderr의 문제(금지어, 길이, 칸 이름, 보고서에 없는 숫자, 모르는 칸)를 고친다 |
| 2 | 장표가 diagram-slides 검사에 걸렸다 | 그 장표만 실패로 적히고 나머지는 만들어졌다. 풀이 글의 장표 제목·아래 글을 줄여 다시 돌린다 |
| 2 | 보고서 자체 검사에 걸렸다(60줄 초과, 칸 이름 누출) | script 결함이다. 보고한다 |
| 2 | 비교할 종목이 7개 이상이다 | 아무 파일도 쓰지 않는다. reit-sector-scan skill의 screening 표로 본다 |
| 2 | 비교에 가볍게 본 종목이 섞였다 | 만들되 머리 줄에 적는다. 지정한 종목은 깊게 본다(D18) |

## 나오는 파일 (단일)

| 파일 | 내용 |
| --- | --- |
| `<날짜>-deep.md` | 읽는 보고서. 용어 표를 빼고 60줄 이하. 차례: 결론 한 줄, 축별 등급, 핵심 숫자, 동종 대비 위치, 8분기 추세, 주요 사건, 위험과 다시 볼 조건, 용어 |
| `<날짜>-deep-audit.md` | 부록: FFO 재계산, AFFO bridge, 만기 사다리, covenant, cap rate 기준선과 economic EV 다리, 등급 근거 전부, 사건 원문, 데이터 구멍, 모든 지표, facts 출처 위치 |
| `<날짜>-deep.view.json` | view model(PDF가 읽는다) |
| `slides/<날짜>-<T>-01-grades.html`·`-02-maturity`·`-03-dividend` | 장표 3장(등급표, 만기 일정, 배당 여력). diagram-slides가 있으면 같은 이름의 `.png` |

- 경쟁 종목에는 등급을 매기지 않고 숫자만 둔다. 깊게 본 종목을 재사용해도 같다(등급은 그 종목의 보고서에 있다).
- 순위는 주인공이 세 종목 중 몇째인지다. 지표마다 낮은 순·높은 순이 정해져 있다(`assets/labels.json`의 `order`). subsector가 다른 경쟁 종목은 P/FFO·cap rate 순위에서 뺀다.
- status는 사람 말로 쓴다: 공시값, 계산값, 추정, 오래됨, 없음, 공시 없음, 가볍게 봄, 견줄 수 없음, 그 분기 거래 없음. 지표 이름과 한 줄 풀이는 `assets/labels.json`이고, 처음 나오는 지표는 표의 "뜻" 칸에 한 줄 풀이가 붙는다. 끝의 용어 표는 본문에 나온 용어만 싣는다.
- 만기 장표는 막대 10개까지다. 만기 해가 더 많으면 10년 안의 해를 두고, 나머지 해가 그중 가장 큰 해보다 작으면 빼고 부제에 적는다(합 막대는 한 해 집중처럼 보여서다). 더 큰 해가 있으면 "N년 이후 k개 해 합"으로 묶는다.
- 기준선(주의 기준)이 막대 값 글자를 지나가면 diagram-slides가 장표를 막으므로 선 대신 부제에 기준을 적는다.
- 장표 굽기: diagram-slides 폴더를 `DIAGRAM_SLIDES_DIR`, 없으면 이 skill 옆 `../diagram-slides`, 없으면 `~/.claude/skills/diagram-slides`에서 찾는다. 없으면 HTML만 쓰고 보고서 머리 줄에 "장표 PNG를 굽지 않았다"를 적는다.

## 나오는 파일 (비교)

| 파일 | 내용 |
| --- | --- |
| `<날짜>-<T1>-<T2>-....md` | 비교 보고서. 용어 표를 빼고 60줄 이하. 차례: (subsector가 섞이면 첫 줄에 그 사실) 결론 한 줄, 등급 matrix(종목 × 축, 강점·약점 줄), 핵심 지표, 차이를 만든 원인(축마다 근거 지표), 금리 민감도, 주요 사건(종목마다 가장 중요한 것 먼저), 종목별 보고서 링크, 용어 |
| `<날짜>-<T1>-....json` | `reit_calc.py compare`와 같은 JSON(지표 102개 표, 경고) |
| `<날짜>-<T1>-...-audit.md` | 종목마다 부록(FFO 재계산·AFFO bridge·만기·covenant·cap rate 다리·데이터 구멍·출처) |
| `<날짜>-<T1>-....view.json` | view model |
| `slides/...-01-matrix`·`-02-quadrant`(3~6개)·`-02-pair`(2개)·`-03-tenant` | 장표 3장: 등급 matrix, 배당수익률 × net debt/EBITDAre 2x2(2개면 핵심 지표 맞대기 막대), 최대 임차인 비중 막대 |

- 2개는 맞대기다: 두 종목 column과 "차이"(보이는 두 값의 차) column, 두 값 중 큰 쪽에 대한 차이 비율이 큰 지표가 위로 온다.
- 3~6개는 matrix에 축마다·지표마다 순위를 괄호로 붙인다(같으면 공동). 등급 순위는 넉넉이 1, valuation은 낮은 편이 1이다. AFFO가 없어 FFO로 대신한 값은 순위에서 뺀다.
- subsector가 섞이면 valuation 줄은 종목끼리 견주지 않고 각자 subsector 기준과의 거리(P/FFO / peer median, 자기 history percentile, 명목 cap rate − Tracker)로 바꾸고 첫 줄에 섞였다고 쓴다. 나머지 네 축은 그대로 견준다.
- 어느 종목을 고르라는 말은 쓰지 않는다.

## 풀이 글 (narrative.json)

agent가 공시와 metrics를 읽고 쓰는 해석 문장이다. 칸 이름과 한도는 `assets/narrative_rules.json`의 `fields`가 기준이다(글자 수는 공백 포함, 장표 아래 글만 공백 빼고). 모든 칸은 생략할 수 있고, 생략한 칸은 script가 숫자로 채운다(결론은 등급 나열, 위험은 가장 나쁜 축 3개와 등급 경계). `_`로 시작하는 칸(`_note`)은 메모로 보고 읽지 않는다.

### 단일 (`build`)

| 칸 | 한도 | 쓰는 것 |
| --- | --- | --- |
| `conclusion` | 120자 | 결론 한 줄. 무엇이 강하고 무엇이 약한지, 동종 대비 어디인지 |
| `axes.<축>` | 축마다 90자 | 등급표 "풀이" 칸. 축 key는 `dividend_capacity`, `leverage`, `maturity_liquidity`, `tenant_concentration`, `valuation`. 등급이 왜 그런지(구조, 사건, 추정의 가정) |
| `peer_position` | 160자 | 동종 대비 위치 표 아래 한두 문장 |
| `trend_note` | 120자 | 8분기 추세 표 아래 한 문장 |
| `risks` | 정확히 3개, `risk` 60자·`trigger` 70자 | 위험과 그것을 다시 볼 사실 조건. 조건은 "X가 Y를 넘으면"처럼 숫자로 확인할 수 있게 |
| `slide_titles.grades`·`.maturity`·`.dividend` | 40자 | 장표 제목(결론 한 문장) |
| `slide_keys.grades`·`.maturity`·`.dividend` | 공백 빼고 90자 | 장표 아래 글("그래서" 한두 문장) |

### 비교 (`compare`)

| 칸 | 한도 | 쓰는 것 |
| --- | --- | --- |
| `conclusion` | 120자 | 차이 한 줄 결론: 어디서 갈리고 왜 갈리나 |
| `axis_causes.<축>` | 축마다 120자 | "차이를 만든 원인": lease 구조, 임차인, 부채 구조 등. 근거 지표 값은 script가 옆에 붙인다 |
| `rate_sensitivity` | 160자 | 금리 민감도(차환 영향, 변동금리 충격) 견줌 |
| `strengths.<T>`·`weaknesses.<T>` | 종목마다 40자 | 강점·약점 하나. 없으면 script가 가장 나은 축·가장 약한 축을 쓴다 |
| `slide_titles.matrix`·`.quadrant`(3~6개)·`.pair`(2개)·`.tenant` | 40자 | 장표 제목 |
| `slide_keys.`(같은 이름) | 공백 빼고 90자 | 장표 아래 글 |

예는 `tests/fixtures/report/narrative_compare.json`(가상)이다.

### 실적 뒤 점검 (`earnings`)

| 칸 | 한도 | 쓰는 것 |
| --- | --- | --- |
| `conclusion` | 120자 | 무엇이 나빠지고 무엇이 좋아졌나 |
| `revisit` | 3개까지, 70자 | 다시 볼 조건 |

### 검사 (어기면 exit 2, 파일을 쓰지 않는다)

- 금지어: 매수, 매도, 목표가, 사라(사라지다는 괜찮다), 팔아라, NAV/share, 적정 주가, 비중을 늘려라·줄여라, buy·sell·target price. 결과는 판단 재료이고 결정은 사용자가 한다.
- 칸 이름·영문 status를 쓰지 않는다: `affo_payout_forward` 같은 snake_case, exit, reported·estimated 같은 영문 status. 사람 말과 지표 이름(`assets/labels.json`)으로 쓴다.
- `%`·`%p`·`x`·`배`가 붙은 숫자는 보고서에 나오는 숫자여야 한다: metrics 값(보고서와 같은 소수 둘째 자리 반올림), 등급 기준값, 사건 글의 숫자, facts 공시값, 추세 표 변화. 손으로 더하거나 빼거나 반올림을 바꾼 숫자(69.47%를 69.5%로)는 걸린다. 차이를 말하고 싶으면 숫자 대신 "더 높다"처럼 쓴다.
- 모르는 칸 이름, 정해진 개수와 다른 `risks`.

### 예 (가상)

```json
{
  "conclusion": "(가상) 배당 여력은 넉넉하지만 24개월 안 만기를 덮을 돈이 모자라 만기·유동성이 위험이다",
  "axes": {"maturity_liquidity": "(가상) 24개월 liquidity coverage 0.94x라 자본시장에 기대야 한다"},
  "peer_position": "(가상) 세 종목 중 interest coverage가 가장 높고 배당수익률은 가운데다",
  "risks": [
    {"risk": "(가상) 24개월 차환을 자본시장에 기댄다", "trigger": "(가상) 24개월 liquidity coverage가 1x 아래에 머물면"},
    {"risk": "(가상) 부채 배수가 빠듯 구간에 가깝다", "trigger": "(가상) net debt/EBITDAre가 6x를 넘으면"},
    {"risk": "(가상) 임대료 미납 임차인", "trigger": "(가상) 다음 10-Q에 미납이 이어지면"}],
  "slide_titles": {"grades": "(가상) AAA는 만기·유동성 하나가 위험이다"},
  "slide_keys": {"grades": "(가상) 배당 여력과 임차인 분산은 넉넉하고 차환 쪽이 약하다."}
}
```

전체 예는 `tests/fixtures/report/narrative_single.json`이다.

### 잘 쓰는 법

- 지표를 다시 읽어 주지 않는다. 표에 이미 있다. "왜 그런지"(lease 구조, 임차인, 부채 구조, 사건), "동종 대비 어디인지", "무엇이 바뀌면 달라지는지"를 쓴다.
- 추정(`(추정)`)인 값으로 한 해석은 추정이라고 쓴다(예: "차환 금리를 Baa로 가정한 추정이라").
- 등급 기준은 경험칙이다. "안전하다"고 단정하지 않는다. valuation은 위치이고 싸다·비싸다가 아니다.

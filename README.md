# invest-skills

투자할 때 쓰는 개인 skill 모음. [Agent Skills](https://agentskills.io) 형식(`SKILL.md`)이라 특정 vendor에 묶이지 않는다. Claude Code와 Codex(GPT)에서 같은 파일을 그대로 쓴다. 일할 때 쓰는 skill은 [work-skills](https://github.com/hy-jaden-jeong/work-skills)에 있다.

> 이 레포의 skill은 투자 판단을 돕는 절차일 뿐 투자 권유가 아니다. 결과의 정확성을 보장하지 않으며, 투자 판단과 그 책임은 쓰는 사람에게 있다.

## skill 목록

| skill | 하는 일 | 요청 예 | 필요한 것 |
| --- | --- | --- | --- |
| [macro-check](skills/macro-check/SKILL.md) | FRED에서 금리·credit·물가·고용·성장·주택·건설 지표와 섹터별 수요·공급 지표를 받아 snapshot(최신값, 1·3·12개월 변화, YoY, 10년 percentile)과 매크로 보고서를 쓴다 | "매크로 점검해줘", "금리·고용 요즘 어때", "오피스 섹터 매크로 봐줘" | python3. `FRED_API_KEY`는 있으면 쓰고 없어도 돈다 |
| [reit-sector-scan](skills/reit-sector-scan/SKILL.md) | Nareit REITWatch를 받아 subsector마다 median P/FFO·배당수익률의 자기 history percentile·z-score, 10년물 대비 spread, 3·5년 상대 성과를 표로 내고, macro-check의 섹터 수요·공급 지표로 구조적 하락 신호를 짚는다. 섹터를 고르면 종목 screening 표를 낸다 | "어느 섹터가 싸", "저평가 섹터 찾아줘", "net lease 종목 비교 표 줘" | python3, `pdftotext`(poppler). macro-check snapshot이 있으면 spread와 섹터 지표를 채운다 |
| [reit-stock-review](skills/reit-stock-review/SKILL.md) | 미국 REIT 종목 하나의 최신 실적 공시(8-K Ex.99, XBRL)를 SEC EDGAR에서 받아, agent가 출처와 함께 적은 facts로 Nareit FFO 재계산·AFFO bridge·배당 지속성·부채·운영·valuation 지표를 계산하고 deep 보고서를 쓴다 | "O 분석해줘", "이 리츠 배당 괜찮아?" | python3, `SEC_USER_AGENT`(연락처). `FINNHUB_API_KEY`는 있으면 쓰고 없으면 가격을 웹에서 채운다 |

skill이 쓰는 데이터(snapshot, 보고서, cache)는 레포 밖 `$INVEST_DATA_DIR`(없으면 `~/invest-data`)에 쌓인다. 환경 변수와 외부 도구는 위 표의 "필요한 것"에 적힌 skill에만 필요하다.

### 함께 설치할 것

skill은 다른 skill을 같은 skills 폴더에서 찾는다. 이 레포의 skill은 work-skills의 skill(사용자에게 묻는 `grill-me`, 판단 모델과 토론하는 `debate-with-top-model`, 장표를 굽는 `diagram-slides`)과 subagent 정의를 함께 쓸 수 있다. work-skills도 함께 설치한다.

## 설치

skill 폴더를 agent의 skills 폴더 아래에 symbolic link로 건다. 레포를 `git pull` 하면 바로 반영된다.

```bash
git clone https://github.com/hy-jaden-jeong/invest-skills.git ~/invest-skills
```

| agent | skills 폴더 |
| --- | --- |
| Claude Code | `~/.claude/skills` |
| Codex (CLI, app) | `~/.agents/skills` |

Claude Code:

```bash
mkdir -p ~/.claude/skills && for s in ~/invest-skills/skills/*/; do ln -sfn "${s%/}" ~/.claude/skills/"$(basename "$s")"; done
```

Codex:

```bash
mkdir -p ~/.agents/skills && for s in ~/invest-skills/skills/*/; do ln -sfn "${s%/}" ~/.agents/skills/"$(basename "$s")"; done
```

skill이 새로 생기면 위 명령을 다시 돌려 link를 건다.

## 개인 정보

보유 종목, 계좌, 매매 기록 같은 내 투자 정보는 이 레포에 넣지 않는다. 레포 밖 파일에 두고 skill이 그 파일을 읽게 한다. API key는 환경 변수로 준다.

## 이 레포를 고칠 때

`main`에는 직접 push할 수 없다. 브랜치에서 고치고 PR을 열어 merge한다. agent가 지킬 규칙은 [AGENTS.md](AGENTS.md)에 있다. Claude Code는 `CLAUDE.md`가 `AGENTS.md`를 불러와 같은 규칙을 읽는다.

## 가져다 쓴 공개 skill

| 원본 | 라이선스 | 가져온 것 | 들어간 곳 |
| --- | --- | --- | --- |
| [CaseMark `analyzing-reit-financials`](https://github.com/CaseMark/skills) | Apache-2.0 | FFO → AFFO → leverage → 배당 지속성 절차, 만기 한 해 집중 15~20% 표시, net debt/EBITDA 5~7x, coverage 1.5x, FFO 표를 줄마다 맞추는 확인 | `skills/reit-stock-review/references/metrics.md`(원본과 변경 고지), `assets/thresholds.json` |
| [joellewis/finance_skills `real-assets` (`real_assets.py`)](https://github.com/joellewis/finance_skills) | MIT | FFO·AFFO·P/FFO 함수 모양과 `--verify` 같은 자기 확인. FFO 식은 Nareit 정의로 고쳤다 | `skills/reit-stock-review/scripts/reit_calc.py` |
| [tradermonty/claude-trading-skills `kanchi-dividend-sop`](https://github.com/tradermonty/claude-trading-skills) | MIT | 정기 배당 × 횟수로 forward yield, 데이터가 없으면 통과로 치지 않음(review), AFFO payout 85/100%, 기준값을 한 곳에 두기 | `skills/reit-stock-review/scripts/reit_calc.py`, `assets/thresholds.json` |

## 라이선스

[MIT](LICENSE). 출처(저작권 표시와 라이선스 문구)만 남기면 자유롭게 쓰고 고치고 배포해도 된다.

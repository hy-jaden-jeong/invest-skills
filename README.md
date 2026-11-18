# invest-skills

투자할 때 쓰는 개인 skill 모음. [Agent Skills](https://agentskills.io) 형식(`SKILL.md`)이라 특정 vendor에 묶이지 않는다. Claude Code와 Codex(GPT)에서 같은 파일을 그대로 쓴다. 일할 때 쓰는 skill은 [work-skills](https://github.com/hy-jaden-jeong/work-skills)에 있다.

> 이 레포의 skill은 투자 판단을 돕는 절차일 뿐 투자 권유가 아니다. 결과의 정확성을 보장하지 않으며, 투자 판단과 그 책임은 쓰는 사람에게 있다.

## skill 목록

아직 없다.

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

skill이 하나도 없을 때 zsh에서 돌리면 "no matches found"가 나온다. skill이 생긴 뒤에 돌린다.

## 개인 정보

보유 종목, 계좌, 매매 기록 같은 내 투자 정보는 이 레포에 넣지 않는다. 레포 밖 파일에 두고 skill이 그 파일을 읽게 한다. API key는 환경 변수로 준다.

## 이 레포를 고칠 때

`main`에는 직접 push할 수 없다. 브랜치에서 고치고 PR을 열어 merge한다. agent가 지킬 규칙은 [AGENTS.md](AGENTS.md)에 있다. Claude Code는 `CLAUDE.md`가 `AGENTS.md`를 불러와 같은 규칙을 읽는다.

## 라이선스

[MIT](LICENSE). 출처(저작권 표시와 라이선스 문구)만 남기면 자유롭게 쓰고 고치고 배포해도 된다.

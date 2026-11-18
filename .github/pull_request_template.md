## 무엇을 왜

<!-- 바꾼 것과 바꾼 이유를 2~4줄로 쓴다. -->

## As-is

<!-- 바꾸기 전 모양. To-be와 같은 종류·같은 상자 이름으로 그린다. 글만 바뀌면 "구조 변화 없음"이라고 쓴다. -->

```mermaid
flowchart TB
  A[바꾸기 전 상자] --> B[...]
```

## To-be

<!-- 바꾼 뒤 모양. 바뀐 상자는 :::changed로 칠한다. -->

```mermaid
flowchart TB
  classDef changed stroke-width:3px
  A[바꾼 뒤 상자]:::changed --> B[...]
```

## 확인

<!-- 돌린 확인과 결과. 개인 투자 정보·API key가 들어가지 않았는지도 본다. -->

- [ ] 보유 종목·계좌·매매 기록·API key가 diff에 없다

## 설치한 사람이 할 일

<!-- link를 다시 걸어야 하는지 등. 없으면 "없음". -->

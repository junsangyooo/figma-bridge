---
name: figma-bridge
description: Figma 파일을 읽거나 쓸 때 사용. 노드 트리·스타일·레이아웃 조회, 화면 렌더, 이미지 에셋 추출, 코멘트 읽기·작성, 디자인 토큰·변수 수집, 캔버스에 디자인 생성·수정, 선택한 노드 확인. Figma URL(figma.com/design/, /file/, /board/)이 언급되거나 "피그마", "디자인 시안", "시안 읽어줘", "Figma에 그려줘" 같은 요청에 트리거.
---

# figma-bridge

Figma 접근 경로는 세 가지다. 능력이 서로 달라서 **작업에 맞는 경로를 골라야** 한다.

명령은 항상 전체 경로로 부른다. zsh는 변수를 단어 분할하지 않아 `FB="python3 ..."` 같은 축약이 깨진다.

```bash
python3 ~/Documents/GitHub/figma-bridge/figma.py <서브커맨드>
```

| 경로 | 호출 | 아무 파일이나 | 쓰기 | 한도 |
|---|---|---|---|---|
| **B (REST)** | `tree` `render` `assets` `comments` `meta` | O | X | 분당 10~50회 |
| **C (Plugin)** | `exec` `tokens` `selection` `pages` `find` `inspect` `components` `export` `text` | **열린 파일만** | O | 없음 |
| **공식 MCP** | `use_figma` | O | O | 쓰기는 면제, **읽기는 월 20회** |

앱과 목록: `serve`(웹 UI + 릴레이), `files [--verify] [--clean]`, `unhide`, `open <url> [--autorun]`, `setup`, `install-agent`.

## 자주 쓰는 플러그인 래퍼

전부 `exec`로 쓰던 JS를 줄인 것이다. 없는 기능은 `exec`로 직접 쓴다.

```bash
FB=~/Documents/GitHub/figma-bridge/figma.py
python3 $FB find --type FRAME --name 카드 --limit 20   # 노드 찾기
python3 $FB inspect --node 1:23                        # 노드 하나 상세
python3 $FB components                                 # 컴포넌트·변형 세트
python3 $FB export --node 1:23 --out ./out             # 내보내기 (REST 한도 안 씀)
python3 $FB text                                       # 텍스트 전량
python3 $FB text --replace "기존" "새것"                # 일괄 교체 (쓰기)
```

**렌더가 반복되면 `render`(REST, 분당 10회) 대신 `export`(플러그인, 한도 없음)를 쓴다.** 단 파일이 Figma에 열려 있어야 한다.

## 라우팅 규칙

1. **읽기 → B.** 공식 MCP 읽기 도구(`get_design_context`, `get_metadata`, `get_screenshot`)는 월 20회뿐이라 쓰지 않는다.
2. **쓰기 → 파일이 Figma에 열려 있으면 C(`exec`), 아니면 공식 MCP `use_figma`.** 둘 다 Plugin API라서 코드가 거의 같다.
3. **변수 값, 실시간 선택, 뷰포트 → C 전용.** B로는 `boundVariables`(어떤 속성이 변수에 묶였는지)까지만 보인다.
4. **새 파일 생성 → 공식 MCP `create_new_file` 전용.** B에도 C에도 없다.
5. **C를 쓰기 전 `python3 ~/Documents/GitHub/figma-bridge/figma.py setup`으로 연결을 확인한다.** 추측하지 않는다. 안 붙어 있거나 다른 파일에 붙어 있으면 `python3 ~/Documents/GitHub/figma-bridge/figma.py open <url> --autorun`으로 직접 연결한다(Figma 실행·파일 열기·플러그인 실행·연결 대기). `connected: false`면 `--wait 12`로 한 번 더 시도하고, 그래도 안 되면 사용자에게 요청하거나 공식 MCP로 우회한다.
6. **경로를 바꿨으면 반드시 말한다.** 조용한 폴백은 불완전한 데이터를 완전한 것처럼 쓰게 만든다.

## exec 쓰는 법

`use_figma`와 같은 계약이다. 평범한 JS 본문에 top-level `await`과 `return`을 쓴다.

```bash
python3 ~/Documents/GitHub/figma-bridge/figma.py exec "<url>" --code '
  const f = figma.createFrame();
  f.name = "Card";
  f.layoutMode = "VERTICAL";
  f.primaryAxisSizingMode = "AUTO";
  f.counterAxisSizingMode = "AUTO";
  f.itemSpacing = 8;
  f.x = 400; f.y = 200;
  return {createdNodeIds: [f.id]};
'
```

**exec와 use_figma는 API 표면이 다르다.** `use_figma`에만 있는 편의 API를 `exec`에 쓰면 `not a function`이 난다.

| use_figma 전용 (exec에서 금지) | exec에서 쓸 것 |
|---|---|
| `figma.createAutoLayout(dir, props)` | `figma.createFrame()` + `layoutMode`·`primaryAxisSizingMode`·`counterAxisSizingMode` |
| `node.query(selector)` | `node.findAll(fn)` / `findAllWithCriteria` |
| `node.set({...})` | 속성을 하나씩 대입 |
| `await node.screenshot()` | `await node.exportAsync()` 또는 REST `render` |
| `node.placeholder` | 없음 |

`documentAccess`가 `dynamic-page`라서 현재 페이지가 아닌 페이지의 `children`은 `await page.loadAsync()` 후에만 읽힌다.

URL을 같이 주면 **다른 파일이 열려 있을 때 실행을 막는다.** 항상 준다.

긴 코드는 파일로 넘긴다: `python3 ~/Documents/GitHub/figma-bridge/figma.py exec "<url>" --file /tmp/job.js`

### Plugin API 주의사항 (use_figma와 동일)

- 색은 0~1 범위다. `{r:1, g:0, b:0}`이 빨강이다.
- `figma.notify()`는 동작하지 않는다. 결과는 `return`으로만 나온다.
- 텍스트를 건드리기 전에 `await figma.loadFontAsync(...)`가 먼저다.
- `fills`는 읽기 전용 배열이다. 복사해서 바꾸고 다시 대입한다.
- 페이지 전환은 `await figma.setCurrentPageAsync(page)`다.
- `layoutSizingHorizontal = 'FILL'`은 `appendChild` **이후**에 건다.
- 감싸는 텍스트는 `textAutoResize='HEIGHT'` + 명시적 폭이 필요하다. `FILL`만 걸면 폭이 붕괴해 한 글자씩 세로로 떨어진다.
- 생성·수정한 노드 ID를 전부 `return`한다.

## 읽는 순서 (큰 파일)

```bash
FB=~/Documents/GitHub/figma-bridge/figma.py
python3 $FB index "<파일URL>"                    # 1회. 화면·컴포넌트·흐름 지도를 로컬에 저장
python3 $FB context "<url>" --node 1:23          # 이 노드의 부모·소속 화면·연결 화면
python3 $FB tree "<url>" --node 1:23 --prune 3   # 그 다음에야 깊게 본다
python3 $FB export --node 1:23 --scale 1 --out ./out
```

1. **`index`를 먼저 만든다.** 파일 전체를 REST 1회로 받아 로컬에 압축한다. 컨텍스트에는 요약만 들어온다.
   - **최상위 노드를 전부 담는다.** 디자인 파일에는 아트보드 옆에 참고 이미지·메모·여분 프레임이 널려 있어서, 걸러내면 진짜 화면을 놓친다. 대신 각 항목에 `signals`(`text`·`flow`·`components`)를 붙여 둔다. `signals`가 빈 것은 화면이 아닐 가능성이 높고, `text`나 `flow`가 있으면 거의 확실하다. **판단은 이 신호로 하고 개수에 속지 않는다.**
2. **`context`로 관계를 확인한다.** 노드만 좁혀 보면 부모의 오토레이아웃 규칙, 컴포넌트 원본, 연결된 화면을 잃는다. 크기가 왜 그런지 판단하려면 이게 먼저다.
3. **그 다음 `tree --node`로 좁혀 깊게 본다.** 파일 전체를 통째로 받지 않는다.
4. 눈으로 볼 게 있으면 `export --scale 1`. **화면 여러 개를 한 장에 담으면 작아서 못 읽으니** 4~6개씩 나눠 찍는다.

인덱스가 없는 작은 파일이면 `tree --depth 2`로 지도만 보고 바로 3번으로 간다.

## 출력이 클 때

모든 명령에 `--save <파일>`을 붙일 수 있다. 결과를 파일에 쓰고 화면에는 경로와 크기만 남는다.

```bash
python3 $FB text "<url>" --save /tmp/t.json
python3 -c "import json;d=json.load(open('/tmp/t.json'));print(len(d['value']))"
```

**저장한 파일을 Read로 통째로 읽으면 아무것도 아끼지 못한다.** `grep`이나 짧은 파이썬으로 **필요한 부분만** 꺼낸다. 이 습관이 절약의 본체이고 `--save`는 그 수단일 뿐이다.

## 줄이면 잃는 것

| 기법 | 잃는 것 | 보완 |
|---|---|---|
| `--depth 2` | 레이어 이름만 남음. Figma 이름은 `Frame 427` 처럼 무의미한 경우가 많다 | `index`의 화면 텍스트로 식별, 또는 `box` 크기로 판별(390×844면 모바일) |
| `--prune` | 자식 내용 | 반복 요소는 1개만 깊게 보고 나머지는 접는다 |
| `--scale 1` | 미세 정렬·작은 글자 | 의심 구간만 그 노드를 따로 2x로 |
| `--node` | 부모 레이아웃·컴포넌트 원본·연결 화면 | `context`로 되찾는다. `inspect`도 부모 3단계를 함께 준다 |

## 그린 뒤에는 반드시 본다

`exec`나 `use_figma`로 무언가를 만들었으면 **렌더해서 확인한다.** 레이아웃 붕괴는 데이터만 봐서는 드러나지 않는다.

## 수치는 데이터로, 모양은 이미지로

색·간격·폰트 크기는 렌더 이미지에서 눈대중으로 읽지 않는다. `tree` 출력의 실제 값을 쓴다.

## 출력 스키마

`_source`가 출처(`rest` 또는 `plugin`)를 표시한다. 값이 없는 키는 `null`이 아니라 **키 자체가 빠진다.** 키가 없으면 "그 경로로는 못 읽음"으로 해석하고 다른 경로를 제안한다.

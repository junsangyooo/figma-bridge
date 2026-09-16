# figma-bridge

Figma를 AI 에이전트가 읽고 쓰기 위한 독립 도구 모음. 기능마다 서브커맨드 하나가 대응하고, 서로를 호출하지 않는다.

## 경로 두 가지

| 경로 | 구현 | 담당 영역 |
|---|---|---|
| **B (REST)** | `figma.py` | 읽기와 탐색. Figma 앱을 켜지 않아도 동작 |
| **C (Plugin)** | 미구현 | 쓰기, 변수, 실시간 선택. Figma 데스크톱 + 로컬 플러그인 필요 |

라우팅 규칙은 `~/.claude/skills/figma-bridge/SKILL.md`에 있다.

## 앱으로 쓰기

```bash
python3 figma.py serve            # 웹 UI + 릴레이
python3 figma.py install-agent    # 로그인할 때 자동 실행 (launchd)
```

브라우저에서 `http://localhost:3055/` 를 열면 파일 목록이 뜬다. 카드를 누르면 `figma://file/<key>` 딥링크로 Figma 데스크톱이 그 파일을 연다.

**목록은 어디서 오는가.** REST에는 사용자의 파일을 나열하는 엔드포인트가 없다(드래프트는 API 모델에 아예 없고, team_id조차 프로그램으로 얻을 수 없다). 그래서 Figma 데스크톱이 기록한 열린 탭(`~/Library/Application Support/Figma/settings.json`)을 읽고, 여기에 직접 고정한 파일을 합친다. 이 파일은 비공식 내부 포맷이라 Figma 버전업으로 바뀔 수 있다.

**플러그인 자동 실행은 보장되지 않는다.** 외부에서 Figma 플러그인을 실행하는 공개 방법이 없어서, "열 때 플러그인도 실행"을 켜면 AppleScript로 명령 팔레트(⌘/)를 열고 이름을 입력하는 방식으로 흉내 낸다. 파일 로딩이 늦으면 실패한다. 실패해도 파일은 열리므로 플러그인만 직접 실행하면 된다.

### 손쉬운 사용 권한

launchd가 띄운 프로세스가 키 입력을 보내므로 **승인 팝업이 뜨지 않는다.** 수동으로 등록해야 한다.

1. 시스템 설정 > 개인정보 보호 및 보안 > **손쉬운 사용**
2. `+` 를 누르고 파일 선택 창에서 ⌘⇧G
3. `python3 figma.py open <url> --autorun` 이 실패했을 때 응답의 `add` 필드에 찍히는 경로를 붙여넣는다 (Homebrew Python이면 `.../Versions/3.14/Resources/Python.app`)
4. 토글을 켠 뒤 `python3 figma.py install-agent` 로 서비스를 다시 띄운다

권한을 주고 싶지 않으면 UI의 "열 때 플러그인도 실행" 체크를 꺼두면 된다. 파일 열기는 권한 없이 동작한다.

## 새 머신 세팅

의존성 없음. Python 3.9 이상이면 `git clone` 직후 바로 돈다.

```bash
python3 figma.py setup
```

무엇이 준비됐고 무엇이 빠졌는지 점검하고, 빠진 것의 절차를 출력한다. 전체 절차는 다음과 같다.

1. **토큰** — figma.com > Settings > Security > Personal access tokens > Generate new token.
   스코프는 `current_user:read`, `file_content:read`, `file_metadata:read`, `file_comments:read`, `file_comments:write`.
   `~/.claude/secrets/.env`에 `FIGMA_PERSONAL_TOKEN=figd_...` 로 저장한다.
   이 경로는 dotclaude Private 레포라서 `git pull` 하면 토큰도 같이 온다. 그 경우 이 단계는 건너뛴다.
2. **Figma 데스크톱** — `brew install --cask figma`. 플러그인 경로를 쓸 때만 필요하다.
3. **플러그인 임포트** — Figma 데스크톱 > Plugins > Development > Import plugin from manifest > 이 레포의 `plugin/manifest.json`.
4. **릴레이 실행** — `python3 figma.py relay` 를 켜 둔다. 실행할 때마다 **새 토큰**을 출력하고 `~/.figma-bridge/relay-token`(0600)에 저장한다.
5. **플러그인 실행** — 작업할 파일을 열고 figma-bridge 플러그인을 실행한다. 처음 한 번 릴레이가 출력한 토큰을 플러그인 창에 붙여넣는다. 토큰은 그 머신의 `figma.clientStorage`에 남아 다음부터는 묻지 않는다. 창의 점이 초록색이면 연결된 것이다.

1번까지만 하면 읽기는 전부 된다. 2~5번은 쓰기와 변수 조회를 쓸 때만 필요하다.

## 서브커맨드

**REST 경로 (토큰 필요, Figma 앱 불필요, 아무 파일이나 접근)**

| 명령 | 하는 일 | Tier |
|---|---|---|
| `ping` | 토큰 확인 (`/v1/me`) | 3 |
| `tree <url>` | 노드 트리를 정규화해서 출력 | 1 |
| `render <url>` | 노드를 png/jpg/svg/pdf로 렌더 | 1 |
| `assets <url>` | 이미지 fill 원본 다운로드 | 2 |
| `comments <url>` | 코멘트 읽기·작성 | 2 |
| `meta <url>` | 파일 메타데이터 | 3 |

**플러그인 경로 (토큰 불필요, Figma에 열려 있는 파일 하나에만 작동, 한도 없음)**

| 명령 | 하는 일 |
|---|---|
| `setup` | 머신 점검과 절차 출력 |
| `relay` | 릴레이 실행 (켜 둔 채로 사용) |
| `exec [url] --code '...'` | **Plugin API JS를 그대로 실행.** 쓰기를 포함한 모든 기능 |
| `tokens` | 로컬 변수와 스타일을 값까지 |
| `selection` | 지금 선택한 노드 |
| `pages` | 열린 파일의 페이지 목록 |

`exec`에 URL을 같이 주면 플러그인이 붙은 파일과 대조해서 **다른 파일에 잘못 쓰는 것을 막는다.**

```bash
python3 figma.py exec "https://figma.com/design/KEY/Name" --code '
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

**`exec`는 순수 Plugin API만 받는다.** 공식 MCP `use_figma`에는 `figma.createAutoLayout()`, `node.query()`, `node.set()`, `node.screenshot()`, `node.placeholder` 같은 편의 API가 얹혀 있지만 이것들은 **실제 플러그인 환경에 없다.** 쓰면 `not a function`이 난다.

페이지도 다르다. `documentAccess`가 `dynamic-page`라서 현재 페이지가 아닌 페이지의 `children`을 읽으려면 먼저 `await page.loadAsync()` 를 호출해야 한다.

## 파일을 새로 만들 때

REST에도 Plugin API에도 **파일 생성 기능이 없다.** 새 파일은 Figma 앱에서 직접 만들거나, Claude Code 안에서 공식 MCP의 `create_new_file`을 쓴다.

## exec의 위험

`exec`는 받은 JS를 Figma 안에서 그대로 실행한다. 릴레이는 `127.0.0.1`에만 바인딩하지만 **이 머신의 다른 프로세스는 릴레이에 작업을 넣을 수 있다.** 신뢰할 수 없는 코드를 넣지 않는다. 쓰지 않을 때는 릴레이를 끈다.

```bash
python3 figma.py tree "https://figma.com/design/KEY/Name?node-id=1-2" --prune 3
python3 figma.py render "https://figma.com/design/KEY/Name?node-id=1-2" --scale 2 --out ./out
python3 figma.py comments "https://figma.com/design/KEY/Name" --post "간격 확인 필요"
```

## 출력 스키마

모든 출력은 JSON이고 `_source` 필드로 경로를 표시한다. 값이 없는 키는 `null`로 두지 않고 **키 자체를 뺀다.** "없음"과 "이 경로에서는 못 읽음"을 구분하기 위해서다.

```json
{
  "_source": "rest",
  "file": "Design System",
  "node": {
    "id": "1:2", "name": "Button", "type": "INSTANCE",
    "box": {"x": 0, "y": 0, "width": 342, "height": 48},
    "layout": {"mode": "HORIZONTAL", "gap": 8, "padding": [12, 16, 12, 16]},
    "fills": [{"type": "SOLID", "hex": "#2ECBA0"}],
    "boundVariables": ["fills"],
    "children": [{"id": "1:3", "type": "TEXT", "text": "확인"}]
  }
}
```

`boundVariables`는 **바인딩된 속성 이름만** 알려준다. 변수의 실제 값은 REST에서 Enterprise 플랜 전용이라 B 경로로 읽을 수 없다. 값이 필요하면 C 경로가 있어야 한다.

## 한도 (Starter 플랜 + Full 시트)

| Tier | 한도 |
|---|---|
| Tier 1 (`tree`, `render`) | 분당 10회 |
| Tier 2 (`assets`, `comments`) | 분당 25회 |
| Tier 3 (`ping`, `meta`) | 분당 50회 |

429를 받으면 `Retry-After` 값과 함께 종료한다.

## REST로 불가능한 것

캔버스 노드 생성·수정·삭제, 변수 값 읽기(Enterprise 미만), 실시간 선택, 뷰포트 제어, 인스턴스 override, 애노테이션. 전부 C 경로 또는 공식 MCP의 `use_figma` 영역이다.

## 테스트

```bash
python3 test_figma.py
```

네트워크를 쓰지 않는 순수 함수(URL 파싱, 정규화)만 검증한다.

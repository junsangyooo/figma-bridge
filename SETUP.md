# 세팅 가이드

처음 쓰는 머신에서 이 문서만 따라가면 된다. macOS 기준.

```bash
git clone git@github.com:junsangyooo/figma-bridge.git ~/Documents/GitHub/figma-bridge
cd ~/Documents/GitHub/figma-bridge
python3 figma.py setup
```

`setup`은 무엇이 준비됐고 무엇이 빠졌는지 표로 알려준다. 아래 단계를 마친 뒤 다시 실행하면 전부 `OK` 가 된다.

```
OK  python                3.14.3
OK  FIGMA_PERSONAL_TOKEN  set
OK  Figma desktop         installed
OK  relay                 http://127.0.0.1:3055
OK  plugin                connected
```

---

## 1단계 — 토큰 (읽기에 필수)

`~/.claude/secrets/.env` 를 dotclaude 레포로 동기화하고 있다면 **이미 있다.** `python3 figma.py ping` 이 handle과 email을 출력하면 건너뛴다.

없다면 발급한다.

1. figma.com 로그인 → 좌상단 계정 아이콘 → **Settings**
2. **Security** 탭 → **Personal access tokens** → **Generate new token**
3. 스코프: `current_user:read`, `file_content:read`, `file_metadata:read`, `file_comments:read`, `file_comments:write`
4. 만료일을 정하고 생성. **이때 복사하지 않으면 다시 못 본다**
5. 저장한다. 값이 화면에 찍히지 않게 입력받는 방식을 쓴다

```bash
read -s "T?Figma token: " && echo "\nFIGMA_PERSONAL_TOKEN=$T" >> ~/.claude/secrets/.env && unset T
python3 figma.py ping
```

여기까지 하면 **읽기는 전부 된다.** 트리 조회, 렌더, 코멘트, 에셋 추출. 아래 단계는 쓰기와 변수 조회를 위한 것이다.

## 2단계 — Figma 데스크톱

```bash
brew install --cask figma
```

플러그인 임포트는 **데스크톱 앱에서만** 된다. 브라우저 버전으로는 불가능하다.

## 3단계 — 플러그인 임포트

1. Figma 데스크톱 상단 메뉴 **Plugins → Development → Import plugin from manifest…**
2. 파일 선택 창에서 **⌘⇧G** 를 누르고 경로를 붙여넣는다

```
~/Documents/GitHub/figma-bridge/plugin/manifest.json
```

## 4단계 — 서버를 로그인 시 자동 실행

```bash
python3 figma.py install-agent
```

launchd에 등록되어 로그인할 때마다 뜬다. 토큰이 출력되는데 다음 단계에서 쓴다. 나중에 다시 볼 일이 있으면:

```bash
cat ~/.figma-bridge/relay-token
```

자동 실행을 원하지 않으면 대신 터미널에서 직접 띄운다: `python3 figma.py serve`

## 5단계 — 플러그인 실행과 토큰 입력

1. 작업할 파일을 Figma에서 연다
2. **Plugins → Development → figma-bridge**
3. 창이 뜨면 4단계의 토큰을 붙여넣고 **저장**

점이 초록색이 되면 연결된 것이다. **토큰은 그 머신에 저장되어 다시 묻지 않는다.** 서버를 재시작해도 같은 토큰을 재사용한다.

## 6단계 — 확인

```bash
python3 figma.py setup                    # 전부 OK 인지
python3 figma.py files                    # 최근 연 파일 목록
```

## 7단계 — Dock에 올리기

```bash
open app/figma-bridge.app
```

서버가 꺼져 있으면 깨운 뒤 주소창 없는 창으로 UI를 띄운다. Chrome이 없으면 기본 브라우저로 연다.

Dock에 고정하려면 **Finder에서 `app/figma-bridge.app` 을 Dock으로 끌어다 놓는다.** 레포를 다른 경로로 옮기면 링크가 끊기므로 옮긴 뒤 다시 끌어다 놓아야 한다.

브라우저에서 바로 열어도 된다: `http://localhost:3055/`

---

## 선택 — 플러그인 자동 실행 (손쉬운 사용 권한)

앱에서 파일을 클릭할 때 플러그인까지 자동으로 띄우려면 권한이 필요하다. **이 단계는 건너뛰어도 된다.** 파일 열기는 권한 없이 동작하고, 플러그인만 직접 실행하면 된다.

**AI 에이전트가 CLI로 플러그인까지 연결하려면(`open <url> --autorun`) 이 권한이 필요하다.** CLI는 키 입력을 릴레이에 넘기므로 권한은 아래 Python 하나에만 준다.

Figma는 외부에서 플러그인을 실행하는 방법을 제공하지 않는다. 그래서 AppleScript로 명령 팔레트(⌘/)를 열고 이름을 타이핑하는 방식으로 흉내 낸다. **launchd가 띄운 프로세스가 키를 보내기 때문에 승인 팝업이 뜨지 않는다.** 직접 등록해야 한다.

1. 등록할 경로를 확인한다

```bash
python3 figma.py open <파일URL> --autorun
```

실패 응답의 `add` 필드에 경로가 찍힌다. Homebrew Python이면 `.../Versions/3.14/Resources/Python.app` 형태다.

2. 시스템 설정 → 개인정보 보호 및 보안 → **손쉬운 사용**
3. `+` → **⌘⇧G** → 위 경로 붙여넣기 → 토글 켜기
4. 권한은 프로세스 단위로 평가되므로 서비스를 다시 띄운다: `python3 figma.py install-agent`

**감수할 점:** 이 권한은 figma-bridge만이 아니라 **그 Python으로 실행되는 모든 스크립트**에 키 입력 권한을 준다. 부담되면 등록하지 않는 편이 낫다.

---

## 알아둘 것

- **플러그인 창을 닫으면 연결이 끊긴다.** 폴링하는 주체가 그 창이다. 최소화해 두면 된다.
- **플러그인은 열려 있는 파일 하나에만 붙는다.** 다른 파일로 옮기면 그 파일에서 다시 실행해야 한다. 닫힌 파일에 쓰려면 Claude Code 안에서 공식 MCP `use_figma`를 쓴다.
- **파일 목록은 로컬에서 온다.** REST에는 사용자 파일을 나열하는 엔드포인트가 없다. Figma 데스크톱이 기록한 열린 탭을 읽으므로, 한 번도 연 적 없는 파일은 목록에 없다. URL을 붙여넣어 고정하면 된다.
- **변수 값은 플러그인으로만 읽힌다.** REST의 변수 API는 Enterprise 전용이다.
- **`exec`의 파일 대조는 이름 기반이다.** Plugin API에는 파일 키가 없다(`figma.fileKey`는 존재하지 않고 `figma.root.id`는 모든 파일에서 `0:0`). 그래서 URL의 키로 REST에서 이름을 받아 열린 파일 이름과 비교한다. **이름이 같은 다른 파일은 구분하지 못한다.** REST 토큰이 없거나 조회에 실패하면 차단하지 않고 경고만 띄운다.
- **새 파일 생성은 공식 MCP `create_new_file` 전용이다.** REST에도 Plugin API에도 없다.

## 문제 해결

| 증상 | 원인과 조치 |
|---|---|
| `Manifest error: Invalid value for devAllowedDomains` | manifest에 원시 IP를 쓴 경우. `http://localhost:3055` 형태여야 한다 |
| 플러그인 창이 "릴레이 없음" | 서버가 안 떠 있다. `python3 figma.py setup` 으로 확인하고 `install-agent` 또는 `serve` 실행 |
| 플러그인 창이 "토큰 거부됨" | 서버를 `--rotate` 로 띄워 토큰이 바뀐 경우. `cat ~/.figma-bridge/relay-token` 값을 다시 붙여넣는다 |
| `exec` 가 `plugin not connected` | 작업할 파일을 열고 플러그인을 실행했는지 확인. 창을 닫았으면 다시 실행 |
| `exec` 코드가 `not a function` | `use_figma` 전용 API를 쓴 것. `createAutoLayout`·`query`·`set`·`screenshot`·`placeholder`는 실제 Plugin API에 없다 |
| `install-agent` 후에도 응답 없음 | 재등록 직후 잠깐 끊긴다. `curl --retry 15 --retry-delay 1 --retry-connrefused http://localhost:3055/status` 로 기다린다. 그래도 없으면 `launchctl print gui/$(id -u)/dev.jsyoo.figma-bridge` 와 `~/.figma-bridge/relay.log` 를 본다 |
| 지워진 파일이 목록에 남음 | Figma에 탭이 열려 있으면 기록에 남는다. UI의 **정리** 버튼(또는 `files --clean`)이 존재를 확인해 없어진 것만 숨긴다. 확인하지 못한 파일은 건드리지 않는다 |
| 숨긴 파일을 되돌리고 싶음 | UI 상단의 "모두 되돌리기" 또는 `unhide <url>` / `unhide --all` |
| REST가 429 | 분당 한도(Tier1 10회)를 넘었다. `Retry-After` 만큼 기다리거나 `--depth` 로 요청을 줄인다 |
| `ping` 이 403 | 토큰 만료 또는 스코프 부족. 1단계로 재발급 |

## 제거

```bash
python3 figma.py uninstall-agent          # 자동 실행 해제
rm -rf ~/.figma-bridge                    # 릴레이 토큰과 고정 목록
```

Figma 플러그인은 Plugins → Manage plugins에서 제거한다. `~/.claude/secrets/.env` 의 토큰은 figma.com Settings에서 revoke한다.

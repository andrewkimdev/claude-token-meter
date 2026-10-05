# Claude Token Meter: 아키텍처와 개발 계획 (v0 기획)

> P0 실측 결과와 그에 따른 설계 변경은 `docs/P0_FINDINGS.md` 참조.

## Context

LilyGO T-Display(클래식 ESP32, 135×240 ST7789, PSRAM 없음)를 **현재 Mac에 로그인된 Claude 구독 계정의 사용 한도 전용 표시기**로 만든다. 요구사항은 다섯 가지다.
- 침습이 적어야 한다. Anthropic 서버를 스팸하지 않는다.
- 갱신은 1~2분 이내면 충분하다.
- 디바이스 파일을 직접 편집하지 않고, 사용자 컴퓨터를 통해 설정한다.
- 가능하면 무선으로 동작한다.
- 다른 기능 없이 이것만 표시한다.

저장소는 현재 `docs/HARDWARE.md`만 있다. 이 문서는 형제 프로젝트 `~/dev/esp32/esp32-bus-eta-display`에서 가져온 실측 하드웨어 사실과 gotcha 3.1~3.17이다.

**사용자 결정 사항 (확정)**
1. 데이터 소스는 **Claude Code statusLine만** 쓴다. 비공개 엔드포인트 폴링은 하지 않는다.
2. 통신은 **WiFi**로 하고, **USB 1회 셋업**으로 설정을 주입한다. USB 시리얼은 대체 경로로 남긴다.

**조사로 확정한 사실** (https://code.claude.com/docs/en/statusline, 로컬 CC 2.1.289)
- statusLine stdin JSON에 `rate_limits.five_hour` / `rate_limits.seven_day`가 들어 있다. 각각 `used_percentage`(0–100)와 `resets_at`(epoch 초) 필드를 가진다. `[V-doc]`
- 이 필드는 Pro/Max 구독자에게만, 그리고 세션의 첫 API 응답 이후에만 나타난다. 창별로 독립적으로 빠질 수 있다. `resets_at`이 지나면 해당 창은 JSON에서 사라진다.
- 실행 트리거는 다음과 같다. 디바운스는 300ms이고, 스크립트가 실행 중일 때 새 트리거가 오면 실행 중인 스크립트는 취소된다.
  - 세션 시작
  - 어시스턴트 메시지 수신
  - `/compact`
  - 리셋 시각 도달
  - `refreshInterval` (설정한 경우)
- 현재 `~/.claude/settings.json`에는 `statusLine`이 없다. iTerm2용 `cc-status` 훅이 여러 이벤트에 걸려 있으며 이것과는 충돌하지 않는다.
- 형제 프로젝트에서 확인한 사항이 두 가지 있다.
  - 디바이스 HTTPS(TLS)는 sprite(64.8KB)와 함께 쓰기에는 힙이 빠듯해서 피했다. 이 설계에서는 디바이스가 TLS를 전혀 쓰지 않으므로 이 문제가 사라진다.
  - 그대로 쓸 수 있는 모듈은 `wifi_ntp.*`, `diag.*`(NVS 미러), `buttons.*`, `display.*`(LEDC 가드), `genstrings.py`다.

---

## 전문가 페르소나 의견과 반영

아래 인물들의 공개된 원칙에 근거해 관점을 상정한 의견이다. 본인들의 실제 발언이 아니다.

| 이슈 | 페르소나 | 핵심 의견 | 계획 반영 |
|---|---|---|---|
| 데이터 흐름·일관성 | **Martin Kleppmann** (『Designing Data-Intensive Applications』) | 진실의 원천은 하나여야 하고, 모든 값에 관측 시각이 붙어야 한다. 여러 CC 세션의 값이 서로 어긋나므로(P0 실측), 순서와 무관한 병합 규칙이 필요하다. 전송은 멱등이어야 한다. | statusLine이 유일한 소스다. 창별 병합 규칙: `resets_at`이 더 늦으면 교체하고, 같으면 `max(pct)`를 취한다(단조 증가 성질 이용, P0_FINDINGS 참조). 페이로드에 `observed_at`과 `seq`를 넣는다. 재전송해도 결과가 같다. |
| 서버 예의·폴링 | Kleppmann 동일 + 사용자 정책 | 가장 좋은 폴링은 하지 않는 폴링이다. 이미 CC가 받고 있는 응답에 편승한다. | Anthropic으로 가는 **추가 요청은 0건**이다. Mac에서 디바이스로 가는 LAN 트래픽만 존재한다. |
| 보안·자격증명 | **Bruce Schneier** | 비밀을 퍼뜨리지 마라. 디바이스가 OAuth 토큰을 가질 이유가 없다. 위협 모델은 "같은 LAN의 누군가가 가짜 수치를 띄운다" 정도이고, 그에 비례한 방어면 충분하다. | 디바이스에는 Anthropic 자격증명이 **전혀 없다**. 셋업 때 무작위 32B 페어링 키를 USB로 주입한다. 모든 푸시는 HMAC-SHA256으로 서명하고 `seq`로 재전송 공격을 막는다. WiFi 비밀번호는 NVS에만 저장하고 로그에 남기지 않는다. |
| 임베디드 신뢰성 | **Jack Ganssle** | 실패는 화면에 보여야 한다. 오래된 데이터는 오래돼 보여야 한다. 재부팅으로 증상을 숨기지 마라(gotcha 3.7과 같은 원칙). 워치독은 정말 멈췄을 때만 쓴다. | 데이터 나이(age)를 항상 표시한다. 5분 이상 하트비트가 없으면 "bridge offline"으로 표시한다. 리셋 시각이 지나면 해당 창을 "리셋됨(추정)"으로 표시한다. `ESP.restart()` 남용을 금지한다. task WDT는 유지한다. `diag` NVS 링을 재사용한다. |
| 소형 화면 정보 디자인 | **Edward Tufte** (sparkline 창안자) | 데이터-잉크 비율을 최대화하라. 숫자 두 개와 막대 두 개면 충분하다. 핵심은 **페이스(pace)**다. 창의 경과 시간 비율을 막대 위 눈금으로 표시하면 "지금 속도로 가면 모자란가"가 한눈에 보인다. | 아래 디스플레이 설계에 반영했다. |
| 셋업 UX | **Don Norman** | 사용자가 시스템 모델을 몰라도 되게 하라. 명령 하나로 셋업하고, 디바이스 화면이 다음 할 일을 알려줘야 한다. | `ctm setup` 한 줄로 끝난다. 미설정 디바이스는 "USB로 연결 후 `ctm setup`"을 화면에 안내한다. 성공하면 수치 화면으로 바로 전환된다. |
| ESP32 네트워킹 | **Ivan Grokhotkov** (Arduino-ESP32 코어 리드) 관점 | modem-sleep을 켜 둔다(gotcha 3.5). 디스커버리는 mDNS로 한다. 서버는 단순한 `WebServer`면 충분하다. 시계는 브리지가 서명해 보낸 시각으로 맞추면 NTP 의존이 사라진다. | `ctmeter-XXXX.local`과 `_ctmeter._tcp`를 광고한다. `POST /v1/state`를 받는다. 시각은 `sent_at`으로 보정하고, NTP는 선택 사항이다. |
| 개발 방법론 | **Kent Beck** | 동작하는 가장 얇은 끝단부터 만든다(walking skeleton). 계약을 먼저 고정하고, 작은 단계와 빠른 피드백으로 진행한다. | P1은 USB로 숫자 하나 띄우기다. 프로토콜과 픽스처를 먼저 고정한다. 순수 로직은 호스트와 `pio test -e native`로 테스트한다. 하드웨어 검증은 `[V]` 태그로 기록한다. |

---

## 아키텍처

```
[Claude Code 세션들] --stdin JSON (이벤트 기반)--> ctm-statusline (≤20ms, 네트워크 없음)
                                                     │ atomic write
                                                     ▼
                                    ~/.claude-token-meter/state.json
                                                     │ watch (kqueue/폴링 2s)
                                                     ▼
                            ctm-bridge (launchd LaunchAgent, Python)
                              ├─ 병합: 창별 (resets_at 큰 쪽 → 같으면 max pct)
                              ├─ 송신 정책: 변화 시 즉시(최소 간격 30s) + 하트비트 120s
                              ├─ 서명: HMAC-SHA256(pairing_key, body)
                              └─ 전송: WiFi(mDNS 해석 → HTTP POST) │ 대체: USB 시리얼 JSON line
                                                     ▼
                         T-Display 펌웨어: 검증 → 상태 머신 → 렌더 (250ms 루프)
```

### 컴포넌트

1. **`ctm-statusline`** (Python 단일 파일. `settings.json`의 `statusLine.command`로 등록)
   - stdin JSON에서 `rate_limits`만 추출한다. 여기에 `observed_at=now`와 `session_id`를 붙여 tmp 파일에 쓴 뒤 rename한다.
   - `rate_limits`가 없으면 **쓰지 않는다**. 이전 값을 지우지 않기 위해서다.
   - stdout으로 짧은 상태줄(`5h 47% · 7d 21%`)을 출력해서 터미널에서도 바로 쓸모가 있게 한다.
   - 네트워크 I/O는 금지한다. CC가 실행 중인 스크립트를 취소하므로 빨라야 한다.
   - 기존 statusLine이 생기면 래핑해서 체인할 수 있게 설계한다.
2. **`ctm-bridge`** (launchd LaunchAgent, 로그인 시 시작)
   - state 파일을 감시한다.
   - 병합 규칙: `resets_at`이 지난 창은 `reset_assumed=true`로 표시한다.
   - 송신 정책
     - 값 변화가 0.5%p 이상이거나 창이 리셋되면 송신한다. 최소 간격은 30초다.
     - 변화가 없으면 120초마다 하트비트를 보낸다. 사용자가 원한 1~2분 주기와 맞다.
   - 디바이스 IP는 mDNS로 해석하고 캐시한다. 실패하면 백오프한다(최대 10분).
   - Mac 슬립/웨이크 후에는 즉시 재송신한다.
3. **`ctm` CLI**: `ctm setup | status | push-test | unpair`
   - `setup`
     1. USB 포트를 자동 탐지한다(`/dev/cu.usbserial-*`, gotcha 3.11 주의).
     2. 디바이스에 `scan`을 보내 SSID 목록을 받는다. macOS는 SSID를 숨기므로 Mac에서 읽지 않는다(P0 ④).
     3. 사용자가 SSID를 고르고 비밀번호를 입력한다. Keychain에서 읽는 기능은 옵트인으로만 둔다.
     4. 페어링 키를 생성해 Mac Keychain(`ctm-pairing`)에 저장한다.
     5. 시리얼로 `{"cmd":"provision",...}`를 보낸다. 디바이스 응답으로 WiFi 접속과 mDNS 이름을 확인받는다.
4. **펌웨어** (기존 `platformio.ini` §6.4 그대로)
   - NVS 저장: `wifi_ssid`, `wifi_pass`, `pair_key`, `last_state`(재부팅 직후 빈 화면 방지)
   - 엔드포인트
     - `POST /v1/state`: HMAC 검증 후 seq 비교, 갱신
     - `GET /v1/info`: fw 버전, uptime, RSSI, 마지막 seq. 서명 없이 열어 두며 비밀은 노출하지 않는다.
   - 시리얼 경로: 같은 JSON 라인 프로토콜과 `provision` 명령을 처리한다. USB 연결 시 전송 대체 경로가 된다.
   - 버튼
     - BTN1 짧게: 상세 뷰(절대 리셋 시각, 데이터 나이, IP, RSSI)
     - BTN1 길게: 재프로비저닝 안내
     - BTN2: 백라이트 3단

### 프로토콜 v1 (`docs/PROTOCOL.md`에 고정, `fixtures/*.json`을 호스트와 펌웨어 테스트가 공유)

```json
{"v":1,"seq":1234,"sent_at":1759650000,
 "observed_at":1759649950,"sessions":2,
 "windows":{"five_hour":{"pct":47.0,"resets_at":1759657800},
            "seven_day":{"pct":21.3,"resets_at":1759990000}}}
```

- 헤더 `X-CTM-Sig: hex(hmac_sha256(key, raw_body))`를 붙인다.
- 디바이스는 다음 경우 요청을 거부한다.
  - `seq ≤ last_seq` (NVS 저장)
  - `|sent_at - device_clock| > 300s`. 첫 수신 시에는 시계를 맞춘다.
- 창은 선택 필드다(문서상 독립적으로 빠질 수 있다). 미래 확장을 위해 알 수 없는 창 키는 무시한다.

### 디스플레이 설계 (240×135 가로, Tufte 반영)

```
┌──────────────────────────────────────────┐
│ 5H                              ↻ 2h13m  │
│ 47%  ███████████▌░░░░|░░░░░░░░░░░░       │  ← | = 페이스 눈금(창 경과 %)
│ 7D                              ↻ 3d 4h  │
│ 21%  █████░░░░░░░░░░░░░░░░|░░░░░░        │
│ ● 2m ago                     ctmeter-a1  │  ← 데이터 나이 + 연결 상태
└──────────────────────────────────────────┘
```

- 5H는 가장 즉각적으로 행동에 영향을 주므로 가장 크게 표시한다(7-seg 폰트 7). 7D는 그보다 작게 둔다.
- 색 규칙
  - 사용률이 페이스 이하이면 중립(흰/회)으로 표시한다.
  - 사용률이 페이스를 넘으면 앰버로 표시한다.
  - 80% 이상이면 주황, 95% 이상이면 빨강으로 표시한다.
- stale 규칙
  - 데이터가 10분 넘게 갱신되지 않으면 상태 점이 회색이 된다. 하트비트가 끊기면 점이 빨강이 되고 "bridge offline"을 표시한다.
  - 리셋 시각이 지난 창은 "0% · reset" 회색 이탤릭 느낌으로 표시한다. CC가 다음 응답을 받기 전까지는 추정값이다.
- 한글은 디바이스 화면에 쓰지 않는다. 영문, 숫자, 기호만 쓴다(gotcha 3.15, 폰트 파이프라인 회피).
- 디자인 반복은 `tools/preview.py`(Pillow)로 같은 레이아웃을 PNG로 렌더링해 디바이스 업로드 없이 빠르게 돈다.
- 전력: 1시간 동안 갱신이 없으면 백라이트를 자동으로 디밍한다. modem-sleep은 유지한다.

---

## 저장소 구조 (계획)

```
CLAUDE.md                     # 입구 문서 + 작업 규칙 (형제 프로젝트 형식 승계)
docs/ HARDWARE.md(기존) SPEC.md PROTOCOL.md ARCHITECTURE.md TASKS.md
firmware/  platformio.ini partitions_16mb.csv
           src/{app,net,ui,state,diag,proto}/   # proto=파싱·HMAC·seq (순수 C++)
           test/native/                         # pio test -e native
host/      ctm/ (statusline.py, bridge.py, cli.py, proto.py)  tests/  launchd/com.ctm.bridge.plist
fixtures/  *.json  (정상, 창 누락, 리셋 경과, 서명 불일치, seq 역행)
tools/     preview.py  genstrings.py(필요 시)
```

형제 프로젝트에서 그대로 복사해 올 파일: `buttons.*`, `diag.*`(DiagCode 수정), `display.*`(레이아웃 상수 제외), `wifi_ntp.*`의 `connectWiFi`, `platformio.ini`, `partitions_16mb.csv`.

---

## 개발 로드맵

| 단계 | 목표 | 완료 기준 |
|---|---|---|
| **P0 스파이크** (반나절) | 미확인 사실 실측 | ① 실제 statusLine JSON을 캡처해 `rate_limits` 수치가 claude.ai `/usage`와 일치하는지 확인한다 `[V]`. ② 동시에 두 세션을 띄워 동작을 본다. ③ launchd 프로세스가 LAN으로 송신할 때 macOS **로컬 네트워크 권한(TCC)** 요구가 있는지 확인한다 `[?]`. ④ `security`로 WiFi 비밀번호를 읽을 때의 UX를 확인한다. |
| **P1 Walking skeleton** | statusline → 파일 → bridge → **USB 시리얼** → 화면에 숫자 2개 | CC로 메시지를 보내면 2초 안에 디바이스 숫자가 바뀐다. |
| **P2 프로토콜·테스트 고정** | PROTOCOL.md, fixtures, 호스트 pytest, `pio test -e native`(파싱·HMAC·seq·stale 판정) | 모든 픽스처가 양쪽에서 같은 판정을 낸다. |
| **P3 디스플레이 v1** | preview.py로 레이아웃을 반복한 뒤 펌웨어로 이식한다. 페이스 눈금, 색 규칙, stale 표시를 넣는다. | 미리보기와 실기 스크린 비교 `[V]` |
| **P4 무선** | `ctm setup` 프로비저닝, NVS, WiFi, mDNS, `POST /v1/state`, HMAC. 브리지는 WiFi를 우선하고 시리얼을 대체로 쓴다. | USB를 뽑고 USB 충전기에 연결해도 갱신된다. 잘못된 서명은 거부된다. |
| **P5 견고성** | Mac 슬립/웨이크, IP 변경, 공유기 재부팅, 디바이스 전원 차단 후 `last_state` 복원, 하트비트 끊김 표시, WDT, diag | 장애 시나리오 체크리스트를 모두 `[V]`로 확인한다. |
| **P6 마감** | launchd 설치 스크립트(`ctm install`), 백라이트 정책, 상세 뷰, OTA(ArduinoOTA, 서명된 키로만) 선택 적용, README | 새 Mac에서 `ctm install && ctm setup`만으로 동작한다. |

## 개발 방법론

- **계약 우선**: PROTOCOL.md와 fixtures를 먼저 고정하고, 양쪽 구현이 같은 픽스처로 테스트한다.
- **Walking skeleton → 점진 확장**: 매 단계가 끝단까지 동작하는 상태를 유지한다.
- **로직과 I/O 분리**: 펌웨어 `proto/`와 `state/`는 Arduino 의존이 없는 순수 C++로 짜서 native 테스트로 검증한다. 렌더와 네트워크는 실기에서 수동 검증하고 `[V]`로 기록한다.
- **문서 규칙 승계**: `[V]`(실측), `[A]`(가정·상속), `[?]`(미결) 태그를 쓴다. 동작이 바뀌면 SPEC/HARDWARE를 갱신한다. 끝난 작업은 TASKS에서 삭제하고, 이력은 커밋에 남긴다.

## CLAUDE.md에 적어 둘 사항 (Claude Code용)

1. 코드를 쓰기 전에 `docs/HARDWARE.md` §6.3 gotcha부터 읽는다. pio 경로는 `~/.platformio/penv/bin/pio`, 포트는 `/dev/cu.usbserial-59150189961`, 업로드 속도는 460800이다.
2. **OAuth 토큰과 Keychain의 `Claude Code-credentials`는 절대 읽거나 출력하거나 사용하지 않는다.** `/api/oauth/*` 같은 비공개 엔드포인트도 호출하지 않는다. 사용자 결정 사항이다.
3. statusline 스크립트에 네트워크 I/O나 느린 작업을 넣지 않는다. 목표는 20ms 이하다. `rate_limits`가 없으면 기존 값을 덮어쓰지 않는다.
4. 테스트와 개발 중에는 Anthropic에 실제 요청을 보내지 않는다. fixtures와 `ctm push-test`(모의 데이터)를 쓴다.
5. 비밀(WiFi 비밀번호, 페어링 키)은 커밋하지 않고, 로그나 시리얼에도 출력하지 않는다. 마스킹한다.
6. 실패를 `ESP.restart()`로 숨기지 않는다. WiFi 이벤트 콜백에서 TFT를 그리지 않는다(gotcha 3.16). modem-sleep을 끄지 않는다(gotcha 3.5).
7. 검증 스크립트는 fail-closed로 짠다. 단계 하나라도 실패하면 결론을 내지 않는다.
8. 모르는 사실은 추측하지 않고 `[?]`로 남긴 뒤 질문한다. 열린 설계 질문은 형제 프로젝트 규칙대로 페르소나 토론으로 다룬다.
9. 파괴적 작업(erase, 파티션 변경) 전후로 커밋한다.

## 검증 (End-to-end)

1. 호스트: `pytest host/tests`(병합 규칙, 서명, 송신 정책)와 `echo <fixture> | ctm-statusline`으로 상태 파일이 생성되는지 확인한다.
2. 펌웨어: `pio test -e native`(파싱, HMAC, seq, stale)를 돌리고 `pio run -e t-display -t upload`로 올린다.
3. 실기 시나리오: 각 결과를 `[V]`로 기록한다.
   - CC로 메시지 1회를 보내면 30초 안에 화면이 반영된다.
   - 두 세션을 동시에 띄운다.
   - USB를 분리하고 충전기 전원으로 바꾼다.
   - Mac을 슬립했다가 깨운다(offline 표시 후 복귀).
   - 잘못된 키로 `ctm push-test`를 보내면 거부된다.
   - 5h 리셋 시각이 지나면 reset 표시가 뜬다.
   - 디바이스 전원을 껐다 켜면 `last_state`가 복원된다.

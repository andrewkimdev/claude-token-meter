# Claude Token Meter — 입구 문서

LilyGO T-Display(클래식 ESP32)에 현재 Mac에 로그인된 Claude 구독 계정의 사용 한도를 표시한다. 표시 대상은 5시간 창과 7일 창이다. 이 장치는 다른 기능을 하지 않는다.

## 문서 지도
- `docs/ARCHITECTURE.md`: 아키텍처, 페르소나 의견, 로드맵, 방법론. **먼저 읽는다.**
- `docs/HARDWARE.md`: 보드 사실, 툴체인, gotcha. **코드를 쓰기 전에 §6.3부터 읽는다.**
- `docs/PROTOCOL.md`, `docs/SPEC.md`, `docs/TASKS.md`: 작성 예정 (P2~)

## 확정된 결정
1. 데이터 소스는 **Claude Code statusLine JSON의 `rate_limits`만** 쓴다. Anthropic으로 가는 추가 요청은 0건이다.
2. 통신은 WiFi로 한다. Mac 브리지가 HMAC으로 서명한 HTTP POST로 푸시한다. 설정은 USB 1회 셋업(`ctm setup`)으로 주입하고, USB 시리얼은 대체 경로로 남긴다.
3. 디바이스는 Anthropic 자격증명을 전혀 갖지 않는다.

## 작업 규칙
1. **OAuth 토큰과 Keychain의 `Claude Code-credentials`는 절대 읽거나 출력하거나 사용하지 않는다.** `/api/oauth/*` 같은 비공개 엔드포인트도 호출하지 않는다.
2. statusline 스크립트에 네트워크 I/O나 느린 작업을 넣지 않는다. 목표는 20ms 이하다. `rate_limits`가 없으면 기존 값을 덮어쓰지 않는다.
3. 테스트와 개발에는 fixtures와 `ctm push-test`(모의 데이터)만 쓴다. Anthropic에 실제 요청을 보내지 않는다.
4. 비밀(WiFi 비밀번호, 페어링 키)은 커밋하지 않고, 로그나 시리얼에도 출력하지 않는다.
5. 실패를 `ESP.restart()`로 숨기지 않는다(gotcha 3.7). WiFi 이벤트 콜백에서 TFT를 그리지 않는다(3.16). modem-sleep을 끄지 않는다(3.5). 화면에 한글을 쓰지 않는다(3.15).
6. 검증 스크립트는 fail-closed로 짠다. 단계 하나라도 실패하면 결론을 내지 않는다. 파일의 부재는 증거가 아니다.
7. 사실 태그: `[V]` 실측, `[A]` 가정·상속, `[?]` 미결. 모르는 것은 추측하지 않고 `[?]`로 남긴 뒤 질문한다.
8. 열린 설계 질문은 페르소나 토론으로 다루고, 기술 질문은 전문가 페르소나 한 명이 답한다. 어느 쪽이든 페르소나를 명시한다.
9. 작업 단위마다 커밋한다. 파괴적 작업(erase, 파티션 변경) 전후에도 커밋한다. 동작이 바뀌면 SPEC/HARDWARE를 갱신한다.

## 툴체인 요약
- pio: `~/.platformio/penv/bin/pio` (PATH에 없음)
- 포트: CH9102 보드는 `/dev/cu.usbserial-<serial>`로 뜬다. 대상 보드는 `ctm select-board`로 골라 `~/.claude-token-meter/config.json`에 저장한다.
  - 우선순위: CLI 플래그 > 환경변수 `CTM_USB_SERIAL` > config.json. 현재 장치 경로는 `cd host && python -m ctm.cli port`로 확인한다.
  - `platformio.ini`에는 `upload_port`를 하드코딩하지 않는다. 업로드: `~/.platformio/penv/bin/pio run -d firmware -e t-display -t upload --upload-port "$(cd host && python -m ctm.cli port)"`
- 업로드 속도: 460800

## 현재 상태
- **P1 완료 `[V]`.** 경로는 statusLine(`host/ctm/statusline.py`) → `~/.claude-token-meter/sessions/` → 브리지 → USB → 디바이스다. 모의 값 변화가 약 1초 만에 화면까지 전달되고 ACK를 받는다.
- 브리지 실행: `cd host && ~/.platformio/penv/bin/python3 -m ctm.bridge`. 브리지가 포트를 TIOCEXCL로 소유한다.
  - 업로드할 때: `kill -USR1 $(cat ~/.claude-token-meter/serial/hold.pid)`로 해제 → 업로드 → 같은 명령으로 재획득.
  - 브리지가 꺼져 있을 때는 `tools/serial_hold.py`로 포트를 붙잡는다.
- 대상 보드: `ctm select-board`로 선택한 USB 시리얼 번호의 보드. **`ctm port`로 확인한 포트에만 업로드한다.**
- 호스트 테스트: `cd host && /usr/bin/python3 -m unittest discover -s tests -t .`
- 펌웨어 빌드: `~/.platformio/penv/bin/pio run -d firmware -e t-display`
- **P2 완료 `[V]`**: `bash tools/check_fixtures.sh`는 호스트 unittest와 `pio test -e native`를 함께 돌리고, 둘 중 하나라도 실패하면 결론을 내지 않는다(fail-closed). 프레임 fixture 16개에서 양쪽 판정이 일치한다.
- **P3 완료 (빌드 `[V]`, 버튼과 디밍은 실기 미검증 `[?]`)**: 상세 뷰(BTN1), 백라이트 3단(BTN2), 60분 무변화 시 자동 디밍, host offline 표시를 넣었다.
  - `tools/preview.py`로 화면을 PNG로 미리 볼 수 있다. `render.cpp`를 바꾸면 `preview.py`도 함께 바꾼다.
- **P4 완료 `[V]`**: 디바이스 이름은 `ctmeter-xxxx`(STA MAC 하위 2바이트)다. 브리지를 `--transport auto`로 띄우면 USB가 있으면 USB로, 없으면 WiFi(서명된 HTTP)로 보낸다.
  - `ctm setup`은 반드시 일반 터미널에서 실행한다(`!` 불가).
- **P5/P6 완료 `[V]`**:
  - 펌웨어 0.3.0: 전원을 다시 켜면 마지막 값을 "restored"로 복원한다. WiFi 재접속 백오프, WDT가 들어갔다.
  - 브리지는 LaunchAgent `com.ctm.bridge`로 돈다(`ctm install`). 점검은 `ctm doctor`로 한다.
  - LaunchAgent로 돌아가므로 업로드할 때 브리지를 일시정지하는 방법은 같다: `kill -USR1 $(cat ~/.claude-token-meter/serial/hold.pid)` 두 번.
- 남은 일과 미결 항목: `docs/TASKS.md`.

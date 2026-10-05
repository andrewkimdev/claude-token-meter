# P0 스파이크 결과 (2026-10-05)

ARCHITECTURE.md의 P0 항목을 실측했다. 이 결과에 따라 ARCHITECTURE.md를 수정했다(아래 "설계 변경").

## ① statusLine `rate_limits` 실측 `[V]`
- 캡처 스크립트: `host/spike/capture_statusline.py`
  - 로그 위치: `~/.claude-token-meter/spike/statusline.jsonl`
  - 등록 위치: `~/.claude/settings.json`의 `statusLine`. 원본 백업은 `settings.json.pre-ctm-spike`다.
- `settings.json`을 바꾸면 **실행 중인 모든 CC 세션에 즉시 반영된다**(재시작 불필요). 이번에 세션 8개가 동시에 스크립트를 호출했다.
- 실제 형태: `"rate_limits":{"five_hour":{"used_percentage":52,"resets_at":1791186600},"seven_day":{"used_percentage":29,"resets_at":1791511200}}`
  - `used_percentage`는 **정수**로 관측됐다. 문서 예시는 실수다. 파서는 둘 다 받아야 한다.
- CC 2.1.283/2.1.284 세션은 `rate_limits: null`이었고, 2.1.289 세션에만 값이 있었다.
- **같은 계정, 같은 순간에도 세션마다 값이 다르다.** 5h가 52 / 48 / 40%로 갈렸고, `five_hour` 없이 `seven_day` 13%만 가진 세션도 있었다. 각 세션이 자기 마지막 API 응답 시점의 값을 들고 있기 때문이다.
- 실행 시간은 약 20–30ms였다(`/usr/bin/python3 -S`). 첫 콜드 실행만 2.8s가 걸렸다.
- `[?]` claude.ai `/usage` 값과 일치하는지는 사용자가 확인해야 한다.

## ② 동시 세션
①에서 함께 관측했다. 세션 수는 많고(8개), 값은 서로 어긋난다. 병합 규칙 변경은 아래 "설계 변경"에 있다.

## ③ launchd 프로세스의 LAN 송신 (Local Network TCC) `[V]`
- 도구: `host/spike/lan_probe.py`
- 셸에서 실행할 때와 LaunchAgent(ppid=1)로 실행할 때 결과가 같았다.
  - 게이트웨이 TCP 연결 성공
  - 닫힌 포트는 ECONNREFUSED(LAN 도달은 허용됨을 뜻한다)
  - UDP 유니캐스트와 mDNS 멀티캐스트 송신 성공
  - EHOSTUNREACH는 한 번도 나오지 않았다.
- 결론: 평범한 LaunchAgent와 시스템 python으로 충분하다. 서명된 .app은 필요 없다.
- `[?]` python3가 이미 로컬 네트워크 허용 목록에 있었을 가능성이 있다. 새 Mac에서는 프롬프트가 뜰 수 있다.
  - 브리지는 LAN 대상에서 EHOSTUNREACH가 나면 "로컬 네트워크 권한" 안내를 출력해야 한다.
- `[?]` 다른 장치(ESP32)의 `.local` 해석은 디바이스가 mDNS를 광고한 뒤(P4)에 검증한다.

## ④ SSID 읽기 `[V]`
- `networksetup -getairportnetwork`는 "not associated"를 돌려준다. `ipconfig getsummary`와 `system_profiler`는 SSID를 `<redacted>`로 숨긴다. 위치 권한 정책 때문이다.
- 따라서 Mac 쪽에서 SSID를 자동으로 읽는 방법은 쓰지 않는다. WiFi 비밀번호를 Keychain에서 읽는 것도 기본으로 하지 않는다.

## 디바이스 식별 `[V]`
| 항목 | 값 |
|---|---|
| 포트 | `/dev/cu.usbserial-59150189961` |
| USB 칩 | CH9102, VID `0x1A86`, PID `0x55D4` |
| USB 시리얼 번호 | **`5915018996`** |
| USB 위치 | locationID `51380224` |

- 현재 펌웨어는 형제 프로젝트(bus-eta)의 것이다. 부팅할 때 `[diag]` 로그가 나온다.
- 포트 점유는 `tools/serial_hold.py`가 한다.
  - 장치를 USB 시리얼 번호로 찾고, `TIOCEXCL`로 독점해 로그를 남긴다.
  - 다른 프로세스가 열면 `[Errno 16] Resource busy`로 실패한다 `[V]`.
  - pyserial의 `exclusive=True`(flock)로는 **막히지 않았다** `[V]`. 권고형 잠금이라서다.
  - 업로드 전에 `kill -USR1 $(cat ~/.claude-token-meter/serial/hold.pid)`로 해제하고, 업로드 후 같은 명령으로 다시 잡는다.
  - 상태 파일: `~/.claude-token-meter/serial/hold.state`. 로그: `.../serial/YYYYMMDD.log`.
  - `[?]` DTR/RTS를 내린 채 열었는데도 처음 open 때 POWERON 리셋 로그가 찍혔다. 리셋 없이 여는 방법은 P1에서 확인한다.

## 설계 변경
1. **병합 규칙**: 관측 시각 기준 last-writer-wins를 폐기한다. 창마다 다음 규칙으로 병합한다.
   - `resets_at`이 다르면 더 늦은 쪽이 새 창이므로 교체한다.
   - `resets_at`이 같으면 같은 창이고 사용률은 줄지 않으므로 `max(pct)`를 취한다.
   - 이 규칙은 순서와 무관하고 멱등이다. 오래된 세션이 다시 실행돼도 값이 퇴행하지 않는다.
2. **WiFi 프로비저닝**: `ctm setup`은 다음 순서로 진행한다.
   1. 디바이스에 USB로 `{"cmd":"scan"}`을 보낸다.
   2. 디바이스가 SSID 목록과 RSSI를 응답한다.
   3. 사용자가 목록에서 SSID를 고르고 비밀번호를 입력한다(`getpass`).
   4. `provision`을 보낸다.
   - Keychain에서 WiFi 비밀번호를 읽는 기능은 옵트인으로만 둔다.

## P1에서 추가로 확인한 것 `[V]`
- 브리지가 포트를 열면 보드가 리셋된다(POWERON). ROM 배너(74880bps)는 개행 없이 끝나서 첫 앱 줄 `@{"t":"hello"}` 앞에 쓰레기 바이트가 **같은 줄로** 붙는다.
  - 호스트 파서는 줄 안의 마지막 `@{`부터 파싱한다.
- 부팅 중에 보낸 첫 프레임은 `nak parse`가 된다. `hello`를 받으면 다시 보내므로 무해하다.
  - 개선 후보: 연결 직후에는 `hello`를 받거나 2초가 지날 때까지 송신을 미룬다(P5).

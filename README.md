# Claude Token Meter

LilyGO T-Display(클래식 ESP32)에 **이 Mac에 로그인된 Claude 구독 계정의 사용 한도**를 표시하는 전용 장치다. 5시간 창과 7일 창의 사용률, 리셋 시각, 페이스를 보여 주고 다른 기능은 없다.

- 데이터는 Claude Code statusLine JSON의 `rate_limits`에서만 가져온다. Claude Code가 이미 받은 응답에 편승하므로 Anthropic으로 가는 추가 요청은 없다.
- Mac 브리지가 HMAC으로 서명한 HTTP POST로 WiFi를 통해 디바이스에 값을 보낸다. USB 시리얼은 대체 경로다.

자세한 설계는 `docs/ARCHITECTURE.md`, 프로토콜은 `docs/PROTOCOL.md`, 실측 기록은 `docs/P0_FINDINGS.md`, 호스트 도구는 `host/README.md`에 있다.

## 구조

```
[Claude Code 세션들] --stdin JSON--> host/ctm/statusline.py (≤20ms, 네트워크 없음)
                                          │ 세션별 atomic write
                                          ▼
                         ~/.claude-token-meter/sessions/<session_id>.json
                                          │ 폴링 2s
                                          ▼
                  ctm-bridge (LaunchAgent com.ctm.bridge, Python)
                    ├─ 병합: 창별 (resets_at 큰 쪽 → 같으면 max pct)
                    ├─ 송신: 변화 시 즉시 + 하트비트 120s, 웨이크 시 즉시
                    ├─ 서명: HMAC-SHA256(pairing_key, body)
                    └─ 전송: USB가 꽂혀 있으면 USB 시리얼, 아니면 WiFi(mDNS → HTTP POST)
                                          ▼
                    T-Display 펌웨어: 검증 → 상태 머신 → 렌더
```

## 빠른 시작

준비물: T-Display, USB 케이블, PlatformIO(`~/.platformio/penv`, pyserial 포함), 2.4GHz WiFi.

1. **펌웨어 굽기** (보드를 USB로 연결)
   ```sh
   ~/.platformio/penv/bin/pio run -d firmware -e t-display -t upload
   ```
2. **브리지와 statusLine 설치**
   ```sh
   cd host
   ~/.platformio/penv/bin/python3 -m ctm.cli install --statusline --dry-run   # 먼저 확인
   ~/.platformio/penv/bin/python3 -m ctm.cli install --statusline
   ```
   - `~/Library/LaunchAgents/com.ctm.bridge.plist`를 쓰고 `launchctl bootstrap`으로 띄운다. 로그인할 때 자동으로 시작되고, 죽으면 다시 뜬다.
   - `~/.claude/settings.json`의 `statusLine`을 등록한다. 기존 파일은 `settings.json.bak-ctm-<시각>`으로 백업한다. 다른 statusLine이 이미 있으면 바꾸지 않고 멈춘다(`--force`로 교체).
   - 셸에서 직접 띄운 브리지가 돌고 있으면 거부한다. `--replace`를 주면 bootstrap 직전에 그것을 멈춘다.
3. **WiFi 페어링**: 반드시 **일반 터미널(Terminal/iTerm)**에서 실행한다.
   ```sh
   cd host && ~/.platformio/penv/bin/python3 -m ctm.cli setup
   ```
   SSID를 고르고 비밀번호를 입력하면 끝이다. 이후에는 USB를 빼고 충전기 전원만 연결해도 WiFi로 갱신된다.
4. **확인**
   ```sh
   cd host && ~/.platformio/penv/bin/python3 -m ctm.cli doctor
   ```

제거: `ctm uninstall [--statusline]`. 로그, 세션 파일, 페어링은 남는다. 페어링까지 지우려면 `ctm unpair`를 실행한다.

## 문제 해결

- **`ctm doctor`부터 실행한다.** statusLine 등록, 세션 파일과 병합 값, 브리지 프로세스, LaunchAgent, 전송 경로, 디바이스 도달(USB 존재, WiFi `/v1/info`), Keychain 페어링 키 존재(값은 출력하지 않음), 마지막 ACK 시각을 각각 PASS/FAIL/SKIP으로 보여 준다. 모든 검사가 실행됐을 때만 최종 판정을 낸다.
- **로컬 네트워크 권한**: 브리지 로그에 `EHOSTUNREACH`나 "Local Network" 안내가 보이면 시스템 설정 > 개인정보 보호 및 보안 > 로컬 네트워크에서 브리지를 실행하는 python을 허용한다. 새 Mac에서는 첫 송신 때 허용 프롬프트가 뜰 수 있다.
- **`curl`로 디바이스를 볼 때는 `curl -4`를 쓴다.** 옵션 없이 쓰면 `.local`의 IPv6를 먼저 시도하다 시간 초과가 난다. 브리지와 `doctor`는 IPv4만 쓰므로 영향이 없다.
  ```sh
  curl -4 http://ctmeter-xxxx.local/v1/info
  ```
- **Claude Code의 `!` 접두사에는 TTY가 없다.** 그래서 `ctm setup`은 거기서 실행되지 않고 exit 2로 거부한다. 일반 터미널에서 실행한다.
- **슬립/웨이크, 공유기 재부팅, IP 변경**: 브리지가 웨이크를 감지하면 즉시 다시 보내고 백오프를 초기화한다. 송신이 실패하면 캐시된 IP를 믿지 않고 매 재시도 전에 `<host>.local`을 다시 해석하며, 성공하면 `device.json`의 IP를 갱신한다.
- **펌웨어를 다시 구울 때**: 브리지가 포트를 독점하고 있다. `kill -USR1 $(cat ~/.claude-token-meter/serial/hold.pid)`로 해제하고 업로드한 다음, 같은 명령으로 다시 잡는다.
- **로그**: `~/.claude-token-meter/serial/YYYYMMDD.log`(14일 보관, 하루 약 5MB 상한. 상한을 넘으면 브리지 이벤트와 프로토콜 줄만 남긴다), launchd 출력은 `~/.claude-token-meter/launchd/`에 있다.

## 프라이버시

- Anthropic 자격증명(OAuth 토큰, Keychain의 `Claude Code-credentials`)을 읽지도 쓰지도 않는다. 디바이스도 자격증명을 전혀 갖지 않는다.
- Anthropic으로 가는 추가 요청은 0건이다. 트래픽은 Mac과 디바이스 사이의 LAN(또는 USB)뿐이다.
- 페어링 키(32B 무작위)는 Mac Keychain(`ctm-pairing`)과 디바이스 NVS에만 있다. WiFi 비밀번호는 디바이스 NVS에만 있다. 둘 다 로그나 화면에 출력하지 않는다.
- 디바이스로 보내는 값은 사용률, 리셋 시각, 관측 시각뿐이다.

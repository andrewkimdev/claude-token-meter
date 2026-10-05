# 프로토콜 v1

호스트(Mac)와 디바이스 사이의 계약이다. 양쪽 구현은 반드시 이 문서와 `fixtures/`를 따른다. 이 문서를 바꿀 때는 양쪽 구현과 픽스처를 함께 바꾼다.

## 1. 호스트 내부: statusLine → 세션 파일

- `ctm-statusline`은 호출될 때마다 **자기 세션 파일 하나만** 원자적으로 쓴다(tmp에 쓴 뒤 rename). 세션 8개가 동시에 실행돼도 경합이 없게 하기 위해서다.
  - 경로: `~/.claude-token-meter/sessions/<session_id>.json`
  - 내용: `{"session_id":"...","observed_at":<epoch float>,"cc_version":"2.1.289","rate_limits":{...원본 그대로...}}`
- `rate_limits`가 없거나 `null`이면 **파일을 쓰지 않는다**. 이전 관측을 지우지 않기 위해서다.

## 2. 병합 규칙 (브리지)

모든 세션 파일을 읽어 창(`five_hour`, `seven_day`)마다 독립적으로 병합한다.

1. `resets_at <= now`인 관측은 버린다. 그 창은 이미 리셋됐다.
2. 남은 관측 중 `resets_at`이 **가장 늦은** 값을 고른다.
3. 같은 `resets_at`을 가진 관측이 여럿이면 `used_percentage`의 **최댓값**을 쓴다.
4. 그 창의 `observed_at`은 채택된 관측들 중 최댓값으로 한다.
5. 남는 관측이 없으면 그 창은 생략한다.

세션 파일의 mtime이 8일보다 오래됐으면 브리지가 삭제한다.

`resets_at`이 같다는 것은 같은 창이라는 뜻이고, 한 창 안에서 사용률은 줄지 않는다. 그래서 위 규칙은 입력 순서와 무관하고, 같은 입력을 여러 번 넣어도 결과가 같다.

## 3. 상태 프레임 (호스트 → 디바이스)

```json
{"t":"state","v":1,"seq":17,"sent_at":1791180000,
 "windows":{"five_hour":{"pct":52,"resets_at":1791186600,"observed_at":1791179913},
            "seven_day":{"pct":29,"resets_at":1791511200,"observed_at":1791179913}}}
```

- 모든 시각은 epoch **초**(정수)다. `pct`는 0–100이며 정수나 실수로 온다.
- `seq`는 **브리지가 재시작해도 줄지 않게** `seq = max(last_seq + 1, int(time.time()))`로 만든다(P4~). 값은 uint32 범위에 들어간다. 디바이스는 `seq`가 직전 값 이하이면 거부한다. 다음 두 경우는 예외로 받아들인다.
  - 디바이스가 재부팅한 직후의 첫 프레임
  - `seq == 1`: P1 호환용으로 남겨 둔 규칙이다. USB에서만 의미가 있고, WiFi에서는 §4.2의 시각 검사가 함께 적용된다.
- `windows` 안의 창은 각각 선택 사항이다. 모르는 키는 무시한다.
- 디바이스는 `sent_at`으로 시계를 맞춘다(`settimeofday`). NTP는 쓰지 않는다.
- **하트비트**: 창 값이 바뀌지 않아도 같은 형식의 프레임을 120초마다 보낸다.

### 3.1 수신 판정 규칙 (정본: `firmware/src/core/protocol.cpp`, 참조 구현: `host/ctm/proto.py:device_accept`)
판정은 아래 순서대로 한다. 각 규칙은 `fixtures/frame_*.json`으로 양쪽에서 검증된다.

1. JSON이 아니거나, 객체가 아니거나, `t`가 문자열이 아니면 `nak parse`.
2. `t != "state"`이면 **무시한다(응답 없음)**. 앞으로 추가될 명령(`provision`, `scan` 등)과의 호환을 위해서다.
3. `v != 1`이면 `nak bad_v`.
4. `seq`가 1 이상 2³²−1 이하의 정수가 아니면 `nak parse`. 0은 무효다.
5. `sent_at`, `resets_at`, `observed_at`이 0 이상의 유한한 수가 아니면 `nak parse`.
6. `windows`가 없으면 빈 객체로 본다. 객체가 아닌 값이면 `nak parse`.
7. `pct`가 0–100 범위를 벗어나면 거부하지 않고 0–100으로 잘라서 쓴다(clamp).
8. `observed_at`이 없으면 `sent_at`으로 대체한다.
9. 위 검사를 모두 통과한 뒤에 seq 규칙(§3)을 적용한다. 따라서 형식이 잘못된 프레임은 seq를 전진시키지 못한다.

## 4. 전송 계층

### 4.1 USB 시리얼 (P1)
- 115200 8N1이다. 한 줄에 프레임 하나이며 `\n`으로 끝난다. 줄 하나의 최대 길이는 1024B다.
- 디바이스 → 호스트 방향으로는 디바이스 로그가 자유 텍스트로 섞여 나온다. 프로토콜 응답 줄은 **`@`로 시작한다**.
  - `@{"t":"ack","seq":17}`: 상태 프레임을 받아 적용했다.
  - `@{"t":"nak","seq":17,"err":"parse|old_seq|bad_v"}`: 프레임을 거부했다.
  - `@{"t":"hello","fw":"0.1.0","board":"t-display"}`: 부팅이 끝났다. 호스트는 이 줄을 보면 즉시 상태 프레임을 다시 보낸다.
- USB에서는 서명하지 않는다. 물리적으로 연결된 경로를 신뢰한다.
- 포트는 브리지가 소유한다. 장치를 USB 시리얼 번호로 찾고 `TIOCEXCL`로 독점하며, `SIGUSR1`로 해제와 재획득을 토글한다. 브리지는 수신한 모든 줄을 로그로 남긴다.

### 4.2 WiFi HTTP (P4)
- 디바이스의 호스트 이름은 `ctmeter-XXXX`다(XXXX는 STA MAC 주소 하위 2바이트의 hex 소문자). mDNS로 `ctmeter-XXXX.local`과 서비스 `_ctmeter._tcp`(포트 80, TXT `fw=<ver>`)를 광고한다.
- `POST /v1/state`
  - 본문은 §3의 프레임 JSON이다. 개행은 있어도 되고 없어도 된다. 최대 1024B다.
  - 헤더 `X-CTM-Sig`에는 본문 바이트 그대로에 대한 `hmac_sha256(key, body)`를 hex 소문자 64자로 담는다.
  - 처리 순서:
    1. 서명을 상수 시간으로 비교한다. 서명이 없거나 틀리면 **401** `{"t":"nak","err":"sig"}`.
    2. §3.1 규칙을 적용한다. 형식 오류는 **400** `{"t":"nak","seq":N,"err":"parse|bad_v"}`.
    3. seq 규칙 위반은 **409** `{"t":"nak","seq":N,"err":"old_seq"}`.
    4. **시각 검사는 seq 규칙의 예외 경로에만 적용한다.**
       - `seq`가 직전 값보다 큰 프레임은 seq 규칙만으로 재생이 막힌다. 그래서 시계 차이가 커도 받아들이고, 그 `sent_at`으로 디바이스 시계를 다시 맞춘다.
       - 이렇게 하는 이유: 디바이스 시계는 받아들인 프레임으로만 맞춰진다. 시계 차이를 이유로 거부하면 영원히 거부되는 교착이 생긴다.
       - 시각 검사는 `seq == 1` 재시작 예외로 들어오는 프레임에만 적용한다. 디바이스 시계가 맞춰져 있고 `|sent_at - now| > 300`이면 **409** `{"t":"nak","seq":1,"err":"skew"}`. 캡처해 둔 seq=1 프레임을 나중에 다시 보내는 공격을 막기 위해서다.
  - 성공하면 **200** `{"t":"ack","seq":N}`.
  - 서명이 맞지 않는 프레임은 seq와 시계 어느 쪽도 바꾸지 못한다.
- `GET /v1/info`: 서명 없이 열어 둔다. 비밀은 담지 않는다.
  - 응답 예: `{"t":"info","fw":"0.2.0","host":"ctmeter-a1b2","ip":"…","rssi":-55,"uptime_s":123,"last_seq":N,"provisioned":true}`
- 페어링 키는 32B 무작위 값이다.
  - 디바이스: NVS에 저장한다.
  - Mac: Keychain의 generic password(`service=ctm-pairing`, `account=<host 이름>`)에 hex로 저장한다.
  - 장치 정보(호스트 이름, 마지막 IP)는 `~/.claude-token-meter/device.json`에 저장한다. 키는 이 파일에 넣지 않는다.
- **전송 선택(브리지)**: 대상 USB 시리얼 장치가 꽂혀 있으면 USB로 보내고, 없으면 WiFi로 보낸다. `--transport usb|wifi|auto`로 강제할 수 있다.
  - WiFi로 보낼 때는 `device.json`의 호스트 이름을 mDNS로 해석한다. 실패하면 캐시된 IP를 쓴다.
  - 송신 정책은 변화 시 즉시(최소 간격 30초)와 하트비트 120초다.
  - LAN 대상에서 EHOSTUNREACH가 나면 로그에 "macOS 로컬 네트워크 권한 확인"을 안내한다.

### 4.3 USB 명령 (프로비저닝, P4)
호스트가 디바이스로 보내는 JSON 한 줄이다. 디바이스는 `@`로 시작하는 줄로 응답한다. 이 명령들은 §3.1 규칙 2에 따라 상태 프레임 판정기가 아니라 명령 분배기가 처리한다.

| 요청 | 응답 |
|---|---|
| `{"t":"scan"}` | `@{"t":"scan","nets":[{"ssid":"…","rssi":-50,"open":false},…]}`: 같은 SSID는 한 번만, RSSI가 강한 순으로 최대 15개, 빈 SSID는 제외한다. |
| `{"t":"provision","ssid":"…","pass":"…","key":"<64 hex>"}` | NVS에 저장한 뒤 최대 15초 동안 접속을 시도한다. 성공하면 `@{"t":"provision","ok":true,"host":"ctmeter-xxxx","ip":"…"}`, 실패하면 `@{"t":"provision","ok":false,"err":"bad_args|no_ap|auth|timeout"}`. 실패해도 저장된 값은 유지한다(나중에 재시도할 수 있게). |
| `{"t":"info"}` | `@{"t":"info",…}`: `/v1/info`와 같은 내용에 `"wifi":"off|connecting|connected"`를 더한다. |
| `{"t":"unpair"}` | NVS의 WiFi 설정과 키를 지우고 WiFi를 끈다. `@{"t":"unpair","ok":true}` |

- **비밀번호와 키는 디바이스 로그에도, 호스트 로그에도 절대 남기지 않는다.** 디바이스는 수신한 명령 줄을 에코하지 않는다.
  - `ctm setup`은 브리지를 SIGUSR1로 잠시 멈춘 상태에서 포트를 직접 쓴다. 이렇게 하면 브리지 로그에도 남지 않는다.
- 아직 설정되지 않은 디바이스는 WiFi를 끈 상태로 두고, 화면에 "USB: run ctm setup"을 안내한다.

### 4.4 HMAC 픽스처
`fixtures/hmac_*.json`은 `{"key_hex":"…","body":"…","sig":"…","expected":"ok|sig"}` 형식이다. Python의 `hmac` 모듈로 만들고 펌웨어 native 테스트로 대조한다.

## 5. 픽스처
`fixtures/`에 넣는다. 각 파일은 입력(세션 파일들 또는 프레임)과 기대 결과를 담는다. 호스트 테스트(pytest)와 펌웨어 테스트(native)가 같은 파일을 쓴다.

# 하드웨어 · 툴체인 · 고차(gotcha)

LilyGO T-Display 보드 자체의 사실(§6.1), 개발 툴체인(§6.2), 코드를 쓰기 전에
반드시 읽어야 하는 고차(§6.3), 현재 알려진 양품 상태인 `platformio.ini`
요약(§6.4)을 담고 있다. **이 문서의 사실들은 실측으로 확정된 뒤 반박된 적이
없다** — 새 코드를 작성하기 전에는 항상 §6.3부터 먼저 읽는다. 입구 문서와
작업 규칙은 루트 `CLAUDE.md`, 기능 사양은 `docs/SPEC.md`.

---

## 6. 하드웨어 · 툴체인 · 하드웨어 고차

### 6.1 하드웨어 `[V]`

LilyGO/TTGO **T-Display** (클래식 ESP32, S3 아님). 리셀러 SKU `TS0636G-T-Display`.
LilyGO 문서와 Setup25를 정본으로 취급한다.

- ESP32-D0WDQ6 rev v1.1, 240MHz 듀얼코어, 40MHz 크리스털
- SRAM 520KB, **PSRAM 없음**
- 플래시 **16MB 진짜** Winbond W25Q128 (앨리어싱 테스트로 확정)
- 파티션 테이블: **`partitions_16mb.csv` 적용 완료 `[V]`** (gotcha 3.8)
- ST7789V 135×240 IPS, 4-wire SPI, `TFT_MISO=-1` (읽기 불가)
- USB-serial **CH9102F**, `1A86:55D4`
- 온보드 센서 없음. I2C 버스(21/22) 깨끗함.
- 힙 `[V]`: 총 330,816B, WiFi 전 여유 ~302KB, 최대 연속 블록 ~110KB

핀아웃:

```
TFT_MOSI 19  TFT_SCLK 18  TFT_CS 5   TFT_DC 16
TFT_RST  23  TFT_BL    4  SDA 21     SCL 22
ADC_IN   34 (배터리, ~1:2 분압)      ADC_EN 14 (gotcha 3.9b)
BUTTON1  35 (input-only, 외부 풀업 있음)   BUTTON2 0
```

터치 채널 사용 가능: **27, 32, 33** 권장 (2/13/15는 부팅 스트랩 겸용,
**12는 금지 — gotcha 3.10**).

### 6.2 툴체인 `[V]`

- 호스트: MacBook Pro M4, macOS. PlatformIO는 VS Code 확장.
- CLI: `~/.platformio/penv/bin/pio` (PATH에 없음)
- 시리얼 포트: **`/dev/cu.usbserial-<serial>`** (`<serial>`은 보드별 USB 시리얼 번호 기반) — `cu.wchusbserial*`이 아니다.
  대상 보드는 `ctm select-board`로 골라 `~/.claude-token-meter/config.json`에 저장한다
  (우선순위: CLI 플래그 > 환경변수 `CTM_USB_SERIAL` > config.json). `ctm port`가 현재 장치 경로를 출력한다.
  macOS 내장 CDC 드라이버를 쓰고 있다. 항상 `cu.*`, 절대 `tty.*` 아님.
- **업로드 속도 460800.** 921600은 macOS 내장 드라이버에서 실패한다
  (`Invalid head of packet`). 칩이 아니라 드라이버 한계다.
  WCH 커널 익스텐션을 깔면 921600이 되겠지만 포트 이름이 바뀌어 모든
  하드코딩 경로가 깨진다. 하지 않는다.
- `espressif32@^6.5.0` → 실제로는 **6.13.0**으로 뜬다. 캐럿이 떠다니므로
  `6.13.0`으로 핀 고정을 고려할 것.
- Arduino core **2.0.17** (framework-arduinoespressif32 3.20017)

### 6.3 고차 — 코드 쓰기 전에 읽을 것

**3.1 TFT_eSPI 폰트는 명시적으로 컴파일해 넣어야 한다.**
7-세그먼트 폰트(6/7/8)는 스톡 설정에서 꺼져 있다. 없는 폰트로 `drawString()`을
부르면 **아무것도 안 그리고 조용히 리턴**한다. 검은 화면에 에러 없음.
진단: `tft.fontHeight(n)`이 **0**이면 미포함. 현재 1/2/4/6/7/8 모두 확인됨
(높이 8/16/26/48/48/75).

**3.2 PlatformIO는 컴파일된 라이브러리를 캐시한다.** `build_flags` 변경 후에는
`pio run -t clean`. `build_src_filter` 변경은 clean 불필요.

**3.3 POSIX 타임존 부호는 반대다.** `KST-9`가 UTC**+**9다.

**3.4 `TFT_RST=23`** — LilyGO Setup25와 일치, 이 유닛에서 동작.

**3.5 발열.** 계산량 때문이 아니다. 순서대로 LDO 레귤레이터, WiFi 라디오,
백라이트. `WiFi.setSleep(false)`는 실수였다 — 모뎀 슬립을 끄고 40–50mA를
무의미하게 더 쓴다. LDO 부품번호 미확인 `[?]`.

**3.6 클래식 ESP32에서 못 믿을 API.**
`temperatureRead()`는 이 유닛에서 정확히 **53.33°C** 고정 — 완전한 불량.
`hallRead()`는 core 2.0.17에서 동작하나 core 3.x에서 제거됨.
LEDC 백라이트 API는 2.x/3.x 시그니처가 다르다 — `ESP_ARDUINO_VERSION_MAJOR`로 가드.

**3.7 디버깅 중 실패에 재부팅하지 않는다.** NTP 타임아웃에 `ESP.restart()`를
불렀다가 증거가 날아가 폰트 버그를 네트워크 버그로 오진한 사고가 있었다.
**그 코드가 `reference/clock_ntp.cpp`다** — 회수된 조상 버전이며, 재사용 시
반드시 제거한다. 같은 파일의 `WiFi.setSleep(false)`가 gotcha 3.5의 출처이기도
하다.

**3.8 플래시는 16MB인데 파티션 테이블은 4MB만 덮는다. — 적용 완료 `[V]`**
앨리어싱 테스트로 상위 영역이 진짜임이 확정되어 잠금 해제됐고, 실제로 적용해
하드웨어에서 검증했다. `partitions_16mb.csv`가 저장소에 있고 `platformio.ini`의
두 줄(§6.4)이 주석 해제된 상태다. 아래 표는 그 파일의 실제 내용이다:

```ini
board_upload.flash_size = 16MB
board_build.partitions  = partitions_16mb.csv
```
```csv
# Name,   Type, SubType,  Offset,   Size
nvs,      data, nvs,      0x9000,   0x5000
otadata,  data, ota,      0xE000,   0x2000
app0,     app,  ota_0,    0x10000,  0x400000
app1,     app,  ota_1,    0x410000, 0x400000
spiffs,   data, spiffs,   0x810000, 0x7E0000
coredump, data, coredump, 0xFF0000, 0x10000
```

**전체 지우기가 필요하다.** 물리 상태와 어긋난 테이블은 빌드가 아니라 **부팅에서**
실패하므로 진단이 훨씬 느리다:

```
pio run -t clean -e t-display
pio run -t erase -e t-display
pio run -e t-display -t upload
```

**실측 결과 `[V, 2026-09-13]`.** 위 세 명령을 그대로 실행. `erase`가 45초 걸림
(칩 전체 지우기). 업로드 후 `app0`가 4MB로 늘어난 것을 빌드 로그에서 확인
(이전 스톡 파티션의 1.31MB 대비). 시리얼로 부팅 확인 — 74880bps로 나오는 ROM
부트로더 배너는 깨져 보이는 게 정상이고(보레이트가 다르다), 그 뒤 앱이
`Serial.begin(115200)`으로 전환한 뒤의 `"Connected, IP: ..."` 출력은 깨끗하게
잡혔다. 크래시 루프 없음 — 새 파티션 테이블이 부팅 시점까지 안정적으로
동작함을 확인.

**3.9 배터리 ADC에 함정 셋.**
(a) 11/12dB 감쇠에서 사용 범위가 **2450mV** 근처에서 천장에 닿는다. 그 값이 나오면
측정이 아니라 **클리핑**이다. (b) LilyGO FactoryTest는 샘플링 전에 GPIO14를 HIGH로
올리고 ADC_EN이라 부르지만 실크스크린은 PWR_EN이라 하고 커뮤니티 해석도 갈린다.
올려두되 분압을 게이팅한다고 **가정하지 않는다**. (c) 분압비는 공칭 2.0이지만
정밀 저항이 아니라 실측값은 2.04 근처. eFuse에 **VRef 캘리브레이션이 있으므로**
`analogReadMilliVolts()`가 ±3% 수준으로 정확하다.

**3.10 GPIO12는 플래시 전압 스트랩 — 쓰지 않는다.**
부팅 시 HIGH면 VDD_SDIO가 1.8V로 선택되어 플래시가 올라오지 않는다. 브레드보드의
떠돌이 풀업 하나가 벽돌처럼 보이는 보드를 만든다. eFuse를 태워 고정하는 것도
하지 않는다 — 영구적이고, 회피가 더 싸다.

**3.11 zsh는 매칭 실패한 글롭에서 명령 전체를 거부한다.**
`/dev/cu.wchusbserial*` 같은 경로가 매칭 안 되면 bash처럼 리터럴로 넘기는 대신
`zsh: no matches found`로 죽는다. `PORT=$(ls /dev/cu.usbserial-* | head -1)`처럼
서브셸로 감싸거나 `ctm port`(선택한 보드의 경로)를 쓴다.

**3.12 zsh는 따옴표 없는 파라미터 확장을 단어 분리하지 않는다.**
`CMD="prog --flag"; $CMD arg` 는 bash에서 되고 **zsh에서 실패한다** — 확장 전체를
하나의 명령 이름으로 본다. **셸 함수**를 쓰거나 배열 `"${CMD[@]}"`, 혹은 `${=CMD}`.

> **Corollary — 실제로 피해를 낸 부분.** 실패한 명령이 출력 파일을 만들지 않았고,
> 다운스트림의 `cmp -s 없는_파일` 이 0이 아닌 값을 돌려주면서 **자신만만하고
> 완전히 거짓인 "genuine 16MB" 판정**이 출력됐다. 검증 스크립트는 모든 단계의
> 성공을 확인한 뒤에만 결론을 내야 하고, 그렇지 않으면 결론을 거부해야 한다.
> 파일의 부재는 증거가 아니다.

**3.13 `platformio.ini` 섹션 순서가 중요하다.**
INI에서 모든 키는 **위쪽** `[section]`에 속한다. 추가 `[env:…]` 아래에 쓴
`build_flags`는 조용히 그 환경만의 옵션이 되고 그 위 환경은 플래그 없이
컴파일된다. 증상: `error: 'TFT_BL' was not declared in this scope`.
이걸 피해가도 `USER_SETUP_LOADED`가 없으면 TFT_eSPI가 자체 `User_Setup.h`로
폴백해 잘못된 드라이버·핀을 쓰므로, 컴파일 에러가 아니라 **깨진 화면**이 나온다.
모든 옵션을 첫 번째 추가 `[env:…]` 헤더 **위**에 유지한다. (지금은 env가
하나뿐이라 이 함정을 밟을 일이 없지만, 두 번째 env가 생기면 그대로 재발한다.)

**3.14 PlatformIO는 `src/`의 모든 `.cpp`를 컴파일한다.**
두 번째 엔트리포인트가 `src/` 안에 같이 있고 메인 환경에 필터가 없으면 둘 다
컴파일되어 깨끗한 빌드의 맨 끝에서 링크가 실패한다(`multiple definition of
'tft'`). 엔트리포인트가 둘 이상인 **모든 환경**에 `build_src_filter`가 필요하다.
**반대 방향도 함정이다**: 파일이 없는데 필터만 남아 있으면 그 env가
"Nothing to build"로 실패한다 — 매칭되는 파일이 하나도 없으면 컴파일할 게
없으므로. 필터를 고칠 때는 실제 파일 목록과 대조한다.

**3.15 TFT_eSPI 내장 폰트에 한글이 없다.** SPEC §3 참조. 증상이 3.1과 같다 —
에러 없이 아무것도 안 그려진다.

**3.16 WiFi 이벤트 콜백은 별도 태스크에서 돈다 — TFT를 건드리지 않는다.**
`WiFi.onEvent()` 콜백은 메인 태스크(`loop()`/`setup()`)가 아니라 Arduino
이벤트 태스크에서 실행되고 TFT_eSPI에는 락이 없다. 콜백 안에서 곧바로 `tft`를
그리면 메인 태스크가 `spr.pushSprite()` 중일 때 같은 SPI 버스를 두 태스크가
동시에 건드리는 경합이 생긴다. 콜백은 `volatile` 갱신과 로깅만 하고, 화면
갱신은 메인 루프가 그 값을 250ms 폴링해서 한다(`src/diag/`, `wifi_ntp.cpp`
참고).

**3.17 이 보드에서 RTC_NOINIT 메모리는 EN/RTS 계열 리셋을 생존하지 못한다
`[V, 3회 실측]`.** esptool 업로드 리셋과 pyserial RTS 토글 모두에서 매번
`esp_reset_reason() == ESP_RST_POWERON`이고 RTC 링버퍼는 매번 무효(매직넘버
불일치)였다 — 클래식 ESP32는 EN 리셋을 EXT로 구분하지 못하고(`esp_system.h`
주석 "not applicable for ESP32") RTC 메모리도 같이 지워진다. 따라서
"리셋을 건너뛰어야 하는" 상태는 RTC_NOINIT이 아니라 **NVS에** 둬야 한다
(`src/diag/`의 링버퍼+NVS 미러가 이 이유로 NVS 미러가 실효 메커니즘이다).
진짜 브라운아웃(전압 새그)은 EN 핀을 거치지 않는 별도 경로라 다를 수 있으나
미검증 `[?]`.

### 6.4 `platformio.ini` — 알려진 양품

이 파일은 빌드·링크·업로드·실행이 확인된 상태다. **퇴행시키지 말 것.**
env는 `env:t-display` 하나뿐이고 `build_src_filter = +<*>`다(엔트리포인트가
`app/main.cpp` 유일 — 두 번째가 생기면 gotcha 3.13/3.14를 재적용한다).
`clock_ntp.cpp`는 `src/` 밖 `reference/`에 있어 필터 예외가 필요 없다.

```ini
[env:t-display]
platform  = espressif32@^6.5.0
board     = esp32dev
framework = arduino

monitor_speed   = 115200
monitor_filters = esp32_exception_decoder

; upload_port는 하드코딩하지 않는다. 업로드 시 지정:
;   pio run -d firmware -e t-display -t upload --upload-port "$(cd host && python -m ctm.cli port)"
upload_speed = 460800          ; 921600은 macOS 내장 드라이버에서 실패

; 엔트리포인트 app/main.cpp 하나뿐 — 필터 단순 (gotcha 3.14)
build_src_filter = +<*>

lib_deps =
    bodmer/TFT_eSPI@^2.5.43
    bblanchon/ArduinoJson@^7.0.0

build_flags =
    -DUSER_SETUP_LOADED=1
    -DST7789_DRIVER=1
    -DTFT_WIDTH=135
    -DTFT_HEIGHT=240
    -DCGRAM_OFFSET=1
    -DTFT_MISO=-1
    -DTFT_MOSI=19
    -DTFT_SCLK=18
    -DTFT_CS=5
    -DTFT_DC=16
    -DTFT_RST=23
    -DTFT_BL=4
    -DTFT_BACKLIGHT_ON=HIGH
    -DLOAD_GLCD=1
    -DLOAD_FONT2=1
    -DLOAD_FONT4=1
    -DLOAD_FONT6=1
    -DLOAD_FONT7=1
    -DLOAD_FONT8=1
    -DLOAD_GFXFF=1
    -DSMOOTH_FONT=1
    -DSPI_FREQUENCY=40000000
    -DSPI_READ_FREQUENCY=6000000

; --- 16MB 플래시: gotcha 3.8. 전체 지우기 후 적용됨 [V] ---
board_upload.flash_size = 16MB
board_build.partitions  = partitions_16mb.csv
```

무해한 빌드 노이즈: `TOUCH_CS pin not defined` — 이 보드에 터치 컨트롤러가 없으니
맞는 말이다. **`-DTOUCH_CS=-1`로 잠재우지 말 것** — 엉뚱한 핀으로 터치 코드 경로를
켜버린다. 노이즈가 거슬리면 `-DDISABLE_ALL_LIBRARY_WARNINGS`.

빌드 배너의 `4MB Flash` / `320KB RAM`은 `esp32dev` 보드 정의가 하는 말이지 측정이
아니다. §6.1을 믿는다.


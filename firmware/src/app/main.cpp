// Claude Token Meter -- P1 walking skeleton + P3 display (USB serial only, no WiFi).
// The only entry point in src/ (gotcha 3.14).
#include <Arduino.h>
#include <WiFi.h>
#include <sys/time.h>
#include <time.h>

#include "../core/meter.h"
#include "../core/protocol.h"
#include "../core/view_state.h"
#include "../ui/buttons.h"
#include "../ui/display.h"
#include "../ui/render.h"

namespace {

constexpr uint32_t REDRAW_MS = 1000;

bool          displayOk = false;
ctm::SeqGate  seqGate;
RenderModel   model;
ctm::UiController ui;
uint32_t      lastFrameMs = 0;
uint32_t      lastDrawMs = 0;
bool          dirty = true;

char   lineBuf[ctm::MAX_LINE + 1];
size_t lineLen = 0;
bool   discarding = false;   // current line exceeded MAX_LINE; drop until '\n'

void reply(const char *json) {
  Serial.print('@');
  Serial.println(json);
}

void sendAck(uint32_t seq) {
  char b[48];
  snprintf(b, sizeof b, "{\"t\":\"ack\",\"seq\":%lu}", (unsigned long)seq);
  reply(b);
}

void sendNak(uint32_t seq, const char *err) {
  char b[64];
  snprintf(b, sizeof b, "{\"t\":\"nak\",\"seq\":%lu,\"err\":\"%s\"}",
           (unsigned long)seq, err);
  reply(b);
}

void handleLine(const char *line, size_t len) {
  // Skip blank lines and leading whitespace.
  while (len && (*line == ' ' || *line == '\t')) { line++; len--; }
  if (len == 0) return;

  ctm::StateFrame f;
  uint32_t seq = 0;
  ctm::ParseStatus st = ctm::parseStateFrame(line, len, f, seq);
  switch (st) {
    case ctm::ParseStatus::NotState:
      Serial.println("log: ignored non-state frame");
      return;
    case ctm::ParseStatus::Parse:
    case ctm::ParseStatus::BadV:
      sendNak(seq, ctm::errString(st));
      return;
    case ctm::ParseStatus::Ok:
      break;
  }
  if (!seqGate.wouldAccept(f.seq)) {
    sendNak(f.seq, "old_seq");
    return;
  }
  seqGate.commit(f.seq);

  // Auto-dim timer restarts only when the shown values change (not heartbeats).
  if (!model.haveFrame || ctm::windowsChanged(model.frame, f)) ui.onWindowsChanged(millis());

  struct timeval tv = {static_cast<time_t>(f.sent_at), 0};
  settimeofday(&tv, nullptr);   // §3: device clock follows sent_at, no NTP

  model.frame = f;
  model.haveFrame = true;
  lastFrameMs = millis();
  dirty = true;
  sendAck(f.seq);
}

// Buttons and UI timers. Everything here runs on the main task (gotcha 3.16).
void pollUi() {
  uint32_t nowMs = millis();
  PressKind p1 = checkPress(button1);
  if (p1 == PRESS_SHORT && ui.onBtn1Short(nowMs)) dirty = true;
  if (p1 == PRESS_LONG && ui.onBtn1Long(nowMs)) dirty = true;
  if (checkPress(button2) == PRESS_SHORT && ui.onBtn2Short(nowMs)) dirty = true;
  if (ui.tick(nowMs)) dirty = true;
  if (displayOk) setBacklight(ui.backlight());   // no-op when unchanged
}

void pollSerial() {
  while (Serial.available() > 0) {
    int c = Serial.read();
    if (c < 0) break;
    if (c == '\n') {
      if (discarding) {
        Serial.println("log: discarded overlong line");
      } else {
        if (lineLen && lineBuf[lineLen - 1] == '\r') lineLen--;
        lineBuf[lineLen] = '\0';
        handleLine(lineBuf, lineLen);
      }
      lineLen = 0;
      discarding = false;
      continue;
    }
    if (discarding) continue;
    if (lineLen >= ctm::MAX_LINE) {
      discarding = true;
      lineLen = 0;
      continue;
    }
    lineBuf[lineLen++] = static_cast<char>(c);
  }
}

}  // namespace

void setup() {
  // A full 1KB frame must not overflow the RX FIFO while we render/push.
  Serial.setRxBufferSize(2048);
  Serial.begin(115200);

  // P1 is USB-only: radio fully off. (Modem sleep is untouched -- gotcha 3.5.)
  WiFi.mode(WIFI_OFF);

  // Detail view prints reset times in local time. POSIX sign is inverted:
  // "KST-9" is UTC+9 (gotcha 3.3).
  setenv("TZ", "KST-9", 1);
  tzset();

  // Sprite first, before anything else touches the heap.
  displayOk = displayInit();
  buttonsInit();
  if (displayOk) setBacklight(ui.backlight());
  if (displayOk) render(model);   // waiting-for-host screen
  lastDrawMs = millis();
  dirty = false;

  // Drop anything the host sent while we were booting: a partial frame left in
  // the RX buffer would glue onto the next real frame and nak it [V, P3].
  while (Serial.available() > 0) Serial.read();
  lineLen = 0;

  char hello[80];
  snprintf(hello, sizeof hello, "{\"t\":\"hello\",\"fw\":\"%s\",\"board\":\"%s\"}",
           ctm::FW_VERSION, ctm::BOARD);
  reply(hello);
}

void loop() {
  pollSerial();
  pollUi();

  uint32_t nowMs = millis();
  if (displayOk && (dirty || nowMs - lastDrawMs >= REDRAW_MS)) {
    model.now = static_cast<int64_t>(time(nullptr));
    model.frameAgeS = model.haveFrame ? (nowMs - lastFrameMs) / 1000u : 0;
    model.link = ctm::linkStateFor(model.frameAgeS);
    model.lastSeq = seqGate.last();
    model.uptimeS = nowMs / 1000u;   // [?] wraps after ~49.7 days of uptime
    model.freeHeap = ESP.getFreeHeap();
    model.view = ui.view();
    model.toast = ui.toast();
    render(model);
    lastDrawMs = nowMs;
    dirty = false;
  }
  delay(5);
}

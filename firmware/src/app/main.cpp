// Claude Token Meter -- P4: USB serial + WiFi HTTP (HMAC-signed) transports,
// USB provisioning commands, P3 display. The only entry point in src/ (gotcha 3.14).
#include <Arduino.h>
#include <ArduinoJson.h>
#include <WiFi.h>
#include <sys/time.h>
#include <time.h>

#include "../core/command.h"
#include "../core/http_accept.h"
#include "../core/meter.h"
#include "../core/protocol.h"
#include "../core/view_state.h"
#include "../ui/buttons.h"
#include "../ui/display.h"
#include "../ui/render.h"
#include "config_store.h"
#include "wifi_link.h"

namespace {

constexpr uint32_t REDRAW_MS = 1000;
constexpr uint32_t PROVISION_TIMEOUT_MS = 15u * 1000u;   // PROTOCOL §4.3

bool          displayOk = false;
ctm::SeqGate  seqGate;
RenderModel   model;
ctm::UiController ui;
uint32_t      lastFrameMs = 0;
uint32_t      lastDrawMs = 0;
bool          dirty = true;
bool          clockSet = false;   // device clock set from an accepted frame (§4.2 skew check)

DeviceConfig  cfg;                // NVS mirror; holds secrets -- never print
bool          provPending = false;
uint32_t      provStartMs = 0;
WifiState     shownWifi = WifiState::Off;

char   lineBuf[ctm::MAX_LINE + 1];
size_t lineLen = 0;
bool   discarding = false;   // current line exceeded MAX_LINE; drop until '\n'

// One write per reply line so nothing can interleave inside it.
void reply(const char *json) {
  size_t n = strlen(json);
  char *b = static_cast<char *>(malloc(n + 3));
  if (!b) return;
  b[0] = '@';
  memcpy(b + 1, json, n);
  b[n + 1] = '\n';
  Serial.write(reinterpret_cast<const uint8_t *>(b), n + 2);
  free(b);
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

// An accepted frame (either transport): clock, model, UI timers.
void applyFrame(const ctm::StateFrame &f, bool viaWifi) {
  // Auto-dim timer restarts only when the shown values change (not heartbeats).
  if (!model.haveFrame || ctm::windowsChanged(model.frame, f)) ui.onWindowsChanged(millis());

  struct timeval tv = {static_cast<time_t>(f.sent_at), 0};
  settimeofday(&tv, nullptr);   // §3: device clock follows sent_at, no NTP
  clockSet = true;

  model.frame = f;
  model.haveFrame = true;
  model.viaWifi = viaWifi;
  lastFrameMs = millis();
  dirty = true;
}

void buildInfo(char *buf, size_t n, bool usb) {
  char ip[16];
  wifiIp(ip, sizeof ip);
  ctm::InfoFields f;
  f.host = wifiHost();
  f.ip = ip;
  f.rssi = wifiRssi();
  f.uptimeS = millis() / 1000u;
  f.lastSeq = seqGate.last();
  f.provisioned = cfg.provisioned;
  f.wifi = usb ? wifiStateName(wifiState()) : nullptr;
  ctm::formatInfo(f, ctm::FW_VERSION, buf, n);
}

// ---- HTTP hooks (run on the main task inside wifiPoll()/handleClient) ----

ctm::HttpVerdict onHttpState(const char *body, size_t len, const char *sig) {
  if (!cfg.provisioned) {   // server is not started then; belt and braces
    ctm::HttpVerdict v;
    v.status = 401;
    snprintf(v.body, sizeof v.body, "{\"t\":\"nak\",\"err\":\"sig\"}");
    return v;
  }
  ctm::HttpVerdict v = ctm::acceptHttpState(cfg.key, body, len, sig, clockSet,
                                            static_cast<int64_t>(time(nullptr)), seqGate);
  if (v.applied) applyFrame(v.frame, true);
  Serial.printf("log: http state %d %s\n", v.status, v.body);
  return v;
}

void onHttpInfo(char *buf, size_t n) { buildInfo(buf, n, false); }

// ---- USB commands (PROTOCOL §4.3). Never echo the line, never print pass/key. ----

void logSsid(const char *ssid) {
  char safe[ctm::WIFI_SSID_MAX + 1];
  size_t i = 0;
  for (; ssid[i] && i < ctm::WIFI_SSID_MAX; i++)
    safe[i] = (ssid[i] >= 0x20 && ssid[i] < 0x7f) ? ssid[i] : '?';
  safe[i] = '\0';
  Serial.printf("log: provision ssid=%s (pass/key hidden)\n", safe);
}

void cmdScan() {
  static ctm::NetInfo nets[40];   // static: keep it off the 8KB loop stack
  int n = wifiScan(nets, sizeof nets / sizeof nets[0]);
  JsonDocument doc;
  doc["t"] = "scan";
  JsonArray arr = doc["nets"].to<JsonArray>();
  for (int i = 0; i < n; i++) {
    JsonObject o = arr.add<JsonObject>();
    o["ssid"] = nets[i].ssid;
    o["rssi"] = nets[i].rssi;
    o["open"] = nets[i].open;
  }
  if (n < 0) doc["err"] = "scan_failed";   // [?] not in §4.3; nets is [] then
  String out;
  serializeJson(doc, out);
  reply(out.c_str());
  Serial.printf("log: scan %d nets\n", n);
}

void cmdProvision(ctm::ProvisionArgs &a, bool argsOk) {
  if (!argsOk) {
    Serial.println("log: provision bad_args");
    reply("{\"t\":\"provision\",\"ok\":false,\"err\":\"bad_args\"}");
    return;
  }
  logSsid(a.ssid);
  if (!configSave(a)) {
    Serial.println("log: provision nvs write failed");
    reply("{\"t\":\"provision\",\"ok\":false,\"err\":\"nvs\"}");   // [?] not in §4.3
    return;
  }
  configLoad(cfg);   // read back what NVS actually holds
  if (!cfg.provisioned) {
    reply("{\"t\":\"provision\",\"ok\":false,\"err\":\"nvs\"}");
    return;
  }
  wifiStart(cfg.ssid, cfg.pass);
  provPending = true;   // reply from loop() once connected or after 15 s
  provStartMs = millis();
  dirty = true;
}

void cmdUnpair() {
  provPending = false;
  configClear();
  ctm::scrub(&cfg, sizeof cfg);
  cfg = DeviceConfig{};
  wifiStop();
  Serial.println("log: unpaired (nvs cleared, wifi off)");
  reply("{\"t\":\"unpair\",\"ok\":true}");
  dirty = true;
}

void pollProvision() {
  if (!provPending) return;
  WifiState s = wifiState();
  if (s == WifiState::Connected) {
    char ip[16], b[96];
    wifiIp(ip, sizeof ip);
    snprintf(b, sizeof b, "{\"t\":\"provision\",\"ok\":true,\"host\":\"%s\",\"ip\":\"%s\"}",
             wifiHost(), ip);
    reply(b);
    provPending = false;
  } else if (millis() - provStartMs >= PROVISION_TIMEOUT_MS) {
    // Credentials stay in NVS and the join keeps retrying (PROTOCOL §4.3).
    char b[80];
    uint8_t r = wifiLastReason();
    snprintf(b, sizeof b, "{\"t\":\"provision\",\"ok\":false,\"err\":\"%s\"}",
             wifiProvisionErr(r));
    Serial.printf("log: provision failed reason=%u\n", static_cast<unsigned>(r));
    reply(b);
    provPending = false;
  }
}

// Returns true if the line was a command (handled here, never reaches the state parser).
bool dispatchCommand(const char *line, size_t len) {
  ctm::ProvisionArgs a;
  bool argsOk = false;
  ctm::CmdType t = ctm::parseCommand(line, len, a, argsOk);
  switch (t) {
    case ctm::CmdType::None:
      return false;
    case ctm::CmdType::Scan:
      cmdScan();
      break;
    case ctm::CmdType::Info: {
      char b[256];
      buildInfo(b, sizeof b, true);
      reply(b);
      break;
    }
    case ctm::CmdType::Unpair:
      cmdUnpair();
      break;
    case ctm::CmdType::Provision:
      cmdProvision(a, argsOk);
      break;
  }
  ctm::scrub(a);
  return true;
}

void handleLine(const char *line, size_t len) {
  // Skip blank lines and leading whitespace.
  while (len && (*line == ' ' || *line == '\t')) { line++; len--; }
  if (len == 0) return;

  if (dispatchCommand(line, len)) return;   // §4.3, before the state parser

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
  applyFrame(f, false);
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
  WifiState w = wifiState();   // event-task flags, polled here (gotcha 3.16)
  if (w != shownWifi) {
    shownWifi = w;
    dirty = true;
  }
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
      ctm::scrub(lineBuf, lineLen);   // a provision line carries pass/key
      lineLen = 0;
      discarding = false;
      continue;
    }
    if (discarding) continue;
    if (lineLen >= ctm::MAX_LINE) {
      discarding = true;
      ctm::scrub(lineBuf, lineLen);
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

  // Radio off until we know we are provisioned. (Modem sleep untouched -- 3.5.)
  WiFi.mode(WIFI_OFF);

  // Detail view prints reset times in local time. POSIX sign is inverted:
  // "KST-9" is UTC+9 (gotcha 3.3).
  setenv("TZ", "KST-9", 1);
  tzset();

  // Sprite first, before anything else touches the heap.
  displayOk = displayInit();
  buttonsInit();

  wifiLinkInit(onHttpState, onHttpInfo);
  model.host = wifiHost();
  if (configLoad(cfg)) {
    Serial.printf("log: provisioned, wifi start host=%s.local\n", wifiHost());
    wifiStart(cfg.ssid, cfg.pass);
  } else {
    Serial.println("log: not provisioned, wifi off (run ctm setup)");
  }
  model.provisioned = cfg.provisioned;
  model.wifi = wifiState();
  shownWifi = model.wifi;

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
  wifiPoll();
  pollProvision();
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
    model.provisioned = cfg.provisioned;
    model.wifi = wifiState();
    model.rssi = wifiRssi();
    wifiIp(model.ip, sizeof model.ip);
    render(model);
    lastDrawMs = nowMs;
    dirty = false;
  }
  delay(5);
}

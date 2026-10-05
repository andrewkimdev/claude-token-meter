#include "wifi_link.h"

#include <Arduino.h>
#include <ESPmDNS.h>
#include <WebServer.h>
#include <WiFi.h>
#include <esp_mac.h>
#include <esp_task_wdt.h>
#include <cstring>

#include "../core/backoff.h"
#include "../core/protocol.h"

namespace {

// A join that associated but never got an IP (or never reported anything)
// is abandoned and retried after this long, even without a disconnect event.
constexpr uint32_t JOIN_STUCK_MS = 20u * 1000u;
constexpr uint32_t IP_CHECK_MS = 5u * 1000u;
constexpr uint32_t BODY_DEADLINE_MS = 2000u;   // whole POST body must arrive within this
constexpr int      MAX_BODY = static_cast<int>(ctm::MAX_LINE);   // §4.2: 1024 B

// WebServer::client() returns a copy; the raw hook needs the server's own
// client object (its Stream timeout drives the remaining raw read loop).
class CtmServer : public WebServer {
 public:
  using WebServer::WebServer;
  WiFiClient &liveClient() { return _currentClient; }
};

CtmServer server(80);

HttpStateHook stateHook = nullptr;
HttpInfoHook  infoHook = nullptr;

char host[20] = "ctmeter-0000";
char ssid[ctm::WIFI_SSID_MAX + 1] = {0};
char pass[ctm::WIFI_PASS_MAX + 1] = {0};

bool     started = false;        // STA requested (provisioned)
bool     serverUp = false;
bool     mdnsUp = false;
bool     shownConnected = false; // last state handled by wifiPoll
bool     everConnected = false;  // since boot (reconnect counting)
uint32_t reconnects = 0;
uint32_t lastBeginMs = 0;
uint32_t nextJoinMs = 0;         // next rejoin attempt while disconnected
uint32_t lastIpCheckMs = 0;
uint32_t announcedIp = 0;        // IP mDNS/HTTP were (re)started for
ctm::Backoff backoff;

// Written by the WiFi event task, read by the main task (gotcha 3.16).
volatile bool     evGotIp = false;
volatile uint8_t  evReason = 0;          // last real disconnect reason (ASSOC_LEAVE ignored)
volatile bool     evDiscSinceBegin = false;
volatile uint32_t evDisconnects = 0;

void onWifiEvent(arduino_event_id_t event, arduino_event_info_t info) {
  switch (event) {
    case ARDUINO_EVENT_WIFI_STA_GOT_IP:
      evGotIp = true;
      break;
    case ARDUINO_EVENT_WIFI_STA_LOST_IP:
      evGotIp = false;
      break;
    case ARDUINO_EVENT_WIFI_STA_DISCONNECTED: {
      evGotIp = false;
      uint8_t r = info.wifi_sta_disconnected.reason;
      // ASSOC_LEAVE is our own WiFi.disconnect() (rejoin/scan/core retry):
      // it must not mask the real cause (e.g. NO_AP_FOUND for provision),
      // nor count as "the attempt just started has failed" (it arrives late).
      if (r != WIFI_REASON_ASSOC_LEAVE) {
        evDiscSinceBegin = true;
        evReason = r;
        evDisconnects = evDisconnects + 1;
      }
      break;
    }
    default:
      break;
  }
}

void sendJson(int code, const char *body) { server.send(code, "application/json", body); }

// After we answered from inside the raw hook: close, and make WebServer's
// remaining raw read loop give up at once (Stream timeout 0) instead of
// spinning its 1 s timedRead on a dead socket.
void finishRaw() {
  WiFiClient &c = server.liveClient();
  static_cast<Stream &>(c).setTimeout(0);
  c.stop();
}

// POST /v1/state with a bounded body (P5). WebServer's plain-body path
// mallocs Content-Length bytes before any handler runs; the raw hook runs
// right after the headers, so an oversize request is refused (413) without
// reading or verifying its body. For Content-Length 1..1024 the body is read
// here into a fixed buffer under a deadline, then signed-checked by the hook.
class StateHandler : public RequestHandler {
 public:
  bool canHandle(HTTPMethod, String uri) override { return uri == "/v1/state"; }
  bool canRaw(String uri) override { return uri == "/v1/state"; }

  void raw(WebServer &s, String, HTTPRaw &r) override {
    if (r.status != RAW_START) return;   // RAW_ABORTED after finishRaw(): nothing to do
    int cl = s.clientContentLength();
    if (cl <= 0) return;                 // empty body: handle() answers (401)
    if (cl > MAX_BODY) {
      Serial.printf("log: http state 413 len=%d\n", cl);
      sendJson(413, "{\"t\":\"nak\",\"err\":\"too_large\"}");
      finishRaw();
      return;
    }
    static char buf[MAX_BODY + 1];
    WiFiClient &c = server.liveClient();
    int got = 0;
    uint32_t t0 = millis();
    while (got < cl && millis() - t0 < BODY_DEADLINE_MS) {
      int a = c.available();
      if (a > 0) {
        int want = cl - got;
        int n = c.read(reinterpret_cast<uint8_t *>(buf) + got, a < want ? a : want);
        if (n > 0) got += n;
      } else if (!c.connected()) {
        break;
      } else {
        delay(2);
      }
    }
    if (got < cl) {
      Serial.printf("log: http state 408 got=%d/%d\n", got, cl);
      sendJson(408, "{\"t\":\"nak\",\"err\":\"timeout\"}");
      finishRaw();
      return;
    }
    buf[got] = '\0';
    reply(s, buf, static_cast<size_t>(got));
    finishRaw();
  }

  // Reached only when raw() did not answer: empty body, non-POST, or a
  // multipart form (WebServer skips raw for those).
  bool handle(WebServer &s, HTTPMethod m, String) override {
    if (m != HTTP_POST) {
      sendJson(405, "{\"t\":\"nak\",\"err\":\"method\"}");
      return true;
    }
    if (s.clientContentLength() > 0) {   // multipart: not our format
      sendJson(415, "{\"t\":\"nak\",\"err\":\"parse\"}");
      return true;
    }
    reply(s, "", 0);
    return true;
  }

 private:
  static void reply(WebServer &s, const char *body, size_t len) {
    String sig = s.header("X-CTM-Sig");
    if (!stateHook) {
      sendJson(503, "{\"t\":\"nak\",\"err\":\"busy\"}");
      return;
    }
    ctm::HttpVerdict v = stateHook(body, len, sig.length() ? sig.c_str() : nullptr);
    sendJson(v.status, v.body);
  }
};

void handleInfo() {
  char buf[ctm::INFO_BUF];
  if (infoHook) {
    infoHook(buf, sizeof buf);
  } else {
    std::snprintf(buf, sizeof buf, "{\"t\":\"info\"}");
  }
  sendJson(200, buf);
}

void handleNotFound() { sendJson(404, "{\"t\":\"nak\",\"err\":\"not_found\"}"); }

void beginJoin() {
  evDiscSinceBegin = false;
  WiFi.begin(ssid, pass[0] ? pass : nullptr);
  lastBeginMs = millis();
}

uint32_t currentIp() { return static_cast<uint32_t>(WiFi.localIP()); }

// (Re)announce on the current IP: MDNS.end/begin so a new DHCP lease is
// advertised (P5), and rebind the HTTP listener if the address changed.
void announce() {
  uint32_t ip = currentIp();
  if (mdnsUp) {
    MDNS.end();
    mdnsUp = false;
  }
  if (MDNS.begin(host)) {
    MDNS.addService("ctmeter", "tcp", 80);
    MDNS.addServiceTxt("ctmeter", "tcp", "fw", ctm::FW_VERSION);
    mdnsUp = true;
  } else {
    Serial.println("log: mdns begin failed");
  }
  if (serverUp && announcedIp != 0 && ip != announcedIp) {
    server.stop();   // [?] lwIP listener on 0.0.0.0 likely survives; rebind anyway
    server.begin();
  }
  announcedIp = ip;
}

}  // namespace

const char *wifiStateName(WifiState s) {
  switch (s) {
    case WifiState::Connected:  return "connected";
    case WifiState::Connecting: return "connecting";
    default:                    return "off";
  }
}

void wifiLinkInit(HttpStateHook onState, HttpInfoHook onInfo) {
  stateHook = onState;
  infoHook = onInfo;
  uint8_t mac[6] = {0};
  esp_read_mac(mac, ESP_MAC_WIFI_STA);   // works with the radio off
  ctm::hostnameFromMac(mac, host, sizeof host);

  WiFi.persistent(false);   // creds live in our "ctm" namespace only
  WiFi.onEvent(onWifiEvent);

  server.addHandler(new StateHandler());   // lives forever
  server.on("/v1/info", HTTP_GET, handleInfo);
  server.onNotFound(handleNotFound);
  static const char *hdrs[] = {"X-CTM-Sig"};
  server.collectHeaders(hdrs, 1);
}

const char *wifiHost() { return host; }

void wifiStart(const char *newSsid, const char *newPass) {
  std::snprintf(ssid, sizeof ssid, "%s", newSsid);
  std::snprintf(pass, sizeof pass, "%s", newPass);
  if (WiFi.getMode() & WIFI_MODE_STA) WiFi.disconnect();   // switching networks
  evGotIp = false;
  evReason = 0;
  shownConnected = false;
  WiFi.setHostname(host);    // must precede STA start (applied on STA_START)
  WiFi.mode(WIFI_STA);
  // Modem sleep: the core's default (WIFI_PS_MIN_MODEM) is kept. gotcha 3.5.
  // Rejoins are ours (backoff 5..60 s), not the core's immediate retry loop.
  WiFi.setAutoReconnect(false);
  backoff.reset();
  beginJoin();
  nextJoinMs = millis() + backoff.next();
  started = true;
  if (!serverUp) {
    server.begin();
    serverUp = true;
  }
}

void wifiStop() {
  if (serverUp) {
    server.stop();
    serverUp = false;
  }
  if (mdnsUp) {
    MDNS.end();
    mdnsUp = false;
  }
  started = false;
  announcedIp = 0;
  ctm::scrub(pass, sizeof pass);
  ctm::scrub(ssid, sizeof ssid);
  if (WiFi.getMode() != WIFI_OFF) WiFi.disconnect(true, true);   // radio off + erase IDF AP config
  WiFi.mode(WIFI_OFF);
  evGotIp = false;
  evReason = 0;
  shownConnected = false;
}

WifiState wifiState() {
  if (!started) return WifiState::Off;
  return evGotIp ? WifiState::Connected : WifiState::Connecting;
}

int wifiRssi() { return wifiState() == WifiState::Connected ? WiFi.RSSI() : 0; }

void wifiIp(char *buf, size_t n) {
  if (wifiState() != WifiState::Connected) {
    if (n) buf[0] = '\0';
    return;
  }
  IPAddress ip = WiFi.localIP();
  std::snprintf(buf, n, "%u.%u.%u.%u", ip[0], ip[1], ip[2], ip[3]);
}

uint8_t  wifiLastReason() { return evReason; }
uint32_t wifiReconnects() { return reconnects; }

const char *wifiProvisionErr(uint8_t reason) {
  switch (reason) {
    case WIFI_REASON_NO_AP_FOUND:
      return "no_ap";
    case WIFI_REASON_AUTH_FAIL:
    case WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT:
    case WIFI_REASON_HANDSHAKE_TIMEOUT:
    case WIFI_REASON_MIC_FAILURE:
    case WIFI_REASON_802_1X_AUTH_FAILED:
      return "auth";
    default:
      return "timeout";   // includes "no disconnect seen at all" (reason 0)
  }
}

void wifiPoll() {
  if (!started) return;
  uint32_t now = millis();
  bool connected = evGotIp;
  if (connected != shownConnected) {
    shownConnected = connected;
    if (connected) {
      if (everConnected) reconnects++;
      everConnected = true;
      backoff.reset();
      char ip[16];
      wifiIp(ip, sizeof ip);
      Serial.printf("log: wifi connected ip=%s rssi=%d host=%s.local reconnects=%lu\n", ip,
                    WiFi.RSSI(), host, static_cast<unsigned long>(reconnects));
      announce();   // every (re)connect: a router reboot may have changed the lease
      lastIpCheckMs = now;
    } else {
      Serial.printf("log: wifi disconnected reason=%u\n", static_cast<unsigned>(evReason));
      backoff.reset();
      nextJoinMs = now + backoff.next();   // first rejoin in 5 s
    }
  }

  if (connected) {
    // DHCP renew can move us without a disconnect (GOT_IP with ip_changed).
    if (now - lastIpCheckMs >= IP_CHECK_MS) {
      lastIpCheckMs = now;
      if (currentIp() != announcedIp) {
        Serial.println("log: ip changed, re-announcing mdns");
        announce();
      }
    }
  } else if (static_cast<int32_t>(now - nextJoinMs) >= 0 &&
             (evDiscSinceBegin || now - lastBeginMs >= JOIN_STUCK_MS)) {
    // Retry only once the previous attempt has failed (disconnect event) or
    // stalled; an association still waiting for DHCP is left alone.
    uint32_t d = backoff.next();
    Serial.printf("log: wifi rejoin #%lu reason=%u next=%lus\n",
                  static_cast<unsigned long>(backoff.attempts()),
                  static_cast<unsigned>(evReason), static_cast<unsigned long>(d / 1000u));
    WiFi.disconnect();
    beginJoin();
    nextJoinMs = millis() + d;
  }
  if (serverUp) server.handleClient();
}

int wifiScan(ctm::NetInfo *out, size_t cap) {
  esp_task_wdt_reset();   // blocking 2-4 s (x2 below), well under the 15 s WDT
  bool wasOff = WiFi.getMode() == WIFI_OFF;
  if (wasOff) WiFi.mode(WIFI_STA);
  int16_t n = WiFi.scanNetworks();
  esp_task_wdt_reset();
  bool rejoin = false;
  if (n < 0 && started && !evGotIp) {
    // A join in progress blocks scanning: pause it, scan, resume.
    WiFi.disconnect();
    n = WiFi.scanNetworks();
    esp_task_wdt_reset();
    rejoin = true;
  }
  size_t k = 0;
  for (int16_t i = 0; i < n && k < cap; i++) {
    String s = WiFi.SSID(i);
    std::snprintf(out[k].ssid, sizeof out[k].ssid, "%s", s.c_str());
    out[k].rssi = WiFi.RSSI(i);
    out[k].open = WiFi.encryptionType(i) == WIFI_AUTH_OPEN;
    k++;
  }
  WiFi.scanDelete();
  if (wasOff) WiFi.mode(WIFI_OFF);
  if (rejoin) beginJoin();
  if (n < 0) return -1;
  return static_cast<int>(ctm::shapeScan(out, k, ctm::SCAN_MAX));
}

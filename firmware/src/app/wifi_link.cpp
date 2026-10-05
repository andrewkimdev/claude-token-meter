#include "wifi_link.h"

#include <Arduino.h>
#include <ESPmDNS.h>
#include <WebServer.h>
#include <WiFi.h>
#include <esp_mac.h>
#include <cstring>

#include "../core/protocol.h"

namespace {

constexpr uint32_t REJOIN_MS = 60u * 1000u;   // our own retry for reasons the core won't retry (e.g. auth)

WebServer server(80);

HttpStateHook stateHook = nullptr;
HttpInfoHook  infoHook = nullptr;

char host[20] = "ctmeter-0000";
char ssid[ctm::WIFI_SSID_MAX + 1] = {0};
char pass[ctm::WIFI_PASS_MAX + 1] = {0};

bool     started = false;        // STA requested (provisioned)
bool     serverUp = false;
bool     mdnsUp = false;
bool     shownConnected = false; // last state logged by wifiPoll
uint32_t lastBeginMs = 0;

// Written by the WiFi event task, read by the main task (gotcha 3.16).
volatile bool    evGotIp = false;
volatile uint8_t evReason = 0;
volatile uint32_t evDisconnects = 0;

void onWifiEvent(arduino_event_id_t event, arduino_event_info_t info) {
  switch (event) {
    case ARDUINO_EVENT_WIFI_STA_GOT_IP:
      evGotIp = true;
      break;
    case ARDUINO_EVENT_WIFI_STA_LOST_IP:
      evGotIp = false;
      break;
    case ARDUINO_EVENT_WIFI_STA_DISCONNECTED:
      evGotIp = false;
      evReason = info.wifi_sta_disconnected.reason;
      evDisconnects = evDisconnects + 1;
      break;
    default:
      break;
  }
}

void sendJson(int code, const char *body) { server.send(code, "application/json", body); }

void handleState() {
  String body = server.arg("plain");   // raw body (not form-encoded)
  String sig = server.header("X-CTM-Sig");
  if (!stateHook) {
    sendJson(503, "{\"t\":\"nak\",\"err\":\"busy\"}");
    return;
  }
  ctm::HttpVerdict v =
      stateHook(body.c_str(), body.length(), sig.length() ? sig.c_str() : nullptr);
  sendJson(v.status, v.body);
}

void handleInfo() {
  char buf[256];
  if (infoHook) {
    infoHook(buf, sizeof buf);
  } else {
    std::snprintf(buf, sizeof buf, "{\"t\":\"info\"}");
  }
  sendJson(200, buf);
}

void handleNotFound() { sendJson(404, "{\"t\":\"nak\",\"err\":\"not_found\"}"); }

void beginJoin() {
  WiFi.begin(ssid, pass[0] ? pass : nullptr);
  lastBeginMs = millis();
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

  server.on("/v1/state", HTTP_POST, handleState);
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
  WiFi.setAutoReconnect(true);
  beginJoin();
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

uint8_t wifiLastReason() { return evReason; }

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
  bool connected = evGotIp;
  if (connected != shownConnected) {
    shownConnected = connected;
    if (connected) {
      char ip[16];
      wifiIp(ip, sizeof ip);
      Serial.printf("log: wifi connected ip=%s rssi=%d host=%s.local\n", ip, WiFi.RSSI(), host);
      if (!mdnsUp) {
        // Started once, on the first IP; IDF mdns follows later reconnects itself.
        if (MDNS.begin(host)) {
          MDNS.addService("ctmeter", "tcp", 80);
          MDNS.addServiceTxt("ctmeter", "tcp", "fw", ctm::FW_VERSION);
          mdnsUp = true;
        } else {
          Serial.println("log: mdns begin failed");
        }
      }
    } else {
      Serial.printf("log: wifi disconnected reason=%u\n", static_cast<unsigned>(evReason));
    }
  }
  // The core auto-reconnects most reasons but not e.g. AUTH_FAIL; nudge it.
  if (!connected && millis() - lastBeginMs > REJOIN_MS) {
    Serial.println("log: wifi rejoin");
    WiFi.disconnect();
    beginJoin();
  }
  if (serverUp) server.handleClient();
}

int wifiScan(ctm::NetInfo *out, size_t cap) {
  bool wasOff = WiFi.getMode() == WIFI_OFF;
  if (wasOff) WiFi.mode(WIFI_STA);
  int16_t n = WiFi.scanNetworks();
  bool rejoin = false;
  if (n < 0 && started && !evGotIp) {
    // A join in progress blocks scanning: pause it, scan, resume.
    WiFi.disconnect();
    n = WiFi.scanNetworks();
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

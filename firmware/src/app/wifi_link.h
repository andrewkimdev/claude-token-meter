#pragma once
// WiFi STA + mDNS + HTTP server (PROTOCOL §4.2). Main task only, except the
// WiFi event callback, which only writes volatile flags (gotcha 3.16): no
// TFT, no Serial there. Modem sleep stays ON (gotcha 3.5): never setSleep(false).
#include <cstddef>
#include <cstdint>

#include "../core/command.h"
#include "../core/http_accept.h"
#include "../core/view_state.h"

using ctm::WifiState;

const char *wifiStateName(WifiState s);   // "off" | "connecting" | "connected"

// Hooks into main.cpp, called from wifiPoll() (main task) while serving HTTP.
using HttpStateHook = ctm::HttpVerdict (*)(const char *body, size_t len, const char *sig);
using HttpInfoHook = void (*)(char *buf, size_t n);   // GET /v1/info body

// Derive hostname from the STA MAC and register the event callback. WiFi stays off.
void wifiLinkInit(HttpStateHook onState, HttpInfoHook onInfo);

const char *wifiHost();   // "ctmeter-xxxx" (valid after wifiLinkInit)

// (Re)start STA with these credentials; starts the HTTP server; mDNS follows
// each IP. Resets the disconnect reason. Copies ssid/pass.
void wifiStart(const char *ssid, const char *pass);

// Stop HTTP + mDNS, disconnect, erase the IDF copy of the AP config, radio off.
void wifiStop();

WifiState wifiState();
int       wifiRssi();                     // 0 unless connected
void      wifiIp(char *buf, size_t n);    // "" unless connected
uint8_t   wifiLastReason();               // last STA_DISCONNECTED reason since wifiStart (0 = none;
                                          // our own ASSOC_LEAVE is not recorded)
uint32_t  wifiReconnects();               // successful re-joins since boot (first join not counted)

// "no_ap" | "auth" | "timeout" for a provision failure (PROTOCOL §4.3).
const char *wifiProvisionErr(uint8_t reason);

// Serve HTTP, (re)start mDNS on every (re)connect / IP change, rejoin with
// backoff 5,10,20,40,60,60.. s (no ESP.restart), log transitions. Call every loop().
// POST /v1/state: Content-Length > 1024 -> 413 before the body is read.
void wifiPoll();

// Blocking scan (~2-4 s). Temporarily enables STA if WiFi is off. Returns
// the number of shaped entries (§4.3), or -1 if the scan failed.
int wifiScan(ctm::NetInfo *out, size_t cap);

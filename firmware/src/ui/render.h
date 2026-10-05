#pragma once
#include <cstdint>

#include "../core/protocol.h"
#include "../core/view_state.h"

struct RenderModel {
  bool             haveFrame = false;   // false -> "waiting for host" screen
  bool             restored = false;    // P5: frame came from NVS, no live frame since boot
  ctm::StateFrame  frame;
  int64_t          now = 0;             // epoch s (system time, set from sent_at)
  bool             clockOk = false;     // P5: clockUsable(); false -> no countdown/age math
  ctm::View        view = ctm::View::Main;
  ctm::LinkState   link = ctm::LinkState::Fresh;
  int64_t          frameAgeS = 0;       // since last accepted frame (device uptime clock)
  uint32_t         lastSeq = 0;
  int64_t          uptimeS = 0;
  uint32_t         freeHeap = 0;
  uint32_t         minHeap = 0;         // P5: ESP.getMinFreeHeap()
  uint32_t         reconnects = 0;      // P5: WiFi re-joins since boot
  uint8_t          discReason = 0;      // P5: last STA disconnect reason (0 = none)
  const char      *toast = nullptr;     // transient centered message, or nullptr
  // P4 transport / WiFi
  bool             viaWifi = false;     // transport of the last accepted frame (footer "USB"/"WiFi")
  bool             provisioned = false; // NVS has ssid+key
  ctm::WifiState   wifi = ctm::WifiState::Off;
  const char      *host = "";           // "ctmeter-xxxx"
  char             ip[16] = {0};        // "" unless connected
  int              rssi = 0;            // dBm, 0 unless connected
};

// Draws into the sprite and pushes it. Main task only (gotcha 3.16).
// Detail view prints local time: TZ must be set (setenv("TZ","KST-9")).
void render(const RenderModel &m);

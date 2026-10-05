#pragma once
// Pure C++ (no Arduino). PROTOCOL §4.3 USB commands (scan / provision / info /
// unpair), scan-result shaping, /v1/info body, and the §4.2 hostname.
// The dispatcher in app/main.cpp runs parseCommand() BEFORE the state parser.
#include <cstddef>
#include <cstdint>

#include "hmac.h"

namespace ctm {

enum class CmdType { None, Scan, Provision, Info, Unpair };

constexpr size_t WIFI_SSID_MAX = 32;   // 802.11 SSID bytes
constexpr size_t WIFI_PASS_MAX = 64;   // WPA2: 8..63 passphrase or 64 hex PSK

struct ProvisionArgs {
  char    ssid[WIFI_SSID_MAX + 1] = {0};
  char    pass[WIFI_PASS_MAX + 1] = {0};
  uint8_t key[PAIR_KEY_LEN] = {0};
};

// Overwrites secrets in place (volatile writes, not elided).
void scrub(ProvisionArgs &a);
void scrub(void *p, size_t n);

// Returns None if the line is not a JSON object whose "t" is one of the four
// commands (the caller then hands it to parseStateFrame). For Provision,
// `argsOk` tells whether ssid (1..32 B), pass (empty = open network, else
// 8..64 B) and key (exactly 64 hex chars) are valid; `out` is filled only
// when they are. Never logs; `out` holds secrets -- scrub() after use.
CmdType parseCommand(const char *line, size_t len, ProvisionArgs &out, bool &argsOk);

// --- scan shaping (§4.3): drop empty SSIDs, one entry per SSID (strongest),
// strongest first, at most `maxOut`.
struct NetInfo {
  char ssid[WIFI_SSID_MAX + 1] = {0};
  int  rssi = 0;
  bool open = false;
};
constexpr size_t SCAN_MAX = 15;
size_t shapeScan(NetInfo *nets, size_t n, size_t maxOut);

// "ctmeter-XXXX": lowercase hex of the STA MAC's last two bytes (§4.2).
void hostnameFromMac(const uint8_t mac[6], char *out, size_t n);

// /v1/info and USB {"t":"info"} body (§4.2/§4.3). `wifi` == nullptr omits the
// "wifi" key (HTTP); USB passes "off|connecting|connected". host/ip must be
// plain ASCII (no JSON escaping is done). Returns snprintf's length.
struct InfoFields {
  const char *host = "";
  const char *ip = "";
  int         rssi = 0;
  int64_t     uptimeS = 0;
  uint32_t    lastSeq = 0;
  bool        provisioned = false;
  const char *wifi = nullptr;
};
int formatInfo(const InfoFields &f, const char *fw, char *buf, size_t n);

}  // namespace ctm

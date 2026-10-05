#include "command.h"

#include <ArduinoJson.h>
#include <algorithm>
#include <cstdio>
#include <cstring>

namespace ctm {

void scrub(void *p, size_t n) {
  volatile uint8_t *v = static_cast<volatile uint8_t *>(p);
  for (size_t i = 0; i < n; i++) v[i] = 0;
}

void scrub(ProvisionArgs &a) { scrub(&a, sizeof a); }

CmdType parseCommand(const char *line, size_t len, ProvisionArgs &out, bool &argsOk) {
  argsOk = false;
  JsonDocument doc;
  if (deserializeJson(doc, line, len)) return CmdType::None;
  if (!doc.is<JsonObjectConst>()) return CmdType::None;
  const char *t = doc["t"];
  if (!t) return CmdType::None;
  if (std::strcmp(t, "scan") == 0) return CmdType::Scan;
  if (std::strcmp(t, "info") == 0) return CmdType::Info;
  if (std::strcmp(t, "unpair") == 0) return CmdType::Unpair;
  if (std::strcmp(t, "provision") != 0) return CmdType::None;

  JsonVariantConst ssidV = doc["ssid"], passV = doc["pass"], keyV = doc["key"];
  if (!ssidV.is<const char *>() || !passV.is<const char *>() || !keyV.is<const char *>())
    return CmdType::Provision;
  const char *ssid = ssidV.as<const char *>();
  const char *pass = passV.as<const char *>();
  const char *key = keyV.as<const char *>();
  size_t sl = std::strlen(ssid), pl = std::strlen(pass), kl = std::strlen(key);
  if (sl < 1 || sl > WIFI_SSID_MAX) return CmdType::Provision;
  if (pl != 0 && (pl < 8 || pl > WIFI_PASS_MAX)) return CmdType::Provision;

  ProvisionArgs a;
  if (!fromHex(key, kl, a.key, PAIR_KEY_LEN)) {
    scrub(a);
    return CmdType::Provision;
  }
  std::memcpy(a.ssid, ssid, sl);
  std::memcpy(a.pass, pass, pl);
  out = a;
  scrub(a);
  argsOk = true;
  // [?] the JsonDocument's heap copy of pass/key is freed but not scrubbed.
  return CmdType::Provision;
}

size_t shapeScan(NetInfo *nets, size_t n, size_t maxOut) {
  // Strongest first, then keep the first occurrence of each non-empty SSID.
  std::stable_sort(nets, nets + n,
                   [](const NetInfo &a, const NetInfo &b) { return a.rssi > b.rssi; });
  size_t out = 0;
  for (size_t i = 0; i < n && out < maxOut; i++) {
    if (nets[i].ssid[0] == '\0') continue;
    bool dup = false;
    for (size_t j = 0; j < out; j++) {
      if (std::strcmp(nets[j].ssid, nets[i].ssid) == 0) { dup = true; break; }
    }
    if (dup) continue;
    if (out != i) nets[out] = nets[i];
    out++;
  }
  return out;
}

void hostnameFromMac(const uint8_t mac[6], char *out, size_t n) {
  std::snprintf(out, n, "ctmeter-%02x%02x", mac[4], mac[5]);
}

int formatInfo(const InfoFields &f, const char *fw, char *buf, size_t n) {
  int k = std::snprintf(
      buf, n,
      "{\"t\":\"info\",\"fw\":\"%s\",\"host\":\"%s\",\"ip\":\"%s\",\"rssi\":%d,"
      "\"uptime_s\":%lld,\"last_seq\":%lu,\"provisioned\":%s",
      fw, f.host ? f.host : "", f.ip ? f.ip : "", f.rssi, static_cast<long long>(f.uptimeS),
      static_cast<unsigned long>(f.lastSeq), f.provisioned ? "true" : "false");
  if (k < 0 || static_cast<size_t>(k) >= n) return k;
  int kh = std::snprintf(buf + k, n - k,
                         ",\"reconnects\":%lu,\"disc_reason\":%u,\"heap_free\":%lu,"
                         "\"heap_min\":%lu,\"reset\":\"%s\",\"restored\":%s",
                         static_cast<unsigned long>(f.reconnects),
                         static_cast<unsigned>(f.discReason),
                         static_cast<unsigned long>(f.heapFree),
                         static_cast<unsigned long>(f.heapMin), f.reset ? f.reset : "",
                         f.restored ? "true" : "false");
  if (kh < 0 || static_cast<size_t>(k + kh) >= n) return kh < 0 ? kh : k + kh;
  k += kh;
  int k2 = f.wifi ? std::snprintf(buf + k, n - k, ",\"wifi\":\"%s\"}", f.wifi)
                  : std::snprintf(buf + k, n - k, "}");
  return k2 < 0 ? k2 : k + k2;
}

}  // namespace ctm

#include "config_store.h"

#include <Preferences.h>
#include <cstring>

namespace {
constexpr const char *NS = "ctm";
}

bool configLoad(DeviceConfig &out) {
  ctm::scrub(&out, sizeof out);
  out = DeviceConfig{};
  Preferences p;
  if (!p.begin(NS, true)) return false;   // namespace absent on first boot: not provisioned
  if (p.isKey("ssid")) p.getString("ssid", out.ssid, sizeof out.ssid);
  if (p.isKey("pass")) p.getString("pass", out.pass, sizeof out.pass);
  size_t k = p.isKey("key") ? p.getBytes("key", out.key, sizeof out.key) : 0;
  p.end();
  out.provisioned = out.ssid[0] != '\0' && k == ctm::PAIR_KEY_LEN;
  return out.provisioned;
}

bool configSave(const ctm::ProvisionArgs &a) {
  Preferences p;
  if (!p.begin(NS, false)) return false;
  bool ok = p.putString("ssid", a.ssid) == std::strlen(a.ssid);
  // putString returns 0 for "" (open network): check by reading back instead.
  p.putString("pass", a.pass);
  ok = ok && p.isKey("pass");
  ok = ok && p.putBytes("key", a.key, sizeof a.key) == sizeof a.key;
  p.end();
  return ok;
}

bool configClear() {
  Preferences p;
  if (!p.begin(NS, false)) return false;
  bool ok = p.clear();
  p.end();
  return ok;
}

#pragma once
// NVS (Preferences namespace "ctm"): ssid, pass, key(32B). PROTOCOL §4.2/§4.3.
// Secrets live only here and in RAM; never logged (CLAUDE.md rule 4).
#include <cstdint>

#include "../core/command.h"

struct DeviceConfig {
  bool    provisioned = false;   // ssid non-empty AND a 32B key present
  char    ssid[ctm::WIFI_SSID_MAX + 1] = {0};
  char    pass[ctm::WIFI_PASS_MAX + 1] = {0};
  uint8_t key[ctm::PAIR_KEY_LEN] = {0};
};

// Reads NVS into `out` (zeroed first). Returns out.provisioned.
bool configLoad(DeviceConfig &out);

// Writes all three keys. Returns false if any write failed.
bool configSave(const ctm::ProvisionArgs &a);

// Removes ssid/pass/key from the "ctm" namespace.
bool configClear();

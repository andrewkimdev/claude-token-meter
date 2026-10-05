#pragma once
// P5 last_state: NVS namespace "ctm", key "last" (blob, core/persist.h).
// NVS, not RTC_NOINIT (gotcha 3.17). `unpair` (configClear) wipes it too.
#include "../core/protocol.h"

// Reads and validates the blob. false if absent or invalid (CRC/magic/...).
bool stateLoad(ctm::StateFrame &out);

// Writes the blob. false if NVS refused it.
bool stateSave(const ctm::StateFrame &f);

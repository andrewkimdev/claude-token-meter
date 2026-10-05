#include "state_store.h"

#include <Preferences.h>

#include "../core/persist.h"

namespace {
constexpr const char *NS = "ctm";
constexpr const char *KEY = "last";
}  // namespace

bool stateLoad(ctm::StateFrame &out) {
  Preferences p;
  if (!p.begin(NS, true)) return false;   // namespace absent: nothing stored
  uint8_t buf[ctm::STATE_BLOB_LEN];
  size_t n = p.isKey(KEY) ? p.getBytesLength(KEY) : 0;
  bool ok = n == sizeof buf && p.getBytes(KEY, buf, sizeof buf) == sizeof buf;
  p.end();
  return ok && ctm::decodeStateBlob(buf, sizeof buf, out);
}

bool stateSave(const ctm::StateFrame &f) {
  uint8_t buf[ctm::STATE_BLOB_LEN];
  ctm::encodeStateBlob(f, buf);
  Preferences p;
  if (!p.begin(NS, false)) return false;
  bool ok = p.putBytes(KEY, buf, sizeof buf) == sizeof buf;
  p.end();
  return ok;
}

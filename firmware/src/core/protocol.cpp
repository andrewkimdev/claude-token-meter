#include "protocol.h"

#include <ArduinoJson.h>
#include <cmath>
#include <cstring>

namespace ctm {

namespace {

// Any JSON number (int or float) -> double.
bool getNumber(JsonVariantConst v, double &out) {
  if (!v.is<double>()) return false;   // NumberBit: true for ints and floats
  out = v.as<double>();
  return std::isfinite(out);
}

bool getEpoch(JsonVariantConst v, int64_t &out) {
  double d;
  if (!getNumber(v, d) || d < 0) return false;
  out = static_cast<int64_t>(d);
  return true;
}

// Missing window -> present=false (ok). Present but malformed -> false.
bool parseWindow(JsonVariantConst v, int64_t sentAt, Window &w) {
  w = Window{};
  if (v.isNull()) return true;
  if (!v.is<JsonObjectConst>()) return false;
  double pct;
  if (!getNumber(v["pct"], pct)) return false;
  if (!getEpoch(v["resets_at"], w.resets_at)) return false;
  JsonVariantConst obs = v["observed_at"];
  if (obs.isNull()) {
    w.observed_at = sentAt;   // [?] spec always sends it; fall back to sent_at
  } else if (!getEpoch(obs, w.observed_at)) {
    return false;
  }
  if (pct < 0) pct = 0;
  if (pct > 100) pct = 100;
  w.pct = static_cast<float>(pct);
  w.present = true;
  return true;
}

}  // namespace

ParseStatus parseStateFrame(const char *line, size_t len, StateFrame &out,
                            uint32_t &seqOut) {
  seqOut = 0;
  JsonDocument doc;
  if (deserializeJson(doc, line, len)) return ParseStatus::Parse;
  if (!doc.is<JsonObjectConst>()) return ParseStatus::Parse;

  const char *t = doc["t"];
  if (!t) return ParseStatus::Parse;
  if (std::strcmp(t, "state") != 0) return ParseStatus::NotState;

  JsonVariantConst seqV = doc["seq"];
  if (seqV.is<uint32_t>()) seqOut = seqV.as<uint32_t>();

  JsonVariantConst vV = doc["v"];
  if (!vV.is<int>() || vV.as<int>() != PROTOCOL_VERSION) return ParseStatus::BadV;

  if (!seqV.is<uint32_t>() || seqOut == 0) return ParseStatus::Parse;

  StateFrame f;
  f.seq = seqOut;
  if (!getEpoch(doc["sent_at"], f.sent_at)) return ParseStatus::Parse;

  JsonVariantConst wins = doc["windows"];
  if (!wins.isNull() && !wins.is<JsonObjectConst>()) return ParseStatus::Parse;
  if (!parseWindow(wins["five_hour"], f.sent_at, f.five_hour)) return ParseStatus::Parse;
  if (!parseWindow(wins["seven_day"], f.sent_at, f.seven_day)) return ParseStatus::Parse;

  out = f;
  return ParseStatus::Ok;
}

bool SeqGate::wouldAccept(uint32_t seq) const {
  if (!have_) return true;            // first frame after device boot
  if (seq > last_) return true;
  if (seq == 1 && seq < last_) return true;   // bridge restarted
  return false;
}

const char *errString(ParseStatus s) {
  switch (s) {
    case ParseStatus::Parse: return "parse";
    case ParseStatus::BadV:  return "bad_v";
    default:                 return "";
  }
}

}  // namespace ctm

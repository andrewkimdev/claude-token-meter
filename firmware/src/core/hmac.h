#pragma once
// Pure C++ (no Arduino, no mbedtls). Self-contained SHA-256 (FIPS 180-4) and
// HMAC-SHA256 (RFC 2104) so the exact same code runs in native tests and on
// the device. Written for this project from the spec, public-domain style.
#include <cstddef>
#include <cstdint>

namespace ctm {

constexpr size_t SHA256_LEN = 32;
constexpr size_t SHA256_BLOCK = 64;
constexpr size_t PAIR_KEY_LEN = 32;          // PROTOCOL §4.2: 32 random bytes
constexpr size_t SIG_HEX_LEN = 2 * SHA256_LEN;   // 64 lowercase hex chars

class Sha256 {
 public:
  Sha256() { reset(); }
  void reset();
  void update(const uint8_t *data, size_t len);
  void finish(uint8_t out[SHA256_LEN]);   // object must be reset() before reuse

 private:
  void block(const uint8_t *p);
  uint32_t h_[8];
  uint8_t  buf_[SHA256_BLOCK];
  size_t   bufLen_;
  uint64_t totalLen_;
};

void sha256(const uint8_t *data, size_t len, uint8_t out[SHA256_LEN]);

void hmacSha256(const uint8_t *key, size_t keyLen, const uint8_t *msg, size_t msgLen,
                uint8_t out[SHA256_LEN]);

// Lowercase hex. `out` must hold 2*len+1 bytes; NUL-terminated.
void toHex(const uint8_t *bytes, size_t len, char *out);

// Exactly 2*len hex chars (either case) -> bytes. False on any bad char/length.
bool fromHex(const char *hex, size_t hexLen, uint8_t *out, size_t len);

// True iff `sigHex` is exactly the 64-char lowercase hex of HMAC(key, msg).
// Content comparison is constant-time (length mismatch / NULL return early:
// the length is public). Uppercase hex is rejected (PROTOCOL §4.2: lowercase).
bool verifyHmacHex(const uint8_t *key, size_t keyLen, const uint8_t *msg, size_t msgLen,
                   const char *sigHex);

// Constant-time equality of two equal-length buffers.
bool constTimeEq(const uint8_t *a, const uint8_t *b, size_t len);

}  // namespace ctm

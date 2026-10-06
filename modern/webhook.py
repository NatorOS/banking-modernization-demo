"""HMAC-SHA256 webhook signing and verification for provider events.

Header format: ``X-Provider-Timestamp: <unix seconds>`` and ``X-Provider-Signature: v1=<hex>``,
where the MAC covers ``"<timestamp>.<raw body>"``. Verification is constant-time and rejects
timestamps outside the tolerance window. This is the simulator's scheme; a real provider's
documented scheme must be implemented and verified separately.
"""
import hashlib
import hmac
import time

TIMESTAMP_HEADER = "x-provider-timestamp"
SIGNATURE_HEADER = "x-provider-signature"


class SignatureError(Exception):
    pass


class WebhookSigner:
    def __init__(self, key, clock=None, tolerance_seconds=300):
        if isinstance(key, str):
            key = key.encode()
        if len(key) < 32:
            raise ValueError("webhook key must be at least 32 bytes")
        self._key = key
        self._clock = clock or time.time
        self.tolerance = tolerance_seconds

    def _now(self):
        now = self._clock()
        return now.timestamp() if hasattr(now, "timestamp") else float(now)

    def _mac(self, timestamp, body):
        return hmac.new(self._key, f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()

    def sign(self, body):
        timestamp = str(int(self._now()))
        return {"X-Provider-Timestamp": timestamp, "X-Provider-Signature": "v1=" + self._mac(timestamp, body)}

    def verify(self, body, headers):
        lowered = {k.lower(): v for k, v in (headers or {}).items()}
        timestamp, signature = lowered.get(TIMESTAMP_HEADER), lowered.get(SIGNATURE_HEADER)
        if not timestamp or not signature or not signature.startswith("v1="):
            raise SignatureError("missing signature headers")
        try:
            ts = int(timestamp)
        except ValueError:
            raise SignatureError("malformed timestamp") from None
        if abs(self._now() - ts) > self.tolerance:
            raise SignatureError("timestamp outside tolerance")
        if not hmac.compare_digest(signature[3:], self._mac(timestamp, body)):
            raise SignatureError("signature mismatch")

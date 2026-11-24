import hashlib
import hmac

SIGNATURE_HEADER = "Settle-Signature"
TIMESTAMP_HEADER = "Settle-Timestamp"


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """HMAC-SHA256 over `timestamp.body`.

    Signing the timestamp too means a captured request cannot be replayed later with a fresh
    timestamp: changing the timestamp breaks the signature.
    """
    message = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def signed_headers(secret: str, timestamp: int, body: bytes) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        TIMESTAMP_HEADER: str(timestamp),
        SIGNATURE_HEADER: sign(secret, timestamp, body),
    }

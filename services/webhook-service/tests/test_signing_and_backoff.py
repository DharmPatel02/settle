import hashlib
import hmac
import random

import pytest

from webhook_service.backoff import retry_delay_seconds
from webhook_service.signing import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign, signed_headers


def test_signature_is_hmac_sha256_of_timestamp_dot_body():
    body = b'{"event_id":"abc"}'
    expected = hmac.new(b"secret", b"1700000000." + body, hashlib.sha256).hexdigest()

    assert sign("secret", 1700000000, body) == expected


def test_signature_changes_when_body_timestamp_or_secret_changes():
    original = sign("secret", 1700000000, b"{}")

    assert sign("secret", 1700000000, b'{"x":1}') != original
    assert sign("secret", 1700000001, b"{}") != original
    assert sign("other", 1700000000, b"{}") != original


def test_signed_headers_carry_timestamp_and_signature():
    headers = signed_headers("secret", 1700000000, b"{}")

    assert headers[TIMESTAMP_HEADER] == "1700000000"
    assert headers[SIGNATURE_HEADER] == sign("secret", 1700000000, b"{}")


@pytest.mark.parametrize(("attempt", "window"), [(1, 1), (2, 2), (3, 4), (8, 128)])
def test_backoff_doubles_and_jitters_within_the_upper_half(attempt: int, window: float):
    rng = random.Random(42)
    delays = [retry_delay_seconds(attempt, rng=rng) for _ in range(200)]

    assert all(window / 2 <= delay <= window for delay in delays)
    assert len(set(delays)) > 1  # jitter actually varies the delay


def test_backoff_is_capped():
    assert retry_delay_seconds(30, cap_seconds=3600, rng=random.Random(1)) <= 3600
    assert retry_delay_seconds(30, cap_seconds=3600, rng=random.Random(1)) >= 1800


def test_backoff_rejects_attempt_zero():
    with pytest.raises(ValueError):
        retry_delay_seconds(0)

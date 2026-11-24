import random


def retry_delay_seconds(
    attempt: int,
    *,
    base_seconds: float = 1.0,
    cap_seconds: float = 3600.0,
    rng: random.Random | None = None,
) -> float:
    """Delay before the next try, after `attempt` failed tries (1-based).

    Exponential: 1 s, 2 s, 4 s, ... capped at one hour. The jitter picks a random point in the
    upper half of that window, so retries still back off but many deliveries that failed at
    the same moment (say, a merchant outage) do not all retry at the same moment too.
    """
    if attempt < 1:
        raise ValueError("attempt must be at least 1")
    exponential = min(cap_seconds, base_seconds * 2 ** (attempt - 1))
    return (rng or random).uniform(exponential / 2, exponential)

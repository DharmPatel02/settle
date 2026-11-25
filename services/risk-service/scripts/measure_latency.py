"""Measure end-to-end latency of POST /v1/score against a running risk-service.

    uv run uvicorn risk_service.main:app --port 8003            # in one terminal
    uv run python scripts/measure_latency.py --requests 2000    # in another

Requests are sent one at a time over a kept-alive connection, so the numbers are per-request
latency (HTTP + validation + prediction), not throughput under load.
"""

import argparse
import platform
import random
import statistics
import time

import httpx


def sample_features(rng: random.Random) -> dict[str, object]:
    return {
        "amount_minor": rng.randint(100, 200_000),
        "hour_of_day": rng.randint(0, 23),
        "account_age_days": rng.uniform(0, 1500),
        "transfers_last_hour": rng.randint(0, 10),
        "avg_amount_30d_minor": rng.choice([0, rng.randint(500, 20_000)]),
        "is_new_recipient": rng.random() < 0.3,
    }


def percentile(sorted_values: list[float], fraction: float) -> float:
    index = min(len(sorted_values) - 1, round(fraction * (len(sorted_values) - 1)))
    return sorted_values[index]


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure risk-service scoring latency.")
    parser.add_argument("--url", default="http://localhost:8003/v1/score")
    parser.add_argument("--requests", type=int, default=2000)
    parser.add_argument("--warmup", type=int, default=100)
    args = parser.parse_args()

    rng = random.Random(1)
    payloads = [sample_features(rng) for _ in range(args.warmup + args.requests)]
    timings_ms: list[float] = []
    with httpx.Client(timeout=5) as client:
        for index, payload in enumerate(payloads):
            started = time.perf_counter()
            client.post(args.url, json=payload).raise_for_status()
            elapsed_ms = (time.perf_counter() - started) * 1000
            if index >= args.warmup:
                timings_ms.append(elapsed_ms)

    timings_ms.sort()
    print(
        f"machine: {platform.platform()}, {platform.machine()}, Python {platform.python_version()}"
    )
    print(f"requests: {len(timings_ms)} (after {args.warmup} warm-up)")
    print(f"mean: {statistics.fmean(timings_ms):.2f} ms")
    for label, fraction in (("p50", 0.50), ("p95", 0.95), ("p99", 0.99)):
        print(f"{label}: {percentile(timings_ms, fraction):.2f} ms")
    print(f"max: {timings_ms[-1]:.2f} ms")


if __name__ == "__main__":
    main()

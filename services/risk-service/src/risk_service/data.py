"""Synthetic transfer data with injected fraud patterns.

There is no real payments data in this project, so the training set is generated. The
assumptions are written down here and in MODEL_CARD.md; the model is only as good as they are.

Legitimate transfers:
- Most accounts are established: age ~ Gamma(shape 1.5, scale 300 days), mean about 450 days.
  10% are new customers, between 0 and 30 days old.
- Each account has a typical spend: 30-day average ~ LogNormal around $40. 5% of accounts have
  no history (average 0), and so do 60% of accounts younger than one day.
- Amounts are log-normal around the account's own average. 3% are one-off large purchases.
- Hour of day peaks in the afternoon; few transfers happen at night.
- Most customers made no other transfer in the last hour (Poisson mean 0.2), but 15% are in an
  active session (mean 1.5), for example splitting a bill.
- 20% of payments go to a new recipient; 50% for new customers, who have few known payees yet.

Fraud (about 2% of rows), three patterns:
- Account takeover (50%): an established account suddenly sends 4x to 20x its usual amount,
  usually to a new recipient, more often at night.
- New mule account (30%): an account a day or two old moves a large amount in a burst of
  transfers to new recipients.
- Rapid small transfers (20%): many small transfers in one hour from a compromised account,
  testing what goes through.

The patterns overlap with legitimate behaviour on purpose (big purchases, new recipients, night
owls), so the problem is not trivially separable.
"""

import numpy as np

CENTS = 100


def _hour_weights(peak: float, width: float, floor: float) -> np.ndarray:
    hours = np.arange(24)
    distance = np.minimum(np.abs(hours - peak), 24 - np.abs(hours - peak))
    weights = floor + np.exp(-(distance**2) / (2 * width**2))
    return weights / weights.sum()


DAY_HOURS = _hour_weights(peak=14, width=4, floor=0.05)
NIGHT_HOURS = _hour_weights(peak=3, width=2.5, floor=0.15)


def _legitimate(rng: np.random.Generator, n: int) -> dict[str, np.ndarray]:
    new_customer = rng.random(n) < 0.1
    age = np.where(new_customer, rng.uniform(0, 30, n), rng.gamma(1.5, 300, n))
    average = np.round(rng.lognormal(np.log(40 * CENTS), 0.9, n))
    average[((age < 1) & (rng.random(n) < 0.6)) | (rng.random(n) < 0.05)] = 0
    active_session = rng.random(n) < 0.15

    amount = np.where(
        average > 0,
        average * rng.lognormal(0, 0.7, n),
        rng.lognormal(np.log(30 * CENTS), 1.0, n),
    )
    big_purchase = rng.random(n) < 0.03
    amount[big_purchase] *= rng.lognormal(1.5, 0.5, big_purchase.sum())

    return {
        "amount_minor": np.maximum(np.round(amount), 1).astype(np.int64),
        "hour_of_day": rng.choice(24, n, p=DAY_HOURS),
        "account_age_days": age,
        "transfers_last_hour": rng.poisson(np.where(active_session, 1.5, 0.2)),
        "avg_amount_30d_minor": average.astype(np.int64),
        "is_new_recipient": rng.random(n) < np.where(new_customer, 0.5, 0.2),
    }


def _account_takeover(rng: np.random.Generator, n: int) -> dict[str, np.ndarray]:
    average = np.round(rng.lognormal(np.log(40 * CENTS), 0.9, n))
    return {
        "amount_minor": np.round(average * rng.uniform(4, 20, n)).astype(np.int64),
        "hour_of_day": rng.choice(24, n, p=NIGHT_HOURS),
        "account_age_days": rng.gamma(1.5, 300, n),
        "transfers_last_hour": rng.poisson(1.5, n),
        "avg_amount_30d_minor": average.astype(np.int64),
        "is_new_recipient": rng.random(n) < 0.85,
    }


def _mule_account(rng: np.random.Generator, n: int) -> dict[str, np.ndarray]:
    average = np.round(rng.lognormal(np.log(15 * CENTS), 0.8, n))
    average[rng.random(n) < 0.8] = 0
    return {
        "amount_minor": np.round(rng.lognormal(np.log(200 * CENTS), 0.8, n)).astype(np.int64),
        "hour_of_day": rng.integers(0, 24, n),
        "account_age_days": rng.exponential(1.5, n),
        "transfers_last_hour": rng.poisson(4, n) + 1,
        "avg_amount_30d_minor": average.astype(np.int64),
        "is_new_recipient": rng.random(n) < 0.95,
    }


def _rapid_small(rng: np.random.Generator, n: int) -> dict[str, np.ndarray]:
    average = np.round(rng.lognormal(np.log(40 * CENTS), 0.9, n))
    return {
        "amount_minor": np.maximum(np.round(rng.lognormal(np.log(5 * CENTS), 0.6, n)), 1).astype(
            np.int64
        ),
        "hour_of_day": rng.integers(0, 24, n),
        "account_age_days": rng.gamma(1.5, 300, n),
        "transfers_last_hour": rng.poisson(8, n) + 3,
        "avg_amount_30d_minor": average.astype(np.int64),
        "is_new_recipient": rng.random(n) < 0.7,
    }


def generate(
    n: int, *, fraud_rate: float = 0.02, seed: int = 42
) -> tuple[dict[str, np.ndarray], np.ndarray]:
    """Return (raw feature columns, labels) with labels 1 = fraud, shuffled."""
    rng = np.random.default_rng(seed)
    n_fraud = int(round(n * fraud_rate))
    n_takeover = int(round(n_fraud * 0.5))
    n_mule = int(round(n_fraud * 0.3))
    n_rapid = n_fraud - n_takeover - n_mule

    parts = [
        _legitimate(rng, n - n_fraud),
        _account_takeover(rng, n_takeover),
        _mule_account(rng, n_mule),
        _rapid_small(rng, n_rapid),
    ]
    labels = np.concatenate([np.zeros(n - n_fraud, dtype=np.int8), np.ones(n_fraud, np.int8)])
    order = rng.permutation(n)
    columns = {name: np.concatenate([part[name] for part in parts])[order] for name in parts[0]}
    return columns, labels[order]

# Model card: risk-hgb-v1

**The training data is synthetic.** Every number below describes how well the model separates
fraud patterns that this project generated on purpose. None of it says how the model would do on
real payments.

## What it does

Scores a transfer between two Settle accounts from 0 to 1 (higher is riskier) and maps the score
to a decision:

| Score | Decision | What payments-api does |
|---|---|---|
| below `review` threshold | `APPROVE` | Moves the money |
| between the thresholds | `REVIEW` | Holds the transfer as `PENDING_REVIEW` for a person |
| at or above `decline` threshold | `DECLINE` | Records a `DECLINED` transfer, moves nothing |

Deposits are not scored. When the service is unavailable, payments-api uses its own fallback
policy (ADR 0010), not this model.

## Inputs

| Feature | Meaning |
|---|---|
| `amount_minor` | Transfer amount in cents |
| `hour_of_day` | UTC hour of the request |
| `account_age_days` | Age of the source account |
| `transfers_last_hour` | Completed transfers from the source account in the past hour |
| `avg_amount_30d_minor` | Average completed outgoing transfer over 30 days, 0 if none |
| `is_new_recipient` | The source account has never completed a transfer to this recipient |
| `amount_to_avg_ratio` | Derived: amount divided by the 30-day average, 0 without history |

The request schema rejects unknown fields, so a misspelled feature fails instead of being ignored.

## Training data

`src/risk_service/data.py` generates 200,000 transfers with a fixed seed. About 2% are fraud. The
module docstring lists every assumption. In short:

- **Legitimate:** mostly established accounts, plus 10% new customers up to 30 days old;
  amounts log-normal around each account's own average, plus 3% one-off large purchases;
  activity peaks in the afternoon; 15% of customers are in an active session with several
  transfers an hour; 20% of payments go to a new recipient (50% for new customers).
- **Account takeover (half of fraud):** an established account sends 4 to 20 times its usual
  amount, usually to a new recipient, more often at night.
- **Mule account (30%):** an account one or two days old sends large amounts in a burst.
- **Rapid small transfers (20%):** many small payments within an hour, testing a stolen account.

Legitimate behaviour overlaps with these patterns on purpose (large purchases, new recipients,
night activity), but the patterns are still cleaner than real fraud, which adapts to the model.

## Method

`HistGradientBoostingClassifier` (300 iterations max, learning rate 0.1, early stopping), compared
with an unsupervised `IsolationForest` baseline. Split 60/20/20: train, validation for
thresholds, and test for every reported number. Thresholds: `decline` is where 0.1% of
legitimate validation transfers score higher; `review` is where 2% do. Reproduce with:

```bash
uv run python -m risk_service.train   # rewrites models/risk_model.joblib and models/metrics.json
```

The run is deterministic: the same seed produces the same metrics.

## Results on the test set (40,000 transfers)

Full output: [`models/metrics.json`](models/metrics.json).

| Model | PR-AUC | ROC-AUC | Recall at 1% FPR |
|---|---|---|---|
| HistGradientBoosting | 0.9633 | 0.9991 | 98.1% |
| IsolationForest baseline | 0.5633 | 0.9880 | 60.1% |
| Always "legitimate" | n/a | n/a | 0% (yet 98.0% accurate) |

PR-AUC is the headline metric because fraud is rare: ROC-AUC looks excellent for both models,
while PR-AUC shows how many flagged transfers are really fraud.

| Decision | Share of transfers | Share of fraud caught | Share of good transfers | Precision |
|---|---|---|---|---|
| APPROVE | 96.2% | 0.9% | 98.1% | 0.02% |
| REVIEW | 2.0% | 11.6% | 1.8% | 11.6% |
| DECLINE | 1.8% | 87.5% | 0.09% | 95.0% |

## Serving latency

`scripts/measure_latency.py`, 2,000 sequential requests after 100 warm-up requests against a
single `uvicorn` worker on localhost (Apple M4, 16 GB, macOS 26.6, Python 3.14.7), three runs
on 2026-11-25:

| Run | mean | p50 | p95 | p99 | max |
|---|---|---|---|---|---|
| 1 | 1.55 ms | 1.42 ms | 2.38 ms | 3.12 ms | 4.58 ms |
| 2 | 1.84 ms | 1.54 ms | 3.31 ms | 4.04 ms | 39.83 ms |
| 3 | 1.90 ms | 1.64 ms | 3.19 ms | 4.50 ms | 52.91 ms |

These are single-client numbers on a laptop, not throughput under load.

payments-api calls the service with a 150 ms timeout.

## Limitations

- Synthetic data: the metrics above are optimistic and do not transfer to real traffic.
- No feedback loop yet: decisions made in the review queue are not fed back as labels.
- No drift monitoring: a change in customer behaviour would silently degrade the model.
- Features are computed before account locks are taken, so concurrent transfers from one account
  can see a slightly stale velocity count.
- `hour_of_day` is UTC, not the customer's local time.
- The artifact is a joblib pickle. Only load files this project produced; the service also
  refuses artifacts from a different scikit-learn version.

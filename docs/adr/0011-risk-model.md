# ADR 0011: Gradient-boosted trees for the risk model, with thresholds set by false-positive rate

- Status: Accepted
- Date: 2026-11-24

## Context

The risk service turns six transfer features into APPROVE, REVIEW, or DECLINE. There is no real
labelled fraud data, so the model trains on generated data with documented fraud patterns
(`services/risk-service/MODEL_CARD.md`). Fraud is about 2% of transfers, so a model that always
answers "legitimate" is 98% accurate and useless.

## Decision

- Train scikit-learn's `HistGradientBoostingClassifier` on labelled data and compare it with an
  unsupervised `IsolationForest` baseline. Evaluate both with PR-AUC and recall at a 1%
  false-positive rate, not accuracy.
- Split the data 60/20/20. Pick thresholds on the validation set: DECLINE where 0.1% of
  legitimate transfers score higher, REVIEW where 2% do. Report every metric on the test set.
- Save the model with joblib together with its version, thresholds, feature names, and the
  scikit-learn version. The service refuses to load an artifact from a different scikit-learn
  version or with different features.
- Training and serving share one `feature_matrix` function, so derived features are computed
  identically in both.

## Alternatives

- **Logistic regression:** easy to explain, but needs hand-made interactions (for example "large
  amount *and* new recipient *and* young account") that trees learn on their own.
- **IsolationForest only:** needs no labels, which matters when labels are scarce, but on the test
  set it catches far less fraud at the same false-positive rate (see `models/metrics.json`).
- **Rules only:** transparent, but every threshold is a guess and rules interact badly as they
  grow. A real system would combine rules and a model.
- **Thresholds by a fixed score (for example 0.5):** ignores the cost of each mistake. Setting
  them by false-positive rate ties them to how many good customers are blocked or reviewed.

## Consequences

The model handles feature interactions and missing history without extra feature engineering, and
predicts in a few milliseconds. The metrics describe synthetic data and say nothing about real
fraud; on real data the model would need retraining, monitoring for drift, and a feedback loop
from the review queue. Thresholds must be re-derived whenever the model is retrained.

"""Train the risk model and write the artifact plus its metrics.

    uv run python -m risk_service.train            # writes models/risk_model.joblib + metrics.json

The data is split 60/20/20. The model trains on the first part, the decision thresholds are
picked on the validation part, and every reported number comes from the test part, which
neither training nor threshold selection has seen.
"""

import argparse
import json
from pathlib import Path

import joblib
import numpy as np
import sklearn
from sklearn.ensemble import HistGradientBoostingClassifier, IsolationForest
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.model_selection import train_test_split

from risk_service.data import generate
from risk_service.features import FEATURE_NAMES, feature_matrix

MODEL_VERSION = "risk-hgb-v1"
# Share of legitimate transfers we accept sending to each bucket. DECLINE must almost never hit
# a good customer; REVIEW is limited by how many transfers a person can look at.
DECLINE_FPR = 0.001
REVIEW_FPR = 0.02
REPORT_FPR = 0.01


def threshold_at_fpr(labels: np.ndarray, scores: np.ndarray, fpr: float) -> float:
    """The score above which only `fpr` of legitimate transfers fall."""
    return float(np.quantile(scores[labels == 0], 1 - fpr))


def recall_at_fpr(labels: np.ndarray, scores: np.ndarray, fpr: float) -> float:
    threshold = threshold_at_fpr(labels, scores, fpr)
    return float((scores[labels == 1] > threshold).mean())


def evaluate(labels: np.ndarray, scores: np.ndarray) -> dict[str, float]:
    return {
        "pr_auc": round(float(average_precision_score(labels, scores)), 4),
        "roc_auc": round(float(roc_auc_score(labels, scores)), 4),
        f"recall_at_{REPORT_FPR:.0%}_fpr": round(recall_at_fpr(labels, scores, REPORT_FPR), 4),
    }


def decision_table(
    labels: np.ndarray, scores: np.ndarray, review: float, decline: float
) -> dict[str, dict[str, float]]:
    buckets = {
        "APPROVE": scores < review,
        "REVIEW": (scores >= review) & (scores < decline),
        "DECLINE": scores >= decline,
    }
    fraud, legit = labels == 1, labels == 0
    return {
        name: {
            "share_of_transfers": round(float(mask.mean()), 4),
            "share_of_fraud": round(float(mask[fraud].mean()), 4),
            "share_of_legitimate": round(float(mask[legit].mean()), 4),
            "precision": round(float(labels[mask].mean()) if mask.any() else 0.0, 4),
        }
        for name, mask in buckets.items()
    }


def train(rows: int, seed: int) -> tuple[dict[str, object], dict[str, object]]:
    columns, labels = generate(rows, seed=seed)
    features = feature_matrix(columns)
    x_train, x_rest, y_train, y_rest = train_test_split(
        features, labels, test_size=0.4, stratify=labels, random_state=seed
    )
    x_val, x_test, y_val, y_test = train_test_split(
        x_rest, y_rest, test_size=0.5, stratify=y_rest, random_state=seed
    )

    model = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.1, random_state=seed)
    model.fit(x_train, y_train)

    # Unsupervised baseline: needs no labels, flags whatever looks unusual.
    baseline = IsolationForest(n_estimators=200, random_state=seed).fit(x_train)

    val_scores = model.predict_proba(x_val)[:, 1]
    review = threshold_at_fpr(y_val, val_scores, REVIEW_FPR)
    decline = max(threshold_at_fpr(y_val, val_scores, DECLINE_FPR), review)

    test_scores = model.predict_proba(x_test)[:, 1]
    baseline_scores = -baseline.score_samples(x_test)  # higher = more anomalous
    metrics: dict[str, object] = {
        "model_version": MODEL_VERSION,
        "sklearn_version": sklearn.__version__,
        "data": {
            "rows": rows,
            "seed": seed,
            "fraud_rate": round(float(labels.mean()), 4),
            "split": {"train": len(y_train), "validation": len(y_val), "test": len(y_test)},
        },
        "test_metrics": {
            "hist_gradient_boosting": evaluate(y_test, test_scores),
            "isolation_forest_baseline": evaluate(y_test, baseline_scores),
            "always_predict_legitimate_accuracy": round(float(1 - y_test.mean()), 4),
        },
        "thresholds": {
            "review": round(review, 6),
            "decline": round(decline, 6),
            "chosen_on": "validation",
            "review_target_fpr": REVIEW_FPR,
            "decline_target_fpr": DECLINE_FPR,
        },
        "test_decisions": decision_table(y_test, test_scores, review, decline),
    }
    artifact: dict[str, object] = {
        "model": model,
        "model_version": MODEL_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "thresholds": {"review": review, "decline": decline},
        "sklearn_version": sklearn.__version__,
    }
    return artifact, metrics


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rows", type=int, default=200_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=Path("models"))
    args = parser.parse_args(argv)

    artifact, metrics = train(args.rows, args.seed)
    args.out.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, args.out / "risk_model.joblib", compress=3)
    (args.out / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()

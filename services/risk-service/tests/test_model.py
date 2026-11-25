import json
from pathlib import Path

import joblib
import numpy as np
import pytest
from helpers import SERVICE_ROOT

from risk_service import train
from risk_service.data import generate
from risk_service.features import FEATURE_NAMES, feature_matrix
from risk_service.model import Decision, ModelLoadError, RiskModel

MODEL_PATH = SERVICE_ROOT / "models" / "risk_model.joblib"


def test_generator_is_reproducible_and_has_two_percent_fraud():
    columns, labels = generate(10_000, seed=7)
    again, labels_again = generate(10_000, seed=7)

    assert labels.mean() == pytest.approx(0.02)
    np.testing.assert_array_equal(labels, labels_again)
    for name, values in columns.items():
        np.testing.assert_array_equal(values, again[name])
    assert (columns["amount_minor"] > 0).all()
    assert columns["amount_minor"].dtype == np.int64  # money stays integer


def test_amount_ratio_is_zero_without_history():
    matrix = feature_matrix(
        {
            "amount_minor": np.array([5000, 5000]),
            "hour_of_day": np.array([10, 10]),
            "account_age_days": np.array([1.0, 90.0]),
            "transfers_last_hour": np.array([0, 0]),
            "avg_amount_30d_minor": np.array([0, 2500]),
            "is_new_recipient": np.array([True, False]),
        }
    )
    ratio = matrix[:, FEATURE_NAMES.index("amount_to_avg_ratio")]
    np.testing.assert_array_equal(ratio, [0.0, 2.0])


def test_decide_uses_the_thresholds():
    model = RiskModel.load(MODEL_PATH)
    assert model.review_threshold < model.decline_threshold

    assert model.decide(model.review_threshold - 1e-9) == Decision.APPROVE
    assert model.decide(model.review_threshold) == Decision.REVIEW
    assert model.decide(model.decline_threshold - 1e-9) == Decision.REVIEW
    assert model.decide(model.decline_threshold) == Decision.DECLINE


def test_committed_metrics_match_the_committed_model():
    metrics = json.loads((SERVICE_ROOT / "models" / "metrics.json").read_text())
    model = RiskModel.load(MODEL_PATH)

    assert metrics["model_version"] == model.model_version
    assert metrics["thresholds"]["review"] == pytest.approx(model.review_threshold, abs=1e-6)
    assert metrics["thresholds"]["decline"] == pytest.approx(model.decline_threshold, abs=1e-6)


def test_model_from_another_sklearn_version_is_refused(tmp_path: Path):
    artifact = joblib.load(MODEL_PATH)
    artifact["sklearn_version"] = "0.0.1"
    joblib.dump(artifact, tmp_path / "old.joblib")

    with pytest.raises(ModelLoadError, match="Retrain"):
        RiskModel.load(tmp_path / "old.joblib")


def test_model_with_different_features_is_refused(tmp_path: Path):
    artifact = joblib.load(MODEL_PATH)
    artifact["feature_names"] = artifact["feature_names"][:-1]
    joblib.dump(artifact, tmp_path / "other.joblib")

    with pytest.raises(ModelLoadError, match="feature contract"):
        RiskModel.load(tmp_path / "other.joblib")


def test_training_writes_a_loadable_model_that_beats_the_baseline(tmp_path: Path):
    train.main(["--rows", "20000", "--seed", "3", "--out", str(tmp_path)])

    model = RiskModel.load(tmp_path / "risk_model.joblib")
    metrics = json.loads((tmp_path / "metrics.json").read_text())
    supervised = metrics["test_metrics"]["hist_gradient_boosting"]
    baseline = metrics["test_metrics"]["isolation_forest_baseline"]
    assert supervised["pr_auc"] > baseline["pr_auc"]
    assert model.review_threshold <= model.decline_threshold
    decisions = metrics["test_decisions"]
    assert sum(bucket["share_of_transfers"] for bucket in decisions.values()) == pytest.approx(
        1, abs=1e-3
    )

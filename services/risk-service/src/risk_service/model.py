from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import joblib
import sklearn
from sklearn.ensemble import HistGradientBoostingClassifier

from risk_service.features import FEATURE_NAMES, TransferFeatures, single_row


class Decision(StrEnum):
    APPROVE = "APPROVE"
    REVIEW = "REVIEW"
    DECLINE = "DECLINE"


class ModelLoadError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RiskModel:
    estimator: HistGradientBoostingClassifier
    model_version: str
    review_threshold: float
    decline_threshold: float

    @classmethod
    def load(cls, path: Path) -> RiskModel:
        # joblib files are pickles: only load artifacts this project produced.
        artifact = joblib.load(path)
        if artifact["sklearn_version"] != sklearn.__version__:
            # A pickle from another scikit-learn version may load but predict differently.
            raise ModelLoadError(
                f"Model was trained with scikit-learn {artifact['sklearn_version']}, "
                f"but {sklearn.__version__} is installed. Retrain the model."
            )
        if tuple(artifact["feature_names"]) != FEATURE_NAMES:
            raise ModelLoadError("Model features do not match the service's feature contract.")
        thresholds = artifact["thresholds"]
        return cls(
            estimator=artifact["model"],
            model_version=artifact["model_version"],
            review_threshold=thresholds["review"],
            decline_threshold=thresholds["decline"],
        )

    def score(self, features: TransferFeatures) -> float:
        return float(self.estimator.predict_proba(single_row(features))[0, 1])

    def decide(self, score: float) -> Decision:
        if score >= self.decline_threshold:
            return Decision.DECLINE
        if score >= self.review_threshold:
            return Decision.REVIEW
        return Decision.APPROVE

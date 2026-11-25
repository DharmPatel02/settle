"""The feature contract shared by training and serving.

payments-api computes the raw features from its database and sends them to `POST /v1/score`.
Training and serving both go through `feature_matrix`, so derived features are computed the
same way in both places (no train/serve skew).
"""

from collections.abc import Mapping

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

RAW_FEATURES = (
    "amount_minor",
    "hour_of_day",
    "account_age_days",
    "transfers_last_hour",
    "avg_amount_30d_minor",
    "is_new_recipient",
)
FEATURE_NAMES = (*RAW_FEATURES, "amount_to_avg_ratio")


class TransferFeatures(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount_minor: int = Field(gt=0, description="Transfer amount in minor units (cents).")
    hour_of_day: int = Field(ge=0, le=23, description="UTC hour the transfer was requested.")
    account_age_days: float = Field(ge=0, description="Age of the source account in days.")
    transfers_last_hour: int = Field(
        ge=0, description="Completed transfers from the source account in the last hour."
    )
    avg_amount_30d_minor: int = Field(
        ge=0, description="Average outgoing transfer over 30 days; 0 when there is no history."
    )
    is_new_recipient: bool = Field(
        description="True when the source account has never paid this recipient before."
    )


def feature_matrix(columns: Mapping[str, np.ndarray]) -> np.ndarray:
    """Turn raw feature columns into the model's input matrix, in FEATURE_NAMES order."""
    amount = np.asarray(columns["amount_minor"], dtype=np.float64)
    average = np.asarray(columns["avg_amount_30d_minor"], dtype=np.float64)
    # 0 means "no history"; the model also sees avg_amount_30d_minor == 0 and learns that case.
    ratio = np.divide(amount, average, out=np.zeros_like(amount), where=average > 0)
    raw = [np.asarray(columns[name], dtype=np.float64) for name in RAW_FEATURES]
    return np.column_stack([*raw, ratio])


def single_row(features: TransferFeatures) -> np.ndarray:
    return feature_matrix({name: np.array([getattr(features, name)]) for name in RAW_FEATURES})

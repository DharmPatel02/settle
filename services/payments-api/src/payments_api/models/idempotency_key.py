from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from payments_api.models.base import Base


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        CheckConstraint("status IN ('IN_PROGRESS', 'COMPLETED')", name="valid_status"),
    )

    client_id: Mapped[str] = mapped_column(Text, primary_key=True)
    idem_key: Mapped[str] = mapped_column(Text, primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    response_code: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

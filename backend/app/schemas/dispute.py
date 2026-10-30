from pydantic import BaseModel, ConfigDict
from typing import Optional, List, Any
from datetime import datetime
from decimal import Decimal


class DisputeBase(BaseModel):
    dispute_id: str
    merchant_id: str = "merchant_default"
    transaction_id: str
    dispute_amount: Decimal
    currency: str = "USD"
    reason: str
    status: str
    lifecycle_stage: str
    response_due_date: Optional[datetime] = None
    adjudication_outcome: Optional[str] = None


class DisputeResponse(DisputeBase):
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class DisputeDetailResponse(DisputeResponse):
    evidence_package: Optional[Any] = None
    telemetry_records: Optional[List[Any]] = None

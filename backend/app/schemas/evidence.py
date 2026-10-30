from pydantic import BaseModel, Field
from typing import List, Optional


class TelemetryProof(BaseModel):
    prior_transaction_id: str = Field(description="Historical undisputed transaction ID")
    prior_date: str = Field(description="ISO timestamp of historical undisputed transaction")
    matched_factor: str = Field(description="Matched parameter: IP_ADDRESS, DEVICE_ID, or SHIPPING_ADDRESS")
    match_value: str = Field(description="Exact value verified across both transactions")


class EvidenceDossier(BaseModel):
    evidence_type: str = Field(
        default="PROOF_OF_FULFILLMENT",
        pattern="^(PROOF_OF_FULFILLMENT|OTHER|PROOF_OF_REFUND)$",
        description="PayPal standard evidence type"
    )
    carrier_name: Optional[str] = Field(None, description="Standardized courier name")
    tracking_number: Optional[str] = Field(None, description="Verified courier tracking number")
    delivery_date: Optional[str] = Field(None, description="Verified ISO timestamp of confirmed delivery")
    compelling_evidence_factor: str = Field(description="Visa CE3.0 justification category")
    historical_matches: List[TelemetryProof] = Field(
        default_factory=list,
        description="Prior undisputed transactions sharing core digital identifiers"
    )
    formal_defense_narrative: str = Field(
        min_length=50,
        max_length=2000,
        description="Concise legal brief synthesized for the adjudicator"
    )
    confidence_score: float = Field(
        default=0.95,
        ge=0.0,
        le=1.0,
        description="Algorithmic confidence rating"
    )
    adjudication_recommendation: str = Field(
        default="SELLER_FAVOR",
        pattern="^(SELLER_FAVOR)$"
    )

from pydantic import BaseModel, Field
from typing import List, Optional, Dict


class TelemetryProof(BaseModel):
    prior_transaction_id: str = Field(description="Historical undisputed transaction ID")
    prior_date: str = Field(description="ISO timestamp of historical undisputed transaction")
    matched_factor: str = Field(description="Matched parameter: IP_ADDRESS, DEVICE_ID, or SHIPPING_ADDRESS")
    match_value: str = Field(description="Exact value verified across both transactions")
    provenance: str = Field(
        default="SYNTHETIC_DEMO",
        description="Source of telemetry: SYNTHETIC_DEMO, VERIFIED_MERCHANT, EXTERNAL_API, MANUAL_ENTRY"
    )
    is_verified: bool = Field(
        default=False,
        description="Whether this telemetry is genuine verified merchant data rather than a synthetic fixture"
    )


class EvidenceAttachment(BaseModel):
    attachment_id: str = Field(description="Unique identifier for uploaded document")
    filename: str = Field(description="Original file name e.g. delivery_receipt.pdf")
    mime_type: str = Field(description="MIME type e.g. application/pdf")
    sha256_hash: str = Field(description="SHA-256 cryptographic digest of document bytes")
    size_bytes: int = Field(default=0, ge=0, description="File byte size")


class EvidenceDossier(BaseModel):
    evidence_type: str = Field(
        default="PROOF_OF_FULFILLMENT",
        pattern="^(PROOF_OF_FULFILLMENT|OTHER|PROOF_OF_REFUND)$",
        description="PayPal standard evidence type"
    )
    carrier_name: Optional[str] = Field(None, description="Standardized courier name")
    tracking_number: Optional[str] = Field(None, description="Verified courier tracking number")
    delivery_date: Optional[str] = Field(None, description="Verified ISO timestamp of confirmed delivery")
    compelling_evidence_factor: str = Field(description="Evidence classification or Visa CE3.0 factor")
    ce30_eligible: bool = Field(
        default=False,
        description="Whether the dispute qualifies for Visa CE3.0 (Reason 10.4 Card-Absent Fraud)"
    )
    ce30_ineligibility_reason: Optional[str] = Field(
        default=None,
        description="Detailed explanation if dispute is ineligible for Visa CE3.0"
    )
    fulfillment_status: str = Field(
        default="INSUFFICIENT_EVIDENCE",
        description="Fulfillment evidence completeness: VERIFIED, INSUFFICIENT_EVIDENCE, or REVIEW_REQUIRED"
    )
    provenance_summary: Dict[str, str] = Field(
        default_factory=dict,
        description="Provenance audit trail mapping each data source to its authenticity tier"
    )
    historical_matches: List[TelemetryProof] = Field(
        default_factory=list,
        description="Prior undisputed transactions sharing core digital identifiers"
    )
    attachments: List[EvidenceAttachment] = Field(
        default_factory=list,
        description="Cryptographically hashed evidence documents"
    )
    formal_defense_narrative: str = Field(
        min_length=50,
        max_length=2000,
        description="Factual legal brief synthesized for the adjudicator without unsupported claims"
    )
    confidence_score: float = Field(
        default=0.50,
        ge=0.0,
        le=1.0,
        description="Algorithmic evidence completeness and consistency score (0.0 to 1.0). Does NOT represent win probability."
    )
    adjudication_recommendation: str = Field(
        default="SELLER_FAVOR",
        pattern="^(SELLER_FAVOR|MERCHANT_REVIEW_REQUIRED|INSUFFICIENT_EVIDENCE)$",
        description="Recommended merchant adjudication posture"
    )

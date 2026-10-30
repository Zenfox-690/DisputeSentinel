from backend.app.database import Base
from sqlalchemy import Column, String, Numeric, DateTime, JSON, Text, func
import uuid


def generate_uuid():
    return str(uuid.uuid4())


class Dispute(Base):
    __tablename__ = "disputes"

    dispute_id = Column(String(64), primary_key=True)
    merchant_id = Column(String(64), nullable=False, default="merchant_default", index=True)
    transaction_id = Column(String(64), nullable=False, index=True)
    dispute_amount = Column(Numeric(12, 2), nullable=False)
    currency = Column(String(3), nullable=False, default="USD")
    reason = Column(String(64), nullable=False)
    status = Column(String(64), nullable=False)
    lifecycle_stage = Column(String(64), nullable=False)
    response_due_date = Column(DateTime(timezone=True), nullable=True)
    adjudication_outcome = Column(String(32), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class TransactionTelemetry(Base):
    __tablename__ = "transaction_telemetry"

    telemetry_id = Column(String(36), primary_key=True, default=generate_uuid)
    transaction_id = Column(String(64), nullable=False, index=True)
    customer_email = Column(String(255), nullable=False, index=True)
    ip_address = Column(String(45), nullable=False)
    device_fingerprint = Column(String(128), nullable=False)
    shipping_address = Column(JSON, nullable=True)
    carrier_tracking_number = Column(String(128), nullable=True)
    carrier_name = Column(String(64), nullable=True)
    delivery_status = Column(String(64), nullable=True)
    delivery_timestamp = Column(DateTime(timezone=True), nullable=True)
    timestamp = Column(DateTime(timezone=True), nullable=False)


class EvidencePackage(Base):
    __tablename__ = "evidence_packages"

    package_id = Column(String(36), primary_key=True, default=generate_uuid)
    dispute_id = Column(String(64), nullable=False, index=True)
    evidence_type = Column(String(64), nullable=False, default="PROOF_OF_FULFILLMENT")
    narrative_text = Column(Text, nullable=False)
    telemetry_matches = Column(JSON, nullable=False)
    submission_payload = Column(JSON, nullable=False)
    status = Column(String(32), nullable=False, default="DRAFT")  # DRAFT, APPROVED, SUBMITTED, REVIEW_REQUIRED, REJECTED
    confidence_score = Column(Numeric(4, 3), nullable=False)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class MerchantApproval(Base):
    __tablename__ = "merchant_approvals"

    approval_id = Column(String(36), primary_key=True, default=generate_uuid)
    dispute_id = Column(String(64), nullable=False, index=True)
    package_id = Column(String(36), nullable=False, index=True)
    content_hash = Column(String(64), nullable=False)
    merchant_id = Column(String(64), nullable=False, index=True)
    approved_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    transmission_id = Column(String(128), primary_key=True)
    event_id = Column(String(128), unique=True, nullable=False)
    event_type = Column(String(128), nullable=False)
    resource_id = Column(String(128), nullable=False)
    raw_payload = Column(JSON, nullable=False)
    processed_status = Column(String(32), nullable=False, default="RECEIVED")  # RECEIVED, PROCESSED, IGNORED, FAILED
    created_at = Column(DateTime(timezone=True), server_default=func.now())

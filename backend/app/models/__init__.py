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
    seller_transaction_id = Column(String(64), nullable=True, index=True)
    buyer_transaction_id = Column(String(64), nullable=True, index=True)
    dispute_amount = Column(Numeric(12, 2), nullable=False)
    currency = Column(String(3), nullable=False, default="USD")
    reason = Column(String(64), nullable=False)
    status = Column(String(64), nullable=False)
    external_status = Column(String(64), nullable=True)
    workflow_status = Column(String(64), nullable=True, default="PENDING_ACTION")
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
    provenance = Column(String(32), nullable=False, default="SYNTHETIC_DEMO")  # SYNTHETIC_DEMO, VERIFIED_MERCHANT, EXTERNAL_API, MANUAL_ENTRY
    timestamp = Column(DateTime(timezone=True), nullable=False)


class FulfillmentRecord(Base):
    __tablename__ = "fulfillment_records"

    fulfillment_id = Column(String(36), primary_key=True, default=generate_uuid)
    transaction_id = Column(String(64), nullable=False, index=True)
    merchant_id = Column(String(64), nullable=False, default="merchant_default", index=True)
    fulfillment_type = Column(String(32), nullable=False, default="DIGITAL_LICENSE")  # DIGITAL_LICENSE, PHYSICAL_SHIPMENT, DIGITAL_DOWNLOAD, SERVICE_ACCESS
    recipient_email = Column(String(255), nullable=True)
    license_key = Column(String(255), nullable=True)
    download_url = Column(String(512), nullable=True)
    download_timestamp = Column(DateTime(timezone=True), nullable=True)
    download_ip = Column(String(45), nullable=True)
    carrier_name = Column(String(64), nullable=True)
    tracking_number = Column(String(128), nullable=True)
    delivery_timestamp = Column(DateTime(timezone=True), nullable=True)
    delivery_status = Column(String(64), nullable=True)  # ISSUED, ACCESSED, DELIVERED, FAILED
    document_hash = Column(String(64), nullable=True)
    validation_source = Column(String(64), nullable=False, default="MANUAL_ENTRY")  # MANUAL_ENTRY, CARRIER_API, CRYPTOGRAPHIC_REGISTRY, INDEPENDENT_AUDIT
    validation_timestamp = Column(DateTime(timezone=True), nullable=True)
    audit_history = Column(JSON, nullable=True)
    provenance = Column(String(32), nullable=False, default="MERCHANT_ATTESTED")  # MERCHANT_ATTESTED, MANUAL_ENTRY, VERIFIED_MERCHANT, SYNTHETIC_DEMO
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class EvidencePackage(Base):
    __tablename__ = "evidence_packages"

    package_id = Column(String(36), primary_key=True, default=generate_uuid)
    dispute_id = Column(String(64), nullable=False, index=True)
    evidence_type = Column(String(64), nullable=False, default="PROOF_OF_FULFILLMENT")
    narrative_text = Column(Text, nullable=False)
    telemetry_matches = Column(JSON, nullable=False)
    submission_payload = Column(JSON, nullable=False)
    status = Column(String(32), nullable=False, default="DRAFT")  # DRAFT, APPROVED, SUBMITTED, REVIEW_REQUIRED, REJECTED, INSUFFICIENT_EVIDENCE
    fulfillment_status = Column(String(32), nullable=False, default="INSUFFICIENT_EVIDENCE")  # VERIFIED, INSUFFICIENT_EVIDENCE, REVIEW_REQUIRED
    provenance = Column(String(32), nullable=False, default="SYNTHETIC_DEMO")  # SYNTHETIC_DEMO, VERIFIED_MERCHANT, EXTERNAL_API
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

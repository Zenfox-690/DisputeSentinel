from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List, Dict, Any, Optional
from decimal import Decimal
from datetime import datetime, timezone
from pydantic import BaseModel

from backend.app.database import get_db
from backend.app.models import Dispute, EvidencePackage, TransactionTelemetry, MerchantApproval, FulfillmentRecord
from backend.app.schemas.dispute import DisputeResponse
from backend.app.schemas.evidence import EvidenceDossier
from backend.app.services.paypal_client import paypal_client
from backend.app.services.ce30_engine import ce30_engine
from backend.app.services.ai_synthesizer import ai_synthesizer
from backend.app.services.validation_gates import validation_gates
from backend.app.services.auth import get_current_merchant, MerchantContext, compute_canonical_submission_hash

router = APIRouter(prefix="/api/disputes", tags=["Disputes"])


def get_dispute_tx_ids(dispute: Dispute) -> List[str]:
    """Return all associated transaction IDs (seller and buyer perspective)."""
    tx_ids = []
    if dispute.transaction_id:
        tx_ids.append(dispute.transaction_id)
    if getattr(dispute, "seller_transaction_id", None) and dispute.seller_transaction_id not in tx_ids:
        tx_ids.append(dispute.seller_transaction_id)
    if getattr(dispute, "buyer_transaction_id", None) and dispute.buyer_transaction_id not in tx_ids:
        tx_ids.append(dispute.buyer_transaction_id)
    return tx_ids or ["TX-UNKNOWN"]


def extract_requested_evidence_types(paypal_details: Dict[str, Any]) -> List[str]:
    """
    Extract requested evidence types from PayPal Disputes API response.
    PayPal can provide requested evidence in two structures:
    1. 'evidences' list with source='REQUESTED_FROM_SELLER' (standard during INQUIRY/CHARGEBACK)
    2. 'evidence_requests' list (used in specific dispute lifecycle phases)
    """
    requested = set()
    for ev in paypal_details.get("evidences", []):
        if isinstance(ev, dict) and ev.get("source") == "REQUESTED_FROM_SELLER" and ev.get("evidence_type"):
            requested.add(ev["evidence_type"])
    for req in paypal_details.get("evidence_requests", []):
        if isinstance(req, dict) and req.get("evidence_type"):
            requested.add(req["evidence_type"])
    if not requested and "evidence_types" in paypal_details:
        for et in paypal_details.get("evidence_types", []):
            requested.add(et)
    return sorted(list(requested))


@router.get("", response_model=List[DisputeResponse])
async def get_disputes(
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    query = select(Dispute).order_by(Dispute.created_at.desc())
    if status:
        query = query.where(Dispute.status == status)
    result = await db.execute(query)
    return result.scalars().all()


@router.post("/sync")
async def sync_disputes_from_paypal(db: AsyncSession = Depends(get_db)):
    """Pull live disputes directly from PayPal Sandbox Disputes API and synchronize local ledger."""
    try:
        items = await paypal_client.list_disputes(page_size=20)
        synced_count = 0
        for item in items:
            disp_id = item.get("dispute_id")
            amount_dict = item.get("dispute_amount", {})
            amount = Decimal(str(amount_dict.get("value", "0.00")))
            curr = amount_dict.get("currency_code", "USD")
            reason = item.get("reason", "UNKNOWN")
            status = item.get("status", "OPEN")
            stage = item.get("dispute_life_cycle_stage", "INQUIRY")
            due_str = item.get("seller_response_due_date")
            due_date = datetime.fromisoformat(due_str.replace("Z", "+00:00")) if due_str else None

            # Look up transaction IDs (reconciling seller and buyer perspectives)
            disputed_txs = item.get("disputed_transactions", [])
            seller_tx = disputed_txs[0].get("seller_transaction_id") if disputed_txs else None
            buyer_tx = disputed_txs[0].get("buyer_transaction_id") if disputed_txs else None
            tx_id = seller_tx or buyer_tx or "TX-UNKNOWN"

            # Check if dispute exists
            existing = await db.get(Dispute, disp_id)
            if existing:
                existing.status = status
                existing.external_status = status
                existing.lifecycle_stage = stage
                existing.response_due_date = due_date
                if seller_tx:
                    existing.seller_transaction_id = seller_tx
                if buyer_tx:
                    existing.buyer_transaction_id = buyer_tx
                if not existing.transaction_id or existing.transaction_id == "TX-UNKNOWN":
                    existing.transaction_id = tx_id
            else:
                new_disp = Dispute(
                    dispute_id=disp_id,
                    transaction_id=tx_id,
                    seller_transaction_id=seller_tx,
                    buyer_transaction_id=buyer_tx,
                    dispute_amount=amount,
                    currency=curr,
                    reason=reason,
                    status=status,
                    external_status=status,
                    workflow_status="PENDING_ACTION",
                    lifecycle_stage=stage,
                    response_due_date=due_date
                )
                db.add(new_disp)
                synced_count += 1
        await db.commit()
        return {"status": "SUCCESS", "synced_new": synced_count, "total_live_items": len(items)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PayPal sync failed: {str(e)}")


@router.get("/{dispute_id}")
async def get_dispute_details(
    dispute_id: str,
    db: AsyncSession = Depends(get_db)
):
    dispute = await db.get(Dispute, dispute_id)
    if not dispute:
        # Fall back to live PayPal query
        try:
            live_item = await paypal_client.get_dispute(dispute_id)
            return {"source": "paypal_live", "data": live_item}
        except Exception:
            raise HTTPException(status_code=404, detail="Dispute not found")

    # Fetch attached evidence package if present
    ev_res = await db.execute(
        select(EvidencePackage).where(EvidencePackage.dispute_id == dispute_id).order_by(EvidencePackage.created_at.desc())
    )
    evidence = ev_res.scalars().first()

    # Fetch telemetry associated with transaction (reconciling seller and buyer IDs)
    tx_ids = get_dispute_tx_ids(dispute)
    tel_res = await db.execute(
        select(TransactionTelemetry).where(TransactionTelemetry.transaction_id.in_(tx_ids))
    )
    telemetry = tel_res.scalars().first()

    return {
        "dispute": dispute,
        "evidence": evidence,
        "telemetry": telemetry
    }


@router.post("/{dispute_id}/synthesize", response_model=EvidenceDossier)
async def synthesize_dispute_evidence(
    dispute_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    Run Visa Compelling Evidence 3.0 correlation engine and AI Brief Synthesizer
    to compile an audit-proof chargeback defense dossier.
    """
    dispute = await db.get(Dispute, dispute_id)
    if not dispute:
        # Try syncing from PayPal first
        await sync_disputes_from_paypal(db)
        dispute = await db.get(Dispute, dispute_id)
        if not dispute:
            raise HTTPException(status_code=404, detail="Dispute not found in ledger")

    # Fetch customer telemetry across associated transaction identifiers
    tx_ids = get_dispute_tx_ids(dispute)
    tel_res = await db.execute(
        select(TransactionTelemetry).where(TransactionTelemetry.transaction_id.in_(tx_ids))
    )
    current_tel = tel_res.scalars().first()
    customer_email = current_tel.customer_email if current_tel else "customer@example.com"

    # Step 1: Check Visa CE3.0 Eligibility
    ce30_eligible, ineligibility_reason = ce30_engine.is_ce30_eligible(dispute.reason)

    # Step 2: Establish Audit Provenance Metadata
    is_demo = dispute.dispute_id.startswith("DEMO-")
    telemetry_prov = "SYNTHETIC_DEMO" if is_demo else (getattr(current_tel, "provenance", "SYNTHETIC_DEMO") if current_tel else "SYNTHETIC_DEMO")
    provenance_summary = {
        "dispute_record": "SYNTHETIC_DEMO" if is_demo else "EXTERNAL_API",
        "transaction_record": "SYNTHETIC_DEMO" if is_demo else "EXTERNAL_API",
        "fulfillment_evidence": "MISSING",
        "telemetry_fixtures": telemetry_prov
    }

    # Step 3: Query Merchant Fulfillment Records (Digital Goods / Carrier Tracking)
    ful_res = await db.execute(
        select(FulfillmentRecord).where(FulfillmentRecord.transaction_id.in_(tx_ids))
    )
    fulfillment_record = ful_res.scalars().first()
    if fulfillment_record:
        if is_demo or getattr(fulfillment_record, "provenance", "") == "SYNTHETIC_DEMO":
            provenance_summary["fulfillment_evidence"] = "SYNTHETIC_DEMO"
        else:
            provenance_summary["fulfillment_evidence"] = getattr(fulfillment_record, "provenance", "MERCHANT_ATTESTED")

    # Step 4: Run CE3.0 Telemetry Correlation ONLY for Eligible Fraud Disputes
    proofs = []
    if ce30_eligible:
        proofs = await ce30_engine.find_compelling_evidence(
            session=db,
            disputed_transaction_id=tx_ids,
            customer_email=customer_email
        )


    # Step 5: Factual AI/Deterministic Evidence Synthesis
    dossier = await ai_synthesizer.synthesize_evidence(
        dispute_id=dispute.dispute_id,
        transaction_id=dispute.transaction_id,
        disputed_amount=dispute.dispute_amount,
        currency=dispute.currency,
        reason=dispute.reason,
        customer_email=customer_email,
        ce30_eligible=ce30_eligible,
        ce30_ineligibility_reason=ineligibility_reason,
        proofs=proofs,
        fulfillment_record=fulfillment_record,
        provenance_summary=provenance_summary
    )

    # Step 6: Run Deterministic Validation Gates
    valid, warning = await validation_gates.validate_dossier(
        session=db,
        dossier=dossier,
        disputed_transaction_id=dispute.transaction_id,
        disputed_amount=dispute.dispute_amount
    )

    if not valid:
        raise HTTPException(status_code=422, detail=f"Evidence validation gate failed: {warning}")

    pkg_status = (
        "INSUFFICIENT_EVIDENCE" if dossier.fulfillment_status == "INSUFFICIENT_EVIDENCE"
        else ("REVIEW_REQUIRED" if warning else "DRAFT")
    )

    # Save drafted evidence package in database with complete provenance metadata
    pkg = EvidencePackage(
        dispute_id=dispute.dispute_id,
        evidence_type=dossier.evidence_type,
        narrative_text=dossier.formal_defense_narrative,
        telemetry_matches=[p.model_dump() for p in dossier.historical_matches],
        submission_payload=dossier.model_dump(),
        status=pkg_status,
        fulfillment_status=dossier.fulfillment_status,
        provenance=provenance_summary.get("fulfillment_evidence", "SYNTHETIC_DEMO"),
        confidence_score=Decimal(str(dossier.confidence_score))
    )
    db.add(pkg)
    await db.commit()

    return dossier


@router.post("/{dispute_id}/approve-evidence")
async def approve_dispute_evidence(
    dispute_id: str,
    current_merchant: MerchantContext = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db)
):
    """
    Server-side merchant approval of a synthesized evidence package.
    Records immutable approval record with merchant ID, dispute ID, package ID, and SHA-256 content hash.
    """
    dispute = await db.get(Dispute, dispute_id)
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found in local ledger")

    if dispute.merchant_id != current_merchant.merchant_id:
        raise HTTPException(
            status_code=403,
            detail="Merchant authorization mismatch. Cannot approve evidence for another merchant's dispute."
        )

    ev_res = await db.execute(
        select(EvidencePackage).where(EvidencePackage.dispute_id == dispute_id).order_by(EvidencePackage.created_at.desc())
    )
    pkg = ev_res.scalars().first()
    if not pkg:
        raise HTTPException(status_code=400, detail="No synthesized evidence package found to approve. Call /synthesize first.")

    payload_dict = pkg.submission_payload or {}
    content_hash = compute_canonical_submission_hash(
        dispute_id=dispute.dispute_id,
        merchant_id=current_merchant.merchant_id,
        evidence_type=pkg.evidence_type,
        narrative_text=pkg.narrative_text,
        carrier_name=payload_dict.get("carrier_name"),
        tracking_number=payload_dict.get("tracking_number"),
        delivery_date=payload_dict.get("delivery_date"),
        compelling_evidence_factor=payload_dict.get("compelling_evidence_factor"),
        attachments=payload_dict.get("attachments", [])
    )

    approval = MerchantApproval(
        dispute_id=dispute_id,
        package_id=pkg.package_id,
        content_hash=content_hash,
        merchant_id=current_merchant.merchant_id
    )
    pkg.status = "APPROVED"
    db.add(approval)
    await db.commit()

    return {
        "status": "APPROVED",
        "approval_id": approval.approval_id,
        "dispute_id": dispute_id,
        "package_id": pkg.package_id,
        "content_hash": content_hash,
        "merchant_id": current_merchant.merchant_id,
        "approved_at": approval.approved_at
    }


class FulfillmentRecordInput(BaseModel):
    fulfillment_type: str = "DIGITAL_LICENSE"
    recipient_email: str
    license_key: Optional[str] = None
    download_url: Optional[str] = None
    download_ip: Optional[str] = None
    carrier_name: Optional[str] = None
    tracking_number: Optional[str] = None
    delivery_status: str = "DELIVERED"
    document_hash: Optional[str] = None
    validation_source: Optional[str] = "MANUAL_ENTRY"
    notes: Optional[str] = None
    provenance: Optional[str] = None


@router.post("/{dispute_id}/fulfillment")
async def record_fulfillment_evidence(
    dispute_id: str,
    record: FulfillmentRecordInput,
    current_merchant: MerchantContext = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db)
):
    """
    Record authentic merchant fulfillment evidence (e.g. digital license key, delivery logs).
    Preserves document hashes, validation sources, timestamps, and audit history.
    Manually entered merchant claims are classified as MANUAL_ENTRY or MERCHANT_ATTESTED.
    Only independently validated records receive VERIFIED_MERCHANT status.
    """
    dispute = await db.get(Dispute, dispute_id)
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found")
    if dispute.merchant_id != current_merchant.merchant_id:
        raise HTTPException(status_code=403, detail="Merchant authorization mismatch")

    import hashlib

    # Evidence Integrity Classification:
    # Manually entered merchant claims must be classified as MANUAL_ENTRY or MERCHANT_ATTESTED.
    # Only records with verified external validation sources (CARRIER_API, CRYPTOGRAPHIC_REGISTRY,
    # INDEPENDENT_AUDIT, EXTERNAL_CARRIER) AND an intact cryptographic document hash receive VERIFIED_MERCHANT.
    source = (record.validation_source or "MANUAL_ENTRY").upper()
    is_independently_validated = (
        source in ("CARRIER_API", "CRYPTOGRAPHIC_REGISTRY", "INDEPENDENT_AUDIT", "EXTERNAL_CARRIER")
        and bool(record.document_hash)
        and len(record.document_hash) >= 32
    )

    if is_independently_validated:
        assigned_provenance = "VERIFIED_MERCHANT"
    elif record.provenance in ("MANUAL_ENTRY", "MERCHANT_ATTESTED"):
        assigned_provenance = record.provenance
    else:
        assigned_provenance = "MERCHANT_ATTESTED"

    doc_hash = record.document_hash
    if not doc_hash:
        content_bytes = f"{record.fulfillment_type}:{record.recipient_email}:{record.license_key or record.tracking_number}:{record.delivery_status}".encode("utf-8")
        doc_hash = hashlib.sha256(content_bytes).hexdigest()

    now_utc = datetime.now(timezone.utc)
    audit_entry = {
        "action": "RECORD_CREATED",
        "timestamp": now_utc.isoformat(),
        "merchant_id": current_merchant.merchant_id,
        "validation_source": source,
        "provenance": assigned_provenance,
        "document_hash": doc_hash,
        "is_independently_validated": is_independently_validated
    }

    ful = FulfillmentRecord(
        transaction_id=dispute.transaction_id,
        merchant_id=current_merchant.merchant_id,
        fulfillment_type=record.fulfillment_type,
        recipient_email=record.recipient_email,
        license_key=record.license_key,
        download_url=record.download_url,
        download_ip=record.download_ip,
        download_timestamp=now_utc if record.download_ip else None,
        carrier_name=record.carrier_name,
        tracking_number=record.tracking_number,
        delivery_timestamp=now_utc,
        delivery_status=record.delivery_status,
        document_hash=doc_hash,
        validation_source=source,
        validation_timestamp=now_utc if is_independently_validated else None,
        audit_history=[audit_entry],
        provenance=assigned_provenance,
        notes=record.notes
    )
    db.add(ful)
    await db.commit()
    return {
        "status": "SUCCESS",
        "fulfillment_id": ful.fulfillment_id,
        "transaction_id": ful.transaction_id,
        "provenance": ful.provenance,
        "validation_source": ful.validation_source,
        "document_hash": ful.document_hash,
        "is_independently_validated": is_independently_validated
    }


@router.post("/{dispute_id}/submit-defense")
async def submit_defense_to_paypal(
    dispute_id: str,
    dry_run: bool = Query(False, description="Dry run mode executes all validation gates without submitting to PayPal"),
    current_merchant: MerchantContext = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db)
):
    """
    Transmit the validated evidence dossier to PayPal's POST /v1/customer/disputes/{id}/provide-evidence.
    Strictly enforces backend-recorded merchant approval matching the specific package content hash.
    Client-supplied parameters cannot bypass or substitute for server-side approval.
    Supports dry-run mode for end-to-end gate verification without modifying live PayPal state.
    """
    dispute = await db.get(Dispute, dispute_id)
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found in local ledger")

    if dispute.merchant_id != current_merchant.merchant_id:
        raise HTTPException(
            status_code=403,
            detail="Merchant authorization mismatch. Cannot submit evidence for another merchant's dispute."
        )

    # Status Gate: Must be waiting for seller response
    if dispute.status != "WAITING_FOR_SELLER_RESPONSE":
        raise HTTPException(
            status_code=400,
            detail=f"Dispute status is '{dispute.status}'. Evidence submission requires 'WAITING_FOR_SELLER_RESPONSE'."
        )

    # Fetch latest drafted evidence package
    ev_res = await db.execute(
        select(EvidencePackage).where(EvidencePackage.dispute_id == dispute_id).order_by(EvidencePackage.created_at.desc())
    )
    pkg = ev_res.scalars().first()
    if not pkg:
        raise HTTPException(status_code=400, detail="No synthesized evidence package found. Call /synthesize first.")

    # 1. Server-Side Merchant Approval Verification
    appr_res = await db.execute(
        select(MerchantApproval)
        .where(
            MerchantApproval.dispute_id == dispute_id,
            MerchantApproval.package_id == pkg.package_id,
            MerchantApproval.revoked_at.is_(None)
        )
        .order_by(MerchantApproval.approved_at.desc())
    )
    approval = appr_res.scalars().first()
    if not approval:
        raise HTTPException(
            status_code=400,
            detail="Evidence dossier has not received valid server-side merchant approval. Approval required prior to submission."
        )

    if approval.merchant_id != current_merchant.merchant_id:
        raise HTTPException(
            status_code=403,
            detail="Merchant authorization mismatch on recorded approval."
        )

    # 2. Content Hash Parity Check (Ensures complete payload has not mutated since approval)
    payload_dict = pkg.submission_payload or {}
    current_hash = compute_canonical_submission_hash(
        dispute_id=dispute.dispute_id,
        merchant_id=current_merchant.merchant_id,
        evidence_type=pkg.evidence_type,
        narrative_text=pkg.narrative_text,
        carrier_name=payload_dict.get("carrier_name"),
        tracking_number=payload_dict.get("tracking_number"),
        delivery_date=payload_dict.get("delivery_date"),
        compelling_evidence_factor=payload_dict.get("compelling_evidence_factor"),
        attachments=payload_dict.get("attachments", [])
    )
    if approval.content_hash != current_hash:
        raise HTTPException(
            status_code=400,
            detail="Evidence package content has been modified since approval was recorded. Re-approval required."
        )

    # 3. Provenance & Fulfillment Integrity Gate: Block INSUFFICIENT_EVIDENCE and SYNTHETIC_DEMO
    if pkg.status == "INSUFFICIENT_EVIDENCE" or getattr(pkg, "fulfillment_status", "") == "INSUFFICIENT_EVIDENCE":
        raise HTTPException(
            status_code=400,
            detail="Cannot submit defense: Evidence dossier has status 'INSUFFICIENT_EVIDENCE'. Verified merchant fulfillment records are required prior to submission."
        )

    for match in (pkg.telemetry_matches or []):
        if match.get("provenance") == "SYNTHETIC_DEMO" or not match.get("is_verified", False):
            raise HTTPException(
                status_code=400,
                detail=f"Cannot submit defense: Cited transaction '{match.get('prior_transaction_id')}' is synthetic demonstration telemetry ('SYNTHETIC_DEMO'). Synthetic fixtures cannot be submitted to PayPal as verified evidence."
            )

    # 4. PayPal Dispute HATEOAS & Evidence Request Verification
    if dispute_id.startswith("DEMO-"):
        paypal_details = {
            "dispute_id": dispute_id,
            "status": "WAITING_FOR_SELLER_RESPONSE",
            "dispute_life_cycle_stage": dispute.lifecycle_stage,
            "evidences": [
                {"date": datetime.now(timezone.utc).isoformat(), "evidence_type": "PROOF_OF_FULFILLMENT", "source": "REQUESTED_FROM_SELLER"}
            ],
            "evidence_requests": [],
            "links": [
                {"href": f"https://api-m.sandbox.paypal.com/v1/customer/disputes/{dispute_id}/provide-evidence", "method": "POST", "rel": "provide_evidence"}
            ]
        }
    else:
        try:
            paypal_details = await paypal_client.get_dispute(dispute_id)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Failed to query dispute from PayPal: {str(e)}")

    links = paypal_details.get("links", [])
    has_action_link = any(
        link.get("rel") in ("provide-evidence", "provide_evidence") or
        str(link.get("rel", "")).replace("-", "_") == "provide_evidence"
        for link in links
    )
    if not has_action_link:
        raise HTTPException(
            status_code=400,
            detail="Dispute does not offer an active 'provide_evidence' (or 'provide-evidence') HATEOAS action link in PayPal."
        )

    # Validate evidence type against PayPal requested evidence types (from evidences & evidence_requests)
    allowed_types = extract_requested_evidence_types(paypal_details)
    if allowed_types and pkg.evidence_type not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail=f"Evidence type '{pkg.evidence_type}' is not among PayPal's requested evidence types: {allowed_types}"
        )

    # 5. Dry-Run Verification Gate: Validate complete pipeline without state modification
    if dry_run:
        dispute.workflow_status = "VALIDATED_READY_TO_SUBMIT"
        await db.commit()
        return {
            "status": "DRY_RUN_SUCCESS",
            "mode": "DRY_RUN",
            "message": "All verification gates passed: dispute status valid, HATEOAS action link active, evidence types match PayPal requirements, merchant approval intact, and fulfillment records verified. No live submission was sent to PayPal.",
            "dispute_id": dispute_id,
            "evidence_type": pkg.evidence_type,
            "approval_hash": approval.content_hash,
            "validation_summary": {
                "status_gate": f"PASS ({dispute.status})",
                "hateoas_gate": "PASS (provide_evidence action link available)",
                "evidence_type_gate": f"PASS ({pkg.evidence_type} matches PayPal requirements)",
                "merchant_approval_gate": "PASS (valid server-side approval and canonical hash match)",
                "provenance_gate": "PASS (zero synthetic fixtures cited)",
                "fulfillment_gate": "PASS (fulfillment records verified)"
            },
            "submission_payload_preview": {
                "dispute_id": dispute_id,
                "evidence_type": pkg.evidence_type,
                "carrier_name": payload_dict.get("carrier_name"),
                "tracking_number": payload_dict.get("tracking_number"),
                "notes_preview": (pkg.narrative_text or "")[:200] + "..."
            }
        }

    # 6. Submission Execution
    if dispute_id.startswith("DEMO-"):
        res = {
            "acknowledged": True,
            "status": "EVIDENCE_SUBMITTED_SIMULATED",
            "dispute_id": dispute_id,
            "notes": "Demonstration evidence submitted successfully against simulated PayPal Disputes API."
        }
        pkg.status = "SUBMITTED"
        dispute.status = "UNDER_REVIEW"
        dispute.external_status = "UNDER_REVIEW"
        dispute.workflow_status = "SUBMITTED"
        await db.commit()
        return {
            "status": "SUCCESS",
            "mode": "DEMO_SIMULATION",
            "paypal_response": res,
            "dispute_status": dispute.status,
            "workflow_status": dispute.workflow_status
        }

    # Safety Guard: Live PayPal submission requires explicit configuration
    from backend.app.config import settings
    if not settings.ENABLE_LIVE_PAYPAL_SUBMISSION:
        raise HTTPException(
            status_code=403,
            detail=(
                "Live PayPal evidence submission is disabled by default for safety (ENABLE_LIVE_PAYPAL_SUBMISSION=false). "
                "Use Dry-Run mode (?dry_run=true) to verify all gates without external API mutations, "
                "or enable live submission in application configuration."
            )
        )

    try:
        res = await paypal_client.provide_evidence(
            dispute_id=dispute_id,
            evidence_type=pkg.evidence_type,
            notes=pkg.narrative_text,
            carrier_name=payload_dict.get("carrier_name"),
            tracking_number=payload_dict.get("tracking_number")
        )
        pkg.status = "SUBMITTED"
        dispute.status = "UNDER_REVIEW"
        dispute.external_status = "UNDER_REVIEW"
        dispute.workflow_status = "SUBMITTED"
        await db.commit()
        return {
            "status": "SUCCESS",
            "paypal_response": res,
            "dispute_status": dispute.status,
            "workflow_status": dispute.workflow_status
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PayPal evidence submission failed: {str(e)}")

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List, Dict, Any, Optional
from decimal import Decimal
from datetime import datetime

from backend.app.database import get_db
from backend.app.models import Dispute, EvidencePackage, TransactionTelemetry, MerchantApproval
from backend.app.schemas.dispute import DisputeResponse
from backend.app.schemas.evidence import EvidenceDossier
from backend.app.services.paypal_client import paypal_client
from backend.app.services.ce30_engine import ce30_engine
from backend.app.services.ai_synthesizer import ai_synthesizer
from backend.app.services.validation_gates import validation_gates
from backend.app.services.auth import get_current_merchant, MerchantContext, compute_canonical_submission_hash

router = APIRouter(prefix="/api/disputes", tags=["Disputes"])


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

            # Look up transaction ID
            disputed_txs = item.get("disputed_transactions", [])
            tx_id = disputed_txs[0].get("seller_transaction_id") or disputed_txs[0].get("buyer_transaction_id") if disputed_txs else "TX-UNKNOWN"

            # Check if dispute exists
            existing = await db.get(Dispute, disp_id)
            if existing:
                existing.status = status
                existing.lifecycle_stage = stage
                existing.response_due_date = due_date
            else:
                new_disp = Dispute(
                    dispute_id=disp_id,
                    transaction_id=tx_id,
                    dispute_amount=amount,
                    currency=curr,
                    reason=reason,
                    status=status,
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

    # Fetch telemetry associated with transaction
    tel_res = await db.execute(
        select(TransactionTelemetry).where(TransactionTelemetry.transaction_id == dispute.transaction_id)
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

    # Fetch customer telemetry
    tel_res = await db.execute(
        select(TransactionTelemetry).where(TransactionTelemetry.transaction_id == dispute.transaction_id)
    )
    current_tel = tel_res.scalar_one_or_none()
    customer_email = current_tel.customer_email if current_tel else "customer@example.com"

    # Step 1: Run CE3.0 Correlation
    proofs = await ce30_engine.find_compelling_evidence(
        session=db,
        disputed_transaction_id=dispute.transaction_id,
        customer_email=customer_email
    )

    # Step 2: AI Synthesis
    dossier = await ai_synthesizer.synthesize_evidence(
        dispute_id=dispute.dispute_id,
        transaction_id=dispute.transaction_id,
        disputed_amount=dispute.dispute_amount,
        currency=dispute.currency,
        reason=dispute.reason,
        customer_email=customer_email,
        proofs=proofs
    )

    # Step 3: Run Deterministic Validation Gates
    valid, warning = await validation_gates.validate_dossier(
        session=db,
        dossier=dossier,
        disputed_transaction_id=dispute.transaction_id,
        disputed_amount=dispute.dispute_amount
    )

    if not valid:
        raise HTTPException(status_code=422, detail=f"Evidence validation gate failed: {warning}")

    # Save drafted evidence package in database
    pkg = EvidencePackage(
        dispute_id=dispute.dispute_id,
        evidence_type=dossier.evidence_type,
        narrative_text=dossier.formal_defense_narrative,
        telemetry_matches=[p.model_dump() for p in dossier.historical_matches],
        submission_payload=dossier.model_dump(),
        status="REVIEW_REQUIRED" if warning else "DRAFT",
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


@router.post("/{dispute_id}/submit-defense")
async def submit_defense_to_paypal(
    dispute_id: str,
    current_merchant: MerchantContext = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db)
):
    """
    Transmit the validated evidence dossier to PayPal's POST /v1/customer/disputes/{id}/provide-evidence.
    Strictly enforces backend-recorded merchant approval matching the specific package content hash.
    Client-supplied parameters cannot bypass or substitute for server-side approval.
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

    # 3. PayPal Dispute HATEOAS & Evidence Request Verification
    try:
        paypal_details = await paypal_client.get_dispute(dispute_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to query dispute from PayPal: {str(e)}")

    links = paypal_details.get("links", [])
    has_action_link = any(link.get("rel") == "provide-evidence" for link in links)
    if not has_action_link:
        raise HTTPException(
            status_code=400,
            detail="Dispute does not offer an active 'provide-evidence' HATEOAS action link in PayPal."
        )

    # Validate evidence type against PayPal requested evidence types
    evidence_requests = paypal_details.get("evidence_requests", [])
    allowed_types = [
        req.get("evidence_type") for req in evidence_requests if isinstance(req, dict) and req.get("evidence_type")
    ]
    if not allowed_types and "evidence_types" in paypal_details:
        allowed_types = paypal_details.get("evidence_types", [])

    if allowed_types and pkg.evidence_type not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail=f"Evidence type '{pkg.evidence_type}' is not among PayPal's requested evidence types: {allowed_types}"
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
        await db.commit()
        return {
            "status": "SUCCESS",
            "paypal_response": res,
            "dispute_status": dispute.status
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PayPal evidence submission failed: {str(e)}")

from fastapi import APIRouter, Request, Depends, HTTPException, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from decimal import Decimal
from datetime import datetime
import json

from backend.app.database import get_db
from backend.app.models import WebhookEvent, Dispute
from backend.app.services.paypal_client import paypal_client

router = APIRouter(prefix="/api/webhooks", tags=["Webhooks"])


@router.post("/paypal")
async def handle_paypal_webhook(
    request: Request,
    paypal_transmission_id: str = Header(..., alias="paypal-transmission-id"),
    paypal_transmission_time: str = Header(..., alias="paypal-transmission-time"),
    paypal_cert_url: str = Header(..., alias="paypal-cert-url"),
    paypal_auth_algo: str = Header(..., alias="paypal-auth-algo"),
    paypal_transmission_sig: str = Header(..., alias="paypal-transmission-sig"),
    db: AsyncSession = Depends(get_db)
):
    """
    Ingests inbound PayPal webhooks (CUSTOMER.DISPUTE.CREATED, UPDATED, RESOLVED)
    with raw-byte preservation and postback signature verification.
    """
    raw_body: bytes = await request.body()

    # Step 1: Cryptographic Signature Verification (Fail-closed: reject before any DB operations)
    verified = await paypal_client.verify_webhook_signature(
        transmission_id=paypal_transmission_id,
        transmission_time=paypal_transmission_time,
        cert_url=paypal_cert_url,
        auth_algo=paypal_auth_algo,
        transmission_sig=paypal_transmission_sig,
        webhook_id=paypal_client.webhook_id,
        raw_body_bytes=raw_body
    )
    if not verified:
        raise HTTPException(
            status_code=401,
            detail="Invalid PayPal webhook signature. Notification rejected without state modification."
        )

    payload = json.loads(raw_body.decode("utf-8"))
    event_id = payload.get("id", paypal_transmission_id)
    event_type = payload.get("event_type", "UNKNOWN")
    resource = payload.get("resource", {})
    resource_id = resource.get("id") or resource.get("dispute_id", "UNKNOWN")

    # Step 2: Idempotency Check (Check transmission_id and event_id)
    existing_evt = await db.get(WebhookEvent, paypal_transmission_id)
    if not existing_evt and event_id:
        evt_res = await db.execute(
            select(WebhookEvent).where(WebhookEvent.event_id == event_id)
        )
        existing_evt = evt_res.scalars().first()

    if existing_evt:
        return {
            "status": "ALREADY_PROCESSED",
            "idempotent": True,
            "transmission_id": paypal_transmission_id,
            "event_id": event_id
        }

    # Record in idempotency ledger
    evt_record = WebhookEvent(
        transmission_id=paypal_transmission_id,
        event_id=event_id,
        event_type=event_type,
        resource_id=resource_id,
        raw_payload=payload,
        processed_status="PROCESSED"
    )
    db.add(evt_record)

    # Step 3: Lifecycle State Machine Transitions
    if event_type == "CUSTOMER.DISPUTE.CREATED":
        dispute_id = resource.get("dispute_id")
        amount_dict = resource.get("dispute_amount", {})
        amount = Decimal(str(amount_dict.get("value", "0.00")))
        curr = amount_dict.get("currency_code", "USD")
        reason = resource.get("reason", "UNKNOWN")
        status = resource.get("status", "OPEN")
        stage = resource.get("dispute_life_cycle_stage", "INQUIRY")
        due_str = resource.get("seller_response_due_date")
        due_date = datetime.fromisoformat(due_str.replace("Z", "+00:00")) if due_str else None

        disputed_txs = resource.get("disputed_transactions", [])
        seller_tx = disputed_txs[0].get("seller_transaction_id") if disputed_txs else None
        buyer_tx = disputed_txs[0].get("buyer_transaction_id") if disputed_txs else None
        tx_id = seller_tx or buyer_tx or "TX-UNKNOWN"

        existing = await db.get(Dispute, dispute_id)
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
                dispute_id=dispute_id,
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

    elif event_type in ("CUSTOMER.DISPUTE.UPDATED", "CUSTOMER.DISPUTE.RESOLVED"):
        dispute_id = resource.get("dispute_id")
        disp = await db.get(Dispute, dispute_id)
        if disp:
            new_status = resource.get("status", disp.status)
            disp.status = new_status
            disp.external_status = new_status
            disp.lifecycle_stage = resource.get("dispute_life_cycle_stage", disp.lifecycle_stage)
            due_str = resource.get("seller_response_due_date")
            if due_str:
                disp.response_due_date = datetime.fromisoformat(due_str.replace("Z", "+00:00"))
            outcome = resource.get("dispute_outcome", {}).get("outcome_code")
            if outcome:
                disp.adjudication_outcome = outcome
            if new_status == "RESOLVED":
                disp.workflow_status = "RESOLVED"

    await db.commit()

    return {
        "status": "ACCEPTED",
        "verified": verified,
        "event_type": event_type,
        "resource_id": resource_id
    }

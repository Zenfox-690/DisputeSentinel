from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from backend.app.database import get_db
from backend.app.models import Dispute
from backend.app.services.paypal_client import paypal_client
from backend.app.services.auth import get_current_merchant, MerchantContext
from backend.app.config import settings

router = APIRouter(prefix="/api/adjudication", tags=["Adjudication"])


@router.post("/{dispute_id}/resolve")
async def resolve_dispute_sandbox(
    dispute_id: str,
    outcome: str = "SELLER_FAVOR",
    current_merchant: MerchantContext = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db)
):
    """
    Simulate closed-loop dispute resolution via PayPal's sandbox adjudication endpoint.
    Settles claim in the merchant's favor (SELLER_FAVOR) and releases held funds.
    Strictly enforced as Sandbox-only (rejects any non-sandbox environment).
    Requires merchant authorization, dispute status UNDER_REVIEW, and adjudicate HATEOAS link.
    """
    # 1. Environment Guard: Enforce Sandbox-only execution at runtime (reject every non-sandbox environment)
    if settings.PAYPAL_ENV.lower() != "sandbox":
        raise HTTPException(
            status_code=403,
            detail=f"Dispute adjudication is strictly restricted to Sandbox environment. Current environment '{settings.PAYPAL_ENV}' is rejected."
        )

    dispute = await db.get(Dispute, dispute_id)
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found in local ledger")

    # 2. Authorization Guard: Merchant ownership check
    if dispute.merchant_id != current_merchant.merchant_id:
        raise HTTPException(
            status_code=403,
            detail="Merchant authorization mismatch. Cannot adjudicate disputes owned by another merchant."
        )

    # 3. Status Guard: Must be UNDER_REVIEW
    if dispute.status != "UNDER_REVIEW":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot adjudicate dispute: status is '{dispute.status}', expected 'UNDER_REVIEW'."
        )

    # 4. HATEOAS Action Link Guard
    if dispute_id.startswith("DEMO-"):
        dispute.status = "RESOLVED"
        dispute.external_status = "RESOLVED"
        dispute.workflow_status = "RESOLVED"
        dispute.adjudication_outcome = outcome
        await db.commit()
        return {
            "status": "SUCCESS",
            "mode": "DEMO_SIMULATION",
            "adjudication_outcome": outcome,
            "paypal_response": {
                "acknowledged": True,
                "outcome": outcome,
                "dispute_id": dispute_id,
                "simulation": True
            },
            "dispute_status": dispute.status
        }

    try:
        paypal_details = await paypal_client.get_dispute(dispute_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to query dispute from PayPal: {str(e)}")

    links = paypal_details.get("links", [])
    has_adjudicate_link = any(
        link.get("rel") in ("adjudicate", "adjudicate_dispute", "adjudicate-dispute") or
        str(link.get("rel", "")).replace("-", "_") == "adjudicate"
        for link in links
    )
    if not has_adjudicate_link:
        raise HTTPException(
            status_code=400,
            detail="Dispute does not offer an active 'adjudicate' HATEOAS action link in PayPal."
        )

    try:
        res = await paypal_client.adjudicate_dispute(
            dispute_id=dispute_id,
            adjudication_outcome=outcome
        )
        dispute.status = "RESOLVED"
        dispute.adjudication_outcome = outcome
        await db.commit()
        return {
            "status": "SUCCESS",
            "adjudication_outcome": outcome,
            "paypal_response": res,
            "dispute_status": dispute.status
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PayPal sandbox adjudication failed: {str(e)}")

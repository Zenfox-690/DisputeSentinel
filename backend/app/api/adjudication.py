from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from backend.app.database import get_db
from backend.app.models import Dispute
from backend.app.services.paypal_client import paypal_client

router = APIRouter(prefix="/api/adjudication", tags=["Adjudication"])


@router.post("/{dispute_id}/resolve")
async def resolve_dispute_sandbox(
    dispute_id: str,
    outcome: str = "SELLER_FAVOR",
    db: AsyncSession = Depends(get_db)
):
    """
    Simulate closed-loop dispute resolution via PayPal's sandbox adjudication endpoint.
    Settles claim in the merchant's favor (SELLER_FAVOR) and releases held funds.
    """
    dispute = await db.get(Dispute, dispute_id)
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found in local ledger")

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

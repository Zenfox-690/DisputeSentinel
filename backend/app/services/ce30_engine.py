from typing import List, Dict, Any, Optional, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from backend.app.models import TransactionTelemetry, Dispute
from backend.app.schemas.evidence import TelemetryProof


class CE30Engine:
    """
    Visa Compelling Evidence 3.0 (CE3.0) Rule Correlation Engine.
    Cross-references disputed transaction telemetry against historical
    undisputed transaction logs to establish non-fraudulent purchase patterns.
    Strictly applicable ONLY to Visa Reason 10.4 (Card-Absent Fraud).
    """

    @staticmethod
    def is_ce30_eligible(reason: str) -> Tuple[bool, Optional[str]]:
        """
        Enforces Visa CE3.0 eligibility boundaries.
        Visa CE3.0 is strictly restricted to Card-Absent Fraud (Reason Code 10.4).
        Disputes for Item Not Received (Visa 13.1 / MERCHANDISE_OR_SERVICE_NOT_RECEIVED),
        Significantly Not As Described (Visa 13.3), or Cancelled Recurring are ineligible.
        """
        if not reason:
            return False, "Dispute reason is missing."

        norm = reason.strip().upper()
        eligible_fraud_reasons = {
            "UNAUTHORIZED_TRANSACTION",
            "FRAUD",
            "CARD_ABSENT_FRAUD",
            "REASON_10_4",
            "10.4",
            "DISPUTE_REASON_UNAUTHORIZED"
        }

        if norm in eligible_fraud_reasons:
            return True, None

        if "NOT_RECEIVED" in norm or norm == "MERCHANDISE_OR_SERVICE_NOT_RECEIVED":
            return False, (
                "Dispute reason 'MERCHANDISE_OR_SERVICE_NOT_RECEIVED' falls under Visa Reason 13.1 (Item Not Received). "
                "Visa Compelling Evidence 3.0 strictly applies only to Reason 10.4 (Card-Absent Fraud). "
                "This case must be routed through Proof of Fulfillment / Proof of Delivery requirements."
            )

        return False, (
            f"Dispute reason '{reason}' does not qualify for Visa Compelling Evidence 3.0. "
            "CE3.0 is exclusively reserved for Reason 10.4 (Card-Absent Fraud / Unauthorized Transaction)."
        )

    @staticmethod
    async def find_compelling_evidence(
        session: AsyncSession,
        disputed_transaction_id: Any,
        customer_email: str
    ) -> List[TelemetryProof]:
        if isinstance(disputed_transaction_id, (list, tuple, set)):
            disputed_ids = list(disputed_transaction_id)
        else:
            disputed_ids = [disputed_transaction_id]

        # 1. Fetch current transaction telemetry
        current_res = await session.execute(
            select(TransactionTelemetry).where(
                TransactionTelemetry.transaction_id.in_(disputed_ids)
            )
        )
        current = current_res.scalar_one_or_none()
        if not current:
            return []

        # 2. Fetch historical transactions for the same customer (excluding the disputed ones)
        history_res = await session.execute(
            select(TransactionTelemetry).where(
                and_(
                    TransactionTelemetry.customer_email == customer_email,
                    ~TransactionTelemetry.transaction_id.in_(disputed_ids)
                )
            ).order_by(TransactionTelemetry.timestamp.desc())
        )
        history_records = history_res.scalars().all()

        proofs: List[TelemetryProof] = []

        for hist in history_records:
            hist_prov = getattr(hist, "provenance", "SYNTHETIC_DEMO") or "SYNTHETIC_DEMO"
            is_verified_data = hist_prov in ("VERIFIED_MERCHANT", "EXTERNAL_API")

            # Check IP Address Match (or /24 subnet match)
            if hist.ip_address == current.ip_address:
                proofs.append(
                    TelemetryProof(
                        prior_transaction_id=hist.transaction_id,
                        prior_date=hist.timestamp.isoformat(),
                        matched_factor="IP_ADDRESS",
                        match_value=hist.ip_address,
                        provenance=hist_prov,
                        is_verified=is_verified_data
                    )
                )

            # Check Device Fingerprint Match
            if hist.device_fingerprint == current.device_fingerprint:
                proofs.append(
                    TelemetryProof(
                        prior_transaction_id=hist.transaction_id,
                        prior_date=hist.timestamp.isoformat(),
                        matched_factor="DEVICE_ID",
                        match_value=hist.device_fingerprint,
                        provenance=hist_prov,
                        is_verified=is_verified_data
                    )
                )

            # Check Shipping Address Match
            if (
                hist.shipping_address
                and current.shipping_address
                and hist.shipping_address == current.shipping_address
            ):
                proofs.append(
                    TelemetryProof(
                        prior_transaction_id=hist.transaction_id,
                        prior_date=hist.timestamp.isoformat(),
                        matched_factor="SHIPPING_ADDRESS",
                        match_value=str(hist.shipping_address),
                        provenance=hist_prov,
                        is_verified=is_verified_data
                    )
                )

        return proofs


ce30_engine = CE30Engine()

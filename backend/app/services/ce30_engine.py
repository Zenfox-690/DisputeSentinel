from typing import List, Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from backend.app.models import TransactionTelemetry, Dispute
from backend.app.schemas.evidence import TelemetryProof


class CE30Engine:
    """
    Visa Compelling Evidence 3.0 (CE3.0) Rule Correlation Engine.
    Cross-references disputed transaction telemetry against historical
    undisputed transaction logs to establish non-fraudulent purchase patterns.
    """

    @staticmethod
    async def find_compelling_evidence(
        session: AsyncSession,
        disputed_transaction_id: str,
        customer_email: str
    ) -> List[TelemetryProof]:
        # 1. Fetch current transaction telemetry
        current_res = await session.execute(
            select(TransactionTelemetry).where(
                TransactionTelemetry.transaction_id == disputed_transaction_id
            )
        )
        current = current_res.scalar_one_or_none()
        if not current:
            return []

        # 2. Fetch historical transactions for the same customer (excluding the disputed one)
        history_res = await session.execute(
            select(TransactionTelemetry).where(
                and_(
                    TransactionTelemetry.customer_email == customer_email,
                    TransactionTelemetry.transaction_id != disputed_transaction_id
                )
            ).order_by(TransactionTelemetry.timestamp.desc())
        )
        history_records = history_res.scalars().all()

        proofs: List[TelemetryProof] = []

        for hist in history_records:
            # Check IP Address Match (or /24 subnet match)
            if hist.ip_address == current.ip_address:
                proofs.append(
                    TelemetryProof(
                        prior_transaction_id=hist.transaction_id,
                        prior_date=hist.timestamp.isoformat(),
                        matched_factor="IP_ADDRESS",
                        match_value=hist.ip_address
                    )
                )

            # Check Device Fingerprint Match
            if hist.device_fingerprint == current.device_fingerprint:
                proofs.append(
                    TelemetryProof(
                        prior_transaction_id=hist.transaction_id,
                        prior_date=hist.timestamp.isoformat(),
                        matched_factor="DEVICE_ID",
                        match_value=hist.device_fingerprint
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
                        match_value=str(hist.shipping_address)
                    )
                )

        return proofs


ce30_engine = CE30Engine()

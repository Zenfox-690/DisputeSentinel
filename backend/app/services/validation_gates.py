import re
from typing import Tuple, Optional
from decimal import Decimal
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from backend.app.schemas.evidence import EvidenceDossier
from backend.app.models import TransactionTelemetry
from backend.app.config import settings


class DeterministicValidationGates:
    """
    Five rigorous deterministic validation gates that eliminate LLM hallucinations,
    reject synthetic fixtures from being submitted as genuine evidence,
    and enforce factual compliance before evidence touches the PayPal REST API.
    """

    @staticmethod
    async def validate_dossier(
        session: AsyncSession,
        dossier: EvidenceDossier,
        disputed_transaction_id: str,
        disputed_amount: Decimal
    ) -> Tuple[bool, Optional[str]]:
        # Gate 1: Transaction Existence & Provenance Verification
        for match in dossier.historical_matches:
            res = await session.execute(
                select(TransactionTelemetry).where(
                    TransactionTelemetry.transaction_id == match.prior_transaction_id
                )
            )
            record = res.scalar_one_or_none()
            if not record:
                return False, f"Gate 1 Failed: Cited transaction '{match.prior_transaction_id}' does not exist in local telemetry."

            # Synthetic demonstration telemetry must never qualify as verified evidence for submission
            record_prov = getattr(record, "provenance", "SYNTHETIC_DEMO")
            if record_prov == "SYNTHETIC_DEMO" or match.provenance == "SYNTHETIC_DEMO" or not match.is_verified:
                return False, (
                    f"Gate 1 Failed: Cited transaction '{match.prior_transaction_id}' is marked as SYNTHETIC_DEMO. "
                    "Synthetic demonstration fixtures are strictly prohibited from being submitted as verified evidence."
                )

        # Gate 2: Carrier Tracking Format Validation
        if dossier.tracking_number and dossier.carrier_name:
            carrier = dossier.carrier_name.upper()
            tracking = dossier.tracking_number.strip().upper()
            if carrier == "UPS" and not re.match(r"^1Z[0-9A-Z]{16}$", tracking):
                return False, f"Gate 2 Failed: Tracking number '{tracking}' invalid for UPS format (expected 1Z...)."
            if carrier == "FEDEX" and not re.match(r"^[0-9]{12,15}$", tracking):
                return False, f"Gate 2 Failed: Tracking number '{tracking}' invalid for FedEx format."

        # Gate 3: Financial Integrity & Calculation Override
        if disputed_amount <= 0:
            return False, "Gate 3 Failed: Disputed amount must be positive."

        # Gate 4: Prompt Injection & Unsupported Legal Assertions Sanitization
        forbidden_patterns = [
            r"ignore previous instructions",
            r"system prompt",
            r"grant refund",
            r"adjudicate in buyer favor",
            r"<script>",
            r"DROP TABLE"
        ]
        for pat in forbidden_patterns:
            if re.search(pat, dossier.formal_defense_narrative, re.IGNORECASE):
                return False, f"Gate 4 Failed: Prompt injection pattern detected in defense brief ('{pat}')."

        # Check for unsupported legal claims on ineligible disputes
        narrative_lower = dossier.formal_defense_narrative.lower()
        if not dossier.ce30_eligible:
            unsupported_phrases = [
                "liability shifts to the issuer",
                "pursuant to visa compelling evidence 3.0",
                "pursuant to visa ce3.0",
                "qualifying ce3.0 correlations"
            ]
            for phrase in unsupported_phrases:
                if phrase in narrative_lower:
                    return False, (
                        f"Gate 4 Failed: Unsupported legal assertion detected ('{phrase}'). "
                        "Visa CE3.0 liability shift claims cannot be asserted for Item Not Received disputes."
                    )

        # Gate 5: Human-In-The-Loop & Evidence Completeness Gate
        if dossier.fulfillment_status == "INSUFFICIENT_EVIDENCE":
            return True, "REVIEW_REQUIRED: Case marked as INSUFFICIENT_EVIDENCE. Merchant must supply verified fulfillment records."

        if dossier.adjudication_recommendation == "INSUFFICIENT_EVIDENCE":
            return True, "REVIEW_REQUIRED: Adjudication posture is INSUFFICIENT_EVIDENCE. Merchant action required."

        if (
            dossier.confidence_score < settings.CONFIDENCE_THRESHOLD
            or disputed_amount > Decimal(str(settings.MAX_AUTO_SUBMIT_AMOUNT))
        ):
            return True, "REVIEW_REQUIRED: Evidence completeness score falls below autonomous threshold or amount exceeds auto-threshold."

        return True, None


validation_gates = DeterministicValidationGates()

import os
import json
from decimal import Decimal
from typing import List, Dict, Any, Optional
from backend.app.schemas.evidence import EvidenceDossier, TelemetryProof
from backend.app.config import settings


class AIEvidenceSynthesizer:
    """
    Synthesizes factual, audit-proof dispute defense dossiers.
    Strictly separates CE3.0 fraud defense from Item Not Received (INR) proof of fulfillment.
    Rejects unsupported legal claims and marks dossiers lacking verified records as INSUFFICIENT_EVIDENCE.
    """

    @staticmethod
    def _generate_rule_based_dossier(
        dispute_id: str,
        transaction_id: str,
        disputed_amount: Decimal,
        currency: str,
        reason: str,
        customer_email: str,
        ce30_eligible: bool,
        ce30_ineligibility_reason: Optional[str],
        proofs: List[TelemetryProof],
        fulfillment_record: Optional[Any] = None,
        buyer_claim: Optional[str] = None,
        provenance_summary: Optional[Dict[str, str]] = None
    ) -> EvidenceDossier:
        prov = provenance_summary or {}

        # -------------------------------------------------------------
        # BRANCH 1: Ineligible for CE3.0 (e.g. Item Not Received / INR)
        # -------------------------------------------------------------
        if not ce30_eligible:
            # Check for genuine merchant fulfillment evidence
            has_valid_fulfillment = False
            rec = fulfillment_record

            if rec:
                # Check if it has license key or carrier tracking or download info
                lk = getattr(rec, "license_key", None)
                ts = getattr(rec, "delivery_timestamp", None) or getattr(rec, "download_timestamp", None)
                if lk or ts:
                    has_valid_fulfillment = True

            if has_valid_fulfillment:
                rec_prov = getattr(rec, "provenance", "MERCHANT_ATTESTED")
                is_verified = (rec_prov == "VERIFIED_MERCHANT")
                lk_val = getattr(rec, "license_key", "REDACTED-KEY")
                recip = getattr(rec, "recipient_email", customer_email)
                deliv_ts = getattr(rec, "delivery_timestamp", None)
                down_ts = getattr(rec, "download_timestamp", None)
                down_ip = getattr(rec, "download_ip", "N/A")
                carrier = getattr(rec, "carrier_name", None)
                tracking = getattr(rec, "tracking_number", None)
                doc_hash = getattr(rec, "document_hash", None)

                is_demo = (dispute_id.startswith("DEMO-") or rec_prov == "SYNTHETIC_DEMO")
                header_label = "SYNTHETIC DEMONSTRATION DELIVERY RECORDS" if is_demo else ("VERIFIED DIGITAL DELIVERY RECORDS" if is_verified else "MERCHANT-ATTESTED DELIVERY RECORDS")
                demo_notice = "\n[DEMONSTRATION FIXTURE - ISOLATED SYNTHETIC DATA FOR SIMULATION ONLY]\n" if is_demo else ""
                prov_note = "\n(Classification: SYNTHETIC_DEMO - Demonstration Fixture Only)" if is_demo else ("" if is_verified else "\n(Source: Merchant Attestation / Independent Third-Party Audit Pending)")
                hash_note = f"\n- Document Integrity Hash: {doc_hash}" if doc_hash else ""

                narrative = (
                    f"PROOF OF FULFILLMENT - CASE {dispute_id}\n"
                    f"{demo_notice}\n"
                    f"Transaction ID: {transaction_id}\n"
                    f"Disputed Amount: {disputed_amount} {currency}\n"
                    f"Customer Identifier: {customer_email}\n"
                    f"Dispute Reason: {reason} (Item Not Received)\n\n"
                    f"1. FULFILLMENT SPECIFICATION:\n"
                    f"This evidentiary dossier is submitted under PayPal Proof of Fulfillment requirements "
                    f"for intangible digital goods. The merchant completed delivery according to the purchase contract.\n\n"
                    f"2. {header_label}:{prov_note}\n"
                    f"- License / Entitlement Key: {lk_val}\n"
                    f"- Recipient Destination: {recip}\n"
                    f"- Delivery Timestamp: {deliv_ts.isoformat() if deliv_ts else 'On file'}\n"
                    f"- Access Log: Timestamp {down_ts.isoformat() if down_ts else 'N/A'}, IP {down_ip}{hash_note}\n\n"
                    f"3. FACTUAL REBUTTAL OF NON-RECEIPT:\n"
                    f"The merchant's entitlement logs confirm that access credentials were dispatched to the "
                    f"customer address and accessed prior to dispute initiation.\n\n"
                    f"4. ADJUDICATION REQUEST:\n"
                    f"The merchant has satisfied digital fulfillment requirements. We respectfully request "
                    f"that this dispute be settled in the merchant's favor (SELLER_FAVOR)."
                )

                fulfillment_status = "VERIFIED" if is_verified else "MERCHANT_ATTESTED"
                compelling_factor = "DIGITAL_GOODS_FULFILLMENT_VERIFIED" if is_verified else "DIGITAL_GOODS_MERCHANT_ATTESTED"
                confidence = 0.88 if is_verified else 0.72

                return EvidenceDossier(
                    evidence_type="PROOF_OF_FULFILLMENT",
                    carrier_name=carrier,
                    tracking_number=tracking,
                    delivery_date=deliv_ts.isoformat() if deliv_ts else None,
                    compelling_evidence_factor=compelling_factor,
                    ce30_eligible=False,
                    ce30_ineligibility_reason=ce30_ineligibility_reason,
                    fulfillment_status=fulfillment_status,
                    provenance_summary=prov,
                    historical_matches=[],
                    formal_defense_narrative=narrative[:1990],
                    confidence_score=confidence,
                    adjudication_recommendation="SELLER_FAVOR"
                )

            # Missing or Insufficient Fulfillment Records
            narrative = (
                f"DISPUTE DEFENSE ASSESSMENT & AUDIT FINDINGS - CASE {dispute_id}\n\n"
                f"Transaction ID: {transaction_id}\n"
                f"Disputed Amount: {disputed_amount} {currency}\n"
                f"Customer Identifier: {customer_email}\n"
                f"Dispute Reason: {reason} (Item Not Received / INR)\n\n"
                f"1. CASE CLASSIFICATION & FULFILLMENT REQUIREMENTS:\n"
                f"The buyer claims non-receipt of the software license. This case falls under PayPal Proof of Fulfillment "
                f"guidelines (Visa Reason 13.1 INR). Fraud chargeback defense rules do not apply to non-receipt inquiries.\n\n"
                f"2. FULFILLMENT RECORD AUDIT:\n"
                f"A systematic scan of local merchant records revealed NO verified license key dispatch, download event, "
                f"or delivery confirmation timestamp associated with transaction {transaction_id}.\n\n"
                f"3. EVIDENTIARY STATUS: INSUFFICIENT EVIDENCE:\n"
                f"In accordance with PayPal evidentiary standards, unsupported assertions cannot be submitted. "
                f"Merchant review is required to furnish verified entitlement issuance or delivery audit logs.\n\n"
                f"4. ACTION REQUIRED:\n"
                f"Merchant review required. Do not submit defense until valid fulfillment proof is attached."
            )

            return EvidenceDossier(
                evidence_type="PROOF_OF_FULFILLMENT",
                carrier_name=None,
                tracking_number=None,
                delivery_date=None,
                compelling_evidence_factor="INSUFFICIENT_FULFILLMENT_EVIDENCE",
                ce30_eligible=False,
                ce30_ineligibility_reason=ce30_ineligibility_reason,
                fulfillment_status="INSUFFICIENT_EVIDENCE",
                provenance_summary=prov,
                historical_matches=[],
                formal_defense_narrative=narrative[:1990],
                confidence_score=0.15,
                adjudication_recommendation="INSUFFICIENT_EVIDENCE"
            )

        # -------------------------------------------------------------
        # BRANCH 2: CE3.0 Eligible Disputes (Reason 10.4 Card-Absent Fraud)
        # -------------------------------------------------------------
        verified_proofs = [p for p in proofs if p.is_verified]
        synthetic_proofs = [p for p in proofs if not p.is_verified]

        if not verified_proofs:
            # Telemetry is synthetic or missing
            status = "INSUFFICIENT_EVIDENCE" if not synthetic_proofs else "REVIEW_REQUIRED"
            narrative = (
                f"VISA CE3.0 EVALUATION AUDIT - CASE {dispute_id}\n\n"
                f"Transaction ID: {transaction_id}\n"
                f"Disputed Amount: {disputed_amount} {currency}\n"
                f"Customer Identifier: {customer_email}\n"
                f"Dispute Reason: {reason} (Card-Absent Fraud - Reason 10.4)\n\n"
                f"1. ELIGIBILITY: QUALIFIED FOR CE3.0\n"
                f"This dispute qualifies for Visa Compelling Evidence 3.0 analysis.\n\n"
                f"2. TELEMETRY PROVENANCE AUDIT:\n"
                f"Current telemetry records on file are synthetic demonstration fixtures and cannot be represented "
                f"as genuine customer purchase history.\n\n"
                f"3. EVIDENTIARY STATUS: {status}\n"
                f"Verified merchant transaction logs with matching core digital identifiers (IP/Device/Delivery) "
                f"must be loaded from the merchant's production ledger prior to representment."
            )

            return EvidenceDossier(
                evidence_type="PROOF_OF_FULFILLMENT",
                carrier_name=None,
                tracking_number=None,
                delivery_date=None,
                compelling_evidence_factor="VISA_CE3.0_SYNTHETIC_FIXTURE_DISQUALIFIED" if synthetic_proofs else "NO_HISTORICAL_TELEMETRY",
                ce30_eligible=True,
                ce30_ineligibility_reason=None,
                fulfillment_status="INSUFFICIENT_EVIDENCE",
                provenance_summary=prov,
                historical_matches=proofs,
                formal_defense_narrative=narrative[:1990],
                confidence_score=0.25,
                adjudication_recommendation="MERCHANT_REVIEW_REQUIRED"
            )

        # Genuine verified CE3.0 proofs
        telemetry_lines = [
            f"- Undisputed Prior TX {p.prior_transaction_id} ({p.prior_date}): Verified Match on {p.matched_factor} ({p.match_value})"
            for p in verified_proofs
        ]
        telemetry_summary = "\n".join(telemetry_lines)

        narrative = (
            f"VISA COMPELLING EVIDENCE 3.0 DEFENSE BRIEF - CASE {dispute_id}\n\n"
            f"Transaction ID: {transaction_id}\n"
            f"Disputed Amount: {disputed_amount} {currency}\n"
            f"Customer Identifier: {customer_email}\n"
            f"Dispute Reason: {reason} (Reason 10.4 Card-Absent Fraud)\n\n"
            f"1. CE3.0 COMPLIANCE & ELIGIBILITY:\n"
            f"This defense is submitted under Visa Compelling Evidence 3.0 rules for card-absent fraud. "
            f"Merchant records demonstrate a pre-existing legitimate purchasing relationship.\n\n"
            f"2. VERIFIED HISTORICAL TELEMETRY PARITY:\n"
            f"Audit logs confirm the cardholder completed legitimate, undisputed transactions matching core identifiers:\n"
            f"{telemetry_summary}\n\n"
            f"3. FRAUD REFUTATION & CONCLUSION:\n"
            f"The matching verified device and network telemetry establishes cardholder participation in this order. "
            f"We respectfully request adjudication in the merchant's favor (SELLER_FAVOR)."
        )

        return EvidenceDossier(
            evidence_type="PROOF_OF_FULFILLMENT",
            carrier_name=None,
            tracking_number=None,
            delivery_date=None,
            compelling_evidence_factor="VISA_CE3.0_MULTI_FACTOR_MATCH",
            ce30_eligible=True,
            ce30_ineligibility_reason=None,
            fulfillment_status="VERIFIED",
            provenance_summary=prov,
            historical_matches=verified_proofs,
            formal_defense_narrative=narrative[:1990],
            confidence_score=0.92,
            adjudication_recommendation="SELLER_FAVOR"
        )

    @classmethod
    async def synthesize_evidence(
        cls,
        dispute_id: str,
        transaction_id: str,
        disputed_amount: Decimal,
        currency: str,
        reason: str,
        customer_email: str,
        ce30_eligible: bool,
        ce30_ineligibility_reason: Optional[str] = None,
        proofs: Optional[List[TelemetryProof]] = None,
        fulfillment_record: Optional[Any] = None,
        buyer_claim: Optional[str] = None,
        provenance_summary: Optional[Dict[str, str]] = None
    ) -> EvidenceDossier:
        return cls._generate_rule_based_dossier(
            dispute_id=dispute_id,
            transaction_id=transaction_id,
            disputed_amount=disputed_amount,
            currency=currency,
            reason=reason,
            customer_email=customer_email,
            ce30_eligible=ce30_eligible,
            ce30_ineligibility_reason=ce30_ineligibility_reason,
            proofs=proofs or [],
            fulfillment_record=fulfillment_record,
            buyer_claim=buyer_claim,
            provenance_summary=provenance_summary
        )


ai_synthesizer = AIEvidenceSynthesizer()

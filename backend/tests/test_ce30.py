import pytest
from datetime import datetime, timezone, timedelta
from backend.app.models import TransactionTelemetry
from backend.app.services.ce30_engine import ce30_engine


def test_ce30_eligibility_rejection_for_item_not_received():
    # Item Not Received (Visa 13.1) must be strictly rejected from CE3.0
    eligible, reason = ce30_engine.is_ce30_eligible("MERCHANDISE_OR_SERVICE_NOT_RECEIVED")
    assert eligible is False
    assert "Visa Reason 13.1 (Item Not Received)" in reason
    assert "Reason 10.4 (Card-Absent Fraud)" in reason


def test_ce30_eligibility_rejection_for_other_non_fraud_reasons():
    for r in ["NOT_AS_DESCRIBED", "CANCELLED_RECURRING", "INCORRECT_AMOUNT", "OTHER"]:
        eligible, reason = ce30_engine.is_ce30_eligible(r)
        assert eligible is False
        assert "exclusively reserved for Reason 10.4" in reason


def test_ce30_eligibility_accepted_for_card_absent_fraud():
    for r in ["UNAUTHORIZED_TRANSACTION", "CARD_ABSENT_FRAUD", "FRAUD", "REASON_10_4"]:
        eligible, reason = ce30_engine.is_ce30_eligible(r)
        assert eligible is True
        assert reason is None


@pytest.mark.asyncio
async def test_ce30_telemetry_correlation_with_provenance(db_session):
    buyer = "test_ce30_buyer@example.com"
    shared_ip = "203.0.113.19"
    shared_dev = "fp_unique_12345"

    # Current disputed transaction
    curr = TransactionTelemetry(
        telemetry_id="tel_test_curr",
        transaction_id="TX_DISPUTED_001",
        customer_email=buyer,
        ip_address=shared_ip,
        device_fingerprint=shared_dev,
        provenance="VERIFIED_MERCHANT",
        timestamp=datetime.now(timezone.utc)
    )
    db_session.add(curr)

    # Historical purchase 1 (Verified Merchant Data)
    hist1 = TransactionTelemetry(
        telemetry_id="tel_test_hist1",
        transaction_id="TX_HIST_001",
        customer_email=buyer,
        ip_address=shared_ip,
        device_fingerprint=shared_dev,
        provenance="VERIFIED_MERCHANT",
        timestamp=datetime.now(timezone.utc) - timedelta(days=150)
    )
    db_session.add(hist1)

    # Historical purchase 2 (Synthetic Demo Fixture)
    hist2 = TransactionTelemetry(
        telemetry_id="tel_test_hist2",
        transaction_id="TX_HIST_002",
        customer_email=buyer,
        ip_address=shared_ip,
        device_fingerprint=shared_dev,
        provenance="SYNTHETIC_DEMO",
        timestamp=datetime.now(timezone.utc) - timedelta(days=200)
    )
    db_session.add(hist2)
    await db_session.commit()

    # Run CE3.0 correlation
    proofs = await ce30_engine.find_compelling_evidence(
        session=db_session,
        disputed_transaction_id="TX_DISPUTED_001",
        customer_email=buyer
    )

    assert len(proofs) >= 4
    # Check that provenance is accurately attributed
    hist1_proofs = [p for p in proofs if p.prior_transaction_id == "TX_HIST_001"]
    assert all(p.provenance == "VERIFIED_MERCHANT" for p in hist1_proofs)
    assert all(p.is_verified is True for p in hist1_proofs)

    hist2_proofs = [p for p in proofs if p.prior_transaction_id == "TX_HIST_002"]
    assert all(p.provenance == "SYNTHETIC_DEMO" for p in hist2_proofs)
    assert all(p.is_verified is False for p in hist2_proofs)

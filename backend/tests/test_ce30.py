import pytest
from datetime import datetime, timezone, timedelta
from backend.app.models import TransactionTelemetry
from backend.app.services.ce30_engine import ce30_engine


@pytest.mark.asyncio
async def test_ce30_telemetry_correlation(db_session):
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
        timestamp=datetime.now(timezone.utc)
    )
    db_session.add(curr)

    # Historical purchase 1 (matching IP and Device)
    hist1 = TransactionTelemetry(
        telemetry_id="tel_test_hist1",
        transaction_id="TX_HIST_001",
        customer_email=buyer,
        ip_address=shared_ip,
        device_fingerprint=shared_dev,
        timestamp=datetime.now(timezone.utc) - timedelta(days=150)
    )
    db_session.add(hist1)
    await db_session.commit()

    # Run CE3.0 correlation
    proofs = await ce30_engine.find_compelling_evidence(
        session=db_session,
        disputed_transaction_id="TX_DISPUTED_001",
        customer_email=buyer
    )

    assert len(proofs) >= 2
    matched_factors = [p.matched_factor for p in proofs]
    assert "IP_ADDRESS" in matched_factors
    assert "DEVICE_ID" in matched_factors

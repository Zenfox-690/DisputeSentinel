import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import StaticPool

from backend.app.database import Base, get_db
from backend.app.main import app
from backend.app.models import Dispute, TransactionTelemetry


@pytest.fixture(autouse=True)
def isolate_paypal_credentials(monkeypatch):
    """
    Isolate PayPal credentials during test runs to prevent test suites from picking up
    placeholder or environment credentials and executing external network calls.
    """
    from backend.app.config import settings
    from backend.app.services.paypal_client import paypal_client

    monkeypatch.setattr(settings, "PAYPAL_CLIENT_ID", "")
    monkeypatch.setattr(settings, "PAYPAL_CLIENT_SECRET", "")
    monkeypatch.setattr(settings, "PAYPAL_WEBHOOK_ID", "")
    monkeypatch.setattr(paypal_client, "client_id", "")
    monkeypatch.setattr(paypal_client, "client_secret", "")
    monkeypatch.setattr(paypal_client, "webhook_id", "")


@pytest.fixture
async def db_session():
    test_engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False
    )

    async with session_factory() as session:
        yield session

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await test_engine.dispose()


@pytest.fixture
async def test_client(db_session):
    # Seed standard test dispute & telemetry into the isolated in-memory DB
    test_tx_id = "33M44420E8977152Y"
    buyer_email = "sb-kjysr53628645@personal.example.com"
    shared_ip = "198.51.100.42"
    shared_device = "fp_9a8b7c6d5e4f3a2b1c"

    curr_tel = TransactionTelemetry(
        telemetry_id="tel_test_curr_tx",
        transaction_id=test_tx_id,
        customer_email=buyer_email,
        ip_address=shared_ip,
        device_fingerprint=shared_device,
        carrier_name="UPS",
        carrier_tracking_number="1Z9999999999999999",
        delivery_status="DELIVERED",
        delivery_timestamp=datetime.now(timezone.utc),
        provenance="SYNTHETIC_DEMO",
        timestamp=datetime.now(timezone.utc)
    )
    hist1 = TransactionTelemetry(
        telemetry_id="tel_test_hist_001",
        transaction_id="TX-HIST-001",
        customer_email=buyer_email,
        ip_address=shared_ip,
        device_fingerprint=shared_device,
        provenance="SYNTHETIC_DEMO",
        timestamp=datetime.now(timezone.utc) - timedelta(days=150)
    )
    hist2 = TransactionTelemetry(
        telemetry_id="tel_test_hist_002",
        transaction_id="TX-HIST-002",
        customer_email=buyer_email,
        ip_address=shared_ip,
        device_fingerprint=shared_device,
        provenance="SYNTHETIC_DEMO",
        timestamp=datetime.now(timezone.utc) - timedelta(days=200)
    )
    disp = Dispute(
        dispute_id="PP-R-PND-10195282",
        merchant_id="merchant_default",
        transaction_id="6EH76875HL360672K",
        seller_transaction_id="6EH76875HL360672K",
        buyer_transaction_id=test_tx_id,
        dispute_amount=Decimal("10.00"),
        currency="USD",
        reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
        status="WAITING_FOR_SELLER_RESPONSE",
        external_status="WAITING_FOR_SELLER_RESPONSE",
        workflow_status="PENDING_ACTION",
        lifecycle_stage="INQUIRY",
        response_due_date=datetime.fromisoformat("2026-11-19T11:24:01.788+00:00")
    )
    db_session.add_all([curr_tel, hist1, hist2, disp])
    await db_session.commit()

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db

    from backend.app.config import settings
    orig_submission_setting = settings.ENABLE_LIVE_PAYPAL_SUBMISSION
    settings.ENABLE_LIVE_PAYPAL_SUBMISSION = True
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            yield ac
    finally:
        settings.ENABLE_LIVE_PAYPAL_SUBMISSION = orig_submission_setting
        app.dependency_overrides.clear()


@pytest.fixture
def merchant_headers():
    return {
        "Authorization": "Bearer merchant_token_default",
        "X-Merchant-Id": "merchant_default"
    }


@pytest.fixture
def secondary_merchant_headers():
    return {
        "Authorization": "Bearer token_merchant_secondary",
        "X-Merchant-Id": "merchant_secondary"
    }

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from decimal import Decimal
from datetime import datetime, timezone, timedelta

from backend.app.config import settings
from backend.app.database import engine, Base, AsyncSessionLocal
from backend.app.models import TransactionTelemetry, Dispute
from backend.app.api.disputes import router as disputes_router
from backend.app.api.webhooks import router as webhooks_router
from backend.app.api.adjudication import router as adjudication_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Create tables if not exist
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Seed transaction telemetry fixtures for live Visa CE3.0 demo
    async with AsyncSessionLocal() as session:
        # Check if live transaction 33M44420E8977152Y is seeded
        test_tx_id = "33M44420E8977152Y"
        buyer_email = "sb-kjysr53628645@personal.example.com"
        shared_ip = "198.51.100.42"
        shared_device = "fp_9a8b7c6d5e4f3a2b1c"
        shared_address = {
            "street": "2211 N First St",
            "city": "San Jose",
            "state": "CA",
            "postal_code": "95131",
            "country": "US"
        }

        # Current disputed transaction telemetry
        existing_curr = await session.get(TransactionTelemetry, "tel_current_test_tx")
        if not existing_curr:
            curr_tel = TransactionTelemetry(
                telemetry_id="tel_current_test_tx",
                transaction_id=test_tx_id,
                customer_email=buyer_email,
                ip_address=shared_ip,
                device_fingerprint=shared_device,
                shipping_address=shared_address,
                carrier_name="UPS",
                carrier_tracking_number="1Z9999999999999999",
                delivery_status="DELIVERED",
                delivery_timestamp=datetime.now(timezone.utc),
                timestamp=datetime.now(timezone.utc)
            )
            session.add(curr_tel)

        # Historical undisputed transaction 1 (180 days ago - CE3.0 Qualifying)
        existing_hist1 = await session.get(TransactionTelemetry, "tel_hist_tx_001")
        if not existing_hist1:
            hist1 = TransactionTelemetry(
                telemetry_id="tel_hist_tx_001",
                transaction_id="TX-HIST-2026-04-12",
                customer_email=buyer_email,
                ip_address=shared_ip,  # Matching IP factor
                device_fingerprint=shared_device,  # Matching Device factor
                shipping_address=shared_address,
                carrier_name="UPS",
                carrier_tracking_number="1Z8888888888888888",
                delivery_status="DELIVERED",
                delivery_timestamp=datetime.now(timezone.utc) - timedelta(days=180),
                timestamp=datetime.now(timezone.utc) - timedelta(days=180)
            )
            session.add(hist1)

        # Historical undisputed transaction 2 (240 days ago - CE3.0 Qualifying)
        existing_hist2 = await session.get(TransactionTelemetry, "tel_hist_tx_002")
        if not existing_hist2:
            hist2 = TransactionTelemetry(
                telemetry_id="tel_hist_tx_002",
                transaction_id="TX-HIST-2026-02-15",
                customer_email=buyer_email,
                ip_address=shared_ip,
                device_fingerprint=shared_device,
                shipping_address=shared_address,
                carrier_name="UPS",
                carrier_tracking_number="1Z7777777777777777",
                delivery_status="DELIVERED",
                delivery_timestamp=datetime.now(timezone.utc) - timedelta(days=240),
                timestamp=datetime.now(timezone.utc) - timedelta(days=240)
            )
            session.add(hist2)

        # Also pre-seed the live dispute record if known
        live_dispute_id = "PP-R-PND-10195282"
        existing_disp = await session.get(Dispute, live_dispute_id)
        if not existing_disp:
            disp_record = Dispute(
                dispute_id=live_dispute_id,
                transaction_id=test_tx_id,
                dispute_amount=Decimal("10.00"),
                currency="USD",
                reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
                status="WAITING_FOR_SELLER_RESPONSE",
                lifecycle_stage="INQUIRY",
                response_due_date=datetime(2026, 11, 19, 7, 24, tzinfo=timezone.utc)
            )
            session.add(disp_record)

        await session.commit()

    yield
    # Shutdown
    await engine.dispose()


app = FastAPI(
    title="DisputeSentinel AI - Autonomous Dispute Intelligence",
    description="Autonomous Chargeback Defense & Visa Compelling Evidence 3.0 Platform for PayPal Merchants",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(disputes_router)
app.include_router(webhooks_router)
app.include_router(adjudication_router)


@app.get("/health", tags=["System"])
async def health_check():
    return {
        "status": "HEALTHY",
        "platform": "DisputeSentinel AI",
        "paypal_env": settings.PAYPAL_ENV,
        "paypal_base_url": settings.paypal_base_url,
        "ai_provider": settings.AI_PROVIDER
    }

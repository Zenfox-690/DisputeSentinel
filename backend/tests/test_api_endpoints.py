import pytest
import json
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from backend.app.config import settings
from backend.app.models import Dispute, EvidencePackage, MerchantApproval, FulfillmentRecord, TransactionTelemetry, WebhookEvent
from backend.app.services.auth import compute_evidence_hash
from sqlalchemy import select


async def seed_verified_fulfillment(db_session, tx_id="33M44420E8977152Y"):
    ful = FulfillmentRecord(
        fulfillment_id="ful_test_valid",
        transaction_id=tx_id,
        merchant_id="merchant_default",
        fulfillment_type="DIGITAL_LICENSE",
        recipient_email="sb-kjysr53628645@personal.example.com",
        license_key="LIC-SENTINEL-12345",
        download_timestamp=datetime.now(timezone.utc) - timedelta(days=1),
        download_ip="198.51.100.42",
        delivery_timestamp=datetime.now(timezone.utc) - timedelta(days=1),
        delivery_status="DELIVERED",
        provenance="VERIFIED_MERCHANT"
    )
    db_session.add(ful)
    await db_session.commit()
    return ful


@pytest.mark.asyncio
async def test_health_endpoint(test_client):
    response = await test_client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "HEALTHY"
    assert data["platform"] == "DisputeSentinel AI"


@pytest.mark.asyncio
async def test_disputes_list_and_synthesize(test_client):
    res = await test_client.get("/api/disputes")
    assert res.status_code == 200
    disputes = res.json()
    assert isinstance(disputes, list)
    assert any(d["dispute_id"] == "PP-R-PND-10195282" for d in disputes)

    # Test synthesis for pre-seeded live dispute (Item Not Received without fulfillment records)
    synth_res = await test_client.post("/api/disputes/PP-R-PND-10195282/synthesize")
    assert synth_res.status_code == 200
    dossier = synth_res.json()
    assert dossier["ce30_eligible"] is False
    assert "Visa Reason 13.1 (Item Not Received)" in dossier["ce30_ineligibility_reason"]
    assert dossier["fulfillment_status"] == "INSUFFICIENT_EVIDENCE"
    assert dossier["compelling_evidence_factor"] == "INSUFFICIENT_FULFILLMENT_EVIDENCE"
    assert dossier["adjudication_recommendation"] == "INSUFFICIENT_EVIDENCE"
    assert dossier["confidence_score"] <= 0.20
    assert len(dossier["historical_matches"]) == 0
    assert "liability shifts to the issuer" not in dossier["formal_defense_narrative"].lower()


# =========================================================================
# Webhook Security Tests
# =========================================================================

@pytest.mark.asyncio
async def test_webhook_rejected_on_invalid_signature(test_client, db_session):
    headers = {
        "paypal-transmission-id": "trans_invalid_999",
        "paypal-transmission-time": "2026-10-31T00:00:00Z",
        "paypal-cert-url": "https://api-m.sandbox.paypal.com/cert",
        "paypal-auth-algo": "SHA256withRSA",
        "paypal-transmission-sig": "INVALID_SIGNATURE",
    }
    payload = {
        "id": "WH-EVT-FORGED",
        "event_type": "CUSTOMER.DISPUTE.CREATED",
        "resource": {
            "dispute_id": "PP-FORGED-DISPUTE-001",
            "dispute_amount": {"value": "999.00", "currency_code": "USD"},
            "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
            "status": "WAITING_FOR_SELLER_RESPONSE",
            "dispute_life_cycle_stage": "INQUIRY",
            "disputed_transactions": [{"seller_transaction_id": "TX_FORGED"}]
        }
    }
    with patch("backend.app.api.webhooks.paypal_client.verify_webhook_signature", return_value=False):
        res = await test_client.post(
            "/api/webhooks/paypal",
            headers=headers,
            content=json.dumps(payload).encode("utf-8")
        )
    assert res.status_code == 401
    assert "Invalid PayPal webhook signature" in res.json()["detail"]

    # Assert NO database write occurred
    forged_disp = await db_session.get(Dispute, "PP-FORGED-DISPUTE-001")
    assert forged_disp is None


@pytest.mark.asyncio
async def test_webhook_accepted_on_valid_signature(test_client, db_session):
    headers = {
        "paypal-transmission-id": "trans_valid_100",
        "paypal-transmission-time": "2026-10-31T00:00:00Z",
        "paypal-cert-url": "https://api-m.sandbox.paypal.com/cert",
        "paypal-auth-algo": "SHA256withRSA",
        "paypal-transmission-sig": "VALID_MOCK_SIGNATURE",
    }
    payload = {
        "id": "WH-EVT-VALID",
        "event_type": "CUSTOMER.DISPUTE.CREATED",
        "resource": {
            "dispute_id": "PP-VALID-DISPUTE-001",
            "dispute_amount": {"value": "50.00", "currency_code": "USD"},
            "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
            "status": "WAITING_FOR_SELLER_RESPONSE",
            "dispute_life_cycle_stage": "INQUIRY",
            "disputed_transactions": [{"seller_transaction_id": "TX_VALID"}]
        }
    }
    with patch("backend.app.api.webhooks.paypal_client.verify_webhook_signature", return_value=True):
        res = await test_client.post(
            "/api/webhooks/paypal",
            headers=headers,
            content=json.dumps(payload).encode("utf-8")
        )
    assert res.status_code == 200
    assert res.json()["status"] == "ACCEPTED"

    # Assert valid webhook wrote record to database
    valid_disp = await db_session.get(Dispute, "PP-VALID-DISPUTE-001")
    assert valid_disp is not None
    assert valid_disp.dispute_amount == Decimal("50.00")


# =========================================================================
# Merchant Authentication & Approval Security Tests: Evidence Submission
# =========================================================================

@pytest.mark.asyncio
async def test_submit_defense_rejected_unauthenticated(test_client):
    # Synthesize first
    await test_client.post("/api/disputes/PP-R-PND-10195282/synthesize")

    # Calling submit without authentication headers must be rejected
    res = await test_client.post("/api/disputes/PP-R-PND-10195282/submit-defense")
    assert res.status_code == 401
    assert "Authentication required" in res.json()["detail"]


@pytest.mark.asyncio
async def test_unauthenticated_request_with_merchant_approved_param_rejected(test_client):
    await test_client.post("/api/disputes/PP-R-PND-10195282/synthesize")

    # Client-controlled parameter cannot bypass authentication
    res = await test_client.post("/api/disputes/PP-R-PND-10195282/submit-defense?merchant_approved=true")
    assert res.status_code == 401
    assert "Authentication required" in res.json()["detail"]


@pytest.mark.asyncio
async def test_submit_defense_rejected_without_server_side_approval(test_client, merchant_headers):
    # Synthesize evidence
    await test_client.post("/api/disputes/PP-R-PND-10195282/synthesize")

    # Authenticated merchant calls submit-defense without recording approval
    res = await test_client.post(
        "/api/disputes/PP-R-PND-10195282/submit-defense",
        headers=merchant_headers
    )
    assert res.status_code == 400
    assert "server-side merchant approval" in res.json()["detail"]


@pytest.mark.asyncio
async def test_non_merchant_cannot_approve_another_merchants_dossier(test_client, secondary_merchant_headers):
    await test_client.post("/api/disputes/PP-R-PND-10195282/synthesize")

    # Secondary merchant attempts to approve dispute owned by merchant_default
    res = await test_client.post(
        "/api/disputes/PP-R-PND-10195282/approve-evidence",
        headers=secondary_merchant_headers
    )
    assert res.status_code == 403
    assert "Merchant authorization mismatch" in res.json()["detail"]


@pytest.mark.asyncio
async def test_non_merchant_cannot_submit_another_merchants_dossier(test_client, secondary_merchant_headers):
    await test_client.post("/api/disputes/PP-R-PND-10195282/synthesize")

    # Secondary merchant attempts to submit dispute owned by merchant_default
    res = await test_client.post(
        "/api/disputes/PP-R-PND-10195282/submit-defense",
        headers=secondary_merchant_headers
    )
    assert res.status_code == 403
    assert "Merchant authorization mismatch" in res.json()["detail"]


@pytest.mark.asyncio
async def test_approval_for_one_package_cannot_authorize_modified_package(
    test_client, db_session, merchant_headers
):
    await seed_verified_fulfillment(db_session)
    # Step 1: Synthesize
    await test_client.post("/api/disputes/PP-R-PND-10195282/synthesize")

    # Step 2: Approve package
    appr_res = await test_client.post(
        "/api/disputes/PP-R-PND-10195282/approve-evidence",
        headers=merchant_headers
    )
    assert appr_res.status_code == 200

    # Step 3: Tamper / modify package narrative text directly after approval
    ev_res = await db_session.execute(
        Dispute.__table__.select().where(Dispute.dispute_id == "PP-R-PND-10195282")
    )
    from sqlalchemy import select
    pkg_res = await db_session.execute(
        select(EvidencePackage).where(EvidencePackage.dispute_id == "PP-R-PND-10195282")
    )
    pkg = pkg_res.scalars().first()
    pkg.narrative_text = "TAMPERED NARRATIVE: Modified after merchant approved."
    await db_session.commit()

    # Step 4: Submission must be rejected due to content hash mismatch
    res = await test_client.post(
        "/api/disputes/PP-R-PND-10195282/submit-defense",
        headers=merchant_headers
    )
    assert res.status_code == 400
    assert "modified since approval" in res.json()["detail"]


@pytest.mark.asyncio
async def test_submit_defense_rejected_on_wrong_status(test_client, db_session, merchant_headers):
    await seed_verified_fulfillment(db_session)
    await test_client.post("/api/disputes/PP-R-PND-10195282/synthesize")
    await test_client.post(
        "/api/disputes/PP-R-PND-10195282/approve-evidence",
        headers=merchant_headers
    )

    # Set dispute status to RESOLVED
    disp = await db_session.get(Dispute, "PP-R-PND-10195282")
    disp.status = "RESOLVED"
    await db_session.commit()

    res = await test_client.post(
        "/api/disputes/PP-R-PND-10195282/submit-defense",
        headers=merchant_headers
    )
    assert res.status_code == 400
    assert "WAITING_FOR_SELLER_RESPONSE" in res.json()["detail"]


@pytest.mark.asyncio
async def test_submit_defense_rejected_on_missing_hateoas_link(test_client, db_session, merchant_headers):
    await seed_verified_fulfillment(db_session)
    await test_client.post("/api/disputes/PP-R-PND-10195282/synthesize")
    await test_client.post(
        "/api/disputes/PP-R-PND-10195282/approve-evidence",
        headers=merchant_headers
    )

    # Mock PayPal returning no provide-evidence link
    mock_details = {
        "dispute_id": "PP-R-PND-10195282",
        "status": "WAITING_FOR_SELLER_RESPONSE",
        "evidence_requests": [{"evidence_type": "PROOF_OF_FULFILLMENT"}],
        "links": [{"rel": "self", "href": "https://api-m.sandbox.paypal.com/v1/customer/disputes/PP-R-PND-10195282"}]
    }
    with patch("backend.app.api.disputes.paypal_client.get_dispute", return_value=mock_details):
        res = await test_client.post(
            "/api/disputes/PP-R-PND-10195282/submit-defense",
            headers=merchant_headers
        )
        assert res.status_code == 400
        assert "provide-evidence" in res.json()["detail"]


@pytest.mark.asyncio
async def test_submit_defense_rejected_on_mismatched_evidence_type(test_client, db_session, merchant_headers):
    await seed_verified_fulfillment(db_session)
    await test_client.post("/api/disputes/PP-R-PND-10195282/synthesize")
    await test_client.post(
        "/api/disputes/PP-R-PND-10195282/approve-evidence",
        headers=merchant_headers
    )

    # Mock PayPal requesting only PROOF_OF_REFUND (not PROOF_OF_FULFILLMENT)
    mock_details = {
        "dispute_id": "PP-R-PND-10195282",
        "status": "WAITING_FOR_SELLER_RESPONSE",
        "evidence_requests": [{"evidence_type": "PROOF_OF_REFUND"}],
        "links": [{"rel": "provide-evidence", "href": "https://api-m.sandbox.paypal.com/v1/customer/disputes/PP-R-PND-10195282/provide-evidence"}]
    }
    with patch("backend.app.api.disputes.paypal_client.get_dispute", return_value=mock_details):
        res = await test_client.post(
            "/api/disputes/PP-R-PND-10195282/submit-defense",
            headers=merchant_headers
        )
        assert res.status_code == 400
        assert "requested evidence types" in res.json()["detail"]


@pytest.mark.asyncio
async def test_submit_defense_success_after_valid_approval(test_client, db_session, merchant_headers):
    await seed_verified_fulfillment(db_session)
    # Step 1: Synthesize
    synth_res = await test_client.post("/api/disputes/PP-R-PND-10195282/synthesize")
    assert synth_res.status_code == 200
    dossier = synth_res.json()
    assert dossier["fulfillment_status"] == "VERIFIED"

    # Step 2: Approve server-side
    appr_res = await test_client.post(
        "/api/disputes/PP-R-PND-10195282/approve-evidence",
        headers=merchant_headers
    )
    assert appr_res.status_code == 200
    assert appr_res.json()["status"] == "APPROVED"

    # Step 3: Submit defense (mock PayPal API calls during unit testing)
    mock_details = {
        "dispute_id": "PP-R-PND-10195282",
        "status": "WAITING_FOR_SELLER_RESPONSE",
        "dispute_life_cycle_stage": "INQUIRY",
        "evidence_requests": [{"evidence_type": "PROOF_OF_FULFILLMENT", "request_source": "PAYPAL"}],
        "links": [
            {
                "href": "https://api-m.sandbox.paypal.com/v1/customer/disputes/PP-R-PND-10195282/provide-evidence",
                "rel": "provide-evidence",
                "method": "POST"
            }
        ]
    }
    mock_provide_resp = {
        "acknowledged": True,
        "status": "EVIDENCE_SUBMITTED",
        "dispute_id": "PP-R-PND-10195282"
    }
    with patch("backend.app.api.disputes.paypal_client.get_dispute", return_value=mock_details), \
         patch("backend.app.api.disputes.paypal_client.provide_evidence", return_value=mock_provide_resp):
        res = await test_client.post(
            "/api/disputes/PP-R-PND-10195282/submit-defense",
            headers=merchant_headers
        )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "SUCCESS"
    assert data["dispute_status"] == "UNDER_REVIEW"

    # Assert status updated in DB
    disp = await db_session.get(Dispute, "PP-R-PND-10195282")
    assert disp.status == "UNDER_REVIEW"


# =========================================================================
# Adjudication Security Tests: Production Guard & Authorization
# =========================================================================

@pytest.mark.asyncio
async def test_adjudication_rejected_unauthenticated(test_client):
    res = await test_client.post("/api/adjudication/PP-R-PND-10195282/resolve")
    assert res.status_code == 401
    assert "Authentication required" in res.json()["detail"]


@pytest.mark.asyncio
async def test_adjudication_rejected_for_non_owner_merchant(test_client, secondary_merchant_headers):
    res = await test_client.post(
        "/api/adjudication/PP-R-PND-10195282/resolve",
        headers=secondary_merchant_headers
    )
    assert res.status_code == 403
    assert "Merchant authorization mismatch" in res.json()["detail"]


@pytest.mark.parametrize("invalid_env", ["production", "staging", "live", "prod", "qa"])
@pytest.mark.asyncio
async def test_adjudication_rejected_in_all_non_sandbox_environments(test_client, invalid_env):
    from backend.app.services.auth import mint_merchant_token
    signed_token = mint_merchant_token("merchant_default", "merchant@example.com")
    auth_headers = {"Authorization": f"Bearer {signed_token}"}
    with patch.object(settings, "PAYPAL_ENV", invalid_env):
        res = await test_client.post(
            "/api/adjudication/PP-R-PND-10195282/resolve",
            headers=auth_headers
        )
        assert res.status_code == 403
        assert "strictly restricted to Sandbox environment" in res.json()["detail"]


@pytest.mark.asyncio
async def test_adjudication_rejected_on_wrong_status(test_client, db_session, merchant_headers):
    # Dispute is initially WAITING_FOR_SELLER_RESPONSE, not UNDER_REVIEW
    disp = await db_session.get(Dispute, "PP-R-PND-10195282")
    assert disp.status == "WAITING_FOR_SELLER_RESPONSE"

    res = await test_client.post(
        "/api/adjudication/PP-R-PND-10195282/resolve",
        headers=merchant_headers
    )
    assert res.status_code == 400
    assert "UNDER_REVIEW" in res.json()["detail"]


@pytest.mark.asyncio
async def test_adjudication_rejected_on_missing_hateoas_link(test_client, db_session, merchant_headers):
    # Set status to UNDER_REVIEW
    disp = await db_session.get(Dispute, "PP-R-PND-10195282")
    disp.status = "UNDER_REVIEW"
    await db_session.commit()

    # Mock PayPal returning no adjudicate link
    mock_details = {
        "dispute_id": "PP-R-PND-10195282",
        "status": "UNDER_REVIEW",
        "links": [{"rel": "self", "href": "https://api-m.sandbox.paypal.com/v1/customer/disputes/PP-R-PND-10195282"}]
    }
    with patch("backend.app.api.adjudication.paypal_client.get_dispute", return_value=mock_details):
        res = await test_client.post(
            "/api/adjudication/PP-R-PND-10195282/resolve",
            headers=merchant_headers
        )
        assert res.status_code == 400
        assert "adjudicate" in res.json()["detail"]


@pytest.mark.asyncio
async def test_adjudication_success_in_sandbox(test_client, db_session, merchant_headers):
    # Set status to UNDER_REVIEW
    disp = await db_session.get(Dispute, "PP-R-PND-10195282")
    disp.status = "UNDER_REVIEW"
    await db_session.commit()

    mock_details = {
        "dispute_id": "PP-R-PND-10195282",
        "status": "UNDER_REVIEW",
        "dispute_life_cycle_stage": "INQUIRY",
        "links": [
            {
                "href": "https://api-m.sandbox.paypal.com/v1/customer/disputes/PP-R-PND-10195282/adjudicate",
                "rel": "adjudicate",
                "method": "POST"
            }
        ]
    }
    mock_adj_resp = {
        "adjudication_outcome": "SELLER_FAVOR",
        "dispute_id": "PP-R-PND-10195282",
        "status": "RESOLVED"
    }
    with patch("backend.app.api.adjudication.paypal_client.get_dispute", return_value=mock_details), \
         patch("backend.app.api.adjudication.paypal_client.adjudicate_dispute", return_value=mock_adj_resp):
        res = await test_client.post(
            "/api/adjudication/PP-R-PND-10195282/resolve?outcome=SELLER_FAVOR",
            headers=merchant_headers
        )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "SUCCESS"
    assert data["adjudication_outcome"] == "SELLER_FAVOR"
    assert data["dispute_status"] == "RESOLVED"

    # Assert resolved in DB
    updated = await db_session.get(Dispute, "PP-R-PND-10195282")
    assert updated.status == "RESOLVED"
    assert updated.adjudication_outcome == "SELLER_FAVOR"



# =========================================================================
# Cryptographic Token Authentication & Production Safety Tests
# =========================================================================

@pytest.mark.asyncio
async def test_mint_and_authenticate_signed_token(test_client):
    from backend.app.services.auth import mint_merchant_token
    token = mint_merchant_token("merchant_default", "sb-zpxtx53591626@business.example.com")
    headers = {"Authorization": f"Bearer {token}"}

    res = await test_client.post("/api/disputes/PP-R-PND-10195282/synthesize")
    assert res.status_code == 200

    # Test approve-evidence with minted signed token
    appr_res = await test_client.post("/api/disputes/PP-R-PND-10195282/approve-evidence", headers=headers)
    assert appr_res.status_code == 200
    assert appr_res.json()["status"] == "APPROVED"


@pytest.mark.asyncio
async def test_expired_merchant_token_rejected(test_client):
    from backend.app.services.auth import mint_merchant_token
    expired_token = mint_merchant_token("merchant_default", "test@example.com", ttl_seconds=-10)
    headers = {"Authorization": f"Bearer {expired_token}"}

    res = await test_client.post("/api/disputes/PP-R-PND-10195282/approve-evidence", headers=headers)
    assert res.status_code == 401
    assert "expired" in res.json()["detail"].lower()


@pytest.mark.asyncio
async def test_revoked_merchant_token_rejected(test_client):
    from backend.app.services.auth import mint_merchant_token, verify_signed_token, revoke_token
    token = mint_merchant_token("merchant_default", "test@example.com")
    claims = verify_signed_token(token)
    revoke_token(claims["jti"])

    headers = {"Authorization": f"Bearer {token}"}
    res = await test_client.post("/api/disputes/PP-R-PND-10195282/approve-evidence", headers=headers)
    assert res.status_code == 401
    assert "revoked" in res.json()["detail"].lower()


@pytest.mark.asyncio
async def test_tampered_token_signature_rejected(test_client):
    from backend.app.services.auth import mint_merchant_token
    token = mint_merchant_token("merchant_default", "test@example.com")
    # Corrupt last character of the signature
    tampered_token = token[:-1] + ("A" if token[-1] != "A" else "B")
    headers = {"Authorization": f"Bearer {tampered_token}"}

    res = await test_client.post("/api/disputes/PP-R-PND-10195282/approve-evidence", headers=headers)
    assert res.status_code == 401
    assert "signature" in res.json()["detail"].lower()


@pytest.mark.asyncio
async def test_dev_token_strictly_rejected_in_production(test_client):
    with patch.object(settings, "PAYPAL_ENV", "production"):
        headers = {"Authorization": "Bearer merchant_token_default"}
        res = await test_client.post("/api/disputes/PP-R-PND-10195282/approve-evidence", headers=headers)
        assert res.status_code == 401
        assert "Development merchant tokens are strictly prohibited" in res.json()["detail"]


@pytest.mark.asyncio
async def test_identity_derived_from_credential_and_spoofed_header_rejected(test_client):
    from backend.app.services.auth import mint_merchant_token
    # Token minted for merchant_default
    token = mint_merchant_token("merchant_default", "test@example.com")
    # Client passes spoofed header claiming to be merchant_secondary
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Merchant-Id": "merchant_secondary"
    }
    res = await test_client.post("/api/disputes/PP-R-PND-10195282/approve-evidence", headers=headers)
    assert res.status_code == 403
    assert "Caller identity mismatch" in res.json()["detail"]


# =========================================================================
# Evidence Approval Integrity & Canonical Hashing Tests
# =========================================================================

@pytest.mark.asyncio
async def test_changing_attachment_invalidates_approval(test_client, db_session, merchant_headers):
    await seed_verified_fulfillment(db_session)
    # Step 1: Synthesize
    await test_client.post("/api/disputes/PP-R-PND-10195282/synthesize")

    # Add an attachment to the package
    from sqlalchemy import select
    pkg_res = await db_session.execute(
        select(EvidencePackage).where(EvidencePackage.dispute_id == "PP-R-PND-10195282")
    )
    pkg = pkg_res.scalars().first()
    pkg.submission_payload = {
        **pkg.submission_payload,
        "attachments": [
            {
                "attachment_id": "att_001",
                "filename": "proof_of_delivery.pdf",
                "mime_type": "application/pdf",
                "sha256_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                "size_bytes": 1024
            }
        ]
    }
    await db_session.commit()

    # Step 2: Approve
    appr_res = await test_client.post(
        "/api/disputes/PP-R-PND-10195282/approve-evidence",
        headers=merchant_headers
    )
    assert appr_res.status_code == 200

    # Step 3: Mutate attachment contents / hash
    pkg.submission_payload = {
        **pkg.submission_payload,
        "attachments": [
            {
                "attachment_id": "att_001",
                "filename": "proof_of_delivery.pdf",
                "mime_type": "application/pdf",
                "sha256_hash": "0000000000000000000000000000000000000000000000000000000000000000",  # Changed hash
                "size_bytes": 1024
            }
        ]
    }
    await db_session.commit()

    # Step 4: Submission must be rejected
    res = await test_client.post(
        "/api/disputes/PP-R-PND-10195282/submit-defense",
        headers=merchant_headers
    )
    assert res.status_code == 400
    assert "modified since approval" in res.json()["detail"]


@pytest.mark.asyncio
async def test_changing_metadata_field_invalidates_approval(test_client, db_session, merchant_headers):
    await seed_verified_fulfillment(db_session)
    await test_client.post("/api/disputes/PP-R-PND-10195282/synthesize")

    # Approve
    await test_client.post(
        "/api/disputes/PP-R-PND-10195282/approve-evidence",
        headers=merchant_headers
    )

    # Mutate tracking number in payload
    from sqlalchemy import select
    pkg_res = await db_session.execute(
        select(EvidencePackage).where(EvidencePackage.dispute_id == "PP-R-PND-10195282")
    )
    pkg = pkg_res.scalars().first()
    pkg.submission_payload = {
        **pkg.submission_payload,
        "tracking_number": "1Z_MODIFIED_TRACKING_999"
    }
    await db_session.commit()

    res = await test_client.post(
        "/api/disputes/PP-R-PND-10195282/submit-defense",
        headers=merchant_headers
    )
    assert res.status_code == 400
    assert "modified since approval" in res.json()["detail"]


def test_reordering_attachments_produces_identical_hash():
    from backend.app.services.auth import compute_canonical_submission_hash
    att1 = {
        "attachment_id": "att_alpha",
        "filename": "a.pdf",
        "mime_type": "application/pdf",
        "sha256_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "size_bytes": 100
    }
    att2 = {
        "attachment_id": "att_beta",
        "filename": "b.pdf",
        "mime_type": "application/pdf",
        "sha256_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "size_bytes": 200
    }

    hash_order_1 = compute_canonical_submission_hash(
        dispute_id="PP-DISP-001",
        merchant_id="merchant_001",
        evidence_type="PROOF_OF_FULFILLMENT",
        narrative_text="Standard defense narrative.",
        attachments=[att1, att2]
    )

    hash_order_2 = compute_canonical_submission_hash(
        dispute_id="PP-DISP-001",
        merchant_id="merchant_001",
        evidence_type="PROOF_OF_FULFILLMENT",
        narrative_text="Standard defense narrative.",
        attachments=[att2, att1]
    )

    assert hash_order_1 == hash_order_2


def test_approval_hash_cannot_be_reused_for_different_dispute_or_merchant():
    from backend.app.services.auth import compute_canonical_submission_hash
    base_params = {
        "evidence_type": "PROOF_OF_FULFILLMENT",
        "narrative_text": "Standard defense narrative for test.",
    }

    hash_disp_1 = compute_canonical_submission_hash(
        dispute_id="PP-DISP-001",
        merchant_id="merchant_001",
        **base_params
    )
    hash_disp_2 = compute_canonical_submission_hash(
        dispute_id="PP-DISP-002",  # Different dispute
        merchant_id="merchant_001",
        **base_params
    )
    hash_merchant_2 = compute_canonical_submission_hash(
        dispute_id="PP-DISP-001",
        merchant_id="merchant_002",  # Different merchant
        **base_params
    )

    assert hash_disp_1 != hash_disp_2
    assert hash_disp_1 != hash_merchant_2


# =========================================================================
# Evidence Provenance & CE3.0 Regression Tests
# =========================================================================

@pytest.mark.asyncio
async def test_submit_defense_rejected_on_insufficient_evidence(test_client, db_session, merchant_headers):
    # Dispute PP-R-PND-10195282 has NO fulfillment records by default (INSUFFICIENT_EVIDENCE)
    await test_client.post("/api/disputes/PP-R-PND-10195282/synthesize")

    # Even if merchant attempts to approve incomplete dossier
    appr_res = await test_client.post(
        "/api/disputes/PP-R-PND-10195282/approve-evidence",
        headers=merchant_headers
    )
    assert appr_res.status_code == 200

    # Submission must be strictly blocked due to INSUFFICIENT_EVIDENCE
    res = await test_client.post(
        "/api/disputes/PP-R-PND-10195282/submit-defense",
        headers=merchant_headers
    )
    assert res.status_code == 400
    assert "status 'INSUFFICIENT_EVIDENCE'" in res.json()["detail"]
    assert "Verified merchant fulfillment records are required" in res.json()["detail"]


@pytest.mark.asyncio
async def test_submit_defense_rejected_on_synthetic_demo_telemetry(test_client, db_session, merchant_headers):
    # Seed an eligible fraud dispute that erroneously incorporates synthetic demo telemetry
    tx_fraud = "TX-FRAUD-SYNTHETIC-999"
    disp_fraud = Dispute(
        dispute_id="PP-R-FRAUD-SYNTHETIC",
        merchant_id="merchant_default",
        transaction_id=tx_fraud,
        dispute_amount=Decimal("15.00"),
        currency="USD",
        reason="UNAUTHORIZED_TRANSACTION",
        status="WAITING_FOR_SELLER_RESPONSE",
        lifecycle_stage="INQUIRY"
    )
    db_session.add(disp_fraud)

    # Synthesize package manually with a synthetic demonstration match
    pkg = EvidencePackage(
        dispute_id="PP-R-FRAUD-SYNTHETIC",
        evidence_type="PROOF_OF_FULFILLMENT",
        narrative_text="Representment based on demonstration fixtures.",
        telemetry_matches=[{
            "prior_transaction_id": "TX-DEMO-001",
            "matched_factor": "IP_ADDRESS",
            "match_value": "198.51.100.42",
            "provenance": "SYNTHETIC_DEMO",
            "is_verified": False
        }],
        submission_payload={
            "dispute_id": "PP-R-FRAUD-SYNTHETIC",
            "merchant_id": "merchant_default",
            "evidence_type": "PROOF_OF_FULFILLMENT",
            "narrative_text": "Representment based on demonstration fixtures.",
            "carrier_name": "",
            "tracking_number": "",
            "delivery_date": "",
            "compelling_evidence_factor": "VISA_CE3.0_MULTI_FACTOR_MATCH",
            "attachments": []
        },
        status="DRAFT",
        fulfillment_status="VERIFIED",
        confidence_score=Decimal("0.90")
    )
    db_session.add(pkg)
    await db_session.commit()

    # Approve
    await test_client.post(
        "/api/disputes/PP-R-FRAUD-SYNTHETIC/approve-evidence",
        headers=merchant_headers
    )

    # Submit defense must be rejected because telemetry is synthetic demo
    res = await test_client.post(
        "/api/disputes/PP-R-FRAUD-SYNTHETIC/submit-defense",
        headers=merchant_headers
    )
    assert res.status_code == 400
    assert "synthetic demonstration telemetry ('SYNTHETIC_DEMO')" in res.json()["detail"]


@pytest.mark.asyncio
async def test_ce30_synthesis_for_eligible_fraud_dispute(test_client, db_session):
    # Seed an eligible card-absent fraud dispute with verified merchant telemetry
    buyer = "verified_fraud_target@example.com"
    curr_tx = "TX-ELIGIBLE-FRAUD-001"
    hist_tx = "TX-ELIGIBLE-HIST-001"

    curr_tel = TransactionTelemetry(
        telemetry_id="tel_fraud_curr",
        transaction_id=curr_tx,
        customer_email=buyer,
        ip_address="192.0.2.100",
        device_fingerprint="fp_verified_device",
        provenance="VERIFIED_MERCHANT",
        timestamp=datetime.now(timezone.utc)
    )
    hist_tel = TransactionTelemetry(
        telemetry_id="tel_fraud_hist",
        transaction_id=hist_tx,
        customer_email=buyer,
        ip_address="192.0.2.100",
        device_fingerprint="fp_verified_device",
        provenance="VERIFIED_MERCHANT",
        timestamp=datetime.now(timezone.utc) - timedelta(days=90)
    )
    disp = Dispute(
        dispute_id="PP-R-ELIGIBLE-FRAUD",
        merchant_id="merchant_default",
        transaction_id=curr_tx,
        dispute_amount=Decimal("50.00"),
        currency="USD",
        reason="UNAUTHORIZED_TRANSACTION",  # Reason 10.4 Card-Absent Fraud
        status="WAITING_FOR_SELLER_RESPONSE",
        lifecycle_stage="CHARGEBACK"
    )
    db_session.add_all([curr_tel, hist_tel, disp])
    await db_session.commit()

    synth_res = await test_client.post("/api/disputes/PP-R-ELIGIBLE-FRAUD/synthesize")
    assert synth_res.status_code == 200
    dossier = synth_res.json()
    assert dossier["ce30_eligible"] is True
    assert dossier["compelling_evidence_factor"] == "VISA_CE3.0_MULTI_FACTOR_MATCH"
    assert dossier["fulfillment_status"] == "VERIFIED"
    assert len(dossier["historical_matches"]) >= 2
    assert all(m["is_verified"] is True for m in dossier["historical_matches"])
    assert all(m["provenance"] == "VERIFIED_MERCHANT" for m in dossier["historical_matches"])
    assert dossier["confidence_score"] >= 0.90
    assert dossier["adjudication_recommendation"] == "SELLER_FAVOR"
    assert "VISA COMPELLING EVIDENCE 3.0" in dossier["formal_defense_narrative"]


@pytest.mark.asyncio
async def test_ce30_rejection_for_ineligible_item_not_received_dispute(test_client):
    # Pre-seeded PP-R-PND-10195282 is MERCHANDISE_OR_SERVICE_NOT_RECEIVED
    synth_res = await test_client.post("/api/disputes/PP-R-PND-10195282/synthesize")
    assert synth_res.status_code == 200
    dossier = synth_res.json()
    assert dossier["ce30_eligible"] is False
    assert "Visa Reason 13.1 (Item Not Received)" in dossier["ce30_ineligibility_reason"]
    assert "liability shifts to the issuer" not in dossier["formal_defense_narrative"].lower()
    assert "visa compelling evidence 3.0" not in dossier["formal_defense_narrative"].lower()


@pytest.mark.asyncio
async def test_evidence_provenance_audit_trail_preserved(test_client, db_session):
    synth_res = await test_client.post("/api/disputes/PP-R-PND-10195282/synthesize")
    assert synth_res.status_code == 200
    dossier = synth_res.json()
    prov = dossier["provenance_summary"]
    assert prov["dispute_record"] == "EXTERNAL_API"
    assert prov["transaction_record"] == "EXTERNAL_API"
    assert prov["fulfillment_evidence"] == "MISSING"
    assert prov["telemetry_fixtures"] == "SYNTHETIC_DEMO"


@pytest.mark.asyncio
async def test_paypal_client_refuses_silent_mock_fallback_when_credentials_missing():
    from backend.app.services.paypal_client import PayPalClient
    unconfigured_client = PayPalClient(client_id="", client_secret="")

    with pytest.raises(RuntimeError, match="Silent fallback to simulated data is disabled"):
        await unconfigured_client.get_dispute("PP-R-PND-10195282")

    with pytest.raises(RuntimeError, match="Cannot perform OAuth authentication"):
        await unconfigured_client.get_access_token()


@pytest.mark.asyncio
async def test_evidence_parser_reconciles_live_paypal_evidences_structure(test_client, db_session, merchant_headers):
    """Verify that when PayPal reports evidence requests in 'evidences' with source='REQUESTED_FROM_SELLER', it is properly parsed."""
    await seed_verified_fulfillment(db_session)
    await test_client.post("/api/disputes/PP-R-PND-10195282/synthesize")
    await test_client.post("/api/disputes/PP-R-PND-10195282/approve-evidence", headers=merchant_headers)

    # Mimic exact raw PayPal live Sandbox response structure
    mock_details = {
        "dispute_id": "PP-R-PND-10195282",
        "status": "WAITING_FOR_SELLER_RESPONSE",
        "dispute_life_cycle_stage": "INQUIRY",
        "evidences": [
            {"date": "2026-10-30T11:24:01.788Z", "evidence_type": "CREATE", "source": "SUBMITTED_BY_BUYER"},
            {"date": "2026-10-30T11:27:23.941Z", "evidence_type": "PROOF_OF_FULFILLMENT", "source": "REQUESTED_FROM_SELLER"}
        ],
        "evidence_requests": [],  # Empty in live inquiry stage
        "links": [
            {"href": "https://api-m.sandbox.paypal.com/v1/customer/disputes/PP-R-PND-10195282/provide-evidence", "method": "POST", "rel": "provide_evidence"}
        ]
    }
    mock_provide_resp = {"acknowledged": True, "status": "EVIDENCE_SUBMITTED", "dispute_id": "PP-R-PND-10195282"}

    with patch("backend.app.api.disputes.paypal_client.get_dispute", return_value=mock_details), \
         patch("backend.app.api.disputes.paypal_client.provide_evidence", return_value=mock_provide_resp):
        res = await test_client.post(
            "/api/disputes/PP-R-PND-10195282/submit-defense",
            headers=merchant_headers
        )
    assert res.status_code == 200
    assert res.json()["status"] == "SUCCESS"


@pytest.mark.asyncio
async def test_transaction_id_dual_reconciliation(test_client, db_session):
    """Verify that fulfillment records attached to either seller_transaction_id or buyer_transaction_id are correctly reconciled."""
    # Seed fulfillment under the buyer_transaction_id
    disp = await db_session.get(Dispute, "PP-R-PND-10195282")
    assert disp.seller_transaction_id == "6EH76875HL360672K"
    assert disp.buyer_transaction_id == "33M44420E8977152Y"

    # Add fulfillment under buyer_transaction_id
    ful = FulfillmentRecord(
        fulfillment_id="ful_test_buyer_id",
        transaction_id="33M44420E8977152Y",  # buyer perspective
        merchant_id="merchant_default",
        fulfillment_type="DIGITAL_LICENSE",
        recipient_email="sb-kjysr53628645@personal.example.com",
        license_key="LIC-KEY-RECONCILED-999",
        delivery_status="DELIVERED",
        provenance="VERIFIED_MERCHANT"
    )
    db_session.add(ful)
    await db_session.commit()

    synth_res = await test_client.post("/api/disputes/PP-R-PND-10195282/synthesize")
    assert synth_res.status_code == 200
    dossier = synth_res.json()
    assert dossier["fulfillment_status"] == "VERIFIED"
    assert "LIC-KEY-RECONCILED-999" in dossier["formal_defense_narrative"]


@pytest.mark.asyncio
async def test_webhook_idempotency_duplicate_notification_ignored(test_client, db_session):
    headers = {
        "paypal-transmission-id": "trans_idemp_100",
        "paypal-transmission-time": "2026-10-31T00:00:00Z",
        "paypal-cert-url": "https://api-m.sandbox.paypal.com/cert",
        "paypal-auth-algo": "SHA256withRSA",
        "paypal-transmission-sig": "VALID_SIGNATURE_100",
    }
    payload = {
        "id": "WH-EVT-IDEMP-001",
        "event_type": "CUSTOMER.DISPUTE.CREATED",
        "resource": {
            "dispute_id": "PP-IDEMP-DISPUTE-001",
            "dispute_amount": {"value": "75.00", "currency_code": "USD"},
            "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
            "status": "WAITING_FOR_SELLER_RESPONSE",
            "dispute_life_cycle_stage": "INQUIRY",
            "disputed_transactions": [{"seller_transaction_id": "TX_IDEMP_001"}]
        }
    }
    with patch("backend.app.api.webhooks.paypal_client.verify_webhook_signature", return_value=True):
        # Initial webhook notification
        res1 = await test_client.post(
            "/api/webhooks/paypal",
            headers=headers,
            content=json.dumps(payload).encode("utf-8")
        )
        assert res1.status_code == 200
        assert res1.json()["status"] == "ACCEPTED"

        # Verify event stored once in database
        events = (await db_session.execute(
            select(WebhookEvent).where(WebhookEvent.transmission_id == "trans_idemp_100")
        )).scalars().all()
        assert len(events) == 1

        # Second notification with identical transmission ID and payload
        res2 = await test_client.post(
            "/api/webhooks/paypal",
            headers=headers,
            content=json.dumps(payload).encode("utf-8")
        )
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["status"] == "ALREADY_PROCESSED"
    assert data2["idempotent"] is True
    assert data2["transmission_id"] == "trans_idemp_100"

    # Event count in database must remain exactly 1 (no duplicate entries)
    events_after = (await db_session.execute(
        select(WebhookEvent).where(WebhookEvent.transmission_id == "trans_idemp_100")
    )).scalars().all()
    assert len(events_after) == 1


@pytest.mark.asyncio
async def test_webhook_dispute_updated_and_resolved_lifecycle(test_client, db_session):
    headers = {
        "paypal-transmission-id": "trans_lifecycle_201",
        "paypal-transmission-time": "2026-10-31T01:00:00Z",
        "paypal-cert-url": "https://api-m.sandbox.paypal.com/cert",
        "paypal-auth-algo": "SHA256withRSA",
        "paypal-transmission-sig": "VALID_SIGNATURE_201",
    }
    update_payload = {
        "id": "WH-EVT-UPDATE-001",
        "event_type": "CUSTOMER.DISPUTE.UPDATED",
        "resource": {
            "dispute_id": "PP-R-PND-10195282",
            "status": "UNDER_REVIEW",
            "dispute_life_cycle_stage": "CHARGEBACK",
            "seller_response_due_date": "2026-11-25T12:00:00Z"
        }
    }
    with patch("backend.app.api.webhooks.paypal_client.verify_webhook_signature", return_value=True):
        res_upd = await test_client.post(
            "/api/webhooks/paypal",
            headers=headers,
            content=json.dumps(update_payload).encode("utf-8")
        )
        assert res_upd.status_code == 200
        assert res_upd.json()["status"] == "ACCEPTED"

        # Verify dispute state in DB reflects UPDATE
        disp = await db_session.get(Dispute, "PP-R-PND-10195282")
        assert disp.status == "UNDER_REVIEW"
        assert disp.external_status == "UNDER_REVIEW"
        assert disp.lifecycle_stage == "CHARGEBACK"

        # Send RESOLVED event
        resolve_headers = {
            **headers,
            "paypal-transmission-id": "trans_lifecycle_202"
        }
        resolve_payload = {
            "id": "WH-EVT-RESOLVE-001",
            "event_type": "CUSTOMER.DISPUTE.RESOLVED",
            "resource": {
                "dispute_id": "PP-R-PND-10195282",
                "status": "RESOLVED",
                "dispute_life_cycle_stage": "RESOLVED",
                "dispute_outcome": {
                    "outcome_code": "RESOLVED_SELLER_FAVOUR"
                }
            }
        }
        res_res = await test_client.post(
            "/api/webhooks/paypal",
            headers=resolve_headers,
            content=json.dumps(resolve_payload).encode("utf-8")
        )
    assert res_res.status_code == 200
    assert res_res.json()["status"] == "ACCEPTED"

    # Verify dispute final outcome in DB
    await db_session.refresh(disp)
    assert disp.status == "RESOLVED"
    assert disp.external_status == "RESOLVED"
    assert disp.workflow_status == "RESOLVED"
    assert disp.adjudication_outcome == "RESOLVED_SELLER_FAVOUR"


@pytest.mark.asyncio
async def test_record_fulfillment_endpoint_and_resynthesize(test_client, db_session, merchant_headers):
    # 1. Post authentic merchant fulfillment record with independent validation
    ful_payload = {
        "fulfillment_type": "DIGITAL_LICENSE",
        "recipient_email": "sb-kjysr53628645@personal.example.com",
        "license_key": "LIC-PROD-ENDPOINT-888",
        "download_url": "https://download.sentinel.example.com/build-888",
        "download_ip": "198.51.100.42",
        "carrier_name": None,
        "tracking_number": None,
        "delivery_status": "DELIVERED",
        "document_hash": "a" * 64,
        "validation_source": "INDEPENDENT_AUDIT"
    }
    ful_res = await test_client.post(
        "/api/disputes/PP-R-PND-10195282/fulfillment",
        headers=merchant_headers,
        json=ful_payload
    )
    assert ful_res.status_code == 200
    ful_data = ful_res.json()
    assert ful_data["status"] == "SUCCESS"
    assert ful_data["provenance"] == "VERIFIED_MERCHANT"
    assert ful_data["is_independently_validated"] is True
    assert ful_data["document_hash"] == "a" * 64
    assert ful_data["transaction_id"] in ("33M44420E8977152Y", "6EH76875HL360672K")

    # 2. Synthesize evidence dossier now that fulfillment is recorded
    synth_res = await test_client.post("/api/disputes/PP-R-PND-10195282/synthesize")
    assert synth_res.status_code == 200
    dossier = synth_res.json()
    assert dossier["fulfillment_status"] == "VERIFIED"
    assert dossier["compelling_evidence_factor"] == "DIGITAL_GOODS_FULFILLMENT_VERIFIED"
    assert dossier["adjudication_recommendation"] == "SELLER_FAVOR"
    assert dossier["confidence_score"] >= 0.85
    assert "LIC-PROD-ENDPOINT-888" in dossier["formal_defense_narrative"]
    assert dossier["provenance_summary"]["fulfillment_evidence"] == "VERIFIED_MERCHANT"


@pytest.mark.asyncio
async def test_manually_entered_fulfillment_is_classified_as_merchant_attested_and_not_automatically_verified(test_client, db_session, merchant_headers):
    # Post manual fulfillment record without independent external audit
    manual_payload = {
        "fulfillment_type": "DIGITAL_LICENSE",
        "recipient_email": "sb-kjysr53628645@personal.example.com",
        "license_key": "LIC-MANUAL-ENTRY-777",
        "delivery_status": "DELIVERED",
        "validation_source": "MANUAL_ENTRY",
        "provenance": "VERIFIED_MERCHANT"  # Attempting to claim VERIFIED_MERCHANT manually
    }
    ful_res = await test_client.post(
        "/api/disputes/PP-R-PND-10195282/fulfillment",
        headers=merchant_headers,
        json=manual_payload
    )
    assert ful_res.status_code == 200
    ful_data = ful_res.json()
    # Manual claim must be classified as MERCHANT_ATTESTED, NOT automatically VERIFIED_MERCHANT
    assert ful_data["provenance"] == "MERCHANT_ATTESTED"
    assert ful_data["is_independently_validated"] is False
    assert ful_data["document_hash"] is not None

    # Synthesize: completeness reflects merchant attestation requiring human review (confidence 0.72 < threshold 0.85)
    synth_res = await test_client.post("/api/disputes/PP-R-PND-10195282/synthesize")
    assert synth_res.status_code == 200
    dossier = synth_res.json()
    assert dossier["fulfillment_status"] == "MERCHANT_ATTESTED"
    assert dossier["compelling_evidence_factor"] == "DIGITAL_GOODS_MERCHANT_ATTESTED"
    assert dossier["confidence_score"] < 0.85
    assert "Merchant Attestation" in dossier["formal_defense_narrative"]


@pytest.mark.asyncio
async def test_live_submission_blocked_when_enable_live_paypal_submission_is_false(test_client, db_session, merchant_headers):
    await seed_verified_fulfillment(db_session)
    await test_client.post("/api/disputes/PP-R-PND-10195282/synthesize")
    await test_client.post("/api/disputes/PP-R-PND-10195282/approve-evidence", headers=merchant_headers)

    mock_paypal_details = {
        "dispute_id": "PP-R-PND-10195282",
        "status": "WAITING_FOR_SELLER_RESPONSE",
        "dispute_life_cycle_stage": "INQUIRY",
        "evidences": [{"date": "2026-10-30T11:27:23.941Z", "evidence_type": "PROOF_OF_FULFILLMENT", "source": "REQUESTED_FROM_SELLER"}],
        "links": [{"href": "https://api-m.sandbox.paypal.com/v1/customer/disputes/PP-R-PND-10195282/provide-evidence", "method": "POST", "rel": "provide_evidence"}]
    }

    from backend.app.config import settings
    orig = settings.ENABLE_LIVE_PAYPAL_SUBMISSION
    settings.ENABLE_LIVE_PAYPAL_SUBMISSION = False
    try:
        with patch("backend.app.api.disputes.paypal_client.get_dispute", return_value=mock_paypal_details):
            res = await test_client.post(
                "/api/disputes/PP-R-PND-10195282/submit-defense",
                headers=merchant_headers
            )
            assert res.status_code == 403
            assert "Live PayPal evidence submission is disabled by default for safety" in res.json()["detail"]
    finally:
        settings.ENABLE_LIVE_PAYPAL_SUBMISSION = orig


@pytest.mark.asyncio
async def test_demo_dispute_end_to_end_simulation(test_client, db_session, merchant_headers):
    # Seed DEMO-INR-FULFILLED with fulfillment
    demo_disp = Dispute(
        dispute_id="DEMO-INR-FULFILLED",
        merchant_id="merchant_default",
        transaction_id="TX-DEMO-001",
        seller_transaction_id="TX-DEMO-001",
        buyer_transaction_id="TX-DEMO-001",
        dispute_amount=Decimal("49.99"),
        currency="USD",
        reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
        status="WAITING_FOR_SELLER_RESPONSE",
        external_status="WAITING_FOR_SELLER_RESPONSE",
        workflow_status="PENDING_ACTION",
        lifecycle_stage="INQUIRY"
    )
    db_session.add(demo_disp)
    ful = FulfillmentRecord(
        fulfillment_id="ful_demo_test",
        transaction_id="TX-DEMO-001",
        merchant_id="merchant_default",
        fulfillment_type="DIGITAL_LICENSE",
        recipient_email="demo@example.com",
        license_key="LIC-DEMO-9999",
        delivery_status="DELIVERED",
        validation_source="INDEPENDENT_AUDIT",
        document_hash="b" * 64,
        provenance="SYNTHETIC_DEMO"
    )
    db_session.add(ful)
    await db_session.commit()

    # 1. Synthesize - must be explicitly labeled as demonstration fixture
    synth_res = await test_client.post("/api/disputes/DEMO-INR-FULFILLED/synthesize")
    assert synth_res.status_code == 200
    dossier = synth_res.json()
    assert dossier["provenance_summary"]["dispute_record"] == "SYNTHETIC_DEMO"
    assert dossier["provenance_summary"]["transaction_record"] == "SYNTHETIC_DEMO"
    assert dossier["provenance_summary"]["fulfillment_evidence"] == "SYNTHETIC_DEMO"
    assert "DEMONSTRATION FIXTURE" in dossier["formal_defense_narrative"]

    # 2. Approve
    appr_res = await test_client.post("/api/disputes/DEMO-INR-FULFILLED/approve-evidence", headers=merchant_headers)
    assert appr_res.status_code == 200

    # 3. Submit defense (simulated PayPal execution - strictly intercepted, never calls live PayPal)
    sub_res = await test_client.post("/api/disputes/DEMO-INR-FULFILLED/submit-defense", headers=merchant_headers)
    assert sub_res.status_code == 200
    assert sub_res.json()["mode"] == "DEMO_SIMULATION"
    assert sub_res.json()["dispute_status"] == "UNDER_REVIEW"

    # 4. Adjudicate (simulated resolution)
    adj_res = await test_client.post(
        "/api/adjudication/DEMO-INR-FULFILLED/resolve?outcome=SELLER_FAVOR",
        headers=merchant_headers
    )
    assert adj_res.status_code == 200
    assert adj_res.json()["mode"] == "DEMO_SIMULATION"
    assert adj_res.json()["dispute_status"] == "RESOLVED"


@pytest.mark.asyncio
async def test_submit_defense_dry_run_mode(test_client, db_session, merchant_headers):
    # Seed verified fulfillment, synthesize, and record merchant approval
    await seed_verified_fulfillment(db_session)
    await test_client.post("/api/disputes/PP-R-PND-10195282/synthesize")
    appr_res = await test_client.post("/api/disputes/PP-R-PND-10195282/approve-evidence", headers=merchant_headers)
    assert appr_res.status_code == 200
    content_hash = appr_res.json()["content_hash"]

    # Mock PayPal live response for get_dispute
    mock_paypal_details = {
        "dispute_id": "PP-R-PND-10195282",
        "status": "WAITING_FOR_SELLER_RESPONSE",
        "dispute_life_cycle_stage": "INQUIRY",
        "evidences": [
            {"date": "2026-10-30T11:27:23.941Z", "evidence_type": "PROOF_OF_FULFILLMENT", "source": "REQUESTED_FROM_SELLER"}
        ],
        "evidence_requests": [],
        "links": [
            {"href": "https://api-m.sandbox.paypal.com/v1/customer/disputes/PP-R-PND-10195282/provide-evidence", "method": "POST", "rel": "provide_evidence"}
        ]
    }

    with patch("backend.app.api.disputes.paypal_client.get_dispute", return_value=mock_paypal_details) as mock_get, \
         patch("backend.app.api.disputes.paypal_client.provide_evidence") as mock_provide:
        dry_run_res = await test_client.post(
            "/api/disputes/PP-R-PND-10195282/submit-defense?dry_run=true",
            headers=merchant_headers
        )
        assert dry_run_res.status_code == 200
        data = dry_run_res.json()
        assert data["status"] == "DRY_RUN_SUCCESS"
        assert data["mode"] == "DRY_RUN"
        assert data["approval_hash"] == content_hash
        assert data["validation_summary"]["status_gate"] == "PASS (WAITING_FOR_SELLER_RESPONSE)"
        assert data["validation_summary"]["hateoas_gate"] == "PASS (provide_evidence action link available)"
        assert data["validation_summary"]["fulfillment_gate"] == "PASS (fulfillment records verified)"
        assert "submission_payload_preview" in data
        assert data["submission_payload_preview"]["evidence_type"] == "PROOF_OF_FULFILLMENT"

        # Crucial check: live provide_evidence was NEVER called during dry run
        mock_provide.assert_not_called()
        mock_get.assert_called_once()

    # Dispute status in DB must remain WAITING_FOR_SELLER_RESPONSE (not prematurely changed)
    disp = await db_session.get(Dispute, "PP-R-PND-10195282")
    assert disp.status == "WAITING_FOR_SELLER_RESPONSE"
    assert disp.workflow_status == "VALIDATED_READY_TO_SUBMIT"

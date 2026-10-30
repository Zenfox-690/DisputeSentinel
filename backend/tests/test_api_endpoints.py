import pytest
import json
from decimal import Decimal
from unittest.mock import patch
from backend.app.config import settings
from backend.app.models import Dispute, EvidencePackage, MerchantApproval
from backend.app.services.auth import compute_evidence_hash


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

    # Test synthesis for pre-seeded live dispute
    synth_res = await test_client.post("/api/disputes/PP-R-PND-10195282/synthesize")
    assert synth_res.status_code == 200
    dossier = synth_res.json()
    assert dossier["compelling_evidence_factor"] == "VISA_CE3.0_MULTI_FACTOR_MATCH"
    assert len(dossier["historical_matches"]) >= 2
    assert dossier["confidence_score"] >= 0.90


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
async def test_submit_defense_rejected_on_missing_hateoas_link(test_client, merchant_headers):
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
async def test_submit_defense_rejected_on_mismatched_evidence_type(test_client, merchant_headers):
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
    # Step 1: Synthesize
    await test_client.post("/api/disputes/PP-R-PND-10195282/synthesize")

    # Step 2: Approve server-side
    appr_res = await test_client.post(
        "/api/disputes/PP-R-PND-10195282/approve-evidence",
        headers=merchant_headers
    )
    assert appr_res.status_code == 200
    assert appr_res.json()["status"] == "APPROVED"

    # Step 3: Submit defense
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

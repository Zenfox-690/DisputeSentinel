import hmac
import hashlib
import json
import base64
import time
import secrets
from typing import Optional, Dict, Any, Set, List
from fastapi import Header, HTTPException, status
from pydantic import BaseModel

from backend.app.config import settings


class MerchantContext(BaseModel):
    merchant_id: str
    email: str
    role: str = "merchant"


# In-memory revocation registry for invalidated token JTIs
REVOKED_TOKEN_JTIS: Set[str] = set()

# Development merchant fixtures (STRICTLY prohibited when PAYPAL_ENV == "production")
DEV_MERCHANTS: Dict[str, Dict[str, str]] = {
    "merchant_default": {
        "email": "sb-zpxtx53591626@business.example.com",
        "token": "merchant_token_default",
        "role": "merchant"
    },
    "merchant_secondary": {
        "email": "secondary@merchant.example.com",
        "token": "token_merchant_secondary",
        "role": "merchant"
    }
}


def mint_merchant_token(
    merchant_id: str,
    email: str,
    role: str = "merchant",
    ttl_seconds: int = 3600,
    secret_key: Optional[str] = None
) -> str:
    """
    Mints a cryptographically signed HMAC-SHA256 merchant bearer token
    with subject, email, role, issuance, expiration, and unique JTI.
    """
    secret = (secret_key or settings.AUTH_SECRET_KEY).encode("utf-8")
    now = int(time.time())
    payload = {
        "sub": merchant_id,
        "email": email,
        "role": role,
        "iat": now,
        "exp": now + ttl_seconds,
        "jti": secrets.token_hex(16)
    }
    payload_json = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_json).decode("ascii").rstrip("=")
    sig = hmac.new(secret, payload_b64.encode("ascii"), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")
    return f"{payload_b64}.{sig_b64}"


def verify_signed_token(token: str, secret_key: Optional[str] = None) -> Dict[str, Any]:
    """
    Verifies token signature, expiration, and revocation status.
    Returns decoded claims payload.
    """
    if "." not in token:
        raise ValueError("Malformed token format.")

    payload_b64, sig_b64 = token.split(".", 1)
    secret = (secret_key or settings.AUTH_SECRET_KEY).encode("utf-8")

    expected_sig = hmac.new(secret, payload_b64.encode("ascii"), hashlib.sha256).digest()
    # Add padding back if necessary
    pad = len(sig_b64) % 4
    padded_sig_b64 = sig_b64 + ("=" * (4 - pad) if pad else "")
    actual_sig = base64.urlsafe_b64decode(padded_sig_b64)

    if not hmac.compare_digest(expected_sig, actual_sig):
        raise ValueError("Invalid cryptographic token signature.")

    pad_p = len(payload_b64) % 4
    padded_payload_b64 = payload_b64 + ("=" * (4 - pad_p) if pad_p else "")
    payload_bytes = base64.urlsafe_b64decode(padded_payload_b64)
    payload = json.loads(payload_bytes.decode("utf-8"))

    # Expiration check
    if time.time() > payload.get("exp", 0):
        raise ValueError("Merchant token has expired.")

    # Revocation check
    if payload.get("jti") in REVOKED_TOKEN_JTIS:
        raise ValueError("Merchant token has been revoked.")

    return payload


def revoke_token(jti: str) -> None:
    """Revokes a signed token by its unique identifier."""
    REVOKED_TOKEN_JTIS.add(jti)


def compute_canonical_submission_hash(
    dispute_id: str,
    merchant_id: str,
    evidence_type: str,
    narrative_text: str,
    carrier_name: Optional[str] = None,
    tracking_number: Optional[str] = None,
    delivery_date: Optional[str] = None,
    compelling_evidence_factor: Optional[str] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    Computes a canonical, deterministic SHA-256 fingerprint of the complete submission-relevant payload.
    Guarantees:
    - Normalizes newlines and whitespace.
    - Sorts attachments deterministically by attachment_id and sha256_hash (reorder-invariant).
    - Incorporates dispute_id and merchant_id (cannot be reused across disputes or merchants).
    - Covers all submitted metadata fields and cryptographic attachment hashes.
    """
    canonical_attachments = []
    if attachments:
        for att in attachments:
            canonical_attachments.append({
                "attachment_id": str(att.get("attachment_id", "")).strip(),
                "filename": str(att.get("filename", "")).strip(),
                "mime_type": str(att.get("mime_type", "")).strip().lower(),
                "sha256_hash": str(att.get("sha256_hash", "")).strip().lower(),
                "size_bytes": int(att.get("size_bytes", 0)),
            })
    # Deterministic canonical sort by attachment_id and sha256_hash
    canonical_attachments.sort(key=lambda x: (x["attachment_id"], x["sha256_hash"]))

    canonical_payload = {
        "dispute_id": dispute_id.strip(),
        "merchant_id": merchant_id.strip(),
        "evidence_type": evidence_type.strip(),
        "narrative_text": narrative_text.strip().replace("\r\n", "\n"),
        "carrier_name": (carrier_name or "").strip(),
        "tracking_number": (tracking_number or "").strip(),
        "delivery_date": (delivery_date or "").strip(),
        "compelling_evidence_factor": (compelling_evidence_factor or "").strip(),
        "attachments": canonical_attachments,
    }

    # Deterministic compact JSON
    encoded_bytes = json.dumps(canonical_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded_bytes).hexdigest()


# Backward-compatible alias
compute_evidence_hash = compute_canonical_submission_hash


async def get_current_merchant(
    authorization: Optional[str] = Header(None),
    x_merchant_id: Optional[str] = Header(None)
) -> MerchantContext:
    """
    Authenticates and identifies the merchant strictly from verified credentials.
    Derives merchant identity from token claims, rejecting client-side header spoofing.
    Blocks development tokens when running in production environment.
    """
    token = None
    if authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1]
        elif len(parts) == 1:
            token = parts[0]

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Missing Bearer token."
        )

    # 1. Production safety: reject dev tokens if in production
    is_prod = settings.PAYPAL_ENV.lower() == "production"
    dev_token_match = next((m_id for m_id, m in DEV_MERCHANTS.items() if m.get("token") == token), None)

    if dev_token_match:
        if is_prod:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Development merchant tokens are strictly prohibited in production environment."
            )
        verified_merchant_id = dev_token_match
        verified_email = DEV_MERCHANTS[dev_token_match]["email"]
        verified_role = DEV_MERCHANTS[dev_token_match]["role"]
    else:
        # 2. Verify signed token
        try:
            claims = verify_signed_token(token)
            verified_merchant_id = claims["sub"]
            verified_email = claims.get("email", "unknown@merchant.example.com")
            verified_role = claims.get("role", "merchant")
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Authentication failed: {str(e)}"
            )

    # 3. Role check
    if verified_role != "merchant":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden. Merchant privileges required."
        )

    # 4. Anti-spoofing check: If X-Merchant-Id is passed, it MUST match verified credential
    if x_merchant_id and x_merchant_id != verified_merchant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Caller identity mismatch: X-Merchant-Id '{x_merchant_id}' does not match authenticated credential."
        )

    return MerchantContext(
        merchant_id=verified_merchant_id,
        email=verified_email,
        role=verified_role
    )

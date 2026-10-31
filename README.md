# DisputeSentinel AI 🛡️
### Autonomous Chargeback Defense & Visa Compelling Evidence 3.0 (CE3.0) Platform for PayPal Merchants

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.13](https://img.shields.io/badge/Python-3.13-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](https://fastapi.tiangolo.com)
[![AG Grid](https://img.shields.io/badge/AG_Grid-Community-e0234e.svg)](https://www.ag-grid.com/)
[![React 19](https://img.shields.io/badge/React-19-61dafb.svg)](https://react.dev/)
[![PayPal REST API](https://img.shields.io/badge/PayPal-Disputes_API_v1-003087.svg)](https://developer.paypal.com/docs/api/customer-disputes/v1/)
[![Tests Passing](https://img.shields.io/badge/Tests-59%2F59%20Passing-brightgreen.svg)](backend/tests)

---

## 1. Executive Summary & The Problem

Chargebacks, first-party friendly fraud, and disputed non-receipt claims inflict over **$100 billion in merchant losses annually**. When a cardholder repudiates a genuine purchase or disputes a digital delivery, merchants face severe structural operational challenges:
- **Strict Response Windows:** Merchants face 10-to-20 day deadlines to compile multi-factor evidentiary dossiers or suffer automatic claim forfeiture.
- **Card Scheme & Workflow Bifurcation:** Non-receipt claims (Visa Reason 13.1 / PayPal `MERCHANDISE_OR_SERVICE_NOT_RECEIVED`) demand strict **Proof of Fulfillment** (license dispatch, delivery logs, access timestamps). Conversely, Card-Absent Fraud claims (Visa Reason 10.4) require **Visa Compelling Evidence 3.0 (CE3.0)** correlation across historical undisputed transactions. Blurring these legal standards causes immediate issuer rejection.
- **LLM Hallucination Risk:** Generative AI models without deterministic guardrails risk fabricating carrier tracking numbers, inventing license keys, or citing synthetic demo fixtures as verified evidence, permanently compromising merchant credibility with payment processors.

### The Solution: DisputeSentinel AI
**DisputeSentinel AI** is an enterprise chargeback defense copilot that connects directly to the **PayPal Disputes API (`/v1/customer/disputes`)**. It enforces strict cryptographic evidence provenance, deterministic safety gates, and authenticated merchant authorization before any payload touches PayPal's network.

---

## 2. Core Architecture & System Flow

```mermaid
flowchart TD
    subgraph PayPal["PayPal Ecosystem"]
        A[Inbound Dispute] -->|Webhook Notification| WH[POST /api/webhooks/paypal]
        B[PayPal REST API v1] <-->|GET /v1/customer/disputes| SYNC[Dispute Synchronizer]
        SUB[POST /v1/customer/disputes/{id}/provide-evidence] <--> ADJ[POST .../adjudicate]
    end

    subgraph IngestionSec["Security & Verification Layer"]
        WH --> V_SIG{HMAC/RSA Postback Valid?}
        V_SIG -->|No| R_401[401 Reject / Fail-Closed]
        V_SIG -->|Yes| V_IDEMP{Transmission ID Seen?}
        V_IDEMP -->|Duplicate| R_ALREADY[200 ALREADY_PROCESSED]
        V_IDEMP -->|New| DISP_DB[(Dispute Ledger)]
        SYNC --> DISP_DB
    end

    subgraph ProvenanceEngine["Evidentiary Provenance & Fulfillment Layer"]
        DISP_DB --> DUAL_ID[Dual Transaction ID Resolver<br/>Seller 6EH... / Buyer 33M...]
        DUAL_ID --> REASON_BRANCH{Dispute Reason?}
        REASON_BRANCH -->|INR / Reason 13.1| FUL_CHECK{Merchant Fulfillment Record?}
        FUL_CHECK -->|Missing| INSUFFICIENT[Status: INSUFFICIENT_EVIDENCE<br/>Confidence: 15% / Block Submission]
        FUL_CHECK -->|Attested / Manual| ATTESTED[Status: MERCHANT_ATTESTED<br/>Confidence: 72% / Review Required]
        FUL_CHECK -->|Verified Third-Party| VERIFIED[Status: VERIFIED_MERCHANT<br/>Confidence: 88% / SELLER_FAVOR]
        REASON_BRANCH -->|Fraud / Reason 10.4| CE30_ENGINE[Visa CE3.0 Correlation Engine<br/>Match IP / Device / Address]
    end

    subgraph Gates["5 Deterministic Validation Gates"]
        VERIFIED --> G1[Gate 1: Transaction & Provenance Invariance]
        ATTESTED --> G1
        CE30_ENGINE --> G1
        G1 --> G2[Gate 2: Courier Tracking Syntax Regex]
        G2 --> G3[Gate 3: Mathematical Cent-Precision Check]
        G3 --> G4[Gate 4: Prompt Injection & Legal Claim Sanitizer]
        G4 --> G5[Gate 5: Human Approval & Completeness Threshold]
    end

    subgraph ApprovalAndSubmission["Merchant Review & Submission Pipeline"]
        G5 --> CANON_HASH[Canonical SHA-256 Payload Hash]
        CANON_HASH --> APPR_REC[(MerchantApproval Ledger)]
        APPR_REC --> SUB_GATE{Safety Lock & Gate Validation}
        SUB_GATE -->|dry_run=true| DRY_RES[DRY_RUN_SUCCESS Preview]
        SUB_GATE -->|Demo Scenario| DEMO_SIM[Simulated PayPal Acceptance]
        SUB_GATE -->|Live & Enabled| SUB
    end
```

---

## 3. Evidence Provenance & Evidentiary Integrity

DisputeSentinel establishes an immutable provenance audit trail for every piece of data cited in an evidence package:

| Provenance Label | Source & Classification Rules | Allowed in Live Submission? |
| :--- | :--- | :---: |
| **`VERIFIED_EXTERNAL`** | Retrieved authoritatively from external PayPal REST API endpoints or signed webhook events. | **Yes** |
| **`VERIFIED_MERCHANT`** | Independently validated merchant records backed by third-party verification (e.g. `CARRIER_API`, `INDEPENDENT_AUDIT`) and an intact SHA-256 document hash. | **Yes** |
| **`MERCHANT_ATTESTED`** | Directly entered by merchant without external cryptographic or third-party validation. Requires explicit human review before submission. | **Yes (After Review)** |
| **`MANUAL_ENTRY`** | Manually entered record. Automatically downgraded from any unverified claims of external validation. | **Yes (After Review)** |
| **`SYNTHETIC_DEMO`** | Synthetic demonstration fixtures and sample telemetry. Strictly blocked by Gate 1 from being submitted to PayPal as verified evidence. | **No (Disqualified)** |

---

## 4. The 5 Deterministic Validation Gates

To ensure complete legal and mathematical fidelity and eliminate generative AI hallucinations before payloads ever touch payment processors, every dossier must clear 5 deterministic gates (`backend/app/services/validation_gates.py`):

| Gate | Validation Enforcement | Violation Result |
| :--- | :--- | :--- |
| **Gate 1: Transaction Existence & Provenance** | Asserts every transaction referenced in the brief exists in local telemetry and is not tagged `SYNTHETIC_DEMO`. | Rejection: Blocks fabricated transaction IDs and demo fixtures. |
| **Gate 2: Carrier Tracking Conformance** | Syntactically validates courier tracking numbers against standard formats (UPS `1Z...`, FedEx 12-15 digits, USPS, DHL). | Rejection: Blocks invalid or fabricated tracking numbers. |
| **Gate 3: Financial Precision & Invariance** | Confirms rebuttal amount matches the exact disputed sum down to the cent. | Rejection: Prevents numeric discrepancies. |
| **Gate 4: Injection & Legal Claim Firewall** | Scans narrative for prompt injection patterns and sanitizes unauthorized CE3.0 liability shift claims on INR disputes. | Rejection: Blocks prompt injection and improper legal claims. |
| **Gate 5: Merchant Review & Completeness** | Enforces human-in-the-loop review whenever completeness falls below 85% or evidence is merchant-attested. | Routing: Shifts to `REVIEW_REQUIRED`. |

---

## 5. Technology Stack & Architecture

- **Backend:** Python 3.13, FastAPI (Async ASGI), SQLAlchemy 2.0 (Async), SQLite / aiosqlite, Pydantic v2.
- **Frontend:** React 19, TypeScript, AG Grid Community (Quartz Dark Theme), Vite 8 with Rolldown code-splitting.
- **Authentication & Authorization:** HMAC-SHA256 signed Bearer tokens with expiration, revocation ledger, and tenant isolation (`X-Merchant-Id`).
- **Security:** Fail-closed webhook signature verification (`verify-webhook-signature`), dual transaction ID correlation, and canonical SHA-256 evidence approval hashes.

---

## 6. Environment Configuration (`.env`)

DisputeSentinel uses environment variables managed by Pydantic Settings (`backend/app/config.py`). Copy `.env.example` to `.env` to configure your local environment:

```ini
# PayPal Sandbox Credentials (leave blank for offline / local simulation testing)
PAYPAL_ENV=sandbox
PAYPAL_CLIENT_ID=
PAYPAL_CLIENT_SECRET=
PAYPAL_WEBHOOK_ID=

# Safety Guard: Keep false by default to prevent accidental live submissions
ENABLE_LIVE_PAYPAL_SUBMISSION=false

# AI Model Provider (Google Gemini or Groq Cloud)
AI_PROVIDER=gemini # or groq
GEMINI_API_KEY=your_gemini_api_key
GROQ_API_KEY=your_groq_api_key

# Infrastructure Settings
PORT=8000
DATABASE_URL=sqlite+aiosqlite:///./disputesentinel.db
CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000
CONFIDENCE_THRESHOLD=0.85
MAX_AUTO_SUBMIT_AMOUNT=500.00
AUTH_SECRET_KEY=dev-disputesentinel-hmac-secret-key-32b
```

---

## 7. Local Setup & Quickstart

### Prerequisites
- Python 3.11+ (Python 3.13 tested)
- Node.js 18+ and npm

### 1. Backend Setup
```bash
# Clone the repository
git clone https://github.com/your-username/DisputeSentinel.git
cd DisputeSentinel

# Install Python dependencies
pip install -r backend/requirements.txt # or install fastapi uvicorn sqlalchemy aiosqlite pydantic-settings httpx greenlet pytest pytest-asyncio

# Copy environment configuration
cp .env.example .env

# Launch Backend Server
uvicorn backend.app.main:app --port 8000 --reload
```
*Backend runs on `http://localhost:8000`. Swagger API documentation available at `http://localhost:8000/docs`.*

### 2. Frontend Setup
```bash
cd frontend
npm install
npm run dev -- --port 3000
```
*Merchant Command Center opens at `http://localhost:3000`.*

### 3. Run Automated Test Suite
```bash
python -m pytest -v
```
*59/59 tests pass in $<5$ seconds verifying CE3.0 correlation, validation gates, merchant approval integrity, and webhook security with isolated in-memory SQLite databases.*

---

## 8. REST API Reference

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/health` | System health check, environment status, and active AI provider. |
| `GET` | `/api/disputes` | List all tracked dispute records with counterparty IDs and statuses. |
| `POST` | `/api/disputes/sync` | Synchronize live dispute records directly from PayPal REST API. |
| `POST` | `/api/disputes/{id}/synthesize` | Run CE3.0 engine / Proof of Fulfillment synthesizer and compile brief. |
| `POST` | `/api/disputes/{id}/fulfillment` | Record authentic merchant fulfillment proof (license keys, tracking logs). |
| `POST` | `/api/disputes/{id}/approve-evidence` | Cryptographically record server-side merchant approval with SHA-256 hash. |
| `POST` | `/api/disputes/{id}/submit-defense` | Validate gates and submit defense to PayPal (supports `?dry_run=true`). |
| `POST` | `/api/adjudication/{id}/resolve` | Sandbox-only programmatically resolve dispute (`SELLER_FAVOR`). |
| `POST` | `/api/webhooks/paypal` | Inbound fail-closed PayPal webhook receiver with signature verification. |

---

## 9. Interactive Demonstration Guide

DisputeSentinel includes three pre-seeded dispute scenarios designed to showcase the complete defense lifecycle without modifying real disputes:

### Scenario A: Fully Verified Offline INR Demonstration & 6-Gate Dry-Run (`DEMO-INR-FULFILLED`)
1. Click **Inspect** on `DEMO-INR-FULFILLED` ($49.99 USD) in the AG Grid ledger.
2. Pre-seeded with verified digital license fulfillment (`LIC-SENTINEL-DEMO-9944`) and audit document hash.
3. Click **Dry-Run Gate Check**: Runs the complete verification pipeline locally. All 6 validation gates pass (dispute status, HATEOAS action links, evidence types, server-side merchant approval, zero synthetic citations, fulfillment integrity) without sending any network requests to PayPal.
4. Click **Submit Demo Defense**: Executes simulated PayPal submission, transitioning status to `UNDER_REVIEW`.
5. Click **Adjudicate Dispute (Sandbox Test)**: Resolves the dispute in `SELLER_FAVOR`, releasing simulated held funds.

### Scenario B: The Live PayPal Sandbox INR Dispute (`PP-R-PND-10195282`)
> [!NOTE] Testing this live Sandbox scenario requires configuring active PayPal Sandbox credentials (`PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET`) in `.env`, because live dispute retrieval and HATEOAS link resolution require authenticating against PayPal REST API (`/v1/customer/disputes/{id}`).
1. Click **Inspect** on `PP-R-PND-10195282` in the AG Grid ledger.
2. Observe dispute details retrieved from PayPal: Item Not Received, $10.00 USD, `WAITING_FOR_SELLER_RESPONSE`.
3. **Honest Insufficient Evidence Demonstration:** Observe that the system flags `INSUFFICIENT_EVIDENCE` (15% completeness) because no fulfillment record exists. Live submission and dry-run are locked.
4. Click **Attach Fulfillment Proof**, enter license key `LIC-SENTINEL-VERIFIED-2026`, select `INDEPENDENT_AUDIT`, and save.
5. The dossier immediately re-synthesizes to `VERIFIED` (88% completeness), unlocking **Dry-Run Gate Check**.
6. When PayPal Sandbox credentials are configured, clicking **Dry-Run Gate Check** queries live HATEOAS action links and validates all 6 gates without mutating PayPal state. Actual live submission remains strictly protected server-side by the safety lock (`ENABLE_LIVE_PAYPAL_SUBMISSION=false`).

### Scenario C: Visa CE3.0 Fraud Correlation Demonstration (`DEMO-CE30-FRAUD`)
1. Click **Inspect** on `DEMO-CE30-FRAUD` ($129.00 USD, Reason: `UNAUTHORIZED_TRANSACTION`).
2. Demonstrates the **Visa CE3.0 Backward Correlation Engine**: Correlates the disputed purchase against historical undisputed checkouts (`TX-HIST-2026-04-12` and `TX-HIST-2026-02-15`) matching customer IP and device fingerprints.
3. Demonstrates **Gate 1 Provenance Enforcement**: Transparently alerts the operator that synthetic demo fixtures cannot be submitted to PayPal as verified evidence.

---

## 10. Security & Limitations

- **Sandbox Adjudication Guard:** Adjudication (`/api/adjudication/...`) is strictly restricted to the Sandbox environment. Executions in `production`, `staging`, or `live` environments are rejected immediately with HTTP 403.
- **Live Submission Safety Lock:** Actual live evidence submissions to PayPal are disabled by default (`ENABLE_LIVE_PAYPAL_SUBMISSION=false`). Operators must explicitly opt in via configuration and the UI safety toggle.
- **Cryptographic Approval Parity:** Client-side flags (`merchant_approved=true`) cannot authorize submissions. Approval is verified server-side against the canonical SHA-256 hash of the entire payload.
- **Fail-Closed Webhooks:** Unsigned or forged webhook events are rejected with HTTP 401 before any database operations or state transitions can occur.

---

## 11. License

This project is licensed under the **MIT License** — see the [LICENSE](LICENSE) file for details.

import pytest
from httpx import AsyncClient, ASGITransport
from backend.app.main import app


@pytest.mark.asyncio
async def test_health_endpoint():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "HEALTHY"
        assert data["platform"] == "DisputeSentinel AI"


@pytest.mark.asyncio
async def test_disputes_list_and_synthesize():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get("/api/disputes")
        assert res.status_code == 200
        disputes = res.json()
        assert isinstance(disputes, list)

        # Test synthesis for pre-seeded live dispute
        synth_res = await ac.post("/api/disputes/PP-R-PND-10195282/synthesize")
        assert synth_res.status_code == 200
        dossier = synth_res.json()
        assert dossier["compelling_evidence_factor"] == "VISA_CE3.0_MULTI_FACTOR_MATCH"
        assert len(dossier["historical_matches"]) >= 2
        assert dossier["confidence_score"] >= 0.90
        assert "CE3.0" in dossier["formal_defense_narrative"] or "compelling evidence" in dossier["formal_defense_narrative"].lower()


@pytest.mark.asyncio
async def test_full_defense_and_adjudication_flow():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # Step 1: Synthesize
        synth_res = await ac.post("/api/disputes/PP-R-PND-10195282/synthesize")
        assert synth_res.status_code == 200

        # Step 2: Submit defense to PayPal
        submit_res = await ac.post("/api/disputes/PP-R-PND-10195282/submit-defense")
        assert submit_res.status_code == 200
        sub_data = submit_res.json()
        assert sub_data["status"] == "SUCCESS"
        assert sub_data["dispute_status"] == "UNDER_REVIEW"

        # Step 3: Programmatic sandbox adjudication
        adj_res = await ac.post("/api/adjudication/PP-R-PND-10195282/resolve", params={"outcome": "SELLER_FAVOR"})
        assert adj_res.status_code == 200
        adj_data = adj_res.json()
        assert adj_data["status"] == "SUCCESS"
        assert adj_data["adjudication_outcome"] == "SELLER_FAVOR"
        assert adj_data["dispute_status"] == "RESOLVED"

"""Hosted-deployment guards: passcode, daily run limit, credit ceiling cap, Vertex selection."""
from fastapi.testclient import TestClient
import pytest
from app.main import create_app
from app.persistence.database import Database
from app.providers.gemini import GeminiClient
from app.providers.interfaces import IntegrationUnavailable

BODY = {"text": "Source ESP32-S3 modules, 500 of each", "category": "electronics", "request_type": "purchase",
        "destination": "Bengaluru", "deadline": "2030-12-30T00:00:00Z", "research_budget_minor": 200000,
        "approval_threshold_minor": 10000000, "mode": "fixture", "scenario": "normal", "specs": [],
        "llm_enabled": False, "credit_budget": 10}


@pytest.fixture
def hosted(tmp_path, monkeypatch):
    monkeypatch.setenv("ACCESS_CODE", "s3cret")
    monkeypatch.setenv("MAX_RUNS_PER_DAY", "1")
    monkeypatch.setenv("MAX_CREDIT_BUDGET", "25")
    with TestClient(create_app(Database(str(tmp_path / "w.db")))) as client:
        yield client


def test_passcode_guards_api_but_not_health(hosted):
    assert hosted.get("/api/health").status_code == 200
    assert hosted.get("/api/runs").status_code == 401
    assert hosted.get("/api/runs", headers={"X-Access-Code": "wrong"}).status_code == 401
    assert hosted.get("/api/runs", headers={"X-Access-Code": "s3cret"}).status_code == 200


def test_caps_limit_spend(hosted):
    auth = {"X-Access-Code": "s3cret"}
    assert hosted.post("/api/runs", json=BODY | {"credit_budget": 100}, headers=auth).status_code == 409
    created = hosted.post("/api/runs", json=BODY, headers=auth)
    assert created.status_code == 201
    run_id = created.json()["id"]
    assert hosted.patch(f"/api/runs/{run_id}/budget", json={"credit_budget": 50}, headers=auth).status_code == 409
    assert hosted.post("/api/runs", json=BODY, headers=auth).status_code == 429


def test_no_guards_when_unconfigured(tmp_path, monkeypatch):
    for name in ["ACCESS_CODE", "MAX_RUNS_PER_DAY", "MAX_CREDIT_BUDGET"]:
        monkeypatch.delenv(name, raising=False)
    with TestClient(create_app(Database(str(tmp_path / "w.db")))) as client:
        assert client.get("/api/runs").status_code == 200


def test_vertex_only_allows_gemini_models(tmp_path):
    with pytest.raises(IntegrationUnavailable, match="GEMINI_ONLY"):
        GeminiClient(model="claude-sonnet", backend="vertex", vertex_client=object(), journal_path=tmp_path / "l.db")
    client = GeminiClient(backend="vertex", vertex_client=object(), journal_path=tmp_path / "l.db")
    assert client.model == "gemini-2.5-flash"

from fastapi.testclient import TestClient

from app.main import app


def test_healthz_reports_status_and_corpus_state():
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "corpus_loaded" in body
    assert isinstance(body["versions"], int)

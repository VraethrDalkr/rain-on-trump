# tests/test_plane_state_endpoint.py
from fastapi.testclient import TestClient

from app.main import app

cli = TestClient(app)


def test_plane_state_endpoint():
    r = cli.get("/plane_state.json")
    assert r.status_code == 200
    payload = r.json()
    assert "source" in payload
    assert "state" in payload

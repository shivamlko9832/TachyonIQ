"""
Integration tests for the /analyses REST API (P3-4).

Uses FastAPI's TestClient with an in-memory SavedAnalysisStore so no disk
files are created and tests are fully isolated.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path):
    """Return a TestClient wired to a temp-file SavedAnalysisStore.

    Passes a MagicMock orchestrator so the lifespan hook skips the full
    pipeline bootstrap (which requires sentence-transformers). The analyses
    routes never touch the orchestrator.

    Note: SavedAnalysisStore uses sqlite3.connect() per call, which creates
    a separate in-memory DB for each connection — so we use a real temp file
    (via pytest tmp_path) that all connections share, then clean it up
    automatically when the fixture scope ends.
    """
    from unittest.mock import MagicMock

    from uada.api.app import create_app
    from uada.db.saved_analysis_store import SavedAnalysisStore

    db_file = tmp_path / "test_analyses.db"
    app = create_app(orchestrator=MagicMock())
    store = SavedAnalysisStore(str(db_file))

    with TestClient(app) as c:
        # Override AFTER lifespan runs; lazy dep otherwise creates one on disk
        app.state.saved_analysis_store = store
        yield c


def _save_payload(**overrides) -> dict:
    base = {
        "name": "Test Analysis",
        "question": "What was total revenue?",
        "sql_text": "SELECT SUM(amount) FROM orders",
        "response": {"answer": "£100", "session_id": "s1", "turn_id": 1},
        "connection_id": "conn-abc",
        "tags": ["revenue", "q1"],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# POST /analyses
# ---------------------------------------------------------------------------

class TestSaveAnalysis:
    def test_save_returns_201(self, client):
        resp = client.post("/analyses", json=_save_payload())
        assert resp.status_code == 201

    def test_save_returns_id_and_name(self, client):
        resp = client.post("/analyses", json=_save_payload(name="Revenue Q1"))
        body = resp.json()
        assert "id" in body
        assert body["name"] == "Revenue Q1"
        assert body["id"]  # non-empty

    def test_save_without_optional_fields(self, client):
        payload = {
            "name": "Minimal",
            "question": "How many users?",
            "response": {"answer": "42"},
        }
        resp = client.post("/analyses", json=payload)
        assert resp.status_code == 201

    def test_save_missing_required_name_returns_422(self, client):
        payload = {"question": "Q?", "response": {}}
        resp = client.post("/analyses", json=payload)
        assert resp.status_code == 422

    def test_save_missing_required_question_returns_422(self, client):
        payload = {"name": "N", "response": {}}
        resp = client.post("/analyses", json=payload)
        assert resp.status_code == 422

    def test_save_missing_required_response_returns_422(self, client):
        payload = {"name": "N", "question": "Q?"}
        resp = client.post("/analyses", json=payload)
        assert resp.status_code == 422

    def test_empty_name_returns_422(self, client):
        resp = client.post("/analyses", json=_save_payload(name=""))
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /analyses
# ---------------------------------------------------------------------------

class TestListAnalyses:
    def test_empty_list_initially(self, client):
        resp = client.get("/analyses")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 0
        assert body["analyses"] == []

    def test_lists_saved_analysis(self, client):
        client.post("/analyses", json=_save_payload(name="Revenue Total"))
        resp = client.get("/analyses")
        body = resp.json()
        assert body["total"] == 1
        assert body["analyses"][0]["name"] == "Revenue Total"

    def test_list_returns_newest_first(self, client):
        client.post("/analyses", json=_save_payload(name="First"))
        client.post("/analyses", json=_save_payload(name="Second"))
        resp = client.get("/analyses")
        names = [a["name"] for a in resp.json()["analyses"]]
        assert names[0] == "Second"
        assert names[1] == "First"

    def test_list_filters_by_connection_id(self, client):
        client.post("/analyses", json=_save_payload(connection_id="conn-1"))
        client.post("/analyses", json=_save_payload(connection_id="conn-2"))
        resp = client.get("/analyses", params={"connection_id": "conn-1"})
        body = resp.json()
        assert all(a["connection_id"] == "conn-1" for a in body["analyses"])
        assert body["total"] == 1

    def test_list_filters_by_tag(self, client):
        client.post("/analyses", json=_save_payload(tags=["revenue", "q1"]))
        client.post("/analyses", json=_save_payload(tags=["users"]))
        resp = client.get("/analyses", params={"tag": "revenue"})
        body = resp.json()
        assert len(body["analyses"]) == 1
        assert "revenue" in body["analyses"][0]["tags"]

    def test_list_does_not_include_response_blob(self, client):
        """List endpoint should not return the heavy response field."""
        client.post("/analyses", json=_save_payload())
        resp = client.get("/analyses")
        item = resp.json()["analyses"][0]
        assert "response" not in item

    def test_list_pagination_limit(self, client):
        for i in range(5):
            client.post("/analyses", json=_save_payload(name=f"A{i}"))
        resp = client.get("/analyses", params={"limit": 2})
        assert len(resp.json()["analyses"]) == 2

    def test_list_pagination_offset(self, client):
        for i in range(4):
            client.post("/analyses", json=_save_payload(name=f"A{i}"))
        resp_all = client.get("/analyses").json()["analyses"]
        resp_offset = client.get("/analyses", params={"offset": 2}).json()["analyses"]
        assert resp_offset[0]["name"] == resp_all[2]["name"]


# ---------------------------------------------------------------------------
# GET /analyses/{id}
# ---------------------------------------------------------------------------

class TestGetAnalysis:
    def test_get_returns_200(self, client):
        save_resp = client.post("/analyses", json=_save_payload()).json()
        resp = client.get(f"/analyses/{save_resp['id']}")
        assert resp.status_code == 200

    def test_get_includes_response_blob(self, client):
        payload = _save_payload()
        aid = client.post("/analyses", json=payload).json()["id"]
        body = client.get(f"/analyses/{aid}").json()
        assert "response" in body
        assert body["response"]["answer"] == "£100"

    def test_get_includes_sql_text(self, client):
        aid = client.post("/analyses", json=_save_payload()).json()["id"]
        body = client.get(f"/analyses/{aid}").json()
        assert body["sql_text"] == "SELECT SUM(amount) FROM orders"

    def test_get_includes_tags(self, client):
        aid = client.post("/analyses", json=_save_payload(tags=["a", "b"])).json()["id"]
        body = client.get(f"/analyses/{aid}").json()
        assert body["tags"] == ["a", "b"]

    def test_get_nonexistent_returns_404(self, client):
        resp = client.get("/analyses/does-not-exist")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /analyses/{id}
# ---------------------------------------------------------------------------

class TestDeleteAnalysis:
    def test_delete_returns_200(self, client):
        aid = client.post("/analyses", json=_save_payload()).json()["id"]
        resp = client.delete(f"/analyses/{aid}")
        assert resp.status_code == 200

    def test_delete_removes_from_list(self, client):
        aid = client.post("/analyses", json=_save_payload()).json()["id"]
        client.delete(f"/analyses/{aid}")
        body = client.get("/analyses").json()
        assert body["total"] == 0

    def test_get_after_delete_returns_404(self, client):
        aid = client.post("/analyses", json=_save_payload()).json()["id"]
        client.delete(f"/analyses/{aid}")
        resp = client.get(f"/analyses/{aid}")
        assert resp.status_code == 404

    def test_delete_nonexistent_returns_404(self, client):
        resp = client.delete("/analyses/does-not-exist")
        assert resp.status_code == 404

    def test_delete_response_contains_id(self, client):
        aid = client.post("/analyses", json=_save_payload()).json()["id"]
        body = client.delete(f"/analyses/{aid}").json()
        assert body["id"] == aid

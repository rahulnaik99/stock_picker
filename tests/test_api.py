from fastapi.testclient import TestClient

from app import api

client = TestClient(api.app)


def test_api_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_api_root_serves_javascript_control_center():
    response = client.get("/")
    assert response.status_code == 200
    assert "Stock Scoring Control Center" in response.text
    assert "Start scoring run" in response.text
    assert "Recent runs" in response.text
    assert "fetch(" in response.text


def test_create_run_rejects_unknown_symbol(monkeypatch):
    def fake_load_universe(*, only=None, sub_sector=None):
        if only and "NOT_A_REAL_SYMBOL" in only:
            raise ValueError("symbols not in universe file: ['NOT_A_REAL_SYMBOL']")
        return [{"symbol": "SBIN", "company_name": "State Bank of India", "sub_sector": "bank"}]

    monkeypatch.setattr(api, "_load_universe", fake_load_universe)
    response = client.post("/api/runs", json={"symbols": ["NOT_A_REAL_SYMBOL"]})
    assert response.status_code == 422


def test_run_traces_endpoint_returns_prompt_and_response(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    run_id = "trace-test-run"
    trace_dir = tmp_path / "runs" / run_id / "traces"
    trace_dir.mkdir(parents=True)
    (trace_dir / "CANBK_fundamental.json").write_text(json.dumps({
        "provider": "openai", "model": "gpt-test", "system": "score fundamentals",
        "user": "{\"symbol\": \"CANBK\"}", "reply": "{\"reasoning\": \"positive\"}", "error": ""
    }))
    monkeypatch.setattr(api.run_store, "list_runs", lambda limit=100: [{"run_id": run_id}])
    monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(log_dir=str(tmp_path), path=lambda value: __import__("pathlib").Path(value)))

    response = client.get(f"/api/runs/{run_id}/traces?symbol=CANBK")
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["traces"][0]["system"] == "score fundamentals"
    assert data["traces"][0]["model"] == "gpt-test"


def test_dashboard_contains_agent_trace_viewer():
    response = client.get("/")
    assert response.status_code == 200
    assert "Agent Trace Viewer" in response.text
    assert "/traces?symbol=" in response.text


def test_dashboard_has_no_llm_selector_and_points_to_model_config():
    response = client.get("/")
    assert response.status_code == 200
    # The LLM provider/model picker was removed: model_config.json is the single source of truth.
    assert 'id="llmProvider"' not in response.text
    assert 'id="llmModel"' not in response.text
    assert "model_config.json" in response.text


def test_dashboard_contains_live_agent_run_and_token_calculation():
    response = client.get("/")
    assert response.status_code == 200
    assert "Live agent run" in response.text
    assert "Token calculation" in response.text
    assert "/progress" in response.text


def test_progress_endpoint_returns_live_snapshot(monkeypatch):
    snap = {"run_id": "r1", "status": "running", "agents": ["fundamental"],
            "symbols": {"SBIN": {"fundamental": {"status": "running", "input_tokens": 10, "output_tokens": 2, "score": None}}},
            "totals": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12, "agents_done": 0, "agents_total": 1, "by_model": {}}}
    monkeypatch.setattr(api.progress, "snapshot", lambda rid: snap if rid == "r1" else None)
    monkeypatch.setattr(api, "_db_progress", lambda rid: None)
    ok = client.get("/api/runs/r1/progress")
    assert ok.status_code == 200 and ok.json()["totals"]["total_tokens"] == 12
    assert ok.json()["symbols"]["SBIN"]["fundamental"]["status"] == "running"
    assert client.get("/api/runs/does-not-exist/progress").status_code == 404

import json
from pathlib import Path
from types import SimpleNamespace

from app.service import logger


def test_llm_usage_summary_logs_and_returns_model_totals(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        log_dir=str(tmp_path / "logs"),
        path=lambda value: Path(value),
    )
    monkeypatch.setattr(logger, "get_settings", lambda: settings)
    run_id = "summary-test"
    run_dir = tmp_path / "logs" / "runs"
    run_dir.mkdir(parents=True)
    (run_dir / f"{run_id}.jsonl").write_text(
        json.dumps({
            "kind": "llm_attempt", "provider": "ollama", "model": "test-model",
            "status": "ok", "input_tokens": 100, "output_tokens": 50,
            "duration_ms": 2000, "queue_ms": 500, "inference_ms": 1500,
            "token_count_source": "provider_usage",
        }) + "\n",
        encoding="utf-8",
    )

    result = logger.log_llm_usage_summary(run_id)

    model = result["ollama:test-model"]
    assert model["calls"] == 1
    assert model["input_tokens"] == 100
    assert model["output_tokens"] == 50
    assert model["tokens_per_second"] == 33.33
    assert model["exact_calls"] == 1
    assert model["estimated_calls"] == 0

"""FastAPI interface for the stock-scoring pipeline.

Start with: uvicorn app.api:app --reload
Interactive API docs: http://127.0.0.1:8000/docs
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field, field_validator

from app.service import logger
from app.service import progress
from app.service.settings import get_settings
from app.store import runs as run_store

# Keep in-memory run bookkeeping bounded so a long-lived server never leaks memory across many runs.
MAX_INMEM_RUNS = 50


def _trim_inmem() -> None:
    for store in (_run_tasks, _run_results):
        while len(store) > MAX_INMEM_RUNS:
            store.pop(next(iter(store)))


def _load_universe(*, only: list[str] | None = None, sub_sector: str | None = None) -> list[dict[str, str]]:
    # Lazy import keeps health/docs available even when optional pipeline dependencies are not installed.
    from app.graph.runner import load_universe
    return load_universe(only=only, sub_sector=sub_sector)

from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    logger.setup_logging()
    yield


app = FastAPI(
    title="Stock Scoring API",
    description=(
        "REST API for the multi-agent stock research and scoring pipeline. "
        "Runs are asynchronous; create a run, poll its status, then fetch rankings and agent traces. "
        "Research only: this API does not place orders."
    ),
    version="1.0.0",
    lifespan=_lifespan,
)


class RunRequest(BaseModel):
    symbols: list[str] | None = Field(default=None, description="Optional symbols from the configured universe, e.g. ['SBIN', 'HDFCBANK']")
    sub_sector: str | None = Field(default=None, description="Optional sub-sector prefix filter, e.g. 'bank' or 'insurance'")
    as_of: str | None = Field(default=None, description="Analysis timestamp/date in ISO format; defaults to now")
    mode: Literal["stdio", "inprocess"] | None = Field(default=None, description="MCP tool mode override")
    concurrency: int | None = Field(default=None, ge=1, le=20)
    refresh: bool = Field(default=True, description="Refresh stale local datasets before scoring")
    force_refresh: bool = Field(default=False, description="Ignore daily refresh gate")
    stub: bool = Field(default=False, description="Use fake LLMs for an offline/demo run")
    llm_provider: Literal["", "ollama", "olmx", "openai", "stub"] = Field(
        default="", description="Optional provider override for this run"
    )
    llm_model: str | None = Field(
        default=None, min_length=1, max_length=200,
        description="Optional exact model ID override; for OLMX use the model ID exposed by its server"
    )
    llm_timeout: int | None = Field(
        default=None, ge=30, le=1800,
        description="Per-agent LLM call timeout in seconds. Override LLM_TIMEOUT_SECONDS for slow/large models."
    )

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized = list(dict.fromkeys(s.strip().upper() for s in value if s.strip()))
        if not normalized:
            raise ValueError("symbols must contain at least one non-empty symbol")
        return normalized


# Runtime task state is kept in memory for polling. Completed runs and agent traces are persisted in SQLite.
_run_tasks: dict[str, dict[str, Any]] = {}
_run_results: dict[str, list[dict[str, Any]]] = {}
_active_run_id: str | None = None
_active_lock = asyncio.Lock()


def _run_payload(run_id: str, state: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "status": state.get("status", "unknown"),
        "created_at": state.get("created_at"),
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
        "scope": state.get("scope"),
        "symbols": state.get("symbols", []),
        "total": state.get("total", len(state.get("symbols", []))),
        "ranked": state.get("ranked"),
        "valid": state.get("valid"),
        "exports": state.get("exports"),
        "refresh_report": state.get("refresh_report"),
        "error": state.get("error"),
    }


async def _execute_run(run_id: str, request: RunRequest, rows: list[dict[str, str]], scope: str) -> None:
    global _active_run_id
    state = _run_tasks[run_id]
    s = get_settings()
    original_provider = s.llm_force_provider
    original_model = s.llm_force_model
    original_timeout = s.llm_timeout_seconds
    original_force_refresh = s.force_refresh
    original_force_refresh_env = os.environ.get("FORCE_REFRESH")
    state["status"] = "running"
    state["started_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        # Heavy pipeline imports stay lazy so health/docs routes can start independently.
        from app.export.writer import export
        from app.graph import runner
        from app.graph.runner import run_universe

        if request.stub:
            s.llm_force_provider = "stub"
            s.llm_force_model = None
        else:
            s.llm_force_provider = request.llm_provider or ""
            s.llm_force_model = (request.llm_model.strip() if request.llm_model else None)
        if request.llm_timeout:
            s.llm_timeout_seconds = request.llm_timeout
        if request.force_refresh:
            os.environ["FORCE_REFRESH"] = "1"
            s.force_refresh = True
        run_id_actual, ranked = await run_universe(
            rows,
            as_of=request.as_of,
            run_id=run_id,
            mode=request.mode,
            concurrency=request.concurrency,
            refresh=request.refresh,
            scope=scope,
        )
        paths = export(ranked, run_id_actual)
        _run_results[run_id] = [r.model_dump(mode="json") for r in ranked]
        _trim_inmem()
        state.update({
            "status": "completed",
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "ranked": len(ranked),
            "valid": sum(1 for r in ranked if r.valid),
            "exports": {k: str(v) for k, v in paths.items()},
            "refresh_report": runner.last_refresh_report or None,
        })
    except Exception as exc:  # noqa: BLE001 - report failure to API client
        logger.get_logger(__name__).exception("API run %s failed", run_id)
        try:
            progress.finish_run(run_id, "failed")
        except Exception:  # noqa: BLE001
            pass
        state.update({
            "status": "failed",
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {exc}",
        })
    finally:
        s.llm_force_provider = original_provider
        s.llm_force_model = original_model
        s.llm_timeout_seconds = original_timeout
        s.force_refresh = original_force_refresh
        if original_force_refresh_env is None:
            os.environ.pop("FORCE_REFRESH", None)
        else:
            os.environ["FORCE_REFRESH"] = original_force_refresh_env
        _active_run_id = None


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def home() -> FileResponse:
    """Serve the JavaScript control center for stock selection and run history."""
    ui_path = Path(__file__).parent / "static" / "index.html"
    return FileResponse(ui_path, media_type="text/html", filename=None)


@app.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "stock-scoring-api"}


@app.get("/api/universe", tags=["universe"])
async def universe(sub_sector: str | None = Query(default=None), limit: int = Query(default=500, ge=1, le=1000)) -> dict[str, Any]:
    try:
        rows = _load_universe(sub_sector=sub_sector)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Could not load stock universe: {exc}") from exc
    return {"count": min(len(rows), limit), "total": len(rows), "stocks": rows[:limit]}


@app.post("/api/runs", status_code=status.HTTP_202_ACCEPTED, tags=["runs"])
async def create_run(request: RunRequest) -> dict[str, Any]:
    global _active_run_id
    async with _active_lock:
        if _active_run_id is not None:
            active = _run_tasks.get(_active_run_id, {})
            if active.get("status") in {"queued", "running"}:
                raise HTTPException(status_code=409, detail={"message": "A scoring run is already active", "run_id": _active_run_id})
            _active_run_id = None

        try:
            rows = _load_universe(only=request.symbols, sub_sector=request.sub_sector)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not rows:
            raise HTTPException(status_code=422, detail="No stocks matched the requested symbols/sub-sector")

        run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        scope = "specific:" + ",".join(r["symbol"] for r in rows) if (request.symbols or request.sub_sector) else "bulk"
        _run_tasks[run_id] = {
            "status": "queued",
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "scope": scope,
            "symbols": [r["symbol"] for r in rows],
            "total": len(rows),
        }
        _active_run_id = run_id
        asyncio.create_task(_execute_run(run_id, request, rows, scope), name=f"stock-scoring-{run_id}")
        return _run_payload(run_id, _run_tasks[run_id])


@app.get("/api/runs", tags=["runs"])
async def list_scoring_runs(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    persisted = run_store.list_runs(limit=limit)
    seen = {row.get("run_id") for row in persisted}
    live = [_run_payload(rid, state) for rid, state in _run_tasks.items() if rid not in seen]
    return {"runs": live + persisted[:max(0, limit - len(live))]}


@app.get("/api/runs/{run_id}", tags=["runs"])
async def get_scoring_run(run_id: str) -> dict[str, Any]:
    if run_id in _run_tasks:
        return _run_payload(run_id, _run_tasks[run_id])
    persisted = next((r for r in run_store.list_runs(limit=100) if r.get("run_id") == run_id), None)
    if persisted is None:
        raise HTTPException(status_code=404, detail="Run not found")
    final_rows = [r for r in run_store.load_run(run_id) if r.get("agent") == "final"]
    persisted["ranked"] = len(final_rows)
    persisted["valid"] = sum(1 for r in final_rows if r.get("status") == "ok")
    persisted["status"] = "completed"
    return persisted


@app.get("/api/runs/{run_id}/results", tags=["runs"])
async def get_run_results(run_id: str, valid_only: bool = False) -> dict[str, Any]:
    if run_id in _run_tasks and _run_tasks[run_id].get("status") in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="Run is not complete yet; poll GET /api/runs/{run_id}")
    results = _run_results.get(run_id)
    if results is None:
        s = get_settings()
        csv_path = s.path(s.output_dir) / f"ranking_{run_id}.csv"
        if not csv_path.exists():
            raise HTTPException(status_code=404, detail="Results not found for this run")
        import pandas as pd
        results = pd.read_csv(csv_path).where(lambda frame: frame.notna(), None).to_dict(orient="records")
    if valid_only:
        results = [r for r in results if r.get("valid") is True or r.get("valid") == 1]
    return {"run_id": run_id, "count": len(results), "results": results}


@app.get("/api/runs/{run_id}/agents", tags=["runs"])
async def get_run_agents(run_id: str, symbol: str | None = None) -> dict[str, Any]:
    known = run_id in _run_tasks or any(r.get("run_id") == run_id for r in run_store.list_runs(limit=100))
    if not known:
        raise HTTPException(status_code=404, detail="Run not found")
    rows = run_store.load_run(run_id, symbol=symbol)
    return {"run_id": run_id, "symbol": symbol.upper() if symbol else None, "count": len(rows), "agents": rows}


_STATUS_TO_CELL = {"ok": "done", "error": "error", "unavailable": "unavailable", "invalid": "done", "missing": "pending"}


def _db_progress(run_id: str) -> dict[str, Any] | None:
    """Reconstruct a progress snapshot from persisted rows + kv token totals for runs no longer in memory."""
    rows = run_store.load_run(run_id)
    if not rows:
        return None
    from app.store.db import kv_get
    symbols: dict[str, dict[str, Any]] = {}
    for r in rows:
        agent = r.get("agent")
        if agent not in progress.AGENT_ORDER:
            continue
        extra = r.get("extra") if isinstance(r.get("extra"), dict) else {}
        sym = symbols.setdefault(r["symbol"], {})
        sym[agent] = {"status": _STATUS_TO_CELL.get(r.get("status"), "done"), "started_at": None, "finished_at": None,
                      "duration_ms": None, "score": r.get("score"), "provider": r.get("provider") or None,
                      "model": r.get("model") or None, "input_tokens": int((extra or {}).get("input_tokens") or 0),
                      "output_tokens": int((extra or {}).get("output_tokens") or 0), "error": None}
    for sym in symbols.values():
        for a in progress.AGENT_ORDER:
            sym.setdefault(a, {"status": "pending", "score": None, "provider": None, "model": None,
                               "input_tokens": 0, "output_tokens": 0, "started_at": None, "finished_at": None,
                               "duration_ms": None, "error": None})
    totals = kv_get("run_tokens", run_id) or {}
    if not totals:
        ti = sum(c["input_tokens"] for s in symbols.values() for c in s.values())
        to = sum(c["output_tokens"] for s in symbols.values() for c in s.values())
        totals = {"input_tokens": ti, "output_tokens": to, "total_tokens": ti + to, "by_model": {}}
    totals.setdefault("agents_total", len(symbols) * len(progress.AGENT_ORDER))
    totals.setdefault("agents_done", sum(1 for s in symbols.values() for c in s.values() if c["status"] != "pending"))
    return {"run_id": run_id, "status": "completed", "agents": list(progress.AGENT_ORDER), "symbols": symbols, "totals": totals}


@app.get("/api/runs/{run_id}/progress", tags=["runs"])
async def get_run_progress(run_id: str) -> dict[str, Any]:
    """Live per-agent status matrix and token totals for a run (for the dashboard's live view)."""
    snap = progress.snapshot(run_id)
    if snap is not None:
        return snap
    snap = _db_progress(run_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return snap


@app.get("/api/runs/{run_id}/report", tags=["runs"])
async def get_run_report(run_id: str, symbol: str | None = None, format: str = Query(default="md", pattern="^(md|json)$")) -> Response:
    """Download a debug report of every agent's model input (system + payload) and output (raw reply) for a run.
    Optional `symbol` limits it to one stock; `format` is md (default) or json."""
    known = run_id in _run_tasks or any(r.get("run_id") == run_id for r in run_store.list_runs(limit=200))
    if not known:
        raise HTTPException(status_code=404, detail="Run not found")
    from app.export import report as report_mod
    safe_symbol = ("".join(c for c in symbol.upper() if c.isalnum() or c in "._-")) if symbol else None
    data = report_mod.build_report(run_id, safe_symbol)
    stem = f"report_{Path(run_id).name}" + (f"_{safe_symbol}" if safe_symbol else "")
    if format == "json":
        body, media, ext = report_mod.report_json(data), "application/json", "json"
    else:
        body, media, ext = report_mod.report_markdown(data), "text/markdown; charset=utf-8", "md"
    return Response(content=body, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{stem}.{ext}"'})


@app.get("/api/runs/{run_id}/traces", tags=["runs"])
async def get_run_traces(run_id: str, symbol: str = Query(..., min_length=1, max_length=32)) -> dict[str, Any]:
    """Return persisted prompt/response traces for one symbol in a run."""
    known = run_id in _run_tasks or any(r.get("run_id") == run_id for r in run_store.list_runs(limit=100))
    if not known:
        raise HTTPException(status_code=404, detail="Run not found")
    safe_run_id = Path(run_id).name
    safe_symbol = "".join(c for c in symbol.upper() if c.isalnum() or c in "._-")
    if safe_run_id != run_id or not safe_symbol:
        raise HTTPException(status_code=400, detail="Invalid run ID or symbol")
    trace_dir = get_settings().path(get_settings().log_dir) / "runs" / safe_run_id / "traces"
    if not trace_dir.exists():
        return {"run_id": run_id, "symbol": safe_symbol, "count": 0, "traces": []}
    traces = []
    for path in sorted(trace_dir.glob(f"{safe_symbol}_*.json")):
        try:
            payload = __import__("json").loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        traces.append({"agent": path.stem[len(safe_symbol) + 1:], "file": path.name, **payload})
    return {"run_id": run_id, "symbol": safe_symbol, "count": len(traces), "traces": traces}


@app.get("/api/models", tags=["config"])
async def list_available_models() -> dict[str, Any]:
    """Enumerate models available across every local inference server (Ollama, MLX/OLMX, vLLM, Docker AI).
    Also returns the current default from model_config.json. Used by the UI model selector."""
    import httpx
    s = get_settings()

    async def _openai_models(url: str, api_key: str = "") -> list[str]:
        """Fetch the model list from any OpenAI-compatible /v1/models endpoint."""
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{url.rstrip('/')}/models", headers=headers)
            r.raise_for_status()
            return [m["id"] for m in (r.json().get("data") or [])]

    providers: dict[str, Any] = {}

    # ── Ollama (/api/tags) ──────────────────────────────────────────────────
    try:
        base = (s.ollama_base_url or "http://localhost:11434").rstrip("/")
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{base}/api/tags")
            r.raise_for_status()
            models = [m["name"] for m in (r.json().get("models") or [])]
        providers["ollama"] = {"available": True, "url": base, "models": sorted(models)}
    except Exception:  # noqa: BLE001
        providers["ollama"] = {"available": False, "url": s.ollama_base_url or "http://localhost:11434", "models": []}

    # ── MLX / OLMX (OpenAI-compatible) ─────────────────────────────────────
    olmx_url = s.olmx_base_url or ""
    if olmx_url:
        try:
            models = await _openai_models(olmx_url, s.olmx_api_key or "")
            providers["mlx"] = {"available": True, "url": olmx_url, "models": sorted(models)}
        except Exception:  # noqa: BLE001
            providers["mlx"] = {"available": False, "url": olmx_url, "models": []}
    else:
        providers["mlx"] = {"available": False, "url": "http://localhost:8008/v1", "models": []}

    # ── vLLM — probe common ports (8000, 8080, 8081) ───────────────────────
    vllm_found: dict[str, Any] = {"available": False, "url": "", "models": []}
    for port in (8000, 8080, 8081):
        try:
            url = f"http://localhost:{port}/v1"
            models = await _openai_models(url)
            if models:
                vllm_found = {"available": True, "url": url, "models": sorted(models)}
                break
        except Exception:  # noqa: BLE001
            pass
    providers["vllm"] = vllm_found

    # ── Docker AI (docker model ls) ─────────────────────────────────────────
    docker_models: list[str] = []
    try:
        import subprocess
        proc = subprocess.run(
            ["docker", "model", "ls", "--format", "{{.Name}}"],
            capture_output=True, text=True, timeout=4)
        if proc.returncode == 0:
            docker_models = [ln.strip() for ln in proc.stdout.strip().splitlines() if ln.strip()]
    except Exception:  # noqa: BLE001
        pass
    providers["docker"] = {"available": bool(docker_models), "models": docker_models}

    # ── Current default from model_config.json ─────────────────────────────
    default_info: dict[str, str] = {}
    try:
        from app.service.models.llm_factory import resolve_spec
        spec = resolve_spec("default")
        default_info = {"provider": spec.provider, "model": spec.model}
    except Exception:  # noqa: BLE001
        default_info = {"provider": s.llm_provider or "ollama", "model": "nemotron-e4b-16k:latest"}

    total = sum(len(p.get("models") or []) for p in providers.values())
    return {"providers": providers, "default": default_info, "total_models": total}


@app.get("/api/chart/{symbol}", tags=["charts"])
async def get_chart(symbol: str, lookback: int = Query(default=160, ge=30, le=420),
                    capital: float = Query(default=100000.0), risk_pct: float = Query(default=1.0)) -> Response:
    """Generate and return a Trader chart PNG for `symbol` on demand: price + EMAs + demand/supply zones
    + VWAP + the bull/bear trade plan (entry / stop-loss / target). Uses FYERS candles if the token is
    set, otherwise falls back to Yahoo. Requires `matplotlib` to be installed."""
    import os, tempfile
    sym = "".join(c for c in symbol.upper() if c.isalnum())
    if not sym:
        raise HTTPException(status_code=400, detail="Invalid symbol")
    try:
        from app.mcp_server.tools import indicators
        from app.viz.chart import plot_trade_chart
        import pandas as pd
        daily: pd.DataFrame | None = None
        provider = "unknown"
        # Try FYERS first (live candles, no separate token setup needed if app is running).
        try:
            from app.mcp_server.tools.market import fetch_candles
            df, provider = fetch_candles(sym, "D", 420, None)
            if len(df) >= 60:
                daily = df
        except Exception:  # noqa: BLE001
            pass
        # Fall back to Yahoo if FYERS is unavailable.
        if daily is None:
            from app.backtest.data import load_yahoo_daily
            from datetime import date, timedelta
            panel = load_yahoo_daily([sym], (date.today() - timedelta(days=760)).isoformat(), date.today().isoformat())
            if sym in panel and len(panel[sym]) >= 60:
                daily, provider = panel[sym], "yahoo"
        if daily is None or len(daily) < 60:
            raise HTTPException(status_code=422, detail=f"Insufficient candle history for {sym} "
                                "(ensure FYERS_ACCESS_TOKEN is set, or run python -m scripts.fyers_login)")
        zones = indicators.demand_supply_zones(daily.tail(420), None)
        long_plan = indicators.build_trade_plan(zones, capital=capital, risk_pct=risk_pct, force_bias="long")
        short_plan = indicators.build_trade_plan(zones, capital=capital, risk_pct=risk_pct, force_bias="short")
        # fetch events for chart markers (best-effort)
        events: list[dict] = []
        try:
            from app.mcp_server.tools import nse as nse_tools
            ev = nse_tools.nse_company_events(sym, days=90)
            if ev.get("status") == "ok":
                events = ev.get("data") or []
        except Exception:  # noqa: BLE001
            pass
        # fetch 15-min candles for intraday panel
        intraday_15m = None
        try:
            from app.mcp_server.tools.market import fetch_candles as _fc
            i15, _ = _fc(sym, "15", 5, None)
            if len(i15) >= 10:
                intraday_15m = i15
        except Exception:  # noqa: BLE001
            pass
        from app.viz.chart import plot_quad_chart
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            plot_quad_chart(daily, intraday_15m, zones, long_plan, short_plan, tmp_path,
                            symbol=sym, lookback_plan=lookback, events=events)
            img = open(tmp_path, "rb").read()
        finally:
            os.unlink(tmp_path)
        return Response(content=img, media_type="image/png",
                        headers={"Cache-Control": "no-store", "Content-Disposition": f'inline; filename="{sym}.png"'})
    except HTTPException:
        raise
    except ImportError as exc:
        raise HTTPException(status_code=503, detail=f"matplotlib not installed: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc

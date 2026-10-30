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

# Market + Stock browse tab routes (price-history, movers, statements, flags, valuation)
from app.market_api import router as _market_router  # noqa: E402
app.include_router(_market_router)


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
    run_mode: Literal["bulk", "deep_dive"] = Field(
        default="bulk",
        description="bulk = nemotron-bolt everywhere (fast, reliable). deep_dive = mixed config (qwen3 for heavy agents) — uses model_config.mixed.json if present.",
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


# Runtime task state kept in memory; completed runs + traces persisted in SQLite.
_run_tasks:   dict[str, dict[str, Any]] = {}
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
    original_config_path = s.llm_config_path
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
        # deep_dive mode: swap to mixed config (qwen3 + bolt + nano) if available
        if request.run_mode == "deep_dive" and not request.stub:
            from pathlib import Path as _P
            from app.service.models.llm_factory import CONFIG_PATH as _CFG
            mixed = _P(_CFG).parent / "model_config.mixed.json"
            if mixed.exists():
                s.llm_config_path = str(mixed)
                state["run_mode_effective"] = "deep_dive (model_config.mixed.json)"
            else:
                state["run_mode_effective"] = "deep_dive requested but model_config.mixed.json not found — using default"
        else:
            state["run_mode_effective"] = "bulk"
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
        s.llm_config_path = original_config_path
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


@app.get("/api/health/tools", tags=["system"])
async def tool_health() -> dict[str, Any]:
    """Probe every external data source and return green/red status per tool.
    Used by the UI tool-health panel. Each probe has a short timeout so the response is fast."""
    import httpx
    s = get_settings()
    results: dict[str, Any] = {}

    async def probe(name: str, coro) -> None:
        try:
            status, note = await coro
            results[name] = {"status": status, "note": note}
        except Exception as exc:  # noqa: BLE001
            results[name] = {"status": "red", "note": str(exc)[:120]}

    async def _http(url: str, timeout: float = 4.0, headers: dict | None = None,
                    yellow_codes: set | None = None) -> tuple[str, str]:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(url, headers=headers or {})
            if r.status_code < 400:
                return "green", f"HTTP {r.status_code}"
            if yellow_codes and r.status_code in yellow_codes:
                return "yellow", f"HTTP {r.status_code} (working but rate-limited/needs session)"
            return "red", f"HTTP {r.status_code}"

    async def _fyers() -> tuple[str, str]:
        if not (s.fyers_app_id and s.fyers_access_token):
            return "yellow", "FYERS_APP_ID / FYERS_ACCESS_TOKEN not set"
        from app.mcp_server.tools.market import _fyers_client
        try:
            _fyers_client()
            return "green", "credentials present"
        except Exception as e:  # noqa: BLE001
            return "red", str(e)[:80]

    async def _ollama() -> tuple[str, str]:
        raw = (s.ollama_base_url or "http://localhost:11434").rstrip("/")
        if raw.endswith("/v1"):
            raw = raw[:-3]
        return await _http(f"{raw}/api/tags")

    async def _yahoo() -> tuple[str, str]:
        # 429 = rate-limited by Yahoo but the actual yfinance library works fine (uses different endpoints/headers)
        return await _http("https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI?interval=1d&range=5d",
                           timeout=5.0, yellow_codes={429, 401})

    async def _nse() -> tuple[str, str]:
        # NSE blocks simple browser-less GETs (403) but NSE API tools work via proper session init
        # Use the public allIndices endpoint which is less strict than the homepage
        try:
            status, note = await _http("https://www.nseindia.com/api/allIndices", timeout=5.0,
                                       headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                                                "Accept": "application/json", "Referer": "https://www.nseindia.com/"},
                                       yellow_codes={403, 401})
            return status, note
        except Exception:  # noqa: BLE001
            return "yellow", "NSE requires browser session (tools work via session init)"

    async def _rbi() -> tuple[str, str]:
        return await _http("https://rbi.org.in/pressreleases_rss.xml", timeout=5.0)

    async def _fred() -> tuple[str, str]:
        if not s.fred_api_key:
            return "yellow", "FRED_API_KEY not set (macro rates unavailable)"
        return await _http(f"https://api.stlouisfed.org/fred/series/observations?series_id=DFF&api_key={s.fred_api_key}&limit=1&file_type=json", timeout=5.0)

    async def _indianapi() -> tuple[str, str]:
        if not s.indianapi_key:
            return "yellow", "INDIANAPI_KEY not set (premium news unavailable)"
        return await _http("https://stock.indianapi.in/news", timeout=5.0,
                           headers={"x-api-key": s.indianapi_key})

    async def _olmx() -> tuple[str, str]:
        if not s.olmx_base_url:
            return "yellow", "OLMX_BASE_URL not set"
        return await _http(f"{s.olmx_base_url.rstrip('/')}/models", timeout=3.0,
                           headers={"Authorization": f"Bearer {s.olmx_api_key}"} if s.olmx_api_key else {})

    await asyncio.gather(
        probe("Ollama (LLM)", _ollama()),
        probe("FYERS (candles)", _fyers()),
        probe("NSE (events/deals)", _nse()),
        probe("Yahoo Finance (index/macro)", _yahoo()),
        probe("RBI RSS (policy)", _rbi()),
        probe("FRED (US rates)", _fred()),
        probe("IndianAPI (premium news)", _indianapi()),
        probe("MLX / OLMX", _olmx()),
    )
    total = len(results)
    green = sum(1 for v in results.values() if v["status"] == "green")
    return {"tools": results, "summary": f"{green}/{total} reachable"}


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


# ─────────────────────────────────────────────────────────────────────────────
#  WATCHLISTS — persistent named stock lists (favourites, themes, etc.)
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
#  DATA VIEWER — browse every SQLite table
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/data/stats", tags=["data"])
async def data_stats() -> dict[str, Any]:
    """Row counts + estimated DB size for every table."""
    from app.store.db import db_stats
    return db_stats()


@app.get("/api/data/fundamentals", tags=["data"])
async def data_fundamentals(symbol: str | None = None, limit: int = Query(default=200, le=1000)) -> dict[str, Any]:
    from app.store.db import connect as _db
    q = "SELECT symbol,period_end,basis,source,revenue,op_profit,op_kind,pbt,pat,eps,reported_on FROM fundamentals_q"
    args: list = []
    if symbol:
        q += " WHERE symbol=?"; args.append(symbol.upper().strip())
    q += " ORDER BY symbol,period_end DESC LIMIT ?"
    args.append(limit)
    with _db() as con:
        rows = [dict(r) for r in con.execute(q, args).fetchall()]
    return {"rows": rows, "count": len(rows)}


@app.get("/api/data/news", tags=["data"])
async def data_news(scope: str | None = None, days: int = Query(default=30, le=365),
                    limit: int = Query(default=200, le=1000)) -> dict[str, Any]:
    from app.store.db import connect as _db
    from datetime import timedelta
    cutoff = (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds")
    q = "SELECT scope,title,publisher,published_at,url FROM news WHERE published_at >= ?"
    args: list = [cutoff]
    if scope:
        q += " AND scope LIKE ?"; args.append(f"%{scope}%")
    q += " ORDER BY published_at DESC LIMIT ?"; args.append(limit)
    with _db() as con:
        rows = [dict(r) for r in con.execute(q, args).fetchall()]
    return {"rows": rows, "count": len(rows)}


@app.get("/api/data/events", tags=["data"])
async def data_events(symbol: str | None = None, event_type: str | None = None,
                      days: int = Query(default=90, le=365), limit: int = Query(default=200, le=1000)) -> dict[str, Any]:
    from app.store.db import connect as _db
    from datetime import timedelta
    cutoff = (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds")
    q = "SELECT event_id,symbol,event_type,event_date,announcement_date,available_at,payload FROM company_events WHERE available_at >= ?"
    args: list = [cutoff]
    if symbol:
        q += " AND symbol=?"; args.append(symbol.upper().strip())
    if event_type:
        q += " AND event_type=?"; args.append(event_type)
    q += " ORDER BY available_at DESC LIMIT ?"; args.append(limit)
    with _db() as con:
        rows = []
        for r in con.execute(q, args).fetchall():
            d = dict(r)
            try:
                import json as _j
                p = _j.loads(d.pop("payload", "{}") or "{}")
                d["event_summary"] = p.get("event_summary", "")[:120]
            except Exception:  # noqa: BLE001
                d["event_summary"] = ""
            rows.append(d)
    return {"rows": rows, "count": len(rows)}


@app.get("/api/data/fii_dii", tags=["data"])
async def data_fii_dii(days: int = Query(default=30, le=365)) -> dict[str, Any]:
    from app.store.db import connect as _db
    from datetime import timedelta
    cutoff = (datetime.now().astimezone() - timedelta(days=days)).date().isoformat()
    with _db() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT day,category,buy_cr,sell_cr,net_cr,fetched_at FROM fii_dii_daily "
            "WHERE day >= ? ORDER BY day DESC, category", (cutoff,)).fetchall()]
    return {"rows": rows, "count": len(rows)}


@app.get("/api/data/oi_snapshots", tags=["data"])
async def data_oi_snapshots(symbol: str | None = None, days: int = Query(default=30, le=180),
                             limit: int = Query(default=200, le=1000)) -> dict[str, Any]:
    from app.store.db import connect as _db
    from datetime import timedelta
    cutoff = (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds")
    q = "SELECT symbol,captured_at,call_oi,put_oi,pcr,ltp,max_pain FROM oi_snapshots WHERE captured_at >= ?"
    args: list = [cutoff]
    if symbol:
        q += " AND symbol=?"; args.append(symbol.upper().strip())
    q += " ORDER BY captured_at DESC LIMIT ?"; args.append(limit)
    with _db() as con:
        rows = [dict(r) for r in con.execute(q, args).fetchall()]
    return {"rows": rows, "count": len(rows)}


@app.get("/api/data/runs", tags=["data"])
async def data_runs(limit: int = Query(default=50, le=200)) -> dict[str, Any]:
    from app.store.db import connect as _db
    with _db() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT run_id,as_of,created_at,n_stocks,scope FROM runs ORDER BY created_at DESC LIMIT ?",
            (limit,)).fetchall()]
    return {"rows": rows, "count": len(rows)}


@app.get("/api/data/agent_io", tags=["data"])
async def data_agent_io(run_id: str | None = None, symbol: str | None = None,
                        agent: str | None = None, limit: int = Query(default=100, le=500)) -> dict[str, Any]:
    from app.store.db import connect as _db
    q = "SELECT run_id,symbol,agent,status,score,confidence,provider,model,reason,missing,note,created_at FROM run_agent_io WHERE 1=1"
    args: list = []
    if run_id:
        q += " AND run_id=?"; args.append(run_id)
    if symbol:
        q += " AND symbol=?"; args.append(symbol.upper().strip())
    if agent:
        q += " AND agent=?"; args.append(agent)
    q += " ORDER BY created_at DESC LIMIT ?"; args.append(limit)
    with _db() as con:
        rows = [dict(r) for r in con.execute(q, args).fetchall()]
    return {"rows": rows, "count": len(rows)}


@app.post("/api/admin/prune", tags=["data"])
async def admin_prune(
    news_days: int = Query(default=90),
    events_days: int = Query(default=180),
    runs_keep: int = Query(default=100),
    oi_days: int = Query(default=60),
) -> dict[str, Any]:
    """Delete old data to keep the DB lean. Returns deleted row counts per table."""
    from app.store.db import prune_old_data
    deleted = prune_old_data(news_days=news_days, events_days=events_days,
                              runs_keep=runs_keep, oi_days=oi_days)
    return {"pruned": deleted}


@app.get("/api/watchlists", tags=["watchlists"])
async def get_watchlists() -> dict[str, Any]:
    """Return all watchlists as {list_name: [symbol, …]}."""
    from app.store.db import connect as _db
    with _db() as con:
        rows = con.execute("SELECT list_name, symbol FROM watchlists WHERE symbol != '__created__' ORDER BY list_name, added_at").fetchall()
    out: dict[str, list[str]] = {}
    for name, sym in rows:
        out.setdefault(name, []).append(sym)
    return {"watchlists": out}


@app.post("/api/watchlists/{list_name}", tags=["watchlists"], status_code=201)
async def add_to_watchlist(list_name: str, body: dict = None) -> dict[str, Any]:
    """Create a named watchlist (empty body) or add symbols to it. Body: {\"symbols\": [\"SBIN\",\"CANBK\"]}"""
    from app.store.db import connect as _db
    symbols = ([s.strip().upper() for s in (body or {}).get("symbols", [])] if body else [])
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with _db() as con:
        # Always ensure the list exists — insert a sentinel row with empty symbol if no symbols provided
        # (SQLite primary key is (list_name, symbol) so we use a zero-width marker)
        if not symbols:
            # Touch the list so it appears in GET /api/watchlists
            con.execute("INSERT OR IGNORE INTO watchlists VALUES (?,?,?)", (list_name, "__created__", now))
        else:
            for sym in symbols:
                con.execute("INSERT OR IGNORE INTO watchlists VALUES (?,?,?)", (list_name, sym, now))
    return {"list_name": list_name, "added": symbols}


@app.delete("/api/watchlists/{list_name}", tags=["watchlists"])
async def delete_from_watchlist(list_name: str, symbol: str | None = None) -> dict[str, Any]:
    """Remove a symbol from a list, or delete the entire list if symbol is omitted."""
    from app.store.db import connect as _db
    with _db() as con:
        if symbol:
            con.execute("DELETE FROM watchlists WHERE list_name=? AND symbol=?", (list_name, symbol.upper()))
            return {"deleted": symbol, "from": list_name}
        con.execute("DELETE FROM watchlists WHERE list_name=?", (list_name,))
        return {"deleted_list": list_name}


@app.get("/api/models", tags=["config"])
async def list_available_models() -> dict[str, Any]:
    """Return the LLM catalog from app/config/llm_catalog.json.
    Edit that file to add/remove models — no server restart needed, change takes effect on next call."""
    import json, pathlib
    catalog_path = pathlib.Path(__file__).parent / "config" / "llm_catalog.json"
    try:
        catalog: dict[str, list[str]] = json.loads(catalog_path.read_text())
    except Exception:  # noqa: BLE001
        catalog = {}

    # Default model from model_config.json
    default_info: dict[str, str] = {}
    try:
        from app.service.models.llm_factory import resolve_spec
        spec = resolve_spec("default")
        default_info = {"provider": spec.provider, "model": spec.model}
    except Exception:  # noqa: BLE001
        default_info = {"provider": "ollama", "model": "nemotron-e4b-16k:latest"}

    total = sum(len(v) for v in catalog.values() if isinstance(v, list))
    return {"catalog": catalog, "default": default_info, "total_models": total}




@app.get("/api/chart/{symbol}/interactive", tags=["charts"])
async def get_interactive_chart(
    symbol: str,
    lookback: int = Query(default=365, ge=60, le=900),
) -> Response:
    """TradingView-style interactive HTML chart: candlesticks + zones + EMAs + events + quarterly PAT + bank KPIs.
    Opens directly in the browser with full zoom/pan/hover.  No server after initial load."""
    sym = "".join(c for c in symbol.upper() if c.isalnum())
    if not sym:
        raise HTTPException(status_code=400, detail="Invalid symbol")
    try:
        from app.mcp_server.tools import indicators
        from app.viz.interactive_chart import plot_interactive_html
        import pandas as pd

        # ── daily candles ──────────────────────────────────────────────────
        daily: pd.DataFrame | None = None
        try:
            from app.mcp_server.tools.market import fetch_candles
            df, _ = fetch_candles(sym, "D", lookback, None)
            if len(df) >= 60:
                daily = df
        except Exception:  # noqa: BLE001
            pass
        if daily is None:
            from app.backtest.data import load_yahoo_daily
            from datetime import date, timedelta
            panel = load_yahoo_daily([sym], (date.today() - timedelta(days=lookback + 60)).isoformat(),
                                     date.today().isoformat())
            if sym in panel and len(panel[sym]) >= 60:
                daily = panel[sym]
        if daily is None or len(daily) < 60:
            raise HTTPException(status_code=422, detail=f"Insufficient candle history for {sym}")

        zones = indicators.demand_supply_zones(daily.tail(420), None)

        # ── 15-min candles (last ~5 days) ──────────────────────────────────
        intraday_15m = None
        try:
            from app.mcp_server.tools.market import fetch_candles as _fc
            i15, _ = _fc(sym, "15", 7, None)   # 7 calendar days ≈ 5 trading days
            if len(i15) >= 10:
                intraday_15m = i15
        except Exception:  # noqa: BLE001
            pass

        # ── events ─────────────────────────────────────────────────────────
        events: list[dict] = []
        try:
            from app.mcp_server.tools import nse as nse_tools
            ev = nse_tools.nse_company_events(sym, days=180)
            if ev.get("status") == "ok":
                events = ev.get("data") or []
        except Exception:  # noqa: BLE001
            pass

        # ── quarterly fundamentals ─────────────────────────────────────────
        fundamentals: dict | None = None
        bank_kpis: dict | None = None
        try:
            from app.mcp_server.tools.fundamentals import quarterly_fundamentals
            q = quarterly_fundamentals(sym)
            if q.get("status") == "ok":
                fundamentals = q["data"]
            from app.store.db import kv_get
            xbrl = (fundamentals or {}).get("latest_filing", {}).get("xbrl", {}) if fundamentals else {}
            if xbrl.get("kpis"):
                bank_kpis = xbrl["kpis"]
        except Exception:  # noqa: BLE001
            pass

        html = plot_interactive_html(
            symbol=sym, daily=daily, intraday_15m=intraday_15m,
            zones=zones, events=events, fundamentals=fundamentals, bank_kpis=bank_kpis,
        )
        return Response(content=html, media_type="text/html",
                        headers={"Cache-Control": "no-store",
                                 "Content-Disposition": f'inline; filename="{sym}_interactive.html"'})
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


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

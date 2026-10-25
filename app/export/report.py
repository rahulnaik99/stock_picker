"""Per-run debug report: bundles every agent's model INPUT (system prompt + payload) and OUTPUT (raw reply)
plus the parsed score/baseline/adjustment/tokens and the final verdict, as JSON or Markdown.

The Markdown is meant to be pasted into a chat for analysis; the JSON is for tooling.
Inputs/outputs come from the per-run trace files (logs/runs/<run>/traces/<SYMBOL>_<agent>.json), enriched with
the persisted run_agent_io rows (score, tokens, status) and the kv token totals.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.service.settings import get_settings
from app.store import runs as run_store

AGENT_ORDER = ["fundamental", "industry", "indicator", "investment_trend", "option_chain", "decision_maker", "final"]


def _trace_dir(run_id: str) -> Path:
    s = get_settings()
    return s.path(s.log_dir) / "runs" / Path(run_id).name / "traces"


def _safe_symbol(symbol: str) -> str:
    return "".join(c for c in symbol.upper() if c.isalnum() or c in "._-")


def _load_traces(run_id: str, symbol: str) -> dict[str, dict[str, Any]]:
    d, sym = _trace_dir(run_id), _safe_symbol(symbol)
    out: dict[str, dict[str, Any]] = {}
    if not sym or not d.exists():
        return out
    for p in sorted(d.glob(f"{sym}_*.json")):
        try:
            out[p.stem[len(sym) + 1:]] = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return out


def _pretty(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            return json.dumps(json.loads(value), indent=2, ensure_ascii=False)
        except ValueError:
            return value
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def build_report(run_id: str, symbol: str | None = None) -> dict[str, Any]:
    rows = run_store.load_run(run_id, symbol=symbol)
    syms = [symbol.upper()] if symbol else sorted({r["symbol"] for r in rows})
    meta = next((r for r in run_store.list_runs(limit=200) if r.get("run_id") == run_id), {})
    by: dict[str, dict[str, dict[str, Any]]] = {}
    for r in rows:
        by.setdefault(r["symbol"], {})[r["agent"]] = r

    stocks: dict[str, Any] = {}
    for sym in syms:
        traces = _load_traces(run_id, sym)
        agents = []
        for name in [a for a in AGENT_ORDER if a != "final"]:
            row, tr = by.get(sym, {}).get(name), traces.get(name)
            if row is None and tr is None:
                continue
            extra = row.get("extra") if row and isinstance(row.get("extra"), dict) else {}
            agents.append({
                "agent": name,
                "provider": (tr or {}).get("provider") or (row or {}).get("provider") or "",
                "model": (tr or {}).get("model") or (row or {}).get("model") or "",
                "status": (row or {}).get("status"),
                "score": (row or {}).get("score"),
                "baseline_score": extra.get("baseline_score"),
                "score_adjustment": extra.get("score_adjustment"),
                "confidence": (row or {}).get("confidence"),
                "input_tokens": extra.get("input_tokens"),
                "output_tokens": extra.get("output_tokens"),
                "system_prompt": (tr or {}).get("system"),
                "tool_responses": (tr or {}).get("tools"),
                "input_payload": (tr or {}).get("user") or (row or {}).get("context_used"),
                "raw_reply": (tr or {}).get("reply"),
                "reasoning": (row or {}).get("reason"),
                "error": (tr or {}).get("error") or None,
            })
        final = by.get(sym, {}).get("final")
        fx = final.get("extra") if final and isinstance(final.get("extra"), dict) else {}
        stocks[sym] = {
            "final": None if not final else {
                "status": final.get("status"), "final_score": final.get("score"), "confidence": final.get("confidence"),
                "reasoning_summary": final.get("reason"), "risk_flags": final.get("missing"),
                "direction": fx.get("direction"), "consensus": fx.get("consensus"), "trade_status": fx.get("trade_status"),
                "data_status": fx.get("data_status"), "completeness": fx.get("completeness"),
                "total_tokens": fx.get("total_tokens")},
            "agents": agents,
        }

    from app.store.db import kv_get
    return {"run_id": run_id, "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "as_of": meta.get("as_of"), "scope": meta.get("scope"), "symbols": syms,
            "tokens": kv_get("run_tokens", run_id) or {}, "stocks": stocks}


def _block(label: str, body: Any, lang: str = "") -> list[str]:
    text = _pretty(body)
    if not text:
        return [f"**{label}:** _(none)_", ""]
    return [f"**{label}:**", f"````{lang}", text, "````", ""]   # 4-backtick fence survives ```json inside prompts


def report_markdown(data: dict[str, Any]) -> str:
    t = data.get("tokens") or {}
    out = [f"# Stock Scoring debug report — run `{data['run_id']}`", "",
           f"- generated: {data.get('generated_at')}", f"- as_of: {data.get('as_of')}", f"- scope: {data.get('scope')}",
           f"- symbols: {', '.join(data.get('symbols') or [])}",
           f"- tokens: input {t.get('input_tokens', '?')} / output {t.get('output_tokens', '?')} / total {t.get('total_tokens', '?')}",
           ""]
    for sym, s in data.get("stocks", {}).items():
        f = s.get("final") or {}
        out += [f"## {sym}", "",
                f"**FINAL** — score `{f.get('final_score')}` · direction {f.get('direction')} · trade_status "
                f"{f.get('trade_status')} · consensus {f.get('consensus')} · completeness {f.get('completeness')}", ""]
        if f.get("reasoning_summary"):
            out += [f"> {f['reasoning_summary']}", ""]
        if f.get("risk_flags"):
            out += ["**risk_flags / validation:**"] + [f"- {x}" for x in f["risk_flags"]] + [""]
        for a in s.get("agents", []):
            head = (f"### {a['agent']} — {a.get('provider')}/{a.get('model')} — status `{a.get('status')}` — "
                    f"score `{a.get('score')}`")
            if a.get("baseline_score") is not None:
                head += f" (baseline {a['baseline_score']} + adj {a.get('score_adjustment')})"
            head += f" — tokens {a.get('input_tokens')}/{a.get('output_tokens')}"
            out += [head, ""]
            if a.get("error"):
                out += [f"**error:** `{a['error']}`", ""]
            out += _block("INPUT — system prompt", a.get("system_prompt"))
            tools = a.get("tool_responses")
            if isinstance(tools, dict) and tools:
                out += ["**TOOL RESPONSES (raw envelopes the agent fetched):**", ""]
                for tname, env in tools.items():
                    out += _block(f"tool · {tname} · status {env.get('status') if isinstance(env, dict) else '?'}", env, "json")
            out += _block("INPUT — payload (evidence sent to the model)", a.get("input_payload"), "json")
            out += _block("OUTPUT — raw model reply", a.get("raw_reply"), "json")
    return "\n".join(out)


def report_json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, default=str)

"""Extract bank/lender KPIs (GNPA, NNPA, CRAR, NIM inputs) from an NSE financial-results XBRL filing.

NSE results XBRL tag names and namespaces vary by taxonomy version, so parsing is deliberately defensive:
we flatten every fact, match KPIs by normalised local-name substrings, pick the value from the LATEST context,
and record EXACTLY what happened (http status, facts parsed, which tag matched each KPI, what is missing, and a
sample of the tags we saw) in a `diagnostics` block. That block is surfaced in the debug report so a failed or
partial extraction is always visible and pin-pointable, never silent.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Callable

from app.mcp_server.tools import nse as nse_tools
from app.service.logger import get_logger

log = get_logger(__name__)

# KPI -> predicate over the normalised (lowercased, alphanumeric-only) local tag name.
# `has` = all of these substrings present; `without` = none of these present.
_KPI_RULES: dict[str, dict[str, tuple[str, ...]]] = {
    "gross_npa_pct":  {"has_any": ("grossnpa", "grossnonperforming"), "with_any": ("percentage", "ratio", "percent"), "without": ()},
    "net_npa_pct":    {"has_any": ("netnpa", "netnonperforming"),     "with_any": ("percentage", "ratio", "percent"), "without": ("gross",)},
    "gross_npa_amt":  {"has_any": ("grossnpa", "grossnonperforming"), "with_any": (), "without": ("percentage", "ratio", "percent", "net")},
    "net_npa_amt":    {"has_any": ("netnpa", "netnonperforming"),     "with_any": (), "without": ("percentage", "ratio", "percent", "gross")},
    "crar_pct":       {"has_any": ("capitaladequacy", "capitaltoriskweighted", "crar", "capitaladequacyratio"), "with_any": (), "without": ()},
    "interest_earned": {"has_any": ("interestearned",),              "with_any": (), "without": ("percentage", "ratio")},
    "interest_expended": {"has_any": ("interestexpended", "interestexpense"), "with_any": (), "without": ("percentage", "ratio")},
    "other_income":   {"has_any": ("otherincome",),                  "with_any": (), "without": ("percentage", "ratio")},
    "provisions":     {"has_any": ("provisionsandcontingencies", "provisionsother", "provisionsandcontigencies"), "with_any": (), "without": ("coverage", "percentage", "ratio")},
    "pcr_pct":        {"has_any": ("provisioncoverage", "provisioningcoverage"), "with_any": (), "without": ()},
}
_CORE_KPIS = ("gross_npa_pct", "net_npa_pct", "crar_pct")   # if none of these are found we call it "unavailable"


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _local(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _num(text: str) -> float | None:
    t = text.strip().replace(",", "")
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()%").strip()
    try:
        v = float(t)
        return -v if neg else v
    except ValueError:
        return None


def _predicate(rule: dict[str, tuple[str, ...]]) -> Callable[[str], bool]:
    def ok(norm: str) -> bool:
        if not any(h in norm for h in rule["has_any"]):
            return False
        if rule["with_any"] and not any(w in norm for w in rule["with_any"]):
            return False
        if any(w in norm for w in rule["without"]):
            return False
        return True
    return ok


def parse_bank_kpis(xml_bytes: bytes) -> dict[str, Any]:
    """Pure function: XBRL bytes -> {status, kpis, matched, missing, facts_parsed, sample_tags, error}.
    `kpis` holds the chosen (latest-context) value per KPI; `matched` records which tag/context it came from."""
    diag: dict[str, Any] = {"facts_parsed": 0, "contexts": 0, "kpis": {}, "matched": {}, "missing": [], "sample_tags": [], "error": None}
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        diag["status"] = "error"
        diag["error"] = f"XML ParseError: {str(exc)[:200]}"
        return diag

    # 1) context id -> end/instant date (to choose the most recent period for each fact)
    ctx_end: dict[str, str] = {}
    for el in root.iter():
        if _local(el.tag).lower() == "context":
            cid = el.get("id", "")
            best = ""
            for sub in el.iter():
                if _local(sub.tag).lower() in ("instant", "enddate") and (sub.text or "").strip():
                    best = max(best, sub.text.strip())
            if cid:
                ctx_end[cid] = best
    diag["contexts"] = len(ctx_end)

    # 2) flatten facts: normalised local name -> list of (ctx_end_date, raw_value, local_name)
    facts: dict[str, list[tuple[str, str, str]]] = {}
    seen_tags: list[str] = []
    for el in root.iter():
        txt = (el.text or "").strip()
        if not txt:
            continue
        ln = _local(el.tag)
        if ln.lower() in ("context", "unit", "schemaref", "xbrl"):
            continue
        norm = _norm(ln)
        if not norm:
            continue
        end = ctx_end.get(el.get("contextRef", ""), "")
        facts.setdefault(norm, []).append((end, txt, ln))
        if ln not in seen_tags:
            seen_tags.append(ln)
    diag["facts_parsed"] = sum(len(v) for v in facts.values())
    diag["sample_tags"] = seen_tags[:60]

    # 3) match each KPI; among candidates take the value from the latest context
    for kpi, rule in _KPI_RULES.items():
        pred = _predicate(rule)
        candidates: list[tuple[str, str, str]] = []
        for norm, occurrences in facts.items():
            if pred(norm):
                candidates.extend(occurrences)
        numeric = [(end, raw, tag) for (end, raw, tag) in candidates if _num(raw) is not None]
        if not numeric:
            diag["missing"].append(kpi)
            continue
        end, raw, tag = max(numeric, key=lambda c: c[0])   # latest period
        diag["kpis"][kpi] = _num(raw)
        diag["matched"][kpi] = {"tag": tag, "context_end": end or None, "raw": raw}

    # 4) derive NII if both interest legs are present
    ie, iexp = diag["kpis"].get("interest_earned"), diag["kpis"].get("interest_expended")
    if ie is not None and iexp is not None:
        diag["kpis"]["nii"] = round(ie - iexp, 2)
        diag["matched"]["nii"] = {"derived": "interest_earned - interest_expended"}

    found_core = [k for k in _CORE_KPIS if k in diag["kpis"]]
    diag["status"] = "ok" if len(found_core) == len(_CORE_KPIS) else ("partial" if diag["kpis"] else "unavailable")
    return diag


def fetch_bank_kpis(url: str, referer: str | None = None) -> dict[str, Any]:
    """Fetch an XBRL URL and parse bank KPIs. NEVER raises: all failures are returned as `status`/`error` in the diagnostics."""
    out: dict[str, Any] = {"url": url, "http_status": None, "bytes": 0}
    try:
        content, http_status = nse_tools.nse_get_bytes(url, referer=referer or f"{nse_tools.BASE}/")
        out["http_status"] = http_status
        out["bytes"] = len(content)
        if not content:
            out["status"] = "error"
            out["error"] = "empty response body"
            return out
        parsed = parse_bank_kpis(content)
        out.update(parsed)
        return out
    except Exception as exc:  # noqa: BLE001 - any failure must surface in the report, not abort the agent
        out["status"] = "error"
        out["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        log.warning("xbrl fetch/parse failed for %s: %s", url, out["error"])
        return out

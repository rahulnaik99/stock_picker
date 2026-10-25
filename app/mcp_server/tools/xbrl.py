"""Extract bank/lender results from an NSE financial-results XBRL filing: the quarterly P&L (total income, operating
profit/PPOP, PBT, PAT, EPS) AND asset-quality/capital KPIs (GNPA%, NNPA%, CRAR, NII, PCR).

The XBRL is the one document that carries everything and is reachable (the summary "detail link" endpoint is
bot-blocked). Parsing is defensive: we flatten every fact, match P&L by exact tag name and KPIs by normalised
substring, pick the value from the LATEST period and — when several contexts share that end-date — the SHORTEST
duration (the 3-month quarter, not the 9-month YTD). Amounts are returned in INR crore, ratios as percent. Every
outcome (http status, facts parsed, which tag matched, what is missing, a sample of tags) is recorded so a failed
or partial extraction is always visible in the debug report, never silent.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date
from typing import Any, Callable

from app.mcp_server.tools import nse as nse_tools
from app.service.logger import get_logger

log = get_logger(__name__)

# Asset-quality / interest KPIs matched by normalised (lowercased, alphanumeric-only) substring rules.
_KPI_RULES: dict[str, dict[str, tuple[str, ...]]] = {
    "gross_npa_pct":  {"has_any": ("grossnpa", "grossnonperforming"), "with_any": ("percentage", "ratio", "percent"), "without": ()},
    "net_npa_pct":    {"has_any": ("netnpa", "netnonperforming", "netnpi"), "with_any": ("percentage", "ratio", "percent"), "without": ("gross",)},
    "gross_npa_amt":  {"has_any": ("grossnpa", "grossnonperforming"), "with_any": (), "without": ("percentage", "ratio", "percent", "net")},
    "net_npa_amt":    {"has_any": ("netnpa", "netnonperforming", "netnpi"), "with_any": (), "without": ("percentage", "ratio", "percent", "gross")},
    "crar_pct":       {"has_any": ("capitaladequacy", "capitaltoriskweighted", "crar"), "with_any": (), "without": ()},
    "interest_earned": {"has_any": ("interestearned",),              "with_any": (), "without": ("percentage", "ratio")},
    "interest_expended": {"has_any": ("interestexpended", "interestexpense"), "with_any": (), "without": ("percentage", "ratio")},
    "other_income":   {"has_any": ("otherincome",),                  "with_any": (), "without": ("percentage", "ratio")},
    "pcr_pct":        {"has_any": ("provisioncoverage", "provisioningcoverage"), "with_any": (), "without": ()},
}
# P&L and some ratios matched by EXACT normalised tag name (substrings are too ambiguous for these).
_XBRL_EXACT: dict[str, tuple[str, ...]] = {
    "total_income": ("income",),
    "op_profit":    ("operatingprofitbeforeprovisionandcontingencies",),
    "pbt":          ("profitlossfromordinaryactivitiesbeforetax", "profitbeforetax"),
    "pat":          ("profitlossaftertaxesminorityinterestandshareofprofitlossofassociates", "profitlossfortheperiod"),
    "eps":          ("basicearningspersharebeforeextraordinaryitems", "basicearningspershareafterextraordinaryitems", "basicearningspershare"),
    "crar_pct":     ("capitaladequacyratio", "capitaltoriskweightedassetsratio", "percentageofcapitaladequacyratio", "capitaladequacyratiobaseliii"),
    "net_npa_pct":  ("percentageofnetnpa", "percentageofnetnpi"),
    "net_npa_amt":  ("netnonperformingassets",),
}
_AMOUNT_KPIS = ("gross_npa_amt", "net_npa_amt", "interest_earned", "interest_expended", "other_income", "nii", "total_income", "pbt", "pat", "op_profit")
_REPORTED = ("gross_npa_pct", "net_npa_pct", "crar_pct", "nii", "total_income", "pbt", "pat", "eps", "op_profit", "pcr_pct")
_CORE_KPIS = ("gross_npa_pct", "net_npa_pct", "crar_pct")   # all present -> "ok"; some -> "partial"; none -> "unavailable"


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


def _days(start: str, end: str) -> int:
    try:
        return (date.fromisoformat(end[:10]) - date.fromisoformat(start[:10])).days
    except (ValueError, TypeError):
        return 10 ** 6


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


def _pick(occs: list[tuple[str, str, str, str]]) -> tuple[str, str, str, str] | None:
    """From occurrences (end, start, raw, tag) pick the latest end-date, then the SHORTEST duration (quarter, not YTD)."""
    numeric = [o for o in occs if _num(o[2]) is not None]
    if not numeric:
        return None
    max_end = max(o[0] for o in numeric)
    return min((o for o in numeric if o[0] == max_end), key=lambda o: _days(o[1], o[0]))


def parse_bank_kpis(xml_bytes: bytes) -> dict[str, Any]:
    """Pure function: XBRL bytes -> {status, kpis (crore / percent), matched, missing, facts_parsed, sample_tags, error}."""
    diag: dict[str, Any] = {"facts_parsed": 0, "contexts": 0, "kpis": {}, "matched": {}, "missing": [], "sample_tags": [], "error": None}
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        diag["status"] = "error"
        diag["error"] = f"XML ParseError: {str(exc)[:200]}"
        return diag

    # 1) context id -> (start, end). Instant contexts have start == end.
    ctx: dict[str, tuple[str, str]] = {}
    for el in root.iter():
        if _local(el.tag).lower() == "context":
            cid, start, end, inst = el.get("id", ""), "", "", ""
            for sub in el.iter():
                ln, txt = _local(sub.tag).lower(), (sub.text or "").strip()
                if ln == "startdate" and txt:
                    start = txt
                elif ln == "enddate" and txt:
                    end = txt
                elif ln == "instant" and txt:
                    inst = txt
            if cid:
                ctx[cid] = (start or inst, end or inst)
    diag["contexts"] = len(ctx)

    # 2) flatten facts: normalised local name -> list of (end, start, raw_value, local_name)
    facts: dict[str, list[tuple[str, str, str, str]]] = {}
    seen_tags: list[str] = []
    for el in root.iter():
        txt = (el.text or "").strip()
        if not txt:
            continue
        ln = _local(el.tag)
        if ln.lower() in ("context", "unit", "schemaref", "xbrl", "identifier", "measure"):
            continue
        norm = _norm(ln)
        if not norm:
            continue
        start, end = ctx.get(el.get("contextRef", ""), ("", ""))
        facts.setdefault(norm, []).append((end, start, txt, ln))
        if ln not in seen_tags:
            seen_tags.append(ln)
    diag["facts_parsed"] = sum(len(v) for v in facts.values())
    diag["sample_tags"] = seen_tags[:120]

    def record(kpi: str, hit: tuple[str, str, str, str]) -> None:
        diag["kpis"][kpi] = _num(hit[2])
        diag["matched"][kpi] = {"tag": hit[3], "context_end": hit[0] or None, "raw": hit[2]}

    # 3a) substring-rule KPIs (NPA / interest / PCR)
    for kpi, rule in _KPI_RULES.items():
        pred = _predicate(rule)
        cand: list[tuple[str, str, str, str]] = []
        for norm, occ in facts.items():
            if pred(norm):
                cand.extend(occ)
        hit = _pick(cand)
        if hit:
            record(kpi, hit)
    # 3b) exact-tag KPIs (P&L + CRAR / net-NPA the substring rules can miss). Do not overwrite a substring hit.
    for kpi, names in _XBRL_EXACT.items():
        if kpi in diag["kpis"]:
            continue
        for n in names:
            if n in facts and (hit := _pick(facts[n])):
                record(kpi, hit)
                break

    # 4) derive NII if both interest legs are present
    ie, iexp = diag["kpis"].get("interest_earned"), diag["kpis"].get("interest_expended")
    if ie is not None and iexp is not None and "nii" not in diag["kpis"]:
        diag["kpis"]["nii"] = ie - iexp
        diag["matched"]["nii"] = {"derived": "interest_earned - interest_expended"}

    # 5) units: amounts (raw INR) -> INR crore; a ratio stored as a fraction (<1) -> percent
    for k in list(diag["kpis"].keys()):
        v = diag["kpis"][k]
        if v is None:
            continue
        if k.endswith("_pct"):
            diag["kpis"][k] = round(v * 100, 2) if abs(v) < 1 else round(v, 2)
        elif k in _AMOUNT_KPIS:
            diag["kpis"][k] = round(v / 1e7, 2)
        else:
            diag["kpis"][k] = round(v, 2)

    diag["missing"] = [k for k in _REPORTED if k not in diag["kpis"]]
    found_core = [k for k in _CORE_KPIS if k in diag["kpis"]]
    diag["status"] = "ok" if len(found_core) == len(_CORE_KPIS) else ("partial" if diag["kpis"] else "unavailable")
    return diag


def fetch_bank_kpis(url: str, referer: str | None = None) -> dict[str, Any]:
    """Fetch an XBRL URL and parse it. NEVER raises: all failures are returned as `status`/`error` in the diagnostics."""
    out: dict[str, Any] = {"url": url, "http_status": None, "bytes": 0}
    try:
        content, http_status = nse_tools.nse_get_bytes(url, referer=referer or f"{nse_tools.BASE}/")
        out["http_status"] = http_status
        out["bytes"] = len(content)
        if not content:
            out["status"] = "error"
            out["error"] = "empty response body"
            return out
        out.update(parse_bank_kpis(content))
        return out
    except Exception as exc:  # noqa: BLE001 - any failure must surface in the report, not abort the agent
        out["status"] = "error"
        out["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        log.warning("xbrl fetch/parse failed for %s: %s", url, out["error"])
        return out

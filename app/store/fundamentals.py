"""Quarterly results store + deterministic metrics (QoQ / YoY / streaks). All maths here is plain Python, not LLM.

Units: INR crore. Sources: "import" (your own CSV), "nse" (financial-results filings), "yahoo".
A series never mixes sources: `pick_series` returns one source (preferring import > nse > yahoo, needing >= MIN_QUARTERS).
"""
from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.mcp_server.tools.common import now_iso
from app.store.db import connect

FIELDS = ("revenue", "op_profit", "pbt", "pat", "eps")
SOURCE_PRIORITY = ("import", "nse", "yahoo")
MIN_QUARTERS = 6
MAX_QUARTERS = 16


def upsert_quarters(rows: list[dict[str, Any]]) -> int:
    n = 0
    with connect() as con:
        for r in rows:
            con.execute(
                "INSERT OR REPLACE INTO fundamentals_q (symbol, period_end, basis, source, revenue, op_profit, op_kind, pbt, pat, eps, reported_on, fetched_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["symbol"], r["period_end"], r.get("basis", "consolidated"), r["source"], r.get("revenue"), r.get("op_profit"),
                 r.get("op_kind"), r.get("pbt"), r.get("pat"), r.get("eps"), r.get("reported_on"), now_iso()))
            n += 1
    return n


def latest_period_end(symbol: str, as_of: date) -> date | None:
    with connect() as con:
        row = con.execute("SELECT MAX(period_end) m FROM fundamentals_q WHERE symbol=? AND period_end<=?", (symbol, as_of.isoformat())).fetchone()
    return date.fromisoformat(row["m"]) if row and row["m"] else None


def source_counts(symbol: str) -> list[dict[str, Any]]:
    """How many stored quarters exist per (source, basis) — so a report can see whether NSE rows ever landed."""
    with connect() as con:
        rows = con.execute("SELECT source, basis, COUNT(*) n, MAX(period_end) latest FROM fundamentals_q WHERE symbol=? GROUP BY source, basis",
                           (symbol,)).fetchall()
    return [{"source": r["source"], "basis": r["basis"], "n": r["n"], "latest": r["latest"]} for r in rows]


def pick_series(symbol: str, as_of: date) -> tuple[list[dict[str, Any]], str | None, str | None]:
    """Return (rows oldest->newest, source, basis). Consolidated preferred; one source only."""
    with connect() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM fundamentals_q WHERE symbol=? AND period_end<=? ORDER BY period_end", (symbol, as_of.isoformat()))]
    if not rows:
        return [], None, None
    best: tuple[int, list[dict[str, Any]], str, str] | None = None
    for src in SOURCE_PRIORITY:
        for basis in ("consolidated", "standalone"):
            sel = [r for r in rows if r["source"] == src and r["basis"] == basis]
            if sel and (best is None or (len(sel) >= MIN_QUARTERS > best[0]) or (len(sel) > best[0] and best[0] < MIN_QUARTERS)):
                best = (len(sel), sel, src, basis)
                if len(sel) >= MIN_QUARTERS:
                    return sel[-MAX_QUARTERS:], src, basis
    assert best is not None
    return best[1][-MAX_QUARTERS:], best[2], best[3]


def _pct(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old <= 0:   # growth from zero/negative is not meaningful
        return None
    return round((new / old - 1) * 100, 2)


def _qord(period_end: str) -> int:
    """Calendar-quarter ordinal: consecutive quarters differ by exactly 1, regardless of gaps in the series.
    Mar/Jun/Sep/Dec quarter-ends map to 0/1/2/3 within a year (robust to filings dated on the 29th/30th/31st)."""
    d = date.fromisoformat(period_end)
    return d.year * 4 + (d.month - 1) // 3


def _label(qord: int) -> str:
    """Human label for a quarter ordinal, e.g. 'Q1 (Apr-Jun) 2025'."""
    year, q = divmod(qord, 4)
    name = ("Jan-Mar", "Apr-Jun", "Jul-Sep", "Oct-Dec")[q]
    return f"Q{q + 1} ({name}) {year}"


def compute_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """rows oldest->newest. For each metric: QoQ changes (qoq_1 = latest quarter vs the immediately preceding calendar
    quarter), YoY changes (yoy_1 = latest quarter vs the SAME quarter one year earlier), a consecutive up/down streak,
    and the latest margin trend.

    All comparisons are aligned by *calendar quarter*, never by row position. If the series has a hole (a missing
    quarter), the affected QoQ/YoY value is `None` and the reason is recorded in `<metric>.null_reasons` rather than
    being silently computed against the wrong quarter.
    """
    out: dict[str, Any] = {"quarters_available": len(rows), "history_short": len(rows) < MAX_QUARTERS, "gaps": _gaps(rows), "metrics": {}}
    if not rows:
        return out
    ords = [_qord(r["period_end"]) for r in rows]
    latest_ord, earliest_ord = ords[-1], ords[0]
    for f in FIELDS:
        vals = [r.get(f) for r in rows]
        if sum(v is not None for v in vals) < 2:
            continue
        by_ord = {o: v for o, v in zip(ords, vals)}
        null_reasons: list[str] = []

        def _cmp(new_o: int, old_o: int) -> float | None:
            v = _pct(by_ord.get(new_o), by_ord.get(old_o))
            if v is None and earliest_ord <= old_o <= latest_ord and (old_o not in by_ord or new_o not in by_ord):
                miss = _label(old_o if old_o not in by_ord else new_o)
                msg = f"{miss} quarter missing from series"
                if msg not in null_reasons:
                    null_reasons.append(msg)
            return v

        # qoq_k compares the quarter k-1 before latest with the quarter k before latest (both must sit within the span).
        qoq = {}
        for k in range(1, 7):
            new_o, old_o = latest_ord - (k - 1), latest_ord - k
            if old_o >= earliest_ord:
                qoq[f"qoq_{k}"] = _cmp(new_o, old_o)
        # yoy_k compares the quarter k-1 before latest with the SAME quarter one year (4 quarters) earlier.
        yoy = {}
        for k in range(1, 5):
            new_o, old_o = latest_ord - (k - 1), latest_ord - (k - 1) - 4
            if old_o >= earliest_ord:
                yoy[f"yoy_{k}"] = _cmp(new_o, old_o)

        # streak: consecutive up/down moves between adjacent *calendar* quarters; a gap ends the streak.
        streak = 0
        for i in range(len(vals) - 1, 0, -1):
            if ords[i] - ords[i - 1] != 1 or vals[i] is None or vals[i - 1] is None or vals[i] == vals[i - 1]:
                break
            d = 1 if vals[i] > vals[i - 1] else -1
            if streak and (d > 0) != (streak > 0):
                break
            streak += d

        metric = {"latest": vals[-1], "latest_period": rows[-1]["period_end"], **qoq, **yoy, "streak_quarters": streak,
                  "kind": rows[-1].get("op_kind") if f == "op_profit" else None}
        if null_reasons:
            metric["null_reasons"] = null_reasons
        out["metrics"][f] = metric

    # net margin: latest quarter vs the SAME quarter one year earlier (calendar-aligned, not rows[-5]).
    rev_by = {o: r.get("revenue") for o, r in zip(ords, rows)}
    pat_by = {o: r.get("pat") for o, r in zip(ords, rows)}
    ly = latest_ord - 4
    if rev_by.get(latest_ord) and pat_by.get(latest_ord) is not None:
        nm: dict[str, Any] = {"latest": round(pat_by[latest_ord] / rev_by[latest_ord] * 100, 2)}
        if rev_by.get(ly) and pat_by.get(ly) is not None:
            nm["year_ago"] = round(pat_by[ly] / rev_by[ly] * 100, 2)
        else:
            nm["year_ago"] = None
            nm["year_ago_note"] = f"{_label(ly)} quarter not in series"
        out["net_margin_pct"] = nm
    return out


def _sum(vals: list[Any]) -> float | None:
    present = [v for v in vals if isinstance(v, (int, float))]
    return round(sum(present), 2) if present else None


def _trend(series: list[Any]) -> str:
    xs = [v for v in series if isinstance(v, (int, float))]
    if len(xs) < 2:
        return "unknown"
    if xs[-1] > xs[0] and all(b >= a for a, b in zip(xs, xs[1:])):
        return "improving"
    if xs[-1] < xs[0] and all(b <= a for a, b in zip(xs, xs[1:])):
        return "deteriorating"
    return "mixed"


def _divergence_signal(lt_trend: str | None, st_trend: str | None) -> dict[str, str]:
    """Characterise the relationship between a 3-year track record and the most-recent-3-quarter momentum.
    The four meaningful combinations map to actionable labels and an interpretation the model can cite."""
    sig_map = {
        ("deteriorating", "improving"): ("turnaround",
            "LT deteriorating but 3Q accelerating — possible early turnaround; treat cautiously until 2+ quarters confirm."),
        ("improving", "deteriorating"): ("slowdown",
            "LT improving but 3Q decelerating — established growth potentially stalling; lower conviction for bullish calls."),
        ("improving", "improving"): ("sustained_growth",
            "Both LT and ST improving — highest-conviction bullish fundamental read; durable, not a one-quarter spike."),
        ("deteriorating", "deteriorating"): ("sustained_decline",
            "Both LT and ST deteriorating — highest-conviction bearish read; avoid attributing the decline to a one-off."),
    }
    key = (lt_trend, st_trend)
    sig, interp = sig_map.get(key, ("mixed", "Mixed or insufficient signals between long-term and short-term."))
    return {"signal": sig, "lt_trend": lt_trend or "unknown", "st_trend": st_trend or "unknown", "interpretation": interp}


def horizon_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Long-term (last 3 complete fiscal years, Apr-Mar) and short-term (last 3 quarters) views of the series.
    Fiscal year of a quarter ending month m, year y = y if m <= 3 else y+1 (so Jun/Sep/Dec/Mar roll into one FY).

    EPS split-adjustment: Indian companies sometimes change face value mid-series, causing a step-change in
    reported EPS (e.g. CANBK ₹10→₹2 FV → apparent 5× EPS drop from one quarter to the next).
    We detect the largest single-step EPS drop in the series and, if it looks like a split (ratio is a round
    2× / 5× / 10× factor and it is not matched by a similar PAT drop), we scale all earlier quarters' EPS
    down to the current face value before aggregating. PAT-based metrics are unaffected."""
    if not rows:
        return {"long_term_3y": None, "short_term_3q": None}

    # ── EPS split detection and normalisation ──────────────────────────────
    eps_vals = [r.get("eps") for r in rows if r.get("eps") and r.get("eps") > 0]
    split_ratios: list[tuple[int, float]] = []  # (row_index after split, ratio)
    for i in range(1, len(rows)):
        a, b = rows[i - 1].get("eps"), rows[i].get("eps")
        if not (a and b and a > 0 and b > 0):
            continue
        ratio = a / b
        # A genuine stock split / face-value change produces a sharp step that's a round factor
        # AND the PAT in the same quarter does NOT drop by the same ratio.
        pat_a, pat_b = rows[i - 1].get("pat"), rows[i].get("pat")
        if ratio > 1.5 and any(abs(ratio - f) < 0.25 for f in (2, 4, 5, 10)):
            pat_drop = (pat_a / pat_b) if pat_a and pat_b and pat_b > 0 else 1.0
            if pat_drop < 1.3:   # PAT did not drop proportionally → it's a split, not a real EPS collapse
                split_ratios.append((i, ratio))
    # Apply the first detected split: scale all rows before the split point down
    rows = [dict(r) for r in rows]  # make mutable copies
    for split_idx, ratio in split_ratios:
        for i in range(split_idx):
            if rows[i].get("eps") is not None:
                rows[i]["eps"] = round(rows[i]["eps"] / ratio, 4)

    fy: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        d = date.fromisoformat(r["period_end"])
        fy.setdefault(d.year if d.month <= 3 else d.year + 1, []).append(r)
    years = []
    for y in sorted(fy):
        qs = fy[y]
        if len(qs) < 4:                     # skip partial years (incomplete history / FY still in progress)
            continue
        years.append({"fy": f"FY{y}", "revenue": _sum([q.get("revenue") for q in qs]),
                      "pat": _sum([q.get("pat") for q in qs]), "eps": _sum([q.get("eps") for q in qs])})
    years = years[-3:]
    long_term = None
    if years:
        def _cagr(key: str) -> float | None:
            a, b = years[0].get(key), years[-1].get(key)
            if len(years) >= 2 and isinstance(a, (int, float)) and isinstance(b, (int, float)) and a > 0:
                return round(((b / a) ** (1 / (len(years) - 1)) - 1) * 100, 2)
            return None
        yoy = [{"fy": b["fy"], "pat_yoy": _pct(b.get("pat"), a.get("pat")),
                "revenue_yoy": _pct(b.get("revenue"), a.get("revenue"))} for a, b in zip(years, years[1:])]
        long_term = {"years": years, "pat_cagr_pct": _cagr("pat"), "revenue_cagr_pct": _cagr("revenue"),
                     "yoy": yoy, "trend": _trend([y.get("pat") for y in years])}
    last3 = rows[-3:]
    short_term = {
        "quarters": [{"period_end": q["period_end"], "revenue": q.get("revenue"), "pat": q.get("pat"),
                      "eps": q.get("eps")} for q in last3],
        "pat_qoq_pct": [_pct(b.get("pat"), a.get("pat")) for a, b in zip(last3, last3[1:])],
        "trend": _trend([q.get("pat") for q in last3]),
    }
    return {"long_term_3y": long_term, "short_term_3q": short_term,
            "divergence": _divergence_signal(long_term.get("trend") if long_term else None,
                                             short_term.get("trend") if short_term else None)}


def _gaps(rows: list[dict[str, Any]]) -> list[str]:
    gaps = []
    for a, b in zip(rows, rows[1:]):
        days = (date.fromisoformat(b["period_end"]) - date.fromisoformat(a["period_end"])).days
        if days > 100:
            gaps.append(f"missing quarter(s) between {a['period_end']} and {b['period_end']}")
    return gaps


def import_csv(path: str | Path, source: str = "import") -> int:
    """Load your own history (e.g. a Screener/Excel export saved as CSV). Columns: symbol, period_end (YYYY-MM-DD), revenue,
    op_profit, pbt, pat, eps; optional: basis, op_kind. Values in INR crore."""
    rows = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            num = lambda k: float(r[k]) if r.get(k) not in (None, "", "-") else None  # noqa: E731
            rows.append({"symbol": r["symbol"].strip().upper(), "period_end": datetime.fromisoformat(r["period_end"]).date().isoformat(),
                         "basis": (r.get("basis") or "consolidated").lower(), "source": source, "op_kind": r.get("op_kind") or None,
                         **{f: num(f) for f in FIELDS}})
    return upsert_quarters(rows)

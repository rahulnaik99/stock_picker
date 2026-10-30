"""TradingView-style interactive HTML chart using Plotly.

Combines TECHNICAL + FUNDAMENTAL data in one zoomable/pannable page:
- Main panel:  1D candlesticks, EMA20/50/200, demand (green) / supply (red) zones, event markers
- Fundamental bar sub-panel: quarterly PAT (INR cr) as bars with YoY % labels
- Volume sub-panel with 20-bar MA
- KPI badge: GNPA / NNPA / NII / CET1 rendered as annotations
- Right panel: latest 15-min candles for entry timing (last 5 days zoomed in)

Output: self-contained HTML (no server needed, opens in any browser).
"""
from __future__ import annotations

import json
from typing import Any

import pandas as pd


# ── color palette ────────────────────────────────────────────────────────────
_UP   = "#26a69a"
_DOWN = "#ef5350"
_DEMAND = "rgba(38,166,154,0.18)"
_SUPPLY = "rgba(239,83,80,0.18)"
_EMA_COLORS = {"EMA20": "#f39c12", "EMA50": "#3498db", "EMA200": "#9b59b6"}
_PAT_POS = "#26a69a"
_PAT_NEG = "#ef5350"
_BG = "#131722"
_GRID = "#1e2230"
_TEXT = "#d1d4dc"


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _fmt_cr(v: float | None) -> str:
    if v is None: return "—"
    if abs(v) >= 1e4: return f"₹{v/1e4:.1f}k cr"
    return f"₹{v:.0f} cr"


def plot_interactive_html(
    symbol: str,
    daily: pd.DataFrame,
    intraday_15m: pd.DataFrame | None,
    zones: dict[str, Any],
    events: list[dict[str, Any]] | None = None,
    fundamentals: dict[str, Any] | None = None,   # quarterly_fundamentals data dict
    bank_kpis: dict[str, Any] | None = None,       # bank_kpis_nse kpis dict
) -> str:
    """Return a self-contained HTML string (Plotly CDN)."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    daily = daily.copy()
    daily["dt"] = pd.to_datetime(daily["time"])

    # ── figure layout ─────────────────────────────────────────────────────
    has_fund = fundamentals is not None
    row_heights = [0.55, 0.2, 0.15, 0.10] if has_fund else [0.65, 0.2, 0.15]
    n_rows = len(row_heights)
    row_titles = (["Price & Technicals", "", "Quarterly PAT (INR cr)", "Volume"]
                  if has_fund else ["Price & Technicals", "", "Volume"])

    fig = make_subplots(
        rows=n_rows, cols=1,
        shared_xaxes=True,
        row_heights=row_heights,
        vertical_spacing=0.02,
        specs=[[{"secondary_y": True}]] + [[{"secondary_y": False}]] * (n_rows - 1),
    )

    dt  = daily["dt"]
    op  = daily["open"]
    hi  = daily["high"]
    lo  = daily["low"]
    cl  = daily["close"]
    vol = daily["volume"]

    # ── candlesticks ──────────────────────────────────────────────────────
    fig.add_trace(go.Candlestick(
        x=dt, open=op, high=hi, low=lo, close=cl,
        name="Price",
        increasing_line_color=_UP, decreasing_line_color=_DOWN,
        increasing_fillcolor=_UP, decreasing_fillcolor=_DOWN,
        line_width=1, whiskerwidth=0.5,
        showlegend=True,
    ), row=1, col=1, secondary_y=False)

    # ── EMAs ──────────────────────────────────────────────────────────────
    for span, name in ((20, "EMA20"), (50, "EMA50"), (200, "EMA200")):
        if len(cl) >= span:
            fig.add_trace(go.Scatter(
                x=dt, y=_ema(cl, span), name=name,
                line=dict(color=_EMA_COLORS[name], width=1.2),
                hovertemplate=f"{name}: %{{y:.2f}}<extra></extra>",
            ), row=1, col=1, secondary_y=False)

    # ── demand / supply zones ─────────────────────────────────────────────
    x0, x1 = daily["dt"].iloc[0], daily["dt"].iloc[-1]
    for z in (zones.get("demand_zones") or []):
        fig.add_hrect(y0=z["low"], y1=z["high"],
                      fillcolor=_DEMAND, line_width=0,
                      annotation_text=f"D str={z.get('strength',0):.0f}", annotation_position="left",
                      annotation_font_size=9, annotation_font_color="#26a69a", row=1, col=1)
    for z in (zones.get("supply_zones") or []):
        fig.add_hrect(y0=z["low"], y1=z["high"],
                      fillcolor=_SUPPLY, line_width=0,
                      annotation_text=f"S str={z.get('strength',0):.0f}", annotation_position="left",
                      annotation_font_size=9, annotation_font_color="#ef5350", row=1, col=1)

    # ── NSE event markers ─────────────────────────────────────────────────
    _TYPE_ICON = {"results": "★", "fundraise": "₹", "credit_rating": "R",
                  "management_change": "M", "regulatory_action": "⚑", "analyst_meet": "A"}
    for ev in (events or []):
        try:
            edt = pd.to_datetime(ev.get("event_date") or ev.get("announcement_date"))
            icon = _TYPE_ICON.get(ev.get("event_type", ""), "•")
            row_match = daily[daily["dt"].dt.date == edt.date()]
            if row_match.empty: continue
            y_val = float(row_match["high"].iloc[0]) * 1.004
            fig.add_trace(go.Scatter(
                x=[row_match["dt"].iloc[0]], y=[y_val],
                mode="markers+text",
                marker=dict(symbol="triangle-up", size=10,
                            color="#f1c40f" if ev.get("event_type") == "results" else "#95a5a6"),
                text=[icon], textposition="top center",
                name=f"{ev.get('event_type','ev')} {edt.date()}",
                hovertext=f"{ev.get('event_summary','')[:80]}",
                hoverinfo="text", showlegend=False,
            ), row=1, col=1, secondary_y=False)
        except Exception:  # noqa: BLE001
            pass

    # ── Fundamental PAT overlay (secondary y on the price chart) ─────────
    if has_fund:
        qtrs = fundamentals.get("quarters", [])
        if qtrs:
            q_dates = [pd.to_datetime(q["period_end"]) for q in qtrs[-12:]]
            q_pat   = [q.get("pat") for q in qtrs[-12:]]
            q_yoy   = []
            for i, q in enumerate(qtrs[-12:]):
                yoy_key = None
                # find same quarter last year
                if i >= 4 and qtrs[-12:][i - 4].get("pat") and q.get("pat"):
                    yoy = (q["pat"] / qtrs[-12:][i - 4]["pat"] - 1) * 100
                    q_yoy.append(f"{yoy:+.1f}%")
                else:
                    q_yoy.append("")
            colors_pat = [_PAT_POS if (p or 0) > 0 else _PAT_NEG for p in q_pat]
            # PAT bars on the PAT sub-row
            fund_row = 3 if has_fund else None
            fig.add_trace(go.Bar(
                x=q_dates, y=q_pat, name="Quarterly PAT",
                marker_color=colors_pat, opacity=0.85,
                text=[f"{_fmt_cr(p)}<br>{y}" for p, y in zip(q_pat, q_yoy)],
                textposition="inside", textfont=dict(size=8, color=_TEXT),
                hovertemplate="PAT: %{y:.0f} cr<extra></extra>",
                showlegend=True,
            ), row=fund_row, col=1)

    # ── volume ────────────────────────────────────────────────────────────
    vol_row = n_rows
    vol_colors = [_UP if c >= o else _DOWN for c, o in zip(cl, op)]
    fig.add_trace(go.Bar(
        x=dt, y=vol, name="Volume", marker_color=vol_colors, opacity=0.7,
        hovertemplate="Vol: %{y:,.0f}<extra></extra>", showlegend=False,
    ), row=vol_row, col=1)
    # vol MA20
    if len(vol) >= 20:
        fig.add_trace(go.Scatter(
            x=dt, y=vol.rolling(20, min_periods=1).mean(),
            name="Vol MA20", line=dict(color="#888", width=1, dash="dot"), showlegend=False,
        ), row=vol_row, col=1)

    # ── bank KPI badge ────────────────────────────────────────────────────
    if bank_kpis:
        kpi_lines = []
        for key, label in (("gross_npa_pct", "GNPA"), ("net_npa_pct", "NNPA"),
                           ("cet1_pct", "CET1"), ("at1_pct", "AT1")):
            v = bank_kpis.get(key)
            if v is not None:
                kpi_lines.append(f"{label}: {v*100 if v < 1 else v:.2f}%")
        nii = bank_kpis.get("nii")
        if nii:
            kpi_lines.append(f"NII: {_fmt_cr(nii)}")
        if kpi_lines:
            fig.add_annotation(
                text="<b>Bank KPIs</b><br>" + "<br>".join(kpi_lines),
                xref="paper", yref="paper", x=0.01, y=0.99,
                showarrow=False, align="left", bgcolor="rgba(19,23,34,0.8)",
                font=dict(size=10, color=_TEXT), bordercolor="#444", borderwidth=1,
            )

    # ── layout & theme ────────────────────────────────────────────────────
    fig.update_layout(
        title=dict(text=f"<b>{symbol}</b> — Technical + Fundamental (interactive)",
                   font=dict(color=_TEXT, size=15)),
        paper_bgcolor=_BG, plot_bgcolor=_BG,
        font=dict(color=_TEXT, family="Inter, sans-serif", size=11),
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
        legend=dict(bgcolor="rgba(19,23,34,0.7)", bordercolor="#333",
                    font=dict(size=10), orientation="h", y=1.02, x=0),
        margin=dict(l=55, r=20, t=55, b=30),
        height=820,
    )
    # dark grid on every axis
    for i in range(1, n_rows + 1):
        ax = f"xaxis{i if i > 1 else ''}"
        ay = f"yaxis{i if i > 1 else ''}"
        fig.update_layout(**{
            ax: dict(gridcolor=_GRID, zerolinecolor=_GRID, showgrid=True, tickfont_size=9),
            ay: dict(gridcolor=_GRID, zerolinecolor=_GRID, showgrid=True,
                     tickprefix="₹", tickfont_size=9),
        })
    # hide x labels on all but bottom row
    for i in range(1, n_rows):
        fig.update_xaxes(showticklabels=False, row=i, col=1)
    # price axis: no ₹ prefix needed (already obvious)
    fig.update_yaxes(tickprefix="₹", row=1, col=1, secondary_y=False)

    # ── 15-min intraday subplot: second figure appended ───────────────────
    html_15m = ""
    if intraday_15m is not None and len(intraday_15m) >= 10:
        i15 = intraday_15m.copy()
        i15["dt"] = pd.to_datetime(i15["time"])
        fig15 = go.Figure()
        fig15.add_trace(go.Candlestick(
            x=i15["dt"], open=i15["open"], high=i15["high"],
            low=i15["low"], close=i15["close"],
            name="15-min",
            increasing_line_color=_UP, decreasing_line_color=_DOWN,
            increasing_fillcolor=_UP, decreasing_fillcolor=_DOWN,
            line_width=0.8,
        ))
        # EMA9 + EMA20 on 15m
        for span, col_ in ((9, "#f39c12"), (20, "#3498db")):
            if len(i15) >= span:
                fig15.add_trace(go.Scatter(
                    x=i15["dt"], y=_ema(i15["close"], span),
                    name=f"EMA{span}", line=dict(color=col_, width=1),
                ))
        # plan levels as horizontal lines
        nd = zones.get("nearest_demand") or {}
        ns = zones.get("nearest_supply") or {}
        if nd.get("high"):
            fig15.add_hline(y=nd["high"], line_dash="dash", line_color="#26a69a",
                            annotation_text="Demand top", annotation_position="right",
                            annotation_font_color="#26a69a", annotation_font_size=9)
        if ns.get("low"):
            fig15.add_hline(y=ns["low"], line_dash="dash", line_color="#ef5350",
                            annotation_text="Supply low", annotation_position="right",
                            annotation_font_color="#ef5350", annotation_font_size=9)
        fig15.update_layout(
            title=dict(text=f"<b>{symbol}</b> — 15-min entry timing (last 5 days, zoomable)",
                       font=dict(color=_TEXT, size=13)),
            paper_bgcolor=_BG, plot_bgcolor=_BG,
            font=dict(color=_TEXT, size=10),
            xaxis_rangeslider_visible=False,
            hovermode="x unified", height=380,
            legend=dict(bgcolor="rgba(19,23,34,0.7)", bordercolor="#333",
                        font=dict(size=9), orientation="h", y=1.02),
            margin=dict(l=55, r=20, t=50, b=30),
            xaxis=dict(gridcolor=_GRID, zerolinecolor=_GRID),
            yaxis=dict(gridcolor=_GRID, zerolinecolor=_GRID, tickprefix="₹"),
        )
        html_15m = fig15.to_html(full_html=False, include_plotlyjs=False)

    main_html = fig.to_html(full_html=False, include_plotlyjs="cdn")

    # combine into one page
    page = f"""<!doctype html><html><head>
<meta charset="utf-8">
<title>{symbol} — Interactive Chart</title>
<style>
  body {{ margin:0; background:{_BG}; color:{_TEXT}; font-family:Inter,sans-serif; }}
  h2   {{ padding:8px 16px; margin:0; font-size:14px; color:{_TEXT}; }}
  .divider {{ border-top:1px solid #2a2e39; margin:4px 0; }}
</style>
</head><body>
<h2>{symbol} — Technical + Fundamental Interactive Chart &nbsp;
  <small style="font-weight:normal;opacity:.6">Scroll or pinch to zoom · drag to pan · double-click to reset</small>
</h2>
{main_html}
{"<div class='divider'></div>" if html_15m else ""}
{html_15m}
</body></html>"""
    return page

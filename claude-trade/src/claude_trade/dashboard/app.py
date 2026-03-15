"""Claude-Trade Dashboard v2 — 实时交易控制面板

Features
--------
• Candlestick K-line chart with volume subplot + MA5 / MA20
• Live data from Binance public API (crypto) or yfinance (stocks) when engine offline
• 6 metric cards: 总资产 / Net PnL / 浮盈 / 已实现 / 费用 / 成交数
• Regime gauge, asset-class donut, target-weight bar
• Positions table + full trade log
• Tab layout: K线 ｜ 总览 ｜ 订单
• Auto-refresh every 10 s

Launch:  claude-trade dashboard
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

try:
    import dash
    from dash import dcc, html, dash_table, Input, Output
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
except ImportError as exc:
    raise ImportError("Run: pip install dash plotly") from exc

# ── Paths ─────────────────────────────────────────────────────────────────────
_DASH_DIR     = Path(__file__).resolve().parent
_PROJECT_ROOT = _DASH_DIR.parents[2]
_RUNTIME_DIR  = _PROJECT_ROOT / "runtime"
_STATUS_FILE  = _RUNTIME_DIR / "status.json"
_HISTORY_FILE = _RUNTIME_DIR / "account_history.jsonl"
_MARKET_DIR   = _RUNTIME_DIR / "market_data"

# ── Palette ───────────────────────────────────────────────────────────────────
BG_DARK   = "#0a0e17"
BG_PANEL  = "#0f1520"
BG_CARD   = "#131c2e"
BG_CARD2  = "#182236"
BORDER    = "#1e2d45"
BORDER2   = "#243450"
TEXT_PRI  = "#e2eaf5"
TEXT_SEC  = "#6b83a6"
TEXT_DIM  = "#3d5270"
GREEN     = "#21c55d"
GREEN2    = "#16a34a"
RED       = "#ef4444"
RED2      = "#dc2626"
YELLOW    = "#eab308"
BLUE      = "#3b82f6"
BLUE2     = "#2563eb"
PURPLE    = "#a855f7"
ORANGE    = "#f97316"
TEAL      = "#14b8a6"

REGIME_COLOURS = {
    "CRISIS":   RED,
    "CAUTIOUS": YELLOW,
    "NEUTRAL":  BLUE,
    "BULLISH":  GREEN,
    "EUPHORIA": ORANGE,
}

_DARK_LAYOUT: dict = dict(
    plot_bgcolor=BG_CARD,
    paper_bgcolor=BG_CARD,
    font=dict(color=TEXT_PRI, family="'Inter', 'SF Pro Display', system-ui, sans-serif", size=12),
    margin=dict(l=8, r=8, t=36, b=8),
    legend=dict(
        bgcolor=BG_CARD2, bordercolor=BORDER2, borderwidth=1,
        font=dict(color=TEXT_SEC, size=11),
        orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0,
    ),
)


# ══════════════════════════════════════════════════════════════════════════════
# Data helpers
# ══════════════════════════════════════════════════════════════════════════════

def _read_status() -> dict[str, Any]:
    if not _STATUS_FILE.exists():
        return {}
    try:
        return json.loads(_STATUS_FILE.read_text("utf-8"))
    except Exception:
        return {}


def _read_history(n: int = 200) -> list[dict]:
    """Return last n rows from account_history.jsonl."""
    if not _HISTORY_FILE.exists():
        return []
    rows: list[dict] = []
    try:
        lines = _HISTORY_FILE.read_text("utf-8").splitlines()
        for line in lines[-n:]:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return rows


def _initial_capital() -> float:
    """Read INITIAL_CAPITAL from .env (best-effort)."""
    env_file = _PROJECT_ROOT / ".env"
    try:
        for line in env_file.read_text().splitlines():
            if line.startswith("INITIAL_CAPITAL="):
                return float(line.split("=", 1)[1].strip())
    except Exception:
        pass
    return 0.0


def _read_recent_jsonl(filename: str, n_days: int = 3) -> list[dict]:
    records: list[dict] = []
    if not _MARKET_DIR.exists():
        return records
    today = datetime.now(ZoneInfo("America/New_York")).date()
    for d in range(n_days):
        p = _MARKET_DIR / (today - timedelta(days=d)).strftime("%Y-%m-%d") / filename
        if p.exists():
            for line in p.read_text("utf-8").splitlines():
                if line.strip():
                    try:
                        records.append(json.loads(line))
                    except Exception:
                        pass
    return records


def _load_kline_from_cache(symbol: str, timeframe: str = "1d") -> pd.DataFrame:
    """Load OHLCV from cached klines.jsonl files."""
    records = _read_recent_jsonl("klines.jsonl", n_days=3)
    rows: list[dict] = []
    for rec in records:
        if rec.get("code") != symbol:
            continue
        if rec.get("tf", "1d") != timeframe:
            continue
        for r in rec.get("rows", []):
            rows.append(r)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    ts_col = "timestamp" if "timestamp" in df.columns else ("ts" if "ts" in df.columns else None)
    if ts_col is None:
        return pd.DataFrame()
    df["timestamp"] = pd.to_datetime(df[ts_col], errors="coerce")
    df = df.dropna(subset=["timestamp"]).sort_values("timestamp").drop_duplicates("timestamp")
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df[["timestamp", "open", "high", "low", "close", "volume"]].dropna()


def _fetch_binance_kline(symbol: str, timeframe: str = "1d", limit: int = 120) -> pd.DataFrame:
    """Fetch OHLCV from Binance public API (no auth required)."""
    try:
        import ccxt
        ex = ccxt.binance({"enableRateLimit": True})
        ohlcv = ex.fetch_ohlcv(symbol, timeframe, limit=limit)
        if not ohlcv:
            return pd.DataFrame()
        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df
    except Exception:
        return pd.DataFrame()


def _fetch_yfinance_kline(symbol: str, timeframe: str = "1d", limit: int = 120) -> pd.DataFrame:
    """Fetch OHLCV from Yahoo Finance for US stock symbols like 'US.SPY'."""
    try:
        import yfinance as yf
        ticker = symbol.removeprefix("US.")
        tf_map = {"1d": "1d", "1h": "1h", "4h": "1h", "1w": "1wk"}
        per_map = {"1d": "6mo", "1h": "60d", "4h": "60d", "1w": "2y"}
        interval = tf_map.get(timeframe, "1d")
        period   = per_map.get(timeframe, "6mo")
        hist = yf.Ticker(ticker).history(period=period, interval=interval)
        if hist.empty:
            return pd.DataFrame()
        hist = hist.reset_index()
        ts_col = "Datetime" if "Datetime" in hist.columns else "Date"
        df = pd.DataFrame({
            "timestamp": pd.to_datetime(hist[ts_col]),
            "open":   hist["Open"].astype(float),
            "high":   hist["High"].astype(float),
            "low":    hist["Low"].astype(float),
            "close":  hist["Close"].astype(float),
            "volume": hist["Volume"].astype(float),
        })
        return df.tail(limit).reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def get_kline(symbol: str, timeframe: str = "1d", limit: int = 120) -> pd.DataFrame:
    """Best-effort OHLCV loader: cache → Binance → yfinance."""
    df = _load_kline_from_cache(symbol, timeframe)
    if not df.empty and len(df) >= 10:
        return df.tail(limit)
    if "/" in symbol:
        df = _fetch_binance_kline(symbol, timeframe, limit)
        if not df.empty:
            return df
    if symbol.startswith("US."):
        df = _fetch_yfinance_kline(symbol, timeframe, limit)
        if not df.empty:
            return df
    return pd.DataFrame()


def _load_order_history() -> list[dict]:
    raw = _read_recent_jsonl("orders.jsonl", n_days=5)
    rows = []
    for rec in raw:
        if rec.get("action") != "submitted":
            continue
        for order in rec.get("orders", []):
            rows.append({
                "时间":  rec.get("ts", "")[:19].replace("T", " "),
                "品种":  order.get("symbol", ""),
                "方向":  order.get("side", ""),
                "数量":  order.get("quantity", ""),
                "价格":  order.get("limit_price", ""),
                "状态":  order.get("submit_status", order.get("status", "")),
                "模式":  rec.get("mode", ""),
            })
    return rows


def _calc_metrics(st: dict) -> dict:
    """Derive display metrics from status, history, and order history."""
    acct   = float(st.get("account_value", 0.0))
    init   = _initial_capital() or float(st.get("initial_capital", acct) or acct)
    orders = _load_order_history()

    # Fees: very rough estimate (0.05% per trade at avg price)
    total_fees = 0.0
    for o in orders:
        try:
            total_fees += float(o.get("数量", 0)) * float(o.get("价格", 0)) * 0.0005
        except Exception:
            pass

    net_pnl = acct - init if acct and init else 0.0
    net_pct = (net_pnl / init * 100) if init else 0.0

    # Daily change from history
    history  = _read_history(2)
    day_chg  = 0.0
    day_pct  = 0.0
    if len(history) >= 2:
        prev    = history[-2].get("account_value", acct)
        day_chg = acct - prev
        day_pct = (day_chg / prev * 100) if prev else 0.0

    return {
        "acct":      acct,
        "init":      init,
        "net_pnl":   net_pnl,
        "net_pct":   net_pct,
        "day_chg":   day_chg,
        "day_pct":   day_pct,
        "unrealized": float(st.get("unrealized_pnl", 0.0)),
        "realized":   float(st.get("realized_pnl", 0.0)),
        "fees":       total_fees,
        "trades":     len(orders),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Figure builders
# ══════════════════════════════════════════════════════════════════════════════

def _fig_empty(msg: str = "暂无数据 / No data") -> go.Figure:
    fig = go.Figure()
    fig.update_layout(
        **_DARK_LAYOUT,
        annotations=[dict(
            text=msg, x=0.5, y=0.5, showarrow=False,
            font=dict(size=13, color=TEXT_SEC),
        )],
        xaxis=dict(visible=False), yaxis=dict(visible=False),
    )
    return fig


def _fig_candlestick(df: pd.DataFrame, symbol: str, show_ma: bool = True) -> go.Figure:
    """Professional candlestick + volume chart."""
    if df.empty:
        return _fig_empty(f"{symbol}  暂无 K 线数据 — 引擎运行后自动加载")

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.02,
        row_heights=[0.75, 0.25],
    )

    # ── Candlestick
    fig.add_trace(go.Candlestick(
        x=df["timestamp"],
        open=df["open"], high=df["high"],
        low=df["low"],   close=df["close"],
        name=symbol,
        increasing=dict(line=dict(color=GREEN, width=1), fillcolor=GREEN2 + "bb"),
        decreasing=dict(line=dict(color=RED,   width=1), fillcolor=RED2   + "bb"),
        showlegend=False,
        hovertext=[
            f"O: {o:.4g}  H: {h:.4g}  L: {l:.4g}  C: {c:.4g}"
            for o, h, l, c in zip(df["open"], df["high"], df["low"], df["close"])
        ],
        hoverinfo="x+text",
    ), row=1, col=1)

    # ── Moving averages
    if show_ma:
        dfc = df.copy()
        if len(dfc) >= 5:
            dfc["ma5"] = dfc["close"].rolling(5).mean()
            fig.add_trace(go.Scatter(
                x=dfc["timestamp"], y=dfc["ma5"],
                mode="lines", name="MA 5",
                line=dict(color=YELLOW, width=1.2),
                hovertemplate="MA5: %{y:,.4g}<extra></extra>",
            ), row=1, col=1)
        if len(dfc) >= 20:
            dfc["ma20"] = dfc["close"].rolling(20).mean()
            fig.add_trace(go.Scatter(
                x=dfc["timestamp"], y=dfc["ma20"],
                mode="lines", name="MA 20",
                line=dict(color=PURPLE, width=1.2),
                hovertemplate="MA20: %{y:,.4g}<extra></extra>",
            ), row=1, col=1)
        if len(dfc) >= 60:
            dfc["ma60"] = dfc["close"].rolling(60).mean()
            fig.add_trace(go.Scatter(
                x=dfc["timestamp"], y=dfc["ma60"],
                mode="lines", name="MA 60",
                line=dict(color=TEAL, width=1.0, dash="dot"),
                hovertemplate="MA60: %{y:,.4g}<extra></extra>",
            ), row=1, col=1)

    # ── Volume bars
    vol_colors = [
        GREEN2 + "99" if row["close"] >= row["open"] else RED2 + "99"
        for _, row in df.iterrows()
    ]
    fig.add_trace(go.Bar(
        x=df["timestamp"], y=df["volume"],
        name="Volume",
        marker_color=vol_colors,
        showlegend=False,
        hovertemplate="成交量: %{y:,.0f}<extra></extra>",
    ), row=2, col=1)

    # ── Price annotation
    last  = float(df["close"].iloc[-1])
    first = float(df["close"].iloc[0])
    pct   = (last / first - 1) * 100 if first else 0
    chg_col = GREEN if pct >= 0 else RED
    sign    = "▲" if pct >= 0 else "▼"

    fig.update_layout(
        **_DARK_LAYOUT,
        height=480,
        margin=dict(l=8, r=60, t=44, b=8),
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
        hoverlabel=dict(bgcolor=BG_CARD2, bordercolor=BORDER2, font=dict(color=TEXT_PRI)),
        title=dict(
            text=(
                f"<b style='color:{TEXT_PRI}'>{symbol}</b>"
                f"  <span style='font-size:18px;color:{chg_col}'>${last:,.4g}</span>"
                f"  <span style='color:{chg_col};font-size:13px'>{sign} {abs(pct):.2f}%</span>"
            ),
            font=dict(size=15, color=TEXT_PRI),
            x=0, xanchor="left",
            pad=dict(t=4, l=4),
        ),
    )

    # ── Axes styling
    _ax = dict(
        gridcolor=BORDER, showgrid=True, zeroline=False,
        tickfont=dict(color=TEXT_SEC, size=10), showline=False,
    )
    fig.update_xaxes(**_ax, showticklabels=False, row=1, col=1)
    fig.update_xaxes(**_ax, showticklabels=True,  row=2, col=1)
    fig.update_yaxes(**_ax, tickformat=",.4g", side="right", row=1, col=1)
    fig.update_yaxes(**_ax, tickformat=".2s",  side="right", showgrid=False, row=2, col=1)

    return fig


def _fig_regime_gauge(score: float, label: str) -> go.Figure:
    colour = REGIME_COLOURS.get(label, BLUE)
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=score,
        number=dict(suffix="", font=dict(color=colour, size=28)),
        gauge=dict(
            axis=dict(
                range=[-1, 1],
                tickvals=[-1, -0.6, -0.25, 0.25, 0.6, 1],
                ticktext=["CRISIS", "CAUT.", "NEUT.", "NEUT.", "BULL.", "EUPH."],
                tickfont=dict(color=TEXT_SEC, size=8),
            ),
            bar=dict(color=colour, thickness=0.22),
            bgcolor=BG_CARD2, borderwidth=1, bordercolor=BORDER,
            steps=[
                dict(range=[-1.0, -0.60], color="#2a1010"),
                dict(range=[-0.60, -0.25], color="#2a2010"),
                dict(range=[-0.25,  0.25], color="#10182a"),
                dict(range=[ 0.25,  0.60], color="#102a14"),
                dict(range=[ 0.60,  1.00], color="#2a2608"),
            ],
            threshold=dict(line=dict(color=colour, width=2), thickness=0.75, value=score),
        ),
        title=dict(
            text=f"<b>{label}</b>",
            font=dict(color=colour, size=16),
        ),
    ))
    fig.update_layout(
        **_DARK_LAYOUT,
        height=210,
        margin=dict(l=16, r=16, t=32, b=8),
    )
    return fig


def _fig_alloc_donut(weights: dict[str, float]) -> go.Figure:
    if not weights:
        return _fig_empty("无仓位数据")
    classes: dict[str, float] = {}
    for sym, w in weights.items():
        if "/" in sym:
            cls = "Crypto"
        elif sym in {"US.AGG", "US.IEF", "US.TLT", "US.SHY", "US.BND"}:
            cls = "Bonds"
        else:
            cls = "Equity"
        classes[cls] = classes.get(cls, 0.0) + w
    cash = max(0.0, 1.0 - sum(classes.values()))
    if cash > 0.01:
        classes["Cash"] = cash
    palette = {"Equity": BLUE, "Crypto": ORANGE, "Bonds": GREEN, "Cash": TEXT_DIM}
    labels = list(classes.keys())
    values = [round(v * 100, 1) for v in classes.values()]
    fig = go.Figure(go.Pie(
        labels=labels, values=values,
        hole=0.58,
        marker=dict(colors=[palette.get(l, PURPLE) for l in labels],
                    line=dict(color=BG_DARK, width=2)),
        textfont=dict(color=TEXT_PRI, size=11),
        texttemplate="%{label}<br>%{value:.1f}%",
        hovertemplate="%{label}: %{value:.1f}%<extra></extra>",
    ))
    total_exp = sum(weights.values())
    fig.update_layout(
        **_DARK_LAYOUT,
        height=210,
        showlegend=False,
        margin=dict(l=8, r=8, t=8, b=8),
        annotations=[dict(
            text=f"<b>{total_exp*100:.0f}%</b><br><span style='color:{TEXT_SEC};font-size:10px'>Exposed</span>",
            x=0.5, y=0.5, font=dict(size=14, color=TEXT_PRI), showarrow=False,
        )],
    )
    return fig


def _fig_pnl_curve(history: list[dict], initial_capital: float) -> go.Figure:
    """Line chart of account value over time with initial capital reference."""
    if not history:
        return _fig_empty("暂无盈亏历史 — 引擎运行后自动积累数据")
    ts_list  = [h.get("ts", "") for h in history]
    val_list = [h.get("account_value", 0.0) for h in history]
    reg_list = [h.get("regime", "NEUTRAL") for h in history]

    try:
        ts_dt = [datetime.fromisoformat(t).astimezone(ZoneInfo("America/New_York")) for t in ts_list]
    except Exception:
        ts_dt = list(range(len(ts_list)))

    # Colour by regime
    colours = [REGIME_COLOURS.get(r, BLUE) for r in reg_list]

    fig = go.Figure()

    # Account value line
    last_val = val_list[-1] if val_list else 0.0
    line_col = GREEN if (not initial_capital or last_val >= initial_capital) else RED
    fig.add_trace(go.Scatter(
        x=ts_dt, y=val_list,
        mode="lines+markers",
        name="账户净值",
        line=dict(color=line_col, width=2),
        marker=dict(color=colours, size=5, line=dict(width=0)),
        hovertemplate="%{x|%m-%d %H:%M}<br>净值: $%{y:,.2f}<extra></extra>",
        fill="tozeroy",
        fillcolor=line_col + "12",
    ))

    # Initial capital reference line
    if initial_capital > 0:
        fig.add_hline(
            y=initial_capital,
            line=dict(color=TEXT_DIM, dash="dot", width=1),
            annotation_text=f"初始资金 ${initial_capital:,.0f}",
            annotation_font=dict(color=TEXT_DIM, size=10),
            annotation_position="right",
        )

    # P&L annotation
    if initial_capital > 0 and val_list:
        pnl     = val_list[-1] - initial_capital
        pnl_pct = pnl / initial_capital * 100
        sign    = "▲" if pnl >= 0 else "▼"
        col     = GREEN if pnl >= 0 else RED
        fig.add_annotation(
            x=ts_dt[-1] if ts_dt else 0, y=val_list[-1],
            text=f"<b>{sign} {pnl_pct:+.2f}%</b>",
            showarrow=False, xanchor="left",
            font=dict(color=col, size=13),
        )

    fig.update_layout(
        **_DARK_LAYOUT,
        height=280,
        margin=dict(l=8, r=80, t=28, b=8),
        hovermode="x unified",
        xaxis=dict(gridcolor=BORDER, showgrid=True, zeroline=False,
                   tickfont=dict(color=TEXT_SEC, size=10)),
        yaxis=dict(gridcolor=BORDER, showgrid=True, zeroline=False,
                   tickformat="$,.0f", side="right",
                   tickfont=dict(color=TEXT_SEC, size=10)),
        showlegend=False,
        title=dict(
            text="<b style='font-size:13px'>账户净值历史</b>",
            font=dict(size=13, color=TEXT_PRI), x=0, xanchor="left",
        ),
    )
    return fig


def _crypto_signals_panel(st: dict) -> html.Div:
    """Small card showing crypto-specific market signals."""
    rd      = st.get("regime_details", {})
    details = rd.get("details", {})

    def _row(label: str, value: str, colour: str = TEXT_PRI) -> html.Div:
        return html.Div([
            html.Span(label, style={"color": TEXT_SEC, "fontSize": "11px", "minWidth": "90px"}),
            html.Span(value, style={"color": colour, "fontSize": "12px", "fontWeight": "700",
                                    "fontFamily": "'SF Mono', monospace"}),
        ], style={"display": "flex", "justifyContent": "space-between",
                  "padding": "4px 0", "borderBottom": f"1px solid {BORDER}18"})

    crypto_pulse = rd.get("crypto_pulse", 0.0)
    funding_sig  = rd.get("funding_signal", 0.0)
    cross_asset  = rd.get("cross_asset_flow", 0.0)
    vol_regime   = rd.get("vol_regime", "—")
    vix          = details.get("vix_level")
    funding_rate = details.get("funding_rate")
    btc_weekend  = details.get("btc_weekend_return")

    def _sig_col(v: float) -> str:
        return GREEN if v > 0.1 else RED if v < -0.1 else TEXT_PRI

    rows: list[html.Div] = [
        _row("加密脉冲",   f"{crypto_pulse:+.3f}",   _sig_col(crypto_pulse)),
        _row("资金费率信号", f"{funding_sig:+.3f}",   RED if funding_sig > 0.2 else GREEN if funding_sig < -0.1 else TEXT_PRI),
        _row("跨资产流向",  f"{cross_asset:+.3f}",   _sig_col(cross_asset)),
        _row("波动制度",   vol_regime,
             RED if vol_regime == "high" else TEAL if vol_regime == "low" else TEXT_PRI),
    ]
    if vix is not None:
        rows.append(_row("VIX 恐慌指数",
                         f"{vix:.1f}",
                         RED if vix > 30 else YELLOW if vix > 20 else GREEN))
    if funding_rate is not None:
        rows.append(_row("资金费率",
                         f"{funding_rate:.4%}",
                         RED if funding_rate > 0.001 else GREEN if funding_rate < 0 else TEXT_PRI))
    if btc_weekend is not None:
        rows.append(_row("BTC 周末表现",
                         f"{btc_weekend:+.2%}",
                         GREEN if btc_weekend > 0 else RED))

    if not any([rd, details]):
        rows = [html.Div("等待数据…", style={"color": TEXT_DIM, "fontSize": "12px", "padding": "8px 0"})]

    return html.Div(rows)


def _budget_bars(budgets: dict[str, float], weights: dict[str, float]) -> html.Div:
    """Stacked progress bars for asset class budgets vs actual weights."""
    if not budgets and not weights:
        return html.Div("等待策略信号…", style={"color": TEXT_DIM, "fontSize": "12px"})

    # Derive actual weights per class if budgets are missing
    if not budgets:
        cls_weights: dict[str, float] = {}
        for sym, w in weights.items():
            cls = "crypto" if "/" in sym else (
                  "bond" if sym in {"US.AGG","US.IEF","US.TLT","US.SHY"} else "equity")
            cls_weights[cls] = cls_weights.get(cls, 0.0) + w
        budgets = cls_weights

    palette = {"equity": BLUE, "crypto": ORANGE, "bond": GREEN}
    labels  = {"equity": "股票 Equity", "crypto": "加密 Crypto", "bond": "债券 Bond"}
    rows    = []

    for cls in ["equity", "crypto", "bond"]:
        budget = budgets.get(cls, 0.0)
        if budget < 0.001:
            continue
        col   = palette.get(cls, TEXT_SEC)
        label = labels.get(cls, cls)
        bar_w = f"{budget * 100:.0f}%"
        rows.append(html.Div([
            html.Div([
                html.Span(label, style={"fontSize": "11px", "color": TEXT_SEC, "minWidth": "90px"}),
                html.Span(f"{budget*100:.1f}%", style={"fontSize": "12px", "color": col,
                                                        "fontWeight": "700", "marginLeft": "auto"}),
            ], style={"display": "flex", "marginBottom": "3px"}),
            html.Div(
                html.Div(style={"width": bar_w, "height": "6px",
                                "background": col, "borderRadius": "3px"}),
                style={"background": col + "22", "borderRadius": "3px",
                       "height": "6px", "marginBottom": "10px"},
            ),
        ]))

    return html.Div(rows)


def _fig_weights_bar(weights: dict[str, float]) -> go.Figure:
    if not weights:
        return _fig_empty("无目标权重")
    items = sorted(weights.items(), key=lambda x: -x[1])
    syms  = [i[0] for i in items]
    wts   = [i[1] * 100 for i in items]
    cols  = [ORANGE if "/" in s else BLUE for s in syms]
    fig = go.Figure(go.Bar(
        x=syms, y=wts,
        marker_color=cols,
        text=[f"{w:.1f}%" for w in wts],
        textposition="outside",
        textfont=dict(color=TEXT_PRI, size=10),
        hovertemplate="%{x}: %{y:.1f}%<extra></extra>",
    ))
    fig.update_layout(
        **_DARK_LAYOUT,
        height=190,
        margin=dict(l=8, r=8, t=16, b=32),
        yaxis=dict(
            showgrid=True, gridcolor=BORDER, zeroline=False,
            ticksuffix="%", tickfont=dict(color=TEXT_SEC, size=9),
        ),
        xaxis=dict(tickfont=dict(color=TEXT_SEC, size=9)),
        bargap=0.35,
        legend_visible=False,
    )
    return fig


# ══════════════════════════════════════════════════════════════════════════════
# Layout helpers
# ══════════════════════════════════════════════════════════════════════════════

def _card(title: str, *children, extra_style: dict | None = None) -> html.Div:
    s = {
        "background": BG_CARD,
        "border": f"1px solid {BORDER}",
        "borderRadius": "10px",
        "padding": "14px 16px",
        "marginBottom": "10px",
    }
    if extra_style:
        s.update(extra_style)
    return html.Div([
        html.Div(title, style={
            "fontSize": "11px", "fontWeight": "600", "letterSpacing": "0.1em",
            "color": TEXT_SEC, "textTransform": "uppercase", "marginBottom": "10px",
        }),
        *children,
    ], style=s)


def _badge(label: str, colour: str, size: str = "12px") -> html.Span:
    return html.Span(label, style={
        "background":  colour + "18",
        "border":      f"1px solid {colour}40",
        "borderRadius": "5px",
        "color":       colour,
        "fontSize":    size,
        "fontWeight":  "700",
        "padding":     "3px 9px",
        "letterSpacing": "0.04em",
        "whiteSpace": "nowrap",
    })


def _metric_card(
    label_en: str, label_zh: str, value: str,
    sub: str = "", sub_color: str = TEXT_SEC,
    border_top_color: str | None = None,
) -> html.Div:
    border_style = (
        f"3px solid {border_top_color}" if border_top_color
        else f"1px solid {BORDER}"
    )
    return html.Div([
        html.Div(f"{label_zh}  /  {label_en}", style={
            "fontSize": "11px", "color": TEXT_SEC, "fontWeight": "500",
            "letterSpacing": "0.05em", "marginBottom": "6px",
        }),
        html.Div(value, style={
            "fontSize": "24px", "fontWeight": "700", "color": TEXT_PRI,
            "letterSpacing": "-0.02em", "lineHeight": "1.15",
            "fontFamily": "'SF Mono', 'Fira Code', monospace",
        }),
        html.Div(sub, style={
            "fontSize": "12px", "color": sub_color, "marginTop": "4px",
            "fontWeight": "600",
        }) if sub else html.Span(),
    ], style={
        "background": BG_CARD,
        "borderRadius": "10px",
        "border": f"1px solid {BORDER}",
        "borderTop": border_style,
        "padding": "16px 18px",
        "flex": "1",
        "minWidth": "130px",
    })


def _status_banner(label: str, status: str, detail: str, is_running: bool) -> html.Div:
    col = GREEN if is_running else TEXT_DIM
    dot = "●" if is_running else "○"
    return html.Div([
        html.Span(f"{dot}  {label}", style={
            "color": col, "fontWeight": "700", "fontSize": "12px",
            "marginRight": "12px", "whiteSpace": "nowrap",
        }),
        html.Span(status, style={
            "color": GREEN if is_running else RED,
            "fontSize": "12px", "fontWeight": "600",
            "marginRight": "10px",
        }),
        html.Span(detail, style={"color": TEXT_SEC, "fontSize": "11px"}),
    ], style={
        "background": (GREEN + "0d") if is_running else (BG_CARD2),
        "border": f"1px solid {(GREEN + '28') if is_running else BORDER}",
        "borderRadius": "8px",
        "padding": "8px 14px",
        "display": "flex", "alignItems": "center",
        "marginBottom": "8px",
    })


# ══════════════════════════════════════════════════════════════════════════════
# Default symbol list
# ══════════════════════════════════════════════════════════════════════════════

_DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT",
    "US.SPY", "US.QQQ", "US.EFA", "US.AGG", "US.GLD",
]


def _available_symbols(st: dict) -> list[str]:
    weights = st.get("target_weights", {})
    syms = list(weights.keys()) if weights else []
    for s in _DEFAULT_SYMBOLS:
        if s not in syms:
            syms.append(s)
    return syms


# ══════════════════════════════════════════════════════════════════════════════
# Table helpers
# ══════════════════════════════════════════════════════════════════════════════

_DT_BASE = dict(
    style_table={"overflowX": "auto", "borderRadius": "8px", "overflow": "hidden"},
    style_header={
        "backgroundColor": BG_DARK, "color": TEXT_SEC, "fontWeight": "700",
        "borderBottom": f"1px solid {BORDER}", "fontSize": "11px",
        "letterSpacing": "0.08em", "textTransform": "uppercase",
        "padding": "8px 12px",
    },
    style_cell={
        "backgroundColor": BG_CARD2, "color": TEXT_PRI, "border": "none",
        "borderBottom": f"1px solid {BORDER}18",
        "padding": "7px 12px", "fontSize": "12px",
        "fontFamily": "'SF Mono', 'Fira Code', monospace",
    },
    style_data_conditional=[
        {"if": {"row_index": "odd"}, "backgroundColor": BG_CARD},
    ],
)


def _positions_table(st: dict) -> html.Div | dash_table.DataTable:
    weights = st.get("target_weights", {})
    if not weights:
        return html.Div("无持仓数据 / No positions", style={"color": TEXT_SEC, "fontSize": "12px", "padding": "8px"})
    rows = [
        {
            "品种 / Symbol": k,
            "目标权重 / Target": f"{v*100:.1f}%",
            "类别 / Class": "Crypto" if "/" in k else ("Bond" if k in {"US.AGG","US.IEF","US.TLT"} else "Equity"),
        }
        for k, v in sorted(weights.items(), key=lambda x: -x[1])
    ]
    return dash_table.DataTable(
        data=rows,
        columns=[{"name": c, "id": c} for c in rows[0]],
        page_size=12,
        **_DT_BASE,
    )


def _orders_table() -> html.Div | dash_table.DataTable:
    rows = _load_order_history()
    if not rows:
        return html.Div("暂无成交记录 / No trades yet", style={"color": TEXT_SEC, "fontSize": "12px", "padding": "8px"})
    return dash_table.DataTable(
        data=rows,
        columns=[{"name": c, "id": c} for c in rows[0]],
        page_size=20,
        sort_action="native",
        filter_action="native",
        style_data_conditional=[
            {"if": {"row_index": "odd"}, "backgroundColor": BG_CARD},
            {"if": {"filter_query": '{方向} = "BUY"',  "column_id": "方向"}, "color": GREEN},
            {"if": {"filter_query": '{方向} = "SELL"', "column_id": "方向"}, "color": RED},
            {"if": {"filter_query": '{模式} = "dry_run"', "column_id": "模式"}, "color": YELLOW},
        ],
        **{k: v for k, v in _DT_BASE.items() if k != "style_data_conditional"},
    )


# ══════════════════════════════════════════════════════════════════════════════
# App factory
# ══════════════════════════════════════════════════════════════════════════════

_TAB_STYLE = {
    "background": BG_PANEL,
    "color": TEXT_SEC,
    "border": "none",
    "borderBottom": f"2px solid {BORDER}",
    "padding": "10px 22px",
    "fontSize": "13px",
    "fontWeight": "600",
}
_TAB_SELECTED = {
    **_TAB_STYLE,
    "color": TEXT_PRI,
    "borderBottom": f"2px solid {BLUE}",
    "background": BG_PANEL,
}


def create_app(settings=None) -> dash.Dash:
    app = dash.Dash(
        __name__,
        title="Claude-Trade  控制面板",
        update_title=None,
        meta_tags=[{"name": "viewport", "content": "width=device-width, initial-scale=1"}],
    )
    # sym-dropdown / tf-* buttons are rendered dynamically inside tab-content;
    # suppress Dash's startup check so it doesn't raise NonExistentIdException.
    app.config.suppress_callback_exceptions = True

    # ── Layout ────────────────────────────────────────────────────────────────
    app.layout = html.Div([
        dcc.Interval(id="iv", interval=10_000, n_intervals=0),
        dcc.Store(id="store-symbol", data="BTC/USDT"),
        dcc.Store(id="store-tf",     data="1d"),

        # ── Header bar ────────────────────────────────────────────────────
        html.Div([
            html.Div([
                html.Span("⛓", style={"fontSize": "18px", "marginRight": "8px"}),
                html.Span("Claude-Trade", style={
                    "fontSize": "17px", "fontWeight": "800",
                    "color": TEXT_PRI, "letterSpacing": "0.04em",
                }),
                html.Span(" / 级联策略控制面板", style={
                    "fontSize": "12px", "color": TEXT_SEC, "marginLeft": "8px",
                }),
            ], style={"display": "flex", "alignItems": "center"}),
            html.Div(id="hdr-badges", style={"display": "flex", "gap": "8px", "alignItems": "center"}),
        ], style={
            "display": "flex", "alignItems": "center", "justifyContent": "space-between",
            "padding": "12px 24px",
            "background": BG_PANEL,
            "borderBottom": f"1px solid {BORDER}",
            "position": "sticky", "top": "0", "zIndex": "100",
        }),

        # ── Body ──────────────────────────────────────────────────────────
        html.Div([

            # ── Status banners ────────────────────────────────────────────
            html.Div(id="status-banners", style={"marginBottom": "4px"}),

            # ── Metric cards ─────────────────────────────────────────────
            html.Div(id="metric-cards", style={
                "display": "flex", "gap": "10px", "flexWrap": "wrap",
                "marginBottom": "16px",
            }),

            # ── Tabs ──────────────────────────────────────────────────────
            dcc.Tabs(id="tabs", value="chart", children=[
                dcc.Tab(label="📈  K线图  /  Chart",    value="chart",    style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                dcc.Tab(label="💰  盈亏  /  P&L",       value="pnl",      style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                dcc.Tab(label="🗂  总览  /  Overview",   value="overview", style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                dcc.Tab(label="📋  订单  /  Orders",    value="orders",   style=_TAB_STYLE, selected_style=_TAB_SELECTED),
            ], style={"marginBottom": "0"}),

            html.Div(id="tab-content"),

        ], style={"padding": "16px 24px", "maxWidth": "1600px", "margin": "0 auto"}),

        # ── Footer ────────────────────────────────────────────────────────
        html.Div(
            html.Span(id="footer-ts", style={"color": TEXT_DIM, "fontSize": "11px"}),
            style={
                "padding": "8px 24px", "textAlign": "right",
                "borderTop": f"1px solid {BORDER}",
                "background": BG_PANEL,
            },
        ),

    ], style={
        "background": BG_DARK,
        "minHeight": "100vh",
        "fontFamily": "'Inter', 'SF Pro Display', 'Segoe UI', system-ui, sans-serif",
        "color": TEXT_PRI,
    })

    # ── Callbacks ─────────────────────────────────────────────────────────────

    @app.callback(
        Output("hdr-badges", "children"),
        Output("status-banners", "children"),
        Output("metric-cards", "children"),
        Output("footer-ts", "children"),
        Input("iv", "n_intervals"),
    )
    def _refresh_header(n):
        st   = _read_status()
        now  = datetime.now(ZoneInfo("America/New_York"))
        ts   = now.strftime("%Y-%m-%d  %H:%M:%S ET")
        running = bool(st)
        mode    = st.get("mode", "unknown")
        errors  = int(st.get("error_count", 0))

        # ── Header badges
        badges = [
            _badge("● LIVE" if running else "○ STOPPED",  GREEN if running else RED),
            _badge("DRY-RUN" if mode == "dry_run" else ("LIVE-TRADE" if mode == "live" else "—"),
                   YELLOW if mode == "dry_run" else (GREEN if mode == "live" else TEXT_DIM)),
        ]
        if errors:
            badges.append(_badge(f"⚠ {errors} ERR", RED))

        # ── Status banners
        cycles = st.get("cycle_count", 0)
        regime = st.get("regime", "N/A")
        detail_engine = (
            f"周期 {cycles}  •  制度 {regime}  •  更新 {st.get('updated_at','')[:19].replace('T',' ')}"
            if running else "引擎未运行 — 使用控制台启动 Engine not running"
        )
        banners = [
            _status_banner("引擎 / Engine", "运行中" if running else "已停止", detail_engine, running),
        ]

        # ── Metric cards
        m = _calc_metrics(st)
        acct_str = f"${m['acct']:,.2f}" if m["acct"] else "—"
        init_sub = f"初始 ${m['init']:,.0f}" if m["init"] else ""
        pnl_col  = GREEN if m["net_pnl"] >= 0 else RED
        pnl_str  = f"${m['net_pnl']:+,.2f}" if m["net_pnl"] != 0 else "—"
        pnl_sub  = f"{'▲' if m['net_pnl']>=0 else '▼'} {abs(m['net_pct']):.2f}%" if m["net_pnl"] != 0 else ""
        day_col  = GREEN if m["day_chg"] >= 0 else RED
        day_str  = f"${m['day_chg']:+,.2f}" if m["day_chg"] != 0 else "—"
        day_sub  = f"{'▲' if m['day_chg']>=0 else '▼'} {abs(m['day_pct']):.2f}%" if m["day_chg"] != 0 else "本次变动"
        exp_val  = float(st.get("total_exposure", 0.0)) * 100
        exp_col  = GREEN if exp_val < 85 else YELLOW
        regime   = st.get("regime", "N/A")
        r_col    = REGIME_COLOURS.get(regime, BLUE)

        cards = [
            _metric_card("Assets",    "总资产",   acct_str,  init_sub,  TEXT_SEC, border_top_color=BLUE),
            _metric_card("Total PnL", "总盈亏",   pnl_str,   pnl_sub,   pnl_col,  border_top_color=pnl_col),
            _metric_card("Last Chg",  "本次变动", day_str,   day_sub,   day_col,  border_top_color=day_col if day_str != "—" else BORDER),
            _metric_card("Exposure",  "仓位敞口", f"{exp_val:.1f}%", "", exp_col, border_top_color=exp_col),
            _metric_card("Regime",    "市场制度", regime,    "",        r_col,    border_top_color=r_col),
            _metric_card("Trades",    "成交笔数", str(m["trades"]), "", TEXT_SEC, border_top_color=PURPLE),
        ]

        footer = f"自动刷新 10s  •  {ts}  •  周期 {cycles}"
        return badges, banners, cards, footer

    @app.callback(
        Output("tab-content", "children"),
        Input("tabs", "value"),
        Input("iv", "n_intervals"),
        Input("store-symbol", "data"),
        Input("store-tf", "data"),
    )
    def _render_tab(tab, n, symbol, tf):
        st = _read_status()

        if tab == "chart":
            return _chart_tab(symbol or "BTC/USDT", tf or "1d", st)
        elif tab == "pnl":
            return _pnl_tab()
        elif tab == "overview":
            return _overview_tab(st)
        else:
            return _orders_tab()

    # ── Symbol / TF selectors (only active on chart tab) ──────────────────────
    @app.callback(
        Output("store-symbol", "data"),
        Input("sym-dropdown", "value"),
        prevent_initial_call=True,
    )
    def _update_symbol(val):
        return val or "BTC/USDT"

    @app.callback(
        Output("store-tf", "data"),
        Input("tf-1h", "n_clicks"),
        Input("tf-4h", "n_clicks"),
        Input("tf-1d", "n_clicks"),
        Input("tf-1w", "n_clicks"),
        prevent_initial_call=True,
    )
    def _update_tf(c1h, c4h, c1d, c1w):
        from dash import ctx
        btn = ctx.triggered_id
        return {"tf-1h": "1h", "tf-4h": "4h", "tf-1d": "1d", "tf-1w": "1w"}.get(btn, "1d")

    return app


# ══════════════════════════════════════════════════════════════════════════════
# Tab renderers
# ══════════════════════════════════════════════════════════════════════════════

def _btn(bid: str, label: str, active: bool = False) -> html.Button:
    return html.Button(label, id=bid, n_clicks=0, style={
        "background":    (BLUE2 if active else BG_CARD2),
        "color":         (TEXT_PRI if active else TEXT_SEC),
        "border":        f"1px solid {BLUE if active else BORDER}",
        "borderRadius":  "6px",
        "padding":       "5px 14px",
        "fontSize":      "12px",
        "fontWeight":    "600",
        "cursor":        "pointer",
    })


def _chart_tab(symbol: str, tf: str, st: dict) -> html.Div:
    symbols = _available_symbols(st)
    df = get_kline(symbol, tf, limit=120)
    fig = _fig_candlestick(df, symbol)

    return html.Div([
        # ── Controls ──────────────────────────────────────────────────────
        html.Div([
            dcc.Dropdown(
                id="sym-dropdown",
                options=[{"label": s, "value": s} for s in symbols],
                value=symbol,
                clearable=False,
                style={
                    "width": "180px", "backgroundColor": BG_CARD2,
                    "border": f"1px solid {BORDER}", "borderRadius": "6px",
                    "color": TEXT_PRI, "fontSize": "13px",
                },
            ),
            html.Div([
                _btn("tf-1h", "1H",  tf == "1h"),
                _btn("tf-4h", "4H",  tf == "4h"),
                _btn("tf-1d", "1D",  tf == "1d"),
                _btn("tf-1w", "1W",  tf == "1w"),
            ], style={"display": "flex", "gap": "6px"}),
            html.Span(
                "数据来源: 引擎缓存 / Binance / Yahoo Finance" if df.empty
                else f"数据: {len(df)} 根 K 线  ({df['timestamp'].iloc[0].strftime('%Y-%m-%d') if len(df) else ''} → {df['timestamp'].iloc[-1].strftime('%Y-%m-%d') if len(df) else ''})",
                style={"fontSize": "11px", "color": TEXT_DIM, "marginLeft": "auto"},
            ),
        ], style={
            "display": "flex", "gap": "12px", "alignItems": "center",
            "marginBottom": "10px", "flexWrap": "wrap",
        }),

        # ── K-line chart ──────────────────────────────────────────────────
        html.Div(
            dcc.Graph(
                id="kline-chart",
                figure=fig,
                config={"displayModeBar": True, "modeBarButtonsToRemove": ["select2d", "lasso2d"], "displaylogo": False},
                style={"height": "480px"},
            ),
            style={
                "background": BG_CARD, "border": f"1px solid {BORDER}",
                "borderRadius": "10px", "overflow": "hidden",
            },
        ),
    ], style={"paddingTop": "12px"})


def _pnl_tab() -> html.Div:
    """P&L history tab: equity curve + stats table."""
    history = _read_history(200)
    initial = _initial_capital()
    fig     = _fig_pnl_curve(history, initial)

    # Stats summary
    stats_rows: list[html.Div] = []
    if history and initial > 0:
        first_val = history[0].get("account_value", initial)
        last_val  = history[-1].get("account_value", initial)
        total_pnl = last_val - initial
        total_pct = total_pnl / initial * 100
        peak      = max(h.get("account_value", 0) for h in history)
        trough    = min(h.get("account_value", 0) for h in history)
        drawdown  = (peak - trough) / peak * 100 if peak > 0 else 0.0
        p_col     = GREEN if total_pnl >= 0 else RED

        def _stat(label: str, val: str, col: str = TEXT_PRI) -> html.Div:
            return html.Div([
                html.Span(label, style={"color": TEXT_SEC, "fontSize": "11px", "minWidth": "110px"}),
                html.Span(val,   style={"color": col, "fontWeight": "700", "fontSize": "13px",
                                        "fontFamily": "'SF Mono', monospace"}),
            ], style={"display": "flex", "justifyContent": "space-between",
                      "padding": "5px 0", "borderBottom": f"1px solid {BORDER}28"})

        stats_rows = [
            _stat("初始资金",   f"${initial:,.2f}"),
            _stat("当前净值",   f"${last_val:,.2f}", p_col),
            _stat("总盈亏",    f"${total_pnl:+,.2f}  ({total_pct:+.2f}%)", p_col),
            _stat("历史最高值", f"${peak:,.2f}", GREEN),
            _stat("历史最低值", f"${trough:,.2f}", RED if trough < initial else TEXT_PRI),
            _stat("最大回撤",   f"{drawdown:.2f}%", RED if drawdown > 10 else YELLOW if drawdown > 5 else GREEN),
            _stat("数据点数",   f"{len(history)} 个周期"),
        ]

    return html.Div([
        _card("账户净值历史  /  P&L Curve",
              dcc.Graph(figure=fig, config={"displayModeBar": True, "displaylogo": False},
                        style={"height": "280px"}),
              extra_style={"marginTop": "12px"}),
        html.Div([
            _card("绩效摘要  /  Performance",
                  *stats_rows if stats_rows else [
                      html.Div("等待数据…", style={"color": TEXT_DIM, "fontSize": "12px"})
                  ],
                  extra_style={"flex": "1", "minWidth": "260px"}),
        ], style={"display": "flex", "gap": "10px", "flexWrap": "wrap"}),
    ], style={"paddingTop": "4px"})


def _overview_tab(st: dict) -> html.Div:
    score   = float(st.get("regime_score", 0.0))
    regime  = st.get("regime", "NEUTRAL")
    weights = st.get("target_weights", {})
    budgets = st.get("asset_class_budgets", {})

    return html.Div([
        # ── Row 1: regime + donut + weights ───────────────────────────────
        html.Div([
            _card("市场制度 / Regime",
                  dcc.Graph(figure=_fig_regime_gauge(score, regime),
                            config={"displayModeBar": False},
                            style={"height": "200px"}),
                  extra_style={"flex": "1", "minWidth": "220px"}),

            _card("资产配置 / Allocation",
                  dcc.Graph(figure=_fig_alloc_donut(weights),
                            config={"displayModeBar": False},
                            style={"height": "200px"}),
                  extra_style={"flex": "1", "minWidth": "200px"}),

            _card("目标权重 / Weights",
                  dcc.Graph(figure=_fig_weights_bar(weights),
                            config={"displayModeBar": False},
                            style={"height": "200px"}),
                  extra_style={"flex": "2", "minWidth": "280px"}),
        ], style={"display": "flex", "gap": "10px", "flexWrap": "wrap", "marginTop": "12px"}),

        # ── Row 2: budget bars + crypto signals ───────────────────────────
        html.Div([
            _card("资金分配预算  /  Budget",
                  _budget_bars(budgets, weights),
                  extra_style={"flex": "1", "minWidth": "200px"}),

            _card("加密市场信号  /  Crypto Signals",
                  _crypto_signals_panel(st),
                  extra_style={"flex": "1", "minWidth": "220px"}),
        ], style={"display": "flex", "gap": "10px", "flexWrap": "wrap"}),

        # ── Row 3: positions ──────────────────────────────────────────────
        _card("当前持仓 / Positions", _positions_table(st)),
    ])


def _orders_tab() -> html.Div:
    return html.Div([
        _card("成交记录 / Trade Log", _orders_table()),
    ], style={"paddingTop": "12px"})


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8051
    print(f"\n  Claude-Trade Dashboard v2  →  http://127.0.0.1:{port}\n")
    create_app().run(host="127.0.0.1", port=port, debug=False)

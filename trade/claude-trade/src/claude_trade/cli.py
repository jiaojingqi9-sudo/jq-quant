"""claude-trade CLI — 级联策略控制台

Usage:
    claude-trade status [--watch N]  # 查看当前运行状态（--watch N 秒自动刷新）
    claude-trade run [--dry-run]     # 启动交易引擎
    claude-trade dashboard           # 打开 Web 控制面板
    claude-trade backtest            # 运行回测
    claude-trade regime              # 查看当前市场状态
    claude-trade positions           # 查看持仓
    claude-trade pnl                 # 查看盈亏历史曲线
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ── Resolve runtime/status paths relative to project root ──────────────────
_CLI_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _CLI_DIR.parents[1]           # claude-trade/
_RUNTIME_DIR = _PROJECT_ROOT / "runtime"
_STATUS_FILE  = _RUNTIME_DIR / "status.json"
_HISTORY_FILE = _RUNTIME_DIR / "account_history.jsonl"
_PID_FILE     = _RUNTIME_DIR / "engine.pid"

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("claude_trade.cli")

# ── Colour helpers (no deps) ────────────────────────────────────────────────
_HAS_COLOUR = sys.stdout.isatty() and os.name != "nt"

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _HAS_COLOUR else text

def _green(t: str)  -> str: return _c("32", t)
def _red(t: str)    -> str: return _c("31", t)
def _yellow(t: str) -> str: return _c("33", t)
def _cyan(t: str)   -> str: return _c("36", t)
def _magenta(t: str)-> str: return _c("35", t)
def _bold(t: str)   -> str: return _c("1",  t)
def _dim(t: str)    -> str: return _c("2",  t)

_DIVIDER     = _dim("  " + "─" * 52)
_SECTION_SEP = ""


# ════════════════════════════════════════════════════════════════════════════
# helpers
# ════════════════════════════════════════════════════════════════════════════

def _read_status() -> dict:
    """Load status.json; return empty dict if missing/corrupt."""
    if not _STATUS_FILE.exists():
        return {}
    try:
        return json.loads(_STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_history(n: int = 30) -> list[dict]:
    """Return last *n* rows from account_history.jsonl."""
    if not _HISTORY_FILE.exists():
        return []
    rows = []
    try:
        lines = _HISTORY_FILE.read_text(encoding="utf-8").splitlines()
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


def _engine_running() -> bool:
    """Return True if engine process is alive."""
    if not _PID_FILE.exists():
        return False
    try:
        pid = int(_PID_FILE.read_text().strip())
        os.kill(pid, 0)   # signal 0 = check existence
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False


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


def _fmt_ts(ts_raw: str) -> str:
    try:
        ts = datetime.fromisoformat(ts_raw)
        ts_et = ts.astimezone(ZoneInfo("America/New_York"))
        return ts_et.strftime("%Y-%m-%d %H:%M:%S ET")
    except Exception:
        return ts_raw


def _pnl_colour(value: float) -> str:
    if value > 0:
        return _green(f"+${value:,.2f}")
    if value < 0:
        return _red(f"-${abs(value):,.2f}")
    return f"${value:,.2f}"


def _pct_colour(pct: float) -> str:
    s = f"{pct:+.2f}%"
    return _green(s) if pct > 0 else _red(s) if pct < 0 else s


def _mini_sparkline(values: list[float], width: int = 20) -> str:
    """ASCII sparkline from a list of floats."""
    if len(values) < 2:
        return ""
    blocks = " ▁▂▃▄▅▆▇█"
    lo, hi = min(values), max(values)
    span = hi - lo or 1.0
    sample = values[-(width):]
    chars = [blocks[int((v - lo) / span * 8)] for v in sample]
    return "".join(chars)


# ════════════════════════════════════════════════════════════════════════════
# status  (main display function)
# ════════════════════════════════════════════════════════════════════════════

def _print_status() -> None:
    """Print a full human-readable status summary."""
    running = _engine_running()
    st      = _read_status()

    # ── Header ──────────────────────────────────────────────────────────────
    print(f"\n{_bold('━━━ Claude-Trade  策略控制台 ━━━━━━━━━━━━━━━━━━━━━━')}\n")

    engine_str = _green("● RUNNING") if running else _red("○ STOPPED")
    mode_raw   = st.get("mode", "unknown")
    mode_str   = _yellow("模拟盘 DRY-RUN") if mode_raw == "dry_run" else _green("实盘 LIVE")
    ts_str     = _fmt_ts(st.get("updated_at", "")) if st else _dim("--")

    print(f"  引擎状态:  {engine_str}  [{mode_str}]")
    print(f"  更新时间:  {ts_str}")

    # ── Active strategies & connectivity ────────────────────────────────────
    active = st.get("active_strategies") or ["cascade"]
    print(f"  策略:      {', '.join(active)}")

    futu_on   = st.get("futu_online")
    crypto_on = st.get("crypto_online")
    mkt_open  = st.get("market_hours_open")

    f_str = (_green("✓ 已连接") if futu_on else _red("✗ 未连接")) if futu_on is not None else _dim("?")
    c_str = (_green("✓ 已连接") if crypto_on else _red("✗ 未连接")) if crypto_on is not None else _dim("?")
    m_str = (_green("开市") if mkt_open else _yellow("休市")) if mkt_open is not None else _dim("?")

    print(f"  富途 OpenD:{f_str}   加密交易所:{c_str}   美股:{m_str}")

    cycles = st.get("cycle_count", 0)
    errors = st.get("error_count", 0)
    err_str = _red(str(errors)) if errors else str(errors)
    print(f"  运行周期:  {cycles}   错误: {err_str}")
    if st.get("last_error"):
        print(f"  最近错误:  {_red(st['last_error'][:80])}")

    # ── P&L ─────────────────────────────────────────────────────────────────
    print(f"\n{_DIVIDER}")
    print(f"  {_bold('📈  账户盈亏')}")
    print(_DIVIDER)

    acct_val     = st.get("account_value", 0.0)
    initial_cap  = _initial_capital()
    last_trade   = st.get("last_trade_at")

    print(f"  账户净值:  ${acct_val:>14,.2f}")

    if initial_cap > 0:
        pnl     = acct_val - initial_cap
        pnl_pct = pnl / initial_cap * 100
        print(f"  初始资金:  ${initial_cap:>14,.2f}")
        print(f"  总盈亏:    {_pnl_colour(pnl):>20}  ({_pct_colour(pnl_pct)})")

    print(f"  最近成交:  {_fmt_ts(last_trade) if last_trade else _dim('从未成交')}")

    # P&L history sparkline
    history = _read_history(30)
    if len(history) >= 3:
        vals = [r.get("account_value", 0) for r in history]
        day_change     = vals[-1] - vals[-2] if len(vals) >= 2 else 0.0
        week_vals      = vals[-7:] if len(vals) >= 7 else vals
        week_change    = vals[-1] - week_vals[0]
        spark          = _mini_sparkline(vals, width=24)
        print(f"  近期走势:  {spark}")
        print(f"  本次变动:  {_pct_colour(day_change / (vals[-2] or 1) * 100)}   "
              f"近7次变动: {_pct_colour(week_change / (week_vals[0] or 1) * 100)}")

    # ── Market Regime ───────────────────────────────────────────────────────
    print(f"\n{_DIVIDER}")
    print(f"  {_bold('🌐  市场制度 (Regime)')}")
    print(_DIVIDER)

    regime = st.get("regime", "?")
    regime_colours = {
        "CRISIS":   _red,
        "CAUTIOUS": _yellow,
        "NEUTRAL":  _cyan,
        "BULLISH":  _green,
        "EUPHORIA": _magenta,
    }
    r_colour = regime_colours.get(regime, lambda x: x)
    regime_labels = {
        "CRISIS":   "危机 🔴",
        "CAUTIOUS": "谨慎 🟡",
        "NEUTRAL":  "中性 🔵",
        "BULLISH":  "牛市 🟢",
        "EUPHORIA": "过热 🟣",
    }
    regime_label = regime_labels.get(regime, regime)
    regime_score = st.get("regime_score") or 0.0

    print(f"  综合制度:  {r_colour(_bold(regime_label))}  (得分: {regime_score:+.3f})")

    rd = st.get("regime_details", {})
    if rd:
        crypto_pulse = rd.get("crypto_pulse", 0.0)
        vol_regime   = rd.get("vol_regime", "?")
        cross_asset  = rd.get("cross_asset_flow", 0.0)
        funding_sig  = rd.get("funding_signal", 0.0)
        details      = rd.get("details", {})
        vix          = details.get("vix_level")
        funding_rate = details.get("funding_rate")
        btc_weekend  = details.get("btc_weekend_return")

        cp_str = _green(f"{crypto_pulse:+.3f}") if crypto_pulse > 0.1 else (
                 _red(f"{crypto_pulse:+.3f}") if crypto_pulse < -0.1 else f"{crypto_pulse:+.3f}")
        ca_str = _green(f"{cross_asset:+.3f}") if cross_asset > 0.1 else (
                 _red(f"{cross_asset:+.3f}") if cross_asset < -0.1 else f"{cross_asset:+.3f}")

        print(f"  加密脉冲:  {cp_str:<20} 跨资产流:  {ca_str}")
        vol_str = _red("高波动 ⚠") if vol_regime == "high" else (
                  _cyan("低波动") if vol_regime == "low" else vol_regime)
        fund_str = f"{funding_rate:.4%}" if funding_rate is not None else _dim("无数据")
        print(f"  波动制度:  {vol_str:<20} 资金费率:  {fund_str}")
        fs_str = _red(f"{funding_sig:+.3f}") if funding_sig > 0.2 else (
                 _green(f"{funding_sig:+.3f}") if funding_sig < -0.1 else f"{funding_sig:+.3f}")
        print(f"  资金信号:  {fs_str}")
        if vix is not None:
            vix_str = _red(f"{vix:.1f} ⚠ (恐慌)") if vix > 30 else (
                      _yellow(f"{vix:.1f} (偏高)") if vix > 20 else _green(f"{vix:.1f} (平静)"))
            print(f"  VIX 恐慌指数: {vix_str}")
        if btc_weekend is not None:
            bw_str = _green(f"{btc_weekend:+.2%}") if btc_weekend > 0 else _red(f"{btc_weekend:+.2%}")
            print(f"  BTC 周末表现: {bw_str}")

    # ── Asset Allocation ────────────────────────────────────────────────────
    print(f"\n{_DIVIDER}")
    print(f"  {_bold('💼  资金分配')}")
    print(_DIVIDER)

    budgets = st.get("asset_class_budgets", {})
    if budgets:
        eq   = budgets.get("equity",  0.0)
        cr   = budgets.get("crypto",  0.0)
        bd   = budgets.get("bond",    0.0)
        cash = 1.0 - eq - cr - bd
        print(f"  资产类别预算:")
        bar_eq   = "█" * int(eq   * 20)
        bar_cr   = "█" * int(cr   * 20)
        bar_bd   = "█" * int(bd   * 20)
        bar_cash = "░" * max(0, 20 - int((eq+cr+bd) * 20))
        print(f"    股票  {eq*100:5.1f}%  {_green(bar_eq)}")
        print(f"    加密  {cr*100:5.1f}%  {_cyan(bar_cr)}")
        print(f"    债券  {bd*100:5.1f}%  {_yellow(bar_bd)}")
        if cash > 0.01:
            print(f"    现金  {cash*100:5.1f}%  {_dim(bar_cash)}")
    else:
        print(f"  {_dim('(等待策略首次运行获取预算数据)')}")

    total_exposure = st.get("total_exposure", 0.0)
    print(f"\n  总仓位敞口: {total_exposure*100:.1f}%   "
          f"({'满仓' if total_exposure > 0.9 else '轻仓' if total_exposure < 0.3 else '中仓'})")

    weights = st.get("target_weights", {})
    if weights:
        print(f"\n  {'标的':<15} {'目标权重':>8}   {'预估市值':>12}")
        print(f"  {'────────':<15} {'────────':>8}   {'────────':>12}")
        for sym, w in sorted(weights.items(), key=lambda x: -x[1]):
            value = acct_val * w
            # Colour by asset type
            sym_str = _cyan(f"{sym:<15}") if "/" in sym else f"{sym:<15}"
            print(f"  {sym_str} {w*100:>7.1f}%   ${value:>11,.0f}")
        print(f"\n  {_dim('青色 = 加密资产  白色 = 股票/ETF')}")
    else:
        print(f"\n  {_yellow('目标仓位为空 — 等待策略信号或检查数据连接')}")
        if not futu_on:
            print(f"  {_dim('→ Futu OpenD 未连接，无法获取美股数据')}")
        if not crypto_on:
            print(f"  {_dim('→ 加密交易所未连接，无法获取 BTC/ETH 数据')}")

    # ── Data Quality ────────────────────────────────────────────────────────
    dq = st.get("data_quality", {})
    if dq and any(v is not False for v in dq.values()):
        print(f"\n{_DIVIDER}")
        print(f"  {_bold('📡  数据质量')}")
        print(_DIVIDER)
        for k, v in dq.items():
            indicator = _green("✓") if v else _red("✗")
            print(f"  {indicator} {k}")

    print()


def cmd_status(args: argparse.Namespace) -> None:
    """Print status; if --watch N, loop every N seconds."""
    watch = getattr(args, "watch", 0)
    if watch > 0:
        try:
            while True:
                # Clear terminal
                os.system("clear" if os.name != "nt" else "cls")
                _print_status()
                print(f"  {_dim(f'自动刷新每 {watch}s — Ctrl-C 退出')}\n")
                time.sleep(watch)
        except KeyboardInterrupt:
            print("\n  已退出实时监控。")
    else:
        _print_status()


# ════════════════════════════════════════════════════════════════════════════
# pnl  — 盈亏历史
# ════════════════════════════════════════════════════════════════════════════

def cmd_pnl(args: argparse.Namespace) -> None:
    """Print account value history table and cumulative P&L."""
    n = getattr(args, "n", 50)
    history = _read_history(n)
    initial = _initial_capital()

    print(f"\n{_bold('━━━ 账户盈亏历史 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━')}\n")

    if not history:
        print(f"  {_yellow('暂无历史数据 — 请先启动引擎积累数据。')}\n")
        return

    print(f"  {'时间 (ET)':<24} {'账户净值':>14}  {'变动':>10}  {'累计盈亏':>12}  制度")
    print(f"  {'──────────────────────':<24} {'──────────────':>14}  {'──────':>10}  {'──────────':>12}  ──────")

    prev_val = history[0]["account_value"]
    for row in history:
        ts_str   = _fmt_ts(row.get("ts", ""))[:19]
        val      = row.get("account_value", 0.0)
        regime   = row.get("regime", "?")
        change   = val - prev_val
        cum_pnl  = val - initial if initial > 0 else 0.0

        c_str   = (_green(f"+${change:,.0f}") if change > 0 else
                   _red(f"-${abs(change):,.0f}") if change < 0 else "  $0")
        pnl_str = (_green(f"+${cum_pnl:,.0f}") if cum_pnl > 0 else
                   _red(f"-${abs(cum_pnl):,.0f}") if cum_pnl < 0 else "$0")

        r_colours = {"CRISIS": _red, "CAUTIOUS": _yellow,
                     "NEUTRAL": _cyan, "BULLISH": _green, "EUPHORIA": _magenta}
        regime_str = r_colours.get(regime, lambda x: x)(regime)

        print(f"  {ts_str:<24} ${val:>13,.2f}  {c_str:>18}  {pnl_str:>20}  {regime_str}")
        prev_val = val

    if len(history) >= 2 and initial > 0:
        total_pnl = history[-1]["account_value"] - initial
        pct       = total_pnl / initial * 100
        print(f"\n  总盈亏: {_pnl_colour(total_pnl)}  ({_pct_colour(pct)})")
    print()


# ════════════════════════════════════════════════════════════════════════════
# run
# ════════════════════════════════════════════════════════════════════════════

def cmd_run(args: argparse.Namespace) -> None:
    """Start the trading engine (blocks until interrupted)."""
    dry_run: bool = args.dry_run
    env_file: str = args.env

    mode_label = _yellow("DRY-RUN 模拟盘") if dry_run else _green("LIVE 实盘交易")
    print(f"\n{_bold('启动 Cascade 交易引擎')}  [{mode_label}]\n")

    if not dry_run:
        print(_red("  ⚠  实盘模式 — 将提交真实订单！"))
        print("  5 秒后启动，按 Ctrl-C 取消 …\n")
        try:
            import time as _time
            for i in range(5, 0, -1):
                print(f"  启动倒计时: {i} …", end="\r", flush=True)
                _time.sleep(1)
        except KeyboardInterrupt:
            print("\n  已取消。")
            return
        print()

    # Write PID
    _RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(os.getpid()))

    try:
        from .config import load_settings
        from .engine.auto_trader import AutoTrader

        settings = load_settings(env_file)
        trader = AutoTrader(settings, dry_run=dry_run)
        trader.run()
    except KeyboardInterrupt:
        print(f"\n{_yellow('  引擎已停止。')}")
    except Exception as exc:
        logger.exception("Engine crashed: %s", exc)
        sys.exit(1)
    finally:
        if _PID_FILE.exists():
            _PID_FILE.unlink(missing_ok=True)


# ════════════════════════════════════════════════════════════════════════════
# dashboard
# ════════════════════════════════════════════════════════════════════════════

def cmd_dashboard(args: argparse.Namespace) -> None:
    """Launch the Dash web dashboard."""
    env_file: str = args.env
    port: int = args.port

    print(f"\n{_bold('启动 Web 控制面板')}  →  http://127.0.0.1:{port}\n")
    try:
        from .config import load_settings
        settings = load_settings(env_file)
        effective_port = port if port else settings.dashboard_port

        from .dashboard.app import create_app
        app = create_app(settings)
        app.run(host="127.0.0.1", port=effective_port, debug=False)
    except ImportError as exc:
        logger.error("Dashboard dependencies missing: %s", exc)
        print(_red(f"\n  缺少依赖: {exc}"))
        print("  运行:  pip install dash plotly")
        sys.exit(1)
    except Exception as exc:
        logger.exception("Dashboard error: %s", exc)
        sys.exit(1)


# ════════════════════════════════════════════════════════════════════════════
# backtest
# ════════════════════════════════════════════════════════════════════════════

def cmd_backtest(args: argparse.Namespace) -> None:
    """Run a quick regime-aware backtest using daily price data."""
    env_file: str = args.env
    start: str = args.start

    print(f"\n{_bold('运行 Cascade 回测')}  [{start} → 今日]\n")

    try:
        from .config import load_settings
        from .engine.backtest import run_cascade_backtest

        settings = load_settings(env_file)
        result = run_cascade_backtest(settings, start_date=start)

        print(f"  {'指标':<22} {'值':>12}")
        print(f"  {'──────':<22} {'─────':>12}")
        metrics = [
            ("总收益",     f"{result['total_return']*100:+.1f}%"),
            ("年化收益 CAGR", f"{result['cagr']*100:+.1f}%"),
            ("年化波动率",  f"{result['volatility']*100:.1f}%"),
            ("夏普比率",   f"{result['sharpe']:.2f}"),
            ("最大回撤",   f"{result['max_drawdown']*100:.1f}%"),
            ("终值",      f"${result['final_value']:,.0f}"),
            ("总费用",    f"${result.get('total_fees', 0.0):,.2f}"),
            ("费用拖累",  f"{result.get('cost_drag_pct', 0.0):.2f}%"),
            ("回测月数",  str(result.get('n_months', '?'))),
        ]
        for label, val in metrics:
            colour = _green if "+" in val and "%" in val else _red if "-" in val and "%" in val else str
            print(f"  {label:<22} {colour(val):>20}")

        rl = result.get("regime_log")
        if rl is not None and not rl.empty:
            print(f"\n  制度分布:")
            for regime, cnt in rl["regime"].value_counts().items():
                bar = "█" * min(cnt, 30)
                print(f"    {regime:<12} {cnt:>3} 月  {bar}")
        print()
    except Exception as exc:
        logger.exception("Backtest failed: %s", exc)
        print(_red(f"\n  回测错误: {exc}"))
        sys.exit(1)


# ════════════════════════════════════════════════════════════════════════════
# regime
# ════════════════════════════════════════════════════════════════════════════

def cmd_regime(args: argparse.Namespace) -> None:
    """Show current market regime from live data or latest status."""
    env_file: str = args.env
    live: bool = args.live

    print(f"\n{_bold('市场制度检测')}\n")

    if live:
        try:
            from .config import load_settings
            from .exchanges.crypto_ex import CryptoExchange
            from .exchanges.futu_ex import FutuExchange
            from .strategies.cascade import CascadeStrategy

            settings = load_settings(env_file)
            strategy = CascadeStrategy(settings)

            crypto = CryptoExchange(settings)
            futu = FutuExchange(settings)
            try:
                crypto.connect()
                futu.connect()
                plan = strategy.run_cycle(crypto, futu)
            finally:
                try: crypto.disconnect()
                except Exception: pass
                try: futu.disconnect()
                except Exception: pass

            regime = plan.regime
        except Exception as exc:
            logger.exception("Live regime detection failed: %s", exc)
            print(_red(f"  实时检测失败: {exc}"))
            print("  改用缓存数据 …\n")
            live = False

    if not live:
        st = _read_status()
        if not st:
            print(_yellow("  无缓存数据，请先运行 `claude-trade run --dry-run`。\n"))
            return
        rd = st.get("regime_details", {})
        details = rd.get("details", {})
        print(f"  数据来源: {_yellow('缓存')}")
        print(f"  制度:    {st.get('regime', '?')}  (得分: {st.get('regime_score', '?')})")
        if rd:
            print(f"  加密脉冲: {rd.get('crypto_pulse', 0.0):+.3f}")
            print(f"  波动制度: {rd.get('vol_regime', '?')}")
            print(f"  跨资产流: {rd.get('cross_asset_flow', 0.0):+.3f}")
            print(f"  资金信号: {rd.get('funding_signal', 0.0):+.3f}")
            vix = details.get("vix_level")
            if vix is not None:
                print(f"  VIX:     {vix:.1f}")
        print()
        return

    # Live output
    regime_colours = {
        "CRISIS": _red, "CAUTIOUS": _yellow, "NEUTRAL": _cyan,
        "BULLISH": _green, "EUPHORIA": _magenta,
    }
    colour_fn = regime_colours.get(regime.label, lambda x: x)
    print(f"  制度:          {colour_fn(_bold(regime.label))}")
    print(f"  综合得分:      {regime.score:+.3f}")
    print(f"  加密脉冲:      {regime.crypto_pulse:+.3f}")
    print(f"  波动制度:      {regime.vol_regime}")
    print(f"  跨资产资金流:  {regime.cross_asset_flow:+.3f}")
    print(f"  资金费率信号:  {regime.funding_signal:+.3f}")
    vix = regime.details.get("vix_level")
    if vix is not None:
        print(f"  VIX 恐慌指数: {vix:.1f}")
    print()


# ════════════════════════════════════════════════════════════════════════════
# positions
# ════════════════════════════════════════════════════════════════════════════

def cmd_positions(args: argparse.Namespace) -> None:
    """Show current open positions from all connected exchanges."""
    env_file: str = args.env

    print(f"\n{_bold('当前持仓')}\n")

    try:
        from .config import load_settings
        from .exchanges.crypto_ex import CryptoExchange
        from .exchanges.futu_ex import FutuExchange

        settings = load_settings(env_file)
        all_rows: list[dict] = []

        # Futu positions
        try:
            futu = FutuExchange(settings)
            futu.connect()
            try:
                pos = futu.get_positions()
                if not pos.empty:
                    pos["exchange"] = "Futu 富途"
                    all_rows.extend(pos.to_dict("records"))
            finally:
                futu.disconnect()
        except Exception as exc:
            print(_yellow(f"  富途不可用: {exc}"))

        # Crypto positions
        try:
            crypto = CryptoExchange(settings)
            crypto.connect()
            try:
                pos = crypto.get_positions()
                if not pos.empty:
                    pos["exchange"] = settings.crypto_exchange.capitalize()
                    all_rows.extend(pos.to_dict("records"))
            finally:
                crypto.disconnect()
        except Exception as exc:
            print(_yellow(f"  加密交易所不可用: {exc}"))

        if not all_rows:
            print(_yellow("  无持仓 (或所有交易所离线)。"))
        else:
            print(f"  {'交易所':<14} {'标的':<16} {'数量':>10} {'市值':>12} {'均价':>10} {'资产类型'}")
            print(f"  {'──────':<14} {'──────':<16} {'──':>10} {'──────':>12} {'──────':>10} {'──────'}")
            total = 0.0
            for r in all_rows:
                mv  = r.get("market_value", 0.0)
                sym = r.get("symbol", "?")
                total += mv
                asset_type = _cyan("加密") if "/" in sym else "股票"
                print(
                    f"  {r.get('exchange','?'):<14}"
                    f" {sym:<16}"
                    f" {r.get('qty', 0.0):>10.4f}"
                    f" ${mv:>11,.2f}"
                    f" ${r.get('avg_cost', 0.0):>9,.2f}"
                    f"  {asset_type}"
                )
            print(f"\n  持仓总市值: ${total:,.2f}")
    except Exception as exc:
        logger.exception("Position query failed: %s", exc)
        print(_red(f"\n  错误: {exc}"))
        sys.exit(1)

    print()


# ════════════════════════════════════════════════════════════════════════════
# Argument parser
# ════════════════════════════════════════════════════════════════════════════

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-trade",
        description="Claude-Trade — Cascade 多市场策略控制台",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
命令一览:
  status      查看引擎状态、盈亏、资金分配  (--watch N 实时刷新)
  pnl         查看账户盈亏历史曲线
  run         启动交易引擎（默认模拟盘）
  dashboard   打开 Web 控制面板
  backtest    快速回测策略表现
  regime      查看当前市场制度
  positions   查看全市场持仓明细
        """,
    )

    # Global options
    parser.add_argument(
        "--env", default=str(_PROJECT_ROOT / ".env"),
        help="配置文件路径 (默认: <project>/.env)",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志详细程度",
    )

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # status
    p_st = sub.add_parser("status", help="查看引擎状态、盈亏、资金分配")
    p_st.add_argument(
        "--watch", type=int, default=0, metavar="SECS",
        help="每隔 N 秒自动刷新（例: --watch 10）",
    )

    # pnl
    p_pnl = sub.add_parser("pnl", help="查看账户盈亏历史")
    p_pnl.add_argument("--n", type=int, default=50, help="显示最近 N 条记录（默认 50）")

    # run
    p_run = sub.add_parser("run", help="启动交易引擎")
    p_run.add_argument(
        "--dry-run", action="store_true",
        help="模拟运行，不提交真实订单",
    )

    # dashboard
    p_dash = sub.add_parser("dashboard", help="启动 Web 控制面板")
    p_dash.add_argument("--port", type=int, default=0, help="覆盖面板端口号")

    # backtest
    p_bt = sub.add_parser("backtest", help="运行快速回测")
    p_bt.add_argument(
        "--start", default="2020-01-01",
        help="回测开始日期 (YYYY-MM-DD，默认: 2020-01-01)",
    )

    # regime
    p_reg = sub.add_parser("regime", help="查看当前市场制度")
    p_reg.add_argument(
        "--live", action="store_true",
        help="获取实时数据而不是读缓存",
    )

    # positions
    sub.add_parser("positions", help="查看全市场持仓明细")

    return parser


# ════════════════════════════════════════════════════════════════════════════
# Entry point
# ════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    logging.getLogger().setLevel(getattr(logging, args.log_level))

    commands = {
        "status":    cmd_status,
        "pnl":       cmd_pnl,
        "run":       cmd_run,
        "dashboard": cmd_dashboard,
        "backtest":  cmd_backtest,
        "regime":    cmd_regime,
        "positions": cmd_positions,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()

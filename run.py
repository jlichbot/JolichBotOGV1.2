#!/usr/bin/env python3
"""
run.py — Railway entrypoint wrapper for FastLoop Trader

Responsibilities:
  1. Run fastloop_trader.py as a subprocess
  2. Parse its stdout for trade signals / errors
  3. Send Telegram alerts accordingly
  4. Exit with correct code so Railway marks the cron as pass/fail

Environment variables required:
  SIMMER_API_KEY          — from simmer.markets/dashboard
  WALLET_PRIVATE_KEY      — Polymarket wallet private key (for --live)
  TELEGRAM_BOT_TOKEN      — from @BotFather
  TELEGRAM_CHAT_ID        — your chat/channel ID or @username

Optional:
  LIVE_TRADING=1          — set to enable --live flag (default: dry-run)
  SMART_SIZING=1          — enable --smart-sizing
  NOTIFY_SKIPS=1          — also send Telegram msg on skip (noisy, off by default)
  DAILY_BUDGET_USD=20     — override daily budget (default 20)
"""

import os
import sys
import json
import subprocess
from datetime import datetime, timezone

# Apply multi-source price fallback patch before subprocess launch
# (patch also applies inside the subprocess via PYTHONPATH)
try:
    import price_fallback  # noqa — patches fastloop_trader.get_momentum at import
except Exception as e:
    print(f"⚠️  price_fallback import warning: {e}", flush=True)

from telegram_notify import (
    send,
    notify_trade,
    notify_error,
    notify_skip,
    notify_startup,
    notify_budget_warning,
)

# ── Config ────────────────────────────────────────────────────────────────────
LIVE_TRADING   = os.environ.get("LIVE_TRADING", "0") == "1"
SMART_SIZING   = os.environ.get("SMART_SIZING", "0") == "1"
DAILY_BUDGET   = float(os.environ.get("DAILY_BUDGET_USD", "20"))
ASSET          = os.environ.get("SIMMER_SPRINT_ASSET", "BTC")

# ── Force AUTOMATON_MANAGED so SDK always emits structured JSON reports ──────
# Without this the SDK detects Railway's environment inconsistently and
# sometimes exits silently with zero output, making failures invisible.
os.environ.setdefault("AUTOMATON_MANAGED", "1")

# ── Force structured JSON output from Simmer SDK ─────────────────────────────
os.environ.setdefault("AUTOMATON_MANAGED", "1")

# ── Build CLI command ─────────────────────────────────────────────────────────
cmd = [sys.executable, "-c",
       "import price_fallback; import runpy; runpy.run_path('fastloop_trader.py', run_name='__main__')",
]
# Append flags via sys.argv trick — simpler: use wrapper args approach
cmd = [sys.executable, "fastloop_trader.py", "--quiet"]
if LIVE_TRADING:
    cmd.append("--live")
if SMART_SIZING:
    cmd.append("--smart-sizing")

# Ensure price_fallback is importable inside the subprocess
_here = os.path.dirname(os.path.abspath(__file__))
_env = os.environ.copy()
_env["PYTHONPATH"] = _here + (":" + _env["PYTHONPATH"] if _env.get("PYTHONPATH") else "")
# Activate patch inside subprocess via sitecustomize trick
_env["PYTHONSTARTUP"] = os.path.join(_here, "price_fallback.py")

mode_label = "LIVE" if LIVE_TRADING else "DRY RUN"
ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
print(f"[{ts}] ⚡ FastLoop cycle starting ({mode_label})", flush=True)

# ── Notify Telegram: cycle start (quiet — skip in non-live to reduce noise) ──
if LIVE_TRADING:
    notify_startup(mode_label, ASSET, DAILY_BUDGET)

# ── Run trader ────────────────────────────────────────────────────────────────
try:
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=90,  # 5-min cron, give plenty of headroom
        env=_env,
    )
except subprocess.TimeoutExpired:
    msg = "FastLoop timed out after 90s"
    print(f"❌ {msg}", flush=True)
    notify_error(msg)
    sys.exit(1)

stdout = result.stdout or ""
stderr = result.stderr or ""

# Always print full output to Railway logs
print(stdout, flush=True)
if stderr:
    print(f"[stderr]\n{stderr}", flush=True)

# ── Parse automaton JSON report ───────────────────────────────────────────────
automaton_data = None
for line in stdout.splitlines():
    line = line.strip()
    if line.startswith('{"automaton"'):
        try:
            automaton_data = json.loads(line).get("automaton", {})
        except json.JSONDecodeError:
            pass

# ── Parse human-readable output for Telegram context ─────────────────────────
def _extract(keyword, lines):
    for l in lines:
        if keyword.lower() in l.lower():
            return l.strip()
    return ""

lines = stdout.splitlines()
market_line  = _extract("Selected:", lines) or _extract("Sprint:", lines)
signal_line  = _extract("Signal:", lines)
momentum_val = 0.0
price_val    = 0.0
side_val     = "YES"

feed_source = "binance"
for l in lines:
    if "Momentum:" in l:
        try:
            momentum_val = float(l.split("Momentum:")[1].strip().split("%")[0].replace("+", ""))
        except Exception:
            pass
    if "YES price:" in l or "YES $" in l:
        try:
            price_val = float(l.split("$")[1].strip().split()[0])
        except Exception:
            pass
    if "Signal: YES" in l or "Action: YES" in l:
        side_val = "YES"
    elif "Signal: NO" in l or "Action: NO" in l:
        side_val = "NO"
    if "Using Kraken" in l:
        feed_source = "kraken"
    elif "Using CoinGecko" in l:
        feed_source = "coingecko"
    elif "Failed to fetch price data" in l:
        feed_source = "none"

market_name = market_line.replace("🎯 Selected:", "").replace("Sprint:", "").strip()

# ── Handle results ────────────────────────────────────────────────────────────
if result.returncode != 0:
    err_snippet = (stderr or stdout)[:300]
    print(f"❌ Trader exited with code {result.returncode}", flush=True)
    notify_error(f"Exit code {result.returncode}\n{err_snippet}")
    sys.exit(result.returncode)

if automaton_data:
    trades_executed = automaton_data.get("trades_executed", 0)
    trades_attempted = automaton_data.get("trades_attempted", 0)
    amount_usd = automaton_data.get("amount_usd", 0.0)
    skip_reason = automaton_data.get("skip_reason", "")
    exec_errors = automaton_data.get("execution_errors", [])

    if trades_executed > 0:
        # 🎉 Trade fired — alert always
        notify_trade(
            side=side_val,
            market=market_name or "BTC Fast Market",
            amount=amount_usd,
            price=price_val,
            momentum=momentum_val,
            dry_run=not LIVE_TRADING,
        )
        print(f"✅ Telegram: trade alert sent", flush=True)

        # Budget warning: >80% consumed
        spent_today = amount_usd  # conservative — actual total is in daily_spend.json
        if spent_today / DAILY_BUDGET > 0.8:
            notify_budget_warning(spent_today, DAILY_BUDGET)

    elif trades_attempted > 0 and exec_errors:
        # Trade was attempted but failed
        notify_error(f"Trade attempted but failed:\n" + "\n".join(exec_errors))
        print(f"⚠️ Telegram: execution error alert sent", flush=True)

    elif skip_reason:
        notify_skip(skip_reason)
        print(f"⏸ Skip: {skip_reason}", flush=True)

    else:
        # No signal
        print(f"💤 No signal this cycle", flush=True)

else:
    # No automaton JSON — parse output for diagnostic info
    low = stdout.lower()
    if "failed to fetch price data" in low:
        notify_error("All price feeds failed (Binance + Kraken + CoinGecko). Check network.")
        print("❌ Telegram: price feed failure alert sent", flush=True)
    elif "no active fast markets" in low:
        print("💤 No markets available this cycle (off-hours or wrong window)", flush=True)
    elif "no tradeable markets" in low or "no live tradeable" in low:
        print("💤 Markets found but none live/within time window", flush=True)
    elif "already holding" in low:
        print("💤 Skipped — already holding a position on this market", flush=True)
    elif "error" in low or "failed" in low:
        notify_error(f"Unexpected error:\n{stdout[-400:]}")
        print("⚠️ Telegram: error alert sent", flush=True)
    elif stdout.strip() == "":
        # Completely blank output = SDK exited silently (automaton env detection issue)
        print("⚠️  Empty output — SDK may have exited silently. Check SIMMER_API_KEY is set correctly.", flush=True)
    else:
        print(f"💤 Cycle complete — no trade signal", flush=True)

print(f"[{ts}] ✅ FastLoop cycle complete", flush=True)
sys.exit(0)

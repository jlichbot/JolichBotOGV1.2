#!/usr/bin/env python3
"""
run.py — Railway entrypoint wrapper for FastLoop Trader
Produces clear, structured logs every cycle so you always know exactly what happened.
"""

import os
import sys
import json
import subprocess
from datetime import datetime, timezone

try:
    import price_fallback  # noqa
except Exception as e:
    print(f"⚠️  price_fallback import warning: {e}", flush=True)

from telegram_notify import notify_trade, notify_error, notify_skip, notify_budget_warning

# ── Config ─────────────────────────────────────────────────────────────────────
LIVE_TRADING  = os.environ.get("LIVE_TRADING", "0") == "1"
SMART_SIZING  = os.environ.get("SMART_SIZING", "0") == "1"
DAILY_BUDGET  = float(os.environ.get("DAILY_BUDGET_USD", "20"))
ASSET         = os.environ.get("SIMMER_SPRINT_ASSET", "BTC")
WINDOW        = os.environ.get("SIMMER_SPRINT_WINDOW", "5m")
ENTRY_THRESH  = os.environ.get("SIMMER_FASTLOOP_ENTRY_THRESHOLD", "0.05")
MOMENTUM_MIN  = os.environ.get("SIMMER_FASTLOOP_MOMENTUM_THRESHOLD", "0.5")
MAX_POS       = os.environ.get("SIMMER_FASTLOOP_MAX_POSITION_USD", "5")

os.environ["AUTOMATON_MANAGED"] = "1"

# ── Header ─────────────────────────────────────────────────────────────────────
ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
mode_label = "LIVE" if LIVE_TRADING else "DRY RUN"

print("", flush=True)
print("=" * 60, flush=True)
print(f"  FASTLOOP  |  {ts}  |  {mode_label}", flush=True)
print("=" * 60, flush=True)
print(f"  Asset: {ASSET}  Window: {WINDOW}  Budget: ${DAILY_BUDGET}", flush=True)
print(f"  Entry: {ENTRY_THRESH}  Momentum min: {MOMENTUM_MIN}%  Max pos: ${MAX_POS}", flush=True)
print("-" * 60, flush=True)

# ── Build subprocess command ───────────────────────────────────────────────────
# NOTE: No --quiet flag — we want full diagnostic output visible in Railway logs
cmd = [sys.executable, "fastloop_trader.py"]
if LIVE_TRADING:
    cmd.append("--live")
if SMART_SIZING:
    cmd.append("--smart-sizing")

_here = os.path.dirname(os.path.abspath(__file__))
_env  = os.environ.copy()
_env["PYTHONPATH"] = _here + (":" + _env["PYTHONPATH"] if _env.get("PYTHONPATH") else "")
_env["PYTHONSTARTUP"] = os.path.join(_here, "price_fallback.py")

# ── Run trader ─────────────────────────────────────────────────────────────────
try:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=90, env=_env)
except subprocess.TimeoutExpired:
    print("RESULT: TIMEOUT — FastLoop took >90s", flush=True)
    notify_error("FastLoop timed out after 90s on Railway")
    sys.exit(1)

stdout = result.stdout or ""
stderr = result.stderr or ""

# Print full trader output — always visible in Railway logs
print(stdout, flush=True)
if stderr.strip():
    print(f"[STDERR]\n{stderr}", flush=True)

# ── Parse automaton JSON ───────────────────────────────────────────────────────
automaton_data = None
for line in stdout.splitlines():
    if line.strip().startswith('{"automaton"'):
        try:
            automaton_data = json.loads(line.strip()).get("automaton", {})
        except json.JSONDecodeError:
            pass

# ── Extract context values from output ────────────────────────────────────────
lines        = stdout.splitlines()
momentum_val = 0.0
price_val    = 0.0
side_val     = "YES"
price_source = "binance"
market_name  = ""

for l in lines:
    if "Momentum:" in l:
        try:
            momentum_val = float(l.split("Momentum:")[1].strip().split("%")[0].replace("+",""))
        except Exception:
            pass
    if "YES price:" in l or "YES $" in l:
        try:
            price_val = float(l.split("$")[1].strip().split()[0])
        except Exception:
            pass
    if "Signal: YES" in l: side_val = "YES"
    elif "Signal: NO" in l: side_val = "NO"
    if "Price source:" in l:
        try: price_source = l.split("Price source:")[1].strip().split()[0]
        except Exception: pass
    if "Selected:" in l:
        market_name = l.replace("Selected:", "").replace("🎯", "").strip()

# ── Results summary ────────────────────────────────────────────────────────────
print("-" * 60, flush=True)

if result.returncode != 0:
    err = (stderr or stdout)[:300]
    print(f"RESULT: CRASH  exit={result.returncode}", flush=True)
    print(f"  {err[:200]}", flush=True)
    notify_error(f"Trader crashed (exit {result.returncode})\n{err}")
    print("=" * 60, flush=True)
    sys.exit(result.returncode)

if automaton_data:
    trades_executed  = automaton_data.get("trades_executed", 0)
    trades_attempted = automaton_data.get("trades_attempted", 0)
    amount_usd       = automaton_data.get("amount_usd", 0.0)
    skip_reason      = automaton_data.get("skip_reason", "")
    signals          = automaton_data.get("signals", 0)
    exec_errors      = automaton_data.get("execution_errors", [])

    if trades_executed > 0:
        print(f"RESULT: TRADE EXECUTED ({'PAPER' if not LIVE_TRADING else 'LIVE'})", flush=True)
        print(f"  Side: {side_val}  Amount: ${amount_usd:.2f}  Price: ${price_val:.3f}", flush=True)
        print(f"  Market: {market_name[:55]}", flush=True)
        print(f"  Momentum: {momentum_val:+.3f}%  Feed: {price_source}", flush=True)
        notify_trade(side=side_val, market=market_name or "BTC Fast Market",
                     amount=amount_usd, price=price_val, momentum=momentum_val,
                     dry_run=not LIVE_TRADING)
        print(f"  Telegram alert: sent", flush=True)
        if DAILY_BUDGET > 0 and amount_usd / DAILY_BUDGET > 0.8:
            notify_budget_warning(amount_usd, DAILY_BUDGET)

    elif trades_attempted > 0 and exec_errors:
        print(f"RESULT: TRADE FAILED (attempted, not executed)", flush=True)
        for e in exec_errors:
            print(f"  Error: {e}", flush=True)
        notify_error("Trade attempted but failed:\n" + "\n".join(exec_errors))

    else:
        low = stdout.lower()
        if "no active fast markets" in low or "found 0 active" in low:
            why = "NO MARKETS — No BTC fast markets live right now (check Polymarket directly)"
        elif "no fast markets with" in low or "no tradeable markets" in low:
            why = "NO MARKETS — All found markets too close to expiry or not yet live"
        elif "momentum" in low and "< minimum" in low:
            # Try to extract actual momentum value from output
            actual = "unknown"
            for l in lines:
                if "Momentum" in l and "< minimum" in l:
                    try: actual = l.strip()
                    except Exception: pass
            why = f"WEAK SIGNAL — {actual or 'BTC momentum below threshold of ' + MOMENTUM_MIN + '%'}"
        elif "divergence" in low and "minimum" in low:
            why = f"WEAK SIGNAL — Price divergence < entry threshold {ENTRY_THRESH}"
        elif "already holding" in low:
            why = "SKIPPED — Already holding a position on this market"
        elif "wide spread" in low:
            why = "SKIPPED — Order book spread too wide (illiquid)"
        elif "fees eat" in low:
            why = "SKIPPED — Edge too small to cover fees"
        elif "daily budget" in low and "exhausted" in low:
            why = f"BUDGET — Daily limit of ${DAILY_BUDGET} reached"
        elif "all price sources failed" in low or "failed to fetch price" in low:
            why = "PRICE FEED ERROR — All sources failed (Binance/OKX/Kraken/Bybit)"
            notify_error(why)
        elif "clob price unavailable" in low:
            why = "PRICE FEED ERROR — Could not fetch live Polymarket CLOB price"
        else:
            why = f"NO SIGNAL — skip_reason={skip_reason or 'none'}"

        print(f"RESULT: NO TRADE — {why}", flush=True)
        if os.environ.get("NOTIFY_SKIPS") == "1":
            notify_skip(why)

else:
    # No automaton JSON — early exit or crash before SDK reporting
    low = stdout.lower()
    if not stdout.strip():
        msg = "EMPTY OUTPUT — SDK silent exit. Likely SIMMER_API_KEY invalid or simmer-sdk not installed."
        print(f"RESULT: ERROR — {msg}", flush=True)
        notify_error(msg)
    elif "api key" in low or "simmer_api_key" in low:
        msg = "API KEY ERROR — check SIMMER_API_KEY in Railway Variables"
        print(f"RESULT: ERROR — {msg}", flush=True)
        notify_error(msg)
    else:
        print(f"RESULT: UNKNOWN — no structured report. See full output above.", flush=True)

print("=" * 60, flush=True)
print(f"  Cycle done  |  {ts}", flush=True)
print("", flush=True)
sys.exit(0)

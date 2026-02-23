#!/usr/bin/env python3
"""
monitor.py — Short 4-Month VIX Futures entry/exit monitor (v3.0)

Strategy:
  TIER 1 (aggressive): VIX < 18  AND contango ≥ 3.0 pts
  TIER 2 (moderate):   VIX 18–22 AND contango ≥ 1.5 pts AND VIX_20d_avg < 21
  No entry:            all other conditions

Dynamic sizing (0.5×–1.5× based on VIX level):
  VIX ≤ 12  → 1.5×
  VIX = 18  → 1.0×   (linear 12→18)
  VIX = 22  → 0.5×   (linear 18→22)

Exit triggers (checked every day regardless of entry):
  IMMEDIATE : VIX > 35
  URGENT    : VIX > EWMA(λ=0.97) × 1.15
  WARNING   : backwardation — contango < 0
  WARNING   : elevated regime — VIX > EWMA

Data sources (all free, no API key):
  VIX spot + history → Yahoo Finance  ^VIX   (via yfinance)
  VIX 6-month        → Yahoo Finance  ^VIX6M (via yfinance)
  Contango           → ^VIX6M − ^VIX (6M constant-maturity proxy for 4M futures premium)
  20d avg / EWMA     → computed from 60-day VIX history
"""

import os
import sys
import time
import logging
from datetime import date

import yfinance as yf
import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ── Strategy parameters ───────────────────────────────────────────────────────
TIER1_VIX_MAX   = 18.0   # Tier 1: VIX must be below this
TIER1_CONTANGO  = 3.0    # Tier 1: minimum contango (VIX6M − VIX, in points)

TIER2_VIX_MIN   = 18.0   # Tier 2: VIX lower bound (inclusive)
TIER2_VIX_MAX   = 22.0   # Tier 2: VIX upper bound (inclusive)
TIER2_CONTANGO  = 1.5    # Tier 2: minimum contango
TIER2_20D_MAX   = 21.0   # Tier 2: VIX 20-day avg must be below this

EWMA_LAMBDA     = 0.97   # Same λ as vix-ewma-alert

EXIT_HARD_VIX   = 35.0   # Immediate exit threshold
EXIT_EWMA_MULT  = 1.15   # Urgent exit if VIX > EWMA × this

SIZE_LOW_VIX    = 12.0   # VIX ≤ this → 1.5× (max size)
SIZE_MID_VIX    = 18.0   # VIX = this → 1.0×
SIZE_HIGH_VIX   = 22.0   # VIX = this → 0.5× (min size for entry)

RETRIES         = 3
RETRY_DELAY     = 5      # seconds × attempt number


# ─────────────────────────────────────────────────────────────────────────────
# Data fetching
# ─────────────────────────────────────────────────────────────────────────────

def _yf_close(ticker: str, period: str) -> pd.Series:
    """Download Yahoo Finance close series, handling MultiIndex columns."""
    df = yf.download(ticker, period=period, progress=False, auto_adjust=False)
    if df.empty:
        raise RuntimeError(f"No data returned for {ticker}")
    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close = close.dropna()
    if close.empty:
        raise RuntimeError(f"'Close' column empty for {ticker}")
    return close


def fetch_vix_series(period: str = "60d") -> pd.Series:
    """Return the full VIX close history needed for EWMA and rolling avg."""
    for attempt in range(1, RETRIES + 1):
        try:
            logging.info("Fetching ^VIX history (period=%s, attempt %d)…", period, attempt)
            series = _yf_close("^VIX", period)
            logging.info(
                "  ^VIX: %d rows, latest=%.2f (%s)",
                len(series), float(series.iloc[-1]), series.index[-1].date(),
            )
            return series
        except Exception as exc:
            logging.warning("Attempt %d failed (^VIX history): %s", attempt, exc)
            if attempt < RETRIES:
                time.sleep(RETRY_DELAY * attempt)
            else:
                raise


def fetch_vix6m() -> float:
    """Return the latest VIX6M close (6-month constant-maturity VIX)."""
    for attempt in range(1, RETRIES + 1):
        try:
            logging.info("Fetching ^VIX6M (attempt %d)…", attempt)
            series = _yf_close("^VIX6M", "5d")
            val = float(series.iloc[-1])
            logging.info("  ^VIX6M = %.2f  (date: %s)", val, series.index[-1].date())
            return val
        except Exception as exc:
            logging.warning("Attempt %d failed (^VIX6M): %s", attempt, exc)
            if attempt < RETRIES:
                time.sleep(RETRY_DELAY * attempt)
            else:
                raise


# ─────────────────────────────────────────────────────────────────────────────
# Metrics
# ─────────────────────────────────────────────────────────────────────────────

def compute_metrics(vix_series: pd.Series, vix6m: float) -> dict:
    """Compute all strategy metrics from VIX history + current VIX6M."""
    vix     = float(vix_series.iloc[-1])
    vix_dt  = vix_series.index[-1].date()
    avg_20d = float(vix_series.tail(20).mean())
    alpha   = 1 - EWMA_LAMBDA
    ewma    = float(vix_series.ewm(alpha=alpha, adjust=False).mean().iloc[-1])
    contango = vix6m - vix

    return {
        "vix":      vix,
        "vix_date": vix_dt,
        "vix6m":    vix6m,
        "contango": contango,
        "avg_20d":  avg_20d,
        "ewma":     ewma,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Sizing
# ─────────────────────────────────────────────────────────────────────────────

def dynamic_size(vix: float) -> float:
    """
    Linearly interpolate position size in [0.5×, 1.5×] based on VIX level.
      VIX ≤ 12  → 1.5×
      VIX = 18  → 1.0×
      VIX = 22  → 0.5×
    """
    if vix <= SIZE_LOW_VIX:
        return 1.5
    elif vix < SIZE_MID_VIX:
        t = (vix - SIZE_LOW_VIX) / (SIZE_MID_VIX - SIZE_LOW_VIX)
        return round(1.5 - t * 0.5, 2)   # 1.5 → 1.0
    else:
        t = (vix - SIZE_MID_VIX) / (SIZE_HIGH_VIX - SIZE_MID_VIX)
        return round(1.0 - t * 0.5, 2)   # 1.0 → 0.5


# ─────────────────────────────────────────────────────────────────────────────
# Entry evaluation
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_entry(m: dict) -> dict:
    """
    Returns:
      tier   : 1 | 2 | 0  (0 = no entry signal)
      size   : float
      checks : dict of individual condition booleans (for display)
    """
    vix      = m["vix"]
    contango = m["contango"]
    avg_20d  = m["avg_20d"]

    # Tier 1 conditions
    t1_vix  = vix < TIER1_VIX_MAX
    t1_cont = contango >= TIER1_CONTANGO

    # Tier 2 conditions
    t2_vix  = TIER2_VIX_MIN <= vix <= TIER2_VIX_MAX
    t2_cont = contango >= TIER2_CONTANGO
    t2_avg  = avg_20d < TIER2_20D_MAX

    tier1_ok = t1_vix and t1_cont
    tier2_ok = t2_vix and t2_cont and t2_avg

    checks = {
        "t1_vix":  t1_vix,
        "t1_cont": t1_cont,
        "t2_vix":  t2_vix,
        "t2_cont": t2_cont,
        "t2_avg":  t2_avg,
    }

    if tier1_ok:
        return {"tier": 1, "size": dynamic_size(vix), "checks": checks}
    elif tier2_ok:
        return {"tier": 2, "size": dynamic_size(vix), "checks": checks}
    else:
        return {"tier": 0, "size": 0.0, "checks": checks}


# ─────────────────────────────────────────────────────────────────────────────
# Exit evaluation
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_exit(m: dict) -> dict:
    """Check all exit / warning conditions independently of entry."""
    vix, ewma, contango = m["vix"], m["ewma"], m["contango"]
    ewma_threshold = ewma * EXIT_EWMA_MULT
    return {
        "hard":          vix > EXIT_HARD_VIX,
        "urgent":        (not vix > EXIT_HARD_VIX) and vix > ewma_threshold,
        "backwardation": contango < 0,
        "regime_warn":   vix > ewma,
        "ewma_threshold": ewma_threshold,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Message formatting
# ─────────────────────────────────────────────────────────────────────────────

def ck(flag: bool) -> str:
    return "✅" if flag else "❌"


def build_message(m: dict, entry: dict, ex: dict) -> str:
    today    = date.today().strftime("%A, %d %b %Y")
    vix      = m["vix"]
    contango = m["contango"]
    c        = entry["checks"]

    lines = [
        "📉 <b>Short 4M VIX Monitor</b>  (v3.0)",
        f"📅 {today}",
        "",
        f"  VIX spot         : <b>{vix:.2f}</b>",
        f"  VIX 6M (^VIX6M)  : <b>{m['vix6m']:.2f}</b>",
        f"  Contango (6M−spot): <b>{contango:+.2f} pts</b>",
        f"  VIX 20d avg       : {m['avg_20d']:.2f}",
        f"  VIX EWMA(λ=0.97)  : {m['ewma']:.2f}",
        "",
    ]

    # ── Exit warnings ─────────────────────────────────────────────────────
    if ex["hard"]:
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━━",
            f"🚨 <b>IMMEDIATE EXIT</b>",
            f"   VIX {vix:.2f} > {EXIT_HARD_VIX:.0f} — close position NOW",
            "━━━━━━━━━━━━━━━━━━━━━━━",
            "",
        ]
    elif ex["urgent"]:
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━━",
            f"🔴 <b>URGENT EXIT</b>",
            f"   VIX {vix:.2f} > EWMA×1.15 ({ex['ewma_threshold']:.2f}) — review/reduce",
            "━━━━━━━━━━━━━━━━━━━━━━━",
            "",
        ]
    else:
        warn_lines = []
        if ex["backwardation"]:
            warn_lines.append(f"⚠️  Backwardation — contango {contango:+.2f} (term structure inverted)")
        if ex["regime_warn"]:
            warn_lines.append(f"⚠️  Elevated regime — VIX ({vix:.2f}) > EWMA ({m['ewma']:.2f})")
        if warn_lines:
            lines += warn_lines + [""]

    # ── Tier condition checklist ──────────────────────────────────────────
    lines += [
        "<b>Tier 1</b> (VIX &lt;18, contango ≥3.0):",
        f"  {ck(c['t1_vix'])}  VIX {vix:.2f} &lt; {TIER1_VIX_MAX}",
        f"  {ck(c['t1_cont'])}  Contango {contango:+.2f} ≥ {TIER1_CONTANGO}",
        "",
        "<b>Tier 2</b> (VIX 18–22, contango ≥1.5, 20d avg &lt;21):",
        f"  {ck(c['t2_vix'])}  VIX {vix:.2f} in [18, 22]",
        f"  {ck(c['t2_cont'])}  Contango {contango:+.2f} ≥ {TIER2_CONTANGO}",
        f"  {ck(c['t2_avg'])}  20d avg {m['avg_20d']:.2f} &lt; {TIER2_20D_MAX}",
        "",
    ]

    # ── Final verdict ─────────────────────────────────────────────────────
    if entry["tier"] == 1:
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━━",
            "🟢 <b>TIER 1 ENTRY SIGNAL</b>",
            f"   Position size: <b>{entry['size']}×</b>  (aggressive)",
            "   Short 4-month VIX futures",
            "━━━━━━━━━━━━━━━━━━━━━━━",
        ]
    elif entry["tier"] == 2:
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━━",
            "🟡 <b>TIER 2 ENTRY SIGNAL</b>",
            f"   Position size: <b>{entry['size']}×</b>  (moderate)",
            "   Short 4-month VIX futures",
            "━━━━━━━━━━━━━━━━━━━━━━━",
        ]
    else:
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━━",
            "⏳ <b>NO ENTRY SIGNAL</b>",
            "   Conditions not fully met — monitor tomorrow",
            "━━━━━━━━━━━━━━━━━━━━━━━",
        ]

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────────────────────────────────────────

def send_telegram(bot_token: str, chat_id: str, text: str) -> dict:
    url     = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.post(url, data=payload, timeout=15)
            r.raise_for_status()
            logging.info("Telegram: sent OK (HTTP %s)", r.status_code)
            return r.json()
        except Exception as exc:
            logging.warning("Attempt %d: Telegram send failed: %s", attempt, exc)
            if attempt < RETRIES:
                time.sleep(RETRY_DELAY * attempt)
            else:
                raise


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id   = os.getenv("TELEGRAM_CHAT_ID")

    if not bot_token or not chat_id:
        logging.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set.")
        sys.exit(2)

    try:
        vix_series = fetch_vix_series(period="60d")
        vix6m      = fetch_vix6m()
        metrics    = compute_metrics(vix_series, vix6m)
        entry      = evaluate_entry(metrics)
        exits      = evaluate_exit(metrics)
        message    = build_message(metrics, entry, exits)

        logging.info("\n%s", message)
        send_telegram(bot_token, chat_id, message)
        logging.info("Done.")

    except Exception as exc:
        logging.exception("Unhandled error in monitor")
        try:
            send_telegram(bot_token, chat_id, f"⚠️ Short 4M VIX monitor error: {exc}")
        except Exception:
            logging.exception("Also failed to send error notification")
        sys.exit(1)


if __name__ == "__main__":
    main()

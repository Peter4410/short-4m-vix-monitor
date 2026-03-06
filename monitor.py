#!/usr/bin/env python3
"""
monitor.py — Short 4-Month VIX Futures entry/exit monitor (v3.2)

Strategy:
  TIER 1 (aggressive): VIX < 18  AND contango ≥ 3.0 pts
  TIER 2 (moderate):   VIX 18–22 AND contango ≥ 1.5 pts AND VIX_20d_avg < 21
  No entry:            all other conditions

Dynamic sizing (0.5×–1.5× based on VIX level):
  VIX ≤ 12  → 1.5×
  VIX = 18  → 1.0×   (linear 12→18)
  VIX = 22  → 0.5×   (linear 18→22)

Contango boost (+0.2×):
  When contango (6M−spot) ≥ 3.5 pts AND an entry signal is active,
  add 0.2× to the base dynamic size.  Highest-conviction environments
  only (deep, steep term structure = favourable roll yield).

Exit triggers (checked every day regardless of entry):
  IMMEDIATE : VIX > 35
  URGENT    : VIX > EWMA(λ=0.97) × 1.15
  WARNING   : backwardation — contango < 0
  WARNING   : elevated regime — VIX > EWMA

FOMC filter (Tier 1 only — Sinclair Ch5 p97–98):
  VIX futures are elevated in the days before FOMC announcements and drop
  after.  If an FOMC meeting falls within 3 calendar days of today, or
  today is 1 calendar day after a meeting, Tier 1 entry is flagged for delay.
  Tier 2 entries are unaffected (variance premium is larger, FOMC effect
  smaller relative to noise).  FOMC dates are fetched from the Federal
  Reserve website; a hardcoded fallback list covers 2025–2027.

Data sources (all free, no API key):
  VIX spot + history → Yahoo Finance  ^VIX   (via yfinance)
  VIX 6-month        → Yahoo Finance  ^VIX6M (via yfinance)
  Contango           → ^VIX6M − ^VIX (6M constant-maturity proxy for 4M futures premium)
  20d avg / EWMA     → computed from 60-day VIX history
  FOMC dates         → federalreserve.gov (with hardcoded fallback)
"""

import os
import re
import sys
import time
import logging
from datetime import date, timedelta

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

# ── Contango boost ────────────────────────────────────────────────────────────
CONTANGO_BOOST_THRESHOLD = 3.5   # Contango (pts) required to trigger boost
CONTANGO_BOOST_SIZE      = 0.2   # Additional position size added when triggered

RETRIES         = 3
RETRY_DELAY     = 5      # seconds × attempt number

# ── FOMC filter parameters ────────────────────────────────────────────────────
FOMC_PRE_HOLD_DAYS  = 3   # Hold Tier 1 if FOMC is within this many calendar days ahead
FOMC_POST_HOLD_DAYS = 1   # Hold Tier 1 for this many calendar days after FOMC day
#                           First safe entry = FOMC date + (FOMC_POST_HOLD_DAYS + 1) days

# Hardcoded fallback: FOMC announcement dates (last day of each 2-day meeting).
# Used when the Fed website cannot be reached or parsed.
# Update annually when the Fed publishes the next year's calendar.
FOMC_DATES_FALLBACK = [
    # 2025
    date(2025, 1, 29), date(2025, 3, 19), date(2025, 5, 7),
    date(2025, 6, 18), date(2025, 7, 30), date(2025, 9, 17),
    date(2025, 10, 29), date(2025, 12, 10),
    # 2026
    date(2026, 1, 28), date(2026, 3, 18), date(2026, 5, 6),
    date(2026, 6, 17), date(2026, 7, 29), date(2026, 9, 16),
    date(2026, 10, 28), date(2026, 12, 9),
    # 2027 (approximate — update when Fed publishes official calendar)
    date(2027, 1, 27), date(2027, 3, 17), date(2027, 5, 5),
    date(2027, 6, 16), date(2027, 7, 28), date(2027, 9, 15),
    date(2027, 10, 27), date(2027, 12, 8),
]


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


def fetch_fomc_dates() -> list[date]:
    """
    Fetch upcoming FOMC announcement dates (last day of each 2-day meeting)
    from the Federal Reserve website.

    Tries two parse strategies against the Fed's FOMC calendar page:
      1. <time datetime="YYYY-MM-DD"> attributes (cleanest signal)
      2. "Month DD-DD" text patterns within year-labelled sections

    Falls back to FOMC_DATES_FALLBACK on any import error, network error,
    or zero-result parse.  Only returns dates >= today.
    """
    today = date.today()
    url   = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

    try:
        from bs4 import BeautifulSoup

        resp = requests.get(
            url, timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (compatible; monitor/3.2)"},
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        MONTH_NUM = {
            "january": 1, "february": 2, "march": 3, "april": 4,
            "may": 5, "june": 6, "july": 7, "august": 8,
            "september": 9, "october": 10, "november": 11, "december": 12,
        }

        parsed: list[date] = []

        # ── Strategy 1: semantic <time datetime="YYYY-MM-DD"> tags ──────────
        for tag in soup.find_all("time", attrs={"datetime": True}):
            dt_str = str(tag.get("datetime", ""))
            m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", dt_str)
            if m:
                d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                if d >= today:
                    parsed.append(d)

        if parsed:
            logging.info("FOMC: %d upcoming dates via <time datetime> tags", len(parsed))
            return sorted(set(parsed))

        # ── Strategy 2: year-panel scan + "Month DD-DD" text patterns ───────
        for section in soup.find_all(["div", "section"]):
            heading = section.find(re.compile(r"^h[1-6]$"))
            if not heading:
                continue
            year_m = re.search(r"\b(20\d{2})\b", heading.get_text())
            if not year_m:
                continue
            year = int(year_m.group(1))
            if year < today.year:
                continue

            for text in section.strings:
                m = re.search(
                    r"(January|February|March|April|May|June|July|August|"
                    r"September|October|November|December)"
                    r"[^0-9]*(\d{1,2})[-–](\d{1,2})",
                    str(text), re.IGNORECASE,
                )
                if m:
                    month = MONTH_NUM.get(m.group(1).lower(), 0)
                    day   = int(m.group(3))  # last day of meeting = announcement day
                    if month:
                        try:
                            d = date(year, month, day)
                            if d >= today:
                                parsed.append(d)
                        except ValueError:
                            pass

        if parsed:
            logging.info("FOMC: %d upcoming dates via text scan", len(parsed))
            return sorted(set(parsed))

        logging.warning("FOMC: Fed website returned 0 parseable dates — using fallback")

    except ImportError:
        logging.warning("FOMC: beautifulsoup4 not installed — using fallback")
    except Exception as exc:
        logging.warning("FOMC: fetch failed (%s) — using fallback", exc)

    fallback = sorted(d for d in FOMC_DATES_FALLBACK if d >= today)
    logging.info("FOMC: using %d hardcoded fallback dates", len(fallback))
    return fallback


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
      tier          : 1 | 2 | 0  (0 = no entry signal)
      size          : float  (base size + contango boost if applicable)
      base_size     : float  (dynamic size before boost)
      contango_boost: bool   (True when +0.2× boost was applied)
      checks        : dict of individual condition booleans (for display)
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

    # Contango boost: active on any entry signal when contango ≥ 3.5 pts
    cont_boost = contango >= CONTANGO_BOOST_THRESHOLD

    checks = {
        "t1_vix":       t1_vix,
        "t1_cont":      t1_cont,
        "t2_vix":       t2_vix,
        "t2_cont":      t2_cont,
        "t2_avg":       t2_avg,
        "cont_boost":   cont_boost,
    }

    if tier1_ok or tier2_ok:
        tier      = 1 if tier1_ok else 2
        base      = dynamic_size(vix)
        boosted   = cont_boost
        final_size = round(base + (CONTANGO_BOOST_SIZE if boosted else 0.0), 2)
        return {"tier": tier, "size": final_size, "base_size": base,
                "contango_boost": boosted, "checks": checks}

    return {"tier": 0, "size": 0.0, "base_size": 0.0,
            "contango_boost": False, "checks": checks}


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
# FOMC check
# ─────────────────────────────────────────────────────────────────────────────

def fomc_check(fomc_dates: list[date]) -> dict:
    """
    Determine whether today falls within the Tier 1 FOMC hold window.

    Hold window (calendar days):
      [FOMC date − FOMC_PRE_HOLD_DAYS,  FOMC date + FOMC_POST_HOLD_DAYS]
      i.e. 3 days before through 1 day after → hold
      First safe Tier 1 entry = FOMC date + FOMC_POST_HOLD_DAYS + 1

    Returns
    -------
    dict with keys:
      hold        : bool — Tier 1 entry should be delayed
      fomc_date   : date | None — the triggering FOMC announcement date
      days_to_fomc: int  | None — signed offset (+ = upcoming, − = past)
      enter_after : date | None — earliest safe Tier 1 entry date
    """
    today = date.today()

    for fomc_date in sorted(fomc_dates):
        days_to = (fomc_date - today).days   # positive = future, negative = past
        if -FOMC_POST_HOLD_DAYS <= days_to <= FOMC_PRE_HOLD_DAYS:
            enter_after = fomc_date + timedelta(days=FOMC_POST_HOLD_DAYS + 1)
            return {
                "hold":         True,
                "fomc_date":    fomc_date,
                "days_to_fomc": days_to,
                "enter_after":  enter_after,
            }

    return {"hold": False, "fomc_date": None, "days_to_fomc": None, "enter_after": None}


# ─────────────────────────────────────────────────────────────────────────────
# Message formatting
# ─────────────────────────────────────────────────────────────────────────────

def ck(flag: bool) -> str:
    return "✅" if flag else "❌"


def _fmt_date(d: date) -> str:
    """Format a date as '18 Mar' (no leading zero, locale-independent)."""
    return f"{d.day} {d.strftime('%b')}"


def build_message(m: dict, entry: dict, ex: dict, fomc: dict) -> str:
    today    = date.today().strftime("%A, %d %b %Y")
    vix      = m["vix"]
    contango = m["contango"]
    c        = entry["checks"]

    lines = [
        "📉 <b>Short 4M VIX Monitor</b>  (v3.2)",
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
            "🚨 <b>IMMEDIATE EXIT</b>",
            f"   VIX {vix:.2f} > {EXIT_HARD_VIX:.0f} — close position NOW",
            "━━━━━━━━━━━━━━━━━━━━━━━",
            "",
        ]
    elif ex["urgent"]:
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━━",
            "🔴 <b>URGENT EXIT</b>",
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
        "<b>Contango Boost</b> (+0.2× when contango ≥3.5):",
        f"  {ck(c['cont_boost'])}  Contango {contango:+.2f} ≥ {CONTANGO_BOOST_THRESHOLD}",
        "",
    ]

    # ── FOMC advisory (Tier 1 only) ───────────────────────────────────────
    if fomc["hold"] and entry["tier"] == 1:
        fd   = fomc["fomc_date"]
        ea   = fomc["enter_after"]
        ddays = fomc["days_to_fomc"]

        if ddays > 0:
            timing = f"FOMC in {ddays} day{'s' if ddays > 1 else ''} ({_fmt_date(fd)})"
        elif ddays == 0:
            timing = f"FOMC announcement today ({_fmt_date(fd)})"
        else:
            timing = f"FOMC {-ddays} day{'s' if -ddays > 1 else ''} ago ({_fmt_date(fd)})"

        lines += [
            f"⚠️  {timing}",
            f"   Delay Tier 1 entry — wait until {_fmt_date(ea)} (post-FOMC vol compression)",
            "",
        ]

    # ── Final verdict ─────────────────────────────────────────────────────
    if entry["tier"] == 1:
        size_detail = f"{entry['base_size']}× base"
        if entry["contango_boost"]:
            size_detail += f" + {CONTANGO_BOOST_SIZE}× contango boost"
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━━",
            "🟢 <b>TIER 1 ENTRY SIGNAL</b>",
            f"   Position size: <b>{entry['size']}×</b>  ({size_detail})",
            "   Short 4-month VIX futures",
            "━━━━━━━━━━━━━━━━━━━━━━━",
        ]
    elif entry["tier"] == 2:
        size_detail = f"{entry['base_size']}× base"
        if entry["contango_boost"]:
            size_detail += f" + {CONTANGO_BOOST_SIZE}× contango boost"
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━━",
            "🟡 <b>TIER 2 ENTRY SIGNAL</b>",
            f"   Position size: <b>{entry['size']}×</b>  ({size_detail})",
            "   Short 4-month VIX futures",
            "━━━━━━━━━━━━━━━━━━━━━━━",
        ]
    else:
        boost_note = f"  Contango boost: {ck(c['cont_boost'])} (≥{CONTANGO_BOOST_THRESHOLD} pts — activates on entry)"
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━━",
            "⏳ <b>NO ENTRY SIGNAL</b>",
            "   Conditions not fully met — monitor tomorrow",
            boost_note,
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
        fomc_dates = fetch_fomc_dates()
        metrics    = compute_metrics(vix_series, vix6m)
        entry      = evaluate_entry(metrics)
        exits      = evaluate_exit(metrics)
        fomc       = fomc_check(fomc_dates)
        message    = build_message(metrics, entry, exits, fomc)

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

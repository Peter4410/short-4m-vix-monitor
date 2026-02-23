# short-4m-vix-monitor

GitHub Actions bot that checks **Short 4-Month VIX Futures** entry and exit conditions daily, sending a Telegram alert at 4:30 PM ET.

## Strategy (v3.0)

### Entry — two-tier system

| Tier | VIX | Contango | Extra filter | Size |
|------|-----|----------|--------------|------|
| **1** (aggressive) | < 18 | ≥ 3.0 pts | — | 1.0×–1.5× |
| **2** (moderate)   | 18–22 | ≥ 1.5 pts | VIX 20d avg < 21 | 0.5×–1.0× |
| **None** | any other | — | — | — |

**Contango** = `^VIX6M − ^VIX` (6-month constant-maturity VIX minus 30-day VIX spot).
Positive contango means the term structure is upward-sloping (normal, favourable for short VIX).

### Dynamic sizing

```
VIX ≤ 12  → 1.5×  (maximum)
VIX = 18  → 1.0×  (linear between 12 and 18)
VIX = 22  → 0.5×  (linear between 18 and 22)
```

### Exit triggers (checked every day)

| Signal | Condition | Action |
|--------|-----------|--------|
| 🚨 Immediate | VIX > 35 | Close now |
| 🔴 Urgent | VIX > EWMA(λ=0.97) × 1.15 | Review / reduce |
| ⚠️ Warning | Contango < 0 (backwardation) | Monitor closely |
| ⚠️ Warning | VIX > EWMA | Elevated vol regime |

## Decision flow

```
Daily at 4:30 PM ET:
  ├── Any exit trigger? → include warning/alert in message
  │
  ├── VIX < 18 AND contango ≥ 3.0?
  │     YES → 🟢 TIER 1 ENTRY  (1.0×–1.5× size)
  │
  ├── 18 ≤ VIX ≤ 22 AND contango ≥ 1.5 AND 20d avg < 21?
  │     YES → 🟡 TIER 2 ENTRY  (0.5×–1.0× size)
  │
  └── Otherwise → ⏳ NO ENTRY
```

## Schedule

Runs at **21:30 UTC** every day:
- Winter (EST, UTC−5): **4:30 PM ET**
- Summer (EDT, UTC−4): **5:30 PM ET** (data is still final by this time)

Manually trigger via **Actions → Run workflow** at any time.

## Setup

### 1. Fork / clone this repo

### 2. Add Telegram secrets

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Value |
|--------|-------|
| `TELEGRAM_BOT_TOKEN` | Your Telegram bot token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | Your Telegram chat / user ID |

### 3. Actions are enabled by default — the schedule starts immediately.

## Data sources

| Metric | Ticker | Provider |
|--------|--------|----------|
| VIX spot + 60d history | `^VIX` | Yahoo Finance via **yfinance** |
| VIX 6-month (contango) | `^VIX6M` | Yahoo Finance via **yfinance** |
| 20-day avg + EWMA | computed | from 60d `^VIX` history |

## Sample Telegram alerts

**Tier 2 entry (VIX in range, moderate contango):**
```
📉 Short 4M VIX Monitor  (v3.0)
📅 Monday, 03 Mar 2025

  VIX spot         : 19.90
  VIX 6M (^VIX6M)  : 23.07
  Contango (6M−spot): +3.17 pts
  VIX 20d avg       : 19.42
  VIX EWMA(λ=0.97)  : 19.65

Tier 1 (VIX <18, contango ≥3.0):
  ❌  VIX 19.90 < 18
  ✅  Contango +3.17 ≥ 3.0

Tier 2 (VIX 18–22, contango ≥1.5, 20d avg <21):
  ✅  VIX 19.90 in [18, 22]
  ✅  Contango +3.17 ≥ 1.5
  ✅  20d avg 19.42 < 21

━━━━━━━━━━━━━━━━━━━━━━━
🟡 TIER 2 ENTRY SIGNAL
   Position size: 0.76×  (moderate)
   Short 4-month VIX futures
━━━━━━━━━━━━━━━━━━━━━━━
```

**Exit warning:**
```
🔴 URGENT EXIT
   VIX 23.10 > EWMA×1.15 (21.30) — review/reduce
```

## Related repos

- [vix-ewma-alert](https://github.com/Peter4410/vix-ewma-alert) — VIX vs EWMA daily monitor
- [vix-vstoxx-monitor](https://github.com/Peter4410/vix-vstoxx-monitor) — VIX/vStoxx spread entry conditions

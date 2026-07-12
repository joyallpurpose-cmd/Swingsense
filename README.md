# SwingSense AI

Personal NSE swing-trading assistant, per the BRD. One always-on Node server
hosts the web app, runs the Python scanner every trading evening, emails your
shortlist, and tracks every recommendation until exit.

```
Market close -> fetch NSE data -> indicators -> filters -> rank -> AI reasoning
             -> recommendations (data.json) -> email -> web app -> track exits
```

## Setup (once, ~10 minutes)

Requirements: Node.js 18+ and Python 3.10+.

```bash
npm install
pip install -r scanner/requirements.txt
copy .env.example .env        # then edit .env  (mac/linux: cp)
npm start
```

Open http://localhost:5000

First scan: press **Run scan** in the app (or wait for the 6:40 PM auto-run).
The first run downloads ~1 year of NSE history (needed for true 52-week-high
detection) and takes 5-10 minutes; watch progress live in the logs panel.
Every later scan takes seconds.

## Secrets go in .env, not in the app

- `SMTP_PASS` — Gmail App Password (myaccount.google.com/apppasswords)
- `GEMINI_API_KEY` — optional; enables AI re-ranking, otherwise a
  deterministic heuristic score is used

The API never returns secrets (they are masked as `********`), TLS
certificate verification stays on, and nothing sensitive needs to be typed
into the browser.

## Daily flow

1. **6:40 PM Mon–Fri** — cron inside the server runs the scanner
   (NSE publishes delivery % around 6:30 PM, hence the time).
2. Scanner scores every in-band NIFTY 500 stock on a 100-point Breakout
   Probability model: proximity to 52-week high (20), consolidation with
   falling ATR / Bollinger squeeze (20), volume dry-up (10), today's volume
   expansion (15), EMA alignment (10), RSI (10), ADX (10), relative strength
   vs market (5), sector strength (5), delivery (5), plus small accumulation
   (OBV/CMF) and candle-pattern bonuses. A-grade requires: score >= your
   threshold (default 70), all hard filters passed, and risk:reward >= your
   minimum. Stops go under the consolidation base when one exists; targets
   are T1 (2.5 x ATR) and T2 (4 x ATR).
3. Up to 10 recommendations are produced: **A-grade** = passed every filter
   (auto-tracked as open positions); **Watch** = closest near-misses with the
   exact failed filters shown (BR-05 max 10; BR-06 "no A-grade" message when
   none qualify).
4. Email goes out with buy zone, stop loss, target, confidence, risk, and
   holding period (BR-07/BR-08: every recommendation carries a stop and a
   holding period).
5. In the app: **Positions** shows open trades with running max gain / max
   drawdown; close them with your real exit price. The scanner also
   auto-closes on stop, target, or the 10-day time limit.
6. **Performance** answers the BRD's learning-loop questions: win rate,
   average return, winner/loser split, average hold, full closed history.

## Configuration without code (FR-009)

Everything in **Settings** — price band, RSI band, volume multiple,
delivery %, stocks per day, email details. Changes apply from the next scan.

## Files

```
server.js            Express server: API, static app, cron, email
data.json            config + recommendation history (no secrets)
.env                 secrets (never commit this)
dist/index.html      the web app (no build step needed)
scanner/scanner.py   the engine: fetch, indicators, rules, rank, review
scanner/market.db    price history cache (created on first scan)
```

## Windows notes

- The server tries `py` first, then `python`. Override with `PYTHON_CMD`
  in `.env` if your install is unusual.
- Keep the folder out of OneDrive (e.g. `C:\swingsense`) — sync can lock
  `data.json` and `market.db` mid-write.
- To start SwingSense automatically at login: Task Scheduler -> new task ->
  action `npm start` in the project folder, trigger "At log on".

## Honest notes

- Signals come from official end-of-day NSE data because delivery % only
  exists in the evening bhavcopy. Decide in the evening, execute next
  morning — the right rhythm for 5–10 day swings anyway.
- Confidence scores and targets are rule/ATR-based trade math, not price
  predictions. The Performance tab exists so the system earns your trust
  with its own record. Informational screening only — not investment advice.

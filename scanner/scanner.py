#!/usr/bin/env python3
"""
SwingSense AI - scanner engine v2 (event-driven).

v2 upgrade: instead of ranking on filters alone, every stock gets a
100-point Breakout Probability Score built from the pre-breakout evidence
professionals look for: proximity to 52-week high, consolidation (tight
range + falling ATR + Bollinger squeeze), volume dry-up before expansion,
EMA alignment, ADX, relative strength vs the market, sector strength,
delivery, accumulation (OBV/CMF) and simple candle patterns.

Pipeline: fetch NSE data -> indicators -> breakout scoring -> hard filters
          -> rank -> AI reasoning -> write data.json -> review open positions

Spawned by server.js. Config lives in ../data.json (editable in Settings).
All stdout ASCII (Windows safe), all file writes UTF-8.
Env: GEMINI_API_KEY (optional), SWINGSENSE_SKIP_FETCH=1 (testing).
"""
import io
import json
import os
import sqlite3
import sys
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import requests

HERE = Path(__file__).resolve().parent
DATA_JSON = HERE.parent / "data.json"
DB_PATH = HERE / "market.db"
CACHE_DIR = HERE / "cache"
LOOKBACK_DAYS = 260                    # ~1 year, enables true 52-week high
MAX_HOLD_DAYS = 10                     # fallback for old records

# horizon definitions live in the scoring section below

# NSE moves its archive hosts from time to time - try these in order.
UNIVERSE_URLS = [
    "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
    "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
    "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
]
BHAV_HOSTS = ["nsearchives.nseindia.com", "archives.nseindia.com"]
BHAV_PATH = "https://{host}/products/content/sec_bhavdata_full_{d}.csv"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
           "Referer": "https://www.nseindia.com/"}

DEFAULT_CFG = dict(
    # core hard filters
    minPrice=30, maxPrice=300, minVolumeMultiplier=1.5, minDeliveryPct=35,
    minRsi=50, maxRsi=70, trendEma=True,
    # breakout model
    minBreakoutScore=70,          # A-grade needs score >= this (80 = very strict)
    consolidationDays=15,         # window for tight-range detection
    resistanceLookback=20,        # days for resistance = highest high
    maxDistToBreakoutPct=3.0,     # "near breakout" if within this % of resistance
    requireEma200=False,          # optionally demand EMA50 > EMA200 too
    minAdx=0,                     # optionally demand ADX >= this (0 = off)
    minRsRank=60,                 # only stocks outperforming 60%+ of NIFTY 500
    # risk
    minRiskReward=1.2,            # A-grade needs T1 risk:reward >= this
    # minimum potential move (entry -> Target 2) required per horizon, in %
    minMovePct={"2d": 3.0, "1w": 5.0, "2w": 7.0, "1m": 12.0},
    # position sizing (used by the app to compute rupee amounts per card)
    capital=100000, riskPerTradePct=1.0,
    # output
    maxRecommendations=10, fillWithNearMisses=True,
)


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- data.json
def read_db_json():
    return json.loads(DATA_JSON.read_text(encoding="utf-8"))


def write_db_json(obj):
    DATA_JSON.write_text(json.dumps(obj, indent=2, ensure_ascii=False),
                         encoding="utf-8")


# ---------------------------------------------------------------- fetching
def _sql():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS prices (
        symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
        volume INTEGER, delivery_pct REAL, PRIMARY KEY (symbol, date))""")
    return conn


def load_universe():
    CACHE_DIR.mkdir(exist_ok=True)
    cache = CACHE_DIR / "nifty500.csv"
    last_err = None
    for url in UNIVERSE_URLS:
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            if b"Symbol" not in r.content[:2000]:
                raise ValueError("unexpected content")
            cache.write_bytes(r.content)
            log(f"  universe list from {url.split('/')[2]}")
            last_err = None
            break
        except Exception as e:
            last_err = e
    if last_err is not None:
        if not cache.exists():
            raise RuntimeError(
                "Cannot fetch the NIFTY 500 list from any known NSE host "
                f"(last error: {last_err}). Check your internet/DNS/firewall, "
                "or if NSE moved again, update UNIVERSE_URLS in scanner.py")
        log(f"  universe fetch failed on all hosts, using cached list")
    df = pd.read_csv(cache)
    ind_col = next((c for c in df.columns if "industry" in c.lower()), None)
    meta = {}
    for _, r in df.iterrows():
        meta[str(r["Symbol"]).strip()] = {
            "companyName": str(r.get("Company Name", "")).strip(),
            "sector": str(r[ind_col]).strip() if ind_col else "",
        }
    return meta


_bhav_host = None            # locked to the first host that answers

def fetch_bhavcopy(session, date):
    global _bhav_host
    ddmmyyyy = date.strftime("%d%m%Y")
    cache = CACHE_DIR / f"bhav_{ddmmyyyy}.csv"
    if cache.exists():
        raw = cache.read_bytes()
    else:
        hosts = [_bhav_host] if _bhav_host else BHAV_HOSTS
        raw = None
        for host in hosts:
            try:
                r = session.get(BHAV_PATH.format(host=host, d=ddmmyyyy),
                                timeout=30)
                if r.status_code == 200 and b"SYMBOL" in r.content[:200]:
                    raw = r.content
                    if _bhav_host != host:
                        _bhav_host = host
                        log(f"  using bhavcopy host: {host}")
                    break
            except requests.RequestException:
                continue
        if raw is None:
            return None
        cache.write_bytes(raw)
    df = pd.read_csv(io.BytesIO(raw))
    df.columns = [c.strip() for c in df.columns]
    df = df[df["SERIES"].str.strip() == "EQ"]
    out = pd.DataFrame({
        "symbol": df["SYMBOL"].str.strip(),
        "open": pd.to_numeric(df["OPEN_PRICE"], errors="coerce"),
        "high": pd.to_numeric(df["HIGH_PRICE"], errors="coerce"),
        "low": pd.to_numeric(df["LOW_PRICE"], errors="coerce"),
        "close": pd.to_numeric(df["CLOSE_PRICE"], errors="coerce"),
        "volume": pd.to_numeric(df["TTL_TRD_QNTY"], errors="coerce"),
        "delivery_pct": pd.to_numeric(df["DELIV_PER"], errors="coerce"),
    })
    out["date"] = date.isoformat()
    return out.dropna(subset=["close"])


def sync_history(universe):
    conn = _sql()
    if os.environ.get("SWINGSENSE_SKIP_FETCH") == "1":
        log("  SWINGSENSE_SKIP_FETCH=1 -> using cached history only")
        return conn
    have = {r[0] for r in conn.execute("SELECT DISTINCT date FROM prices")}
    added, checked = 0, 0
    d = dt.date.today()
    with requests.Session() as s:
        s.headers.update(HEADERS)
        try:
            s.get("https://www.nseindia.com", timeout=15)
        except requests.RequestException:
            pass
        while checked < LOOKBACK_DAYS:
            if d.weekday() < 5:
                checked += 1
                if d.isoformat() not in have:
                    df = fetch_bhavcopy(s, d)
                    if df is not None:
                        df = df[df["symbol"].isin(universe)]
                        df.to_sql("prices", conn, if_exists="append",
                                  index=False, method="multi", chunksize=500)
                        conn.commit()
                        added += 1
                        if added % 20 == 0 or added < 5:
                            log(f"  + {d} : {len(df)} symbols")
            d -= dt.timedelta(days=1)
    log(f"  history sync complete ({added} new day(s))")
    return conn


# ---------------------------------------------------------------- indicators
def ema(s, n): return s.ewm(span=n, adjust=False).mean()


def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def atr_series(df, n=14):
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"]-df["low"], (df["high"]-pc).abs(),
                    (df["low"]-pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()


def adx(df, n=14):
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([df["high"]-df["low"],
                    (df["high"]-df["close"].shift()).abs(),
                    (df["low"]-df["close"].shift()).abs()], axis=1).max(axis=1)
    atr_ = tr.ewm(alpha=1/n, adjust=False).mean()
    pdi = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1/n, adjust=False).mean() / atr_
    mdi = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1/n, adjust=False).mean() / atr_
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1/n, adjust=False).mean()


def snapshot(df):
    """Latest per-symbol state, including all pre-breakout evidence."""
    if len(df) < 60:
        return None
    df = df.reset_index(drop=True)
    n = len(df)
    close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]

    e20, e50 = ema(close, 20), ema(close, 50)
    e200 = ema(close, 200) if n >= 200 else e50
    macd_l = ema(close, 12) - ema(close, 26)
    macd_s = macd_l.ewm(span=9, adjust=False).mean()
    hist = macd_l - macd_s
    r = rsi(close)
    a = atr_series(df)
    ax = adx(df)
    avg_vol = vol.rolling(20).mean()

    # Bollinger bandwidth + its percentile over the last 120 bars (squeeze)
    mid = close.rolling(20).mean()
    sd = close.rolling(20).std()
    bw = ((mid + 2*sd) - (mid - 2*sd)) / mid
    bw_hist = bw.dropna().tail(120)
    bw_pctile = float((bw_hist <= bw_hist.iloc[-1]).mean() * 100) if len(bw_hist) > 20 else 50.0

    # OBV and CMF (accumulation)
    obv = (np.sign(close.diff().fillna(0)) * vol).cumsum()
    rng = (high - low).replace(0, np.nan)
    mfm = ((close - low) - (high - close)) / rng
    cmf = float((mfm * vol).rolling(20).sum().iloc[-1] /
                max(vol.rolling(20).sum().iloc[-1], 1))

    # candle patterns (yesterday -> today)
    o1, c1, h1, l1 = df["open"].iloc[-2], close.iloc[-2], high.iloc[-2], low.iloc[-2]
    o0, c0, h0, l0 = df["open"].iloc[-1], close.iloc[-1], high.iloc[-1], low.iloc[-1]
    ranges7 = (high - low).tail(7)
    patterns = []
    if (h0 - l0) == ranges7.min(): patterns.append("NR7")
    if h0 < h1 and l0 > l1: patterns.append("Inside bar")
    if c0 > o0 and c1 < o1 and c0 >= o1 and o0 <= c1: patterns.append("Bullish engulfing")

    i = n - 1
    look52 = min(n - 1, 250)
    hi52 = float(high.iloc[-look52-1:-1].max())
    ret20 = float(close.pct_change(20).iloc[-1] * 100) if n > 20 else 0.0
    ret63 = float(close.pct_change(min(63, n-1)).iloc[-1] * 100)
    ret126 = float(close.pct_change(min(126, n-1)).iloc[-1] * 100)
    ret252 = float(close.pct_change(min(252, n-1)).iloc[-1] * 100)

    # close location value: where in today's range did we close
    clv = float((c0 - l0) / max(h0 - l0, 1e-9))
    # today's volume percentile vs last 120 sessions
    v120 = vol.tail(120)
    vol_pctile = float((v120 <= vol.iloc[-1]).mean() * 100)
    # opening gap vs yesterday's close
    gap_pct = float((o0 - c1) / max(c1, 1e-9) * 100)
    # range expansion day (true range above ATR = trend igniting)
    tr_today = max(h0 - l0, abs(h0 - c1), abs(l0 - c1))
    tr_expand = bool(tr_today > a.iloc[-1])
    # weekly timeframe alignment
    weekly_up = False
    try:
        wk = df.set_index(pd.to_datetime(df["date"]))["close"].resample("W-FRI").last().dropna()
        if len(wk) >= 30:
            we10 = wk.ewm(span=10, adjust=False).mean()
            we30 = wk.ewm(span=30, adjust=False).mean()
            weekly_up = bool(we10.iloc[-1] > we30.iloc[-1] and wk.iloc[-1] > we10.iloc[-1])
    except Exception:
        pass
    # anchored VWAP from the lowest low of the last 120 sessions
    above_avwap = False
    try:
        a_idx = int(low.tail(120).idxmin())
        tp = (high + low + close) / 3
        seg_tp, seg_v = tp.iloc[a_idx:], vol.iloc[a_idx:]
        avwap = float((seg_tp * seg_v).sum() / max(seg_v.sum(), 1))
        above_avwap = bool(close.iloc[-1] > avwap)
    except Exception:
        pass
    # swing structure: higher highs AND higher lows (5-bar pivots, last 90 bars)
    hhhl = False
    try:
        hh, ll = high.tail(90).reset_index(drop=True), low.tail(90).reset_index(drop=True)
        ph = [hh[j] for j in range(5, len(hh)-5)
              if hh[j] == hh[j-5:j+6].max()]
        pl = [ll[j] for j in range(5, len(ll)-5)
              if ll[j] == ll[j-5:j+6].min()]
        hhhl = bool(len(ph) >= 2 and len(pl) >= 2
                    and ph[-1] > ph[-2] and pl[-1] > pl[-2])
    except Exception:
        pass
    # base length: sessions spent within 15% under the 20d pivot
    pivot20 = float(high.iloc[-21:-1].max())
    base_len = 0
    for j in range(i - 1, max(i - 121, 0), -1):
        if low.iloc[j] > pivot20 * 0.85 and high.iloc[j] <= pivot20 * 1.005:
            base_len += 1
        else:
            break

    return {
        "close": float(close.iloc[i]), "volume": float(vol.iloc[i]),
        "delivery_pct": float(df["delivery_pct"].iloc[i] or 0),
        "ema20": float(e20.iloc[-1]), "ema50": float(e50.iloc[-1]),
        "ema200": float(e200.iloc[-1]),
        "rsi": float(r.iloc[-1]),
        "macd": float(macd_l.iloc[-1]), "macd_signal": float(macd_s.iloc[-1]),
        "macd_hist": float(hist.iloc[-1]), "macd_hist_prev": float(hist.iloc[-2]),
        "macd_cross_up": bool(macd_l.iloc[-1] > macd_s.iloc[-1]
                              and macd_l.iloc[-2] <= macd_s.iloc[-2]),
        "atr": float(a.iloc[-1]),
        "atr_falling": bool(a.iloc[-1] < a.iloc[-11]) if n > 11 else False,
        "adx": float(ax.iloc[-1]) if pd.notna(ax.iloc[-1]) else 0.0,
        "adx_rising": bool(ax.iloc[-1] > ax.iloc[-6]) if n > 6 and pd.notna(ax.iloc[-6]) else False,
        "avg_volume_20": float(avg_vol.iloc[-1]) if pd.notna(avg_vol.iloc[-1]) else 0.0,
        "avg_volume_5_prior": float(vol.iloc[-6:-1].mean()),
        "avg_volume_30_prior": float(vol.iloc[-31:-1].mean()),
        "ret63": ret63, "ret126": ret126, "ret252": ret252,
        "clv": clv, "vol_pctile": vol_pctile, "gap_pct": gap_pct,
        "tr_expand": tr_expand, "weekly_up": weekly_up,
        "above_avwap": above_avwap, "hhhl": hhhl, "base_len": base_len,
        "bw_pctile": bw_pctile,
        "obv_rising": bool(obv.iloc[-1] > obv.iloc[-21]) if n > 21 else False,
        "cmf": cmf,
        "patterns": patterns,
        "hi52": hi52, "yr_bars": look52,
        "cons_low": float(low.iloc[-16:-1].min()),
        "chg1": float(close.pct_change().iloc[-1] * 100),
        "ret20": ret20,
        # filled later (need full df window per config):
        "_df_tail": df.tail(60)[["open", "high", "low", "close"]],
    }


# ---------------------------------------------------------------- scoring
HORIZONS = {
    "2d": dict(label="2 days",  hold=2,  stopATR=1.2, t1ATR=1.5, t2ATR=2.5, watch=2),
    "1w": dict(label="1 week",  hold=5,  stopATR=2.0, t1ATR=2.5, t2ATR=4.0, watch=5),
    "2w": dict(label="2 weeks", hold=10, stopATR=2.5, t1ATR=3.5, t2ATR=5.5, watch=5),
    "1m": dict(label="1 month", hold=22, stopATR=3.0, t1ATR=5.0, t2ATR=8.0, watch=7),
}

# what each horizon cares about (weights sum to 100)
H_WEIGHTS = {
    "2d": dict(volq=20, pivot=20, atrexp=10, adx=10, fresh=10, candle=10,
               rsrank=10, notext=5, deliv=5),
    "1w": dict(pivot=15, volq=15, vcp=12, rsrank=12, dryup=8, ema=8, weekly=7,
               rsi=6, adx=6, candle=4, deliv=4, accum=3),
    "2w": dict(vcp=18, rsrank=15, prox52=12, ema=10, volq=10, weekly=8,
               dryup=6, adx=6, sector=5, accum=5, deliv=5),
    "1m": dict(rsrank=18, ema=15, weekly=12, prox52=12, accum=12, sector=10,
               hhhl=8, vcp=5, rsi=4, deliv=4),
}


def components(s, cfg, rel_strength, sector_strong, res_dist, rs_rank=50):
    """Normalized 0-1 evidence fractions + human labels, computed once."""
    c = {}
    close = s["close"]

    prox = close / s["hi52"] * 100 if s["hi52"] else 0
    yr = "52w" if s["yr_bars"] >= 240 else f"{s['yr_bars']}d"
    c["prox52"] = ((1.0, f"At/near new {yr} high ({prox:.0f}%)") if prox >= 95 else
                   (0.6, f"Near {yr} high ({prox:.0f}%)") if prox >= 90 else
                   (0.3, f"{prox:.0f}% of {yr} high") if prox >= 85 else
                   (0.0, f"Far from {yr} high ({prox:.0f}%)"))

    tail = s["_df_tail"]; cd = int(cfg["consolidationDays"])
    win = tail.tail(cd + 1).iloc[:-1]
    rng_pct = (win["high"].max() - win["low"].min()) / close * 100 if len(win) >= 5 else 99
    base = 0.7 if rng_pct <= 8 else 0.45 if rng_pct <= 12 else 0.2 if rng_pct <= 16 else 0
    if base and s["atr_falling"]: base += 0.15
    if base and s["bw_pctile"] <= 25: base += 0.15
    c["consol"] = (min(base, 1.0),
                   (f"Consolidating {cd}d (range {rng_pct:.0f}%"
                    + (", ATR falling" if s["atr_falling"] else "")
                    + (", BB squeeze" if s["bw_pctile"] <= 25 else "") + ")")
                   if base else f"No consolidation (range {rng_pct:.0f}%)")
    c["_rng_pct"] = rng_pct

    dry = s["avg_volume_5_prior"] / max(s["avg_volume_20"], 1)
    c["dryup"] = ((1.0, "Volume dry-up before move") if dry <= 0.75 else
                  (0.5, "Mild volume dry-up") if dry <= 0.9 else
                  (0.0, "No volume dry-up"))

    vx = s["volume"] / max(s["avg_volume_20"], 1)
    c["volexp"] = ((1.0, f"Volume expansion {vx:.1f}x") if vx >= 2 else
                   (0.65, f"Volume {vx:.1f}x average") if vx >= 1.5 else
                   (0.3, f"Volume {vx:.1f}x average") if vx >= 1.2 else
                   (0.0, f"Volume only {vx:.1f}x average"))

    c["ema"] = ((1.0, "EMA20 > EMA50 > EMA200 aligned")
                if s["ema20"] > s["ema50"] > s["ema200"] and close > s["ema20"] else
                (0.6, "EMA20 above EMA50") if s["ema20"] > s["ema50"] else
                (0.0, "EMAs not aligned"))

    c["rsi"] = ((1.0, f"RSI {s['rsi']:.0f} in sweet spot")
                if cfg["minRsi"] <= s["rsi"] <= cfg["maxRsi"] else
                (0.5, f"RSI {s['rsi']:.0f} acceptable")
                if cfg["minRsi"]-5 <= s["rsi"] <= cfg["maxRsi"]+4 else
                (0.0, f"RSI {s['rsi']:.0f} outside range"))

    c["adx"] = ((1.0, f"ADX {s['adx']:.0f} and rising")
                if s["adx"] >= 25 and s["adx_rising"] else
                (0.6, f"ADX {s['adx']:.0f}") if s["adx"] >= 20 else
                (0.0, f"ADX weak ({s['adx']:.0f})"))

    c["rel"] = ((1.0, f"Outperforming market by {rel_strength:.1f}% (20d)")
                if rel_strength > 0 else (0.0, "Underperforming market (20d)"))
    c["sector"] = ((1.0, "Sector among market leaders") if sector_strong
                   else (0.0, "Sector not leading"))
    c["deliv"] = ((1.0, f"Delivery {s['delivery_pct']:.0f}%") if s["delivery_pct"] >= 45 else
                  (0.6, f"Delivery {s['delivery_pct']:.0f}%")
                  if s["delivery_pct"] >= cfg["minDeliveryPct"] else
                  (0.0, f"Delivery only {s['delivery_pct']:.0f}%"))
    c["accum"] = ((1.0, "Accumulation (OBV rising, CMF positive)")
                  if s["obv_rising"] and s["cmf"] > 0 else
                  (0.5, "OBV rising") if s["obv_rising"] else
                  (0.0, "No accumulation signature"))
    c["fresh"] = ((1.0, "Fresh MACD bullish crossover") if s["macd_cross_up"] else
                  (0.5, "MACD improving")
                  if s["macd_hist"] > s["macd_hist_prev"] else (0.0, "MACD flat"))
    c["notext"] = ((1.0, f"Not extended (today {s['chg1']:+.1f}%)")
                   if 0 < s["chg1"] <= 6 else
                   (0.4, f"Today {s['chg1']:+.1f}%") if -1 <= s["chg1"] <= 0 else
                   (0.0, f"Extended/weak today ({s['chg1']:+.1f}%)"))
    c["res"] = ((1.0, "Broke resistance today") if res_dist <= 0 else
                (0.8, f"Within {res_dist:.1f}% of resistance")
                if res_dist <= cfg["maxDistToBreakoutPct"] else
                (0.0, f"{res_dist:.1f}% below resistance"))
    # --- upgraded evidence (institutional additions) ---
    c["rsrank"] = ((1.0, f"RS Rank {rs_rank:.0f} - market leader") if rs_rank >= 90 else
                   (0.75, f"RS Rank {rs_rank:.0f}") if rs_rank >= 80 else
                   (0.5, f"RS Rank {rs_rank:.0f}") if rs_rank >= 70 else
                   (0.25, f"RS Rank {rs_rank:.0f}") if rs_rank >= 60 else
                   (0.0, f"RS Rank {rs_rank:.0f} - laggard"))

    # VCP: successive range contraction 30d -> 20d -> 10d (Minervini)
    t = s["_df_tail"]
    def _rng(nn):
        w = t.tail(nn + 1).iloc[:-1]
        return (w["high"].max() - w["low"].min()) / close * 100 if len(w) >= 5 else 99
    r30, r20, r10 = _rng(30), _rng(20), _rng(10)
    shrink = r30 > r20 > r10
    vcp = (1.0 if shrink and r10 <= 5 else
           0.75 if shrink and r10 <= 8 else
           0.45 if shrink else
           0.25 if r10 <= 6 else 0.0)
    bl = s["base_len"]
    if vcp and 25 <= bl <= 80: vcp = min(vcp + 0.15, 1.0)
    elif vcp and bl < 10: vcp = max(vcp - 0.25, 0.0)      # too-short base: fake-out risk
    c["vcp"] = (vcp,
                (f"VCP: contractions {r30:.0f}%->{r20:.0f}%->{r10:.0f}%"
                 + (f", {bl}d base" if bl else "")) if vcp >= 0.45 else
                (f"Tight 10d range {r10:.0f}%" if vcp > 0 else
                 f"No contraction pattern ({r30:.0f}/{r20:.0f}/{r10:.0f}%)"))

    # pivot proximity: ideal is -1% to +0.5% around the pivot (dist>0 = below)
    d = res_dist
    c["pivot"] = ((1.0, f"At pivot ({d:+.1f}% to breakout)") if -0.5 <= d <= 1.0 else
                  (0.7, f"{d:.1f}% below pivot") if 1.0 < d <= 2.0 else
                  (0.45, f"{d:.1f}% below pivot")
                  if 2.0 < d <= cfg["maxDistToBreakoutPct"] else
                  (0.3, f"Broke pivot, {-d:.1f}% above") if -2.0 <= d < -0.5 else
                  (0.0, f"Extended {-d:.1f}% past pivot - late") if d < -2.0 else
                  (0.0, f"{d:.1f}% from pivot - not set up"))

    # volume quality: percentile beats fixed multiples
    vp = s["vol_pctile"]
    c["volq"] = ((1.0, f"Volume P{vp:.0f} of 120d ({vx:.1f}x)") if vp >= 95 else
                 (0.7, f"Volume P{vp:.0f} ({vx:.1f}x)") if vp >= 85 else
                 (0.45, f"Volume {vx:.1f}x average") if vx >= 1.5 else
                 (0.2, f"Volume {vx:.1f}x average") if vx >= 1.2 else
                 (0.0, f"Volume quiet ({vx:.1f}x)"))

    # dry-up quality vs 30d baseline (institutional bases go very quiet)
    dq = s["avg_volume_5_prior"] / max(s["avg_volume_30_prior"], 1)
    c["dryup"] = ((1.0, f"Deep volume dry-up ({dq*100:.0f}% of 30d avg)") if dq <= 0.4 else
                  (0.7, f"Volume dry-up ({dq*100:.0f}%)") if dq <= 0.6 else
                  (0.4, f"Mild dry-up ({dq*100:.0f}%)") if dq <= 0.75 else
                  (0.0, "No volume dry-up"))

    c["weekly"] = ((1.0, "Weekly trend aligned (10>30 wEMA)") if s["weekly_up"]
                   else (0.0, "Weekly trend not aligned"))
    c["hhhl"] = ((1.0, "Higher highs + higher lows structure") if s["hhhl"]
                 else (0.0, "No HH/HL structure"))
    c["atrexp"] = ((1.0, "Range expansion day (TR > ATR)") if s["tr_expand"]
                   else (0.0, "No range expansion"))

    # candle quality: strong close + pattern
    cl = s["clv"]
    cq = (0.6 if cl >= 0.75 else 0.3 if cl >= 0.6 else 0.0)
    if s["patterns"]: cq = min(cq + 0.4, 1.0)
    c["candle"] = (cq, (f"Closed top of range (CLV {cl:.2f})"
                        + (", " + ", ".join(s["patterns"]) if s["patterns"] else ""))
                   if cq else f"Weak close (CLV {cl:.2f})")

    # accumulation upgraded: OBV + CMF + anchored VWAP
    acc = (0.3 if s["obv_rising"] else 0) + (0.3 if s["cmf"] > 0 else 0)           + (0.4 if s["above_avwap"] else 0)
    lbl = [x for x, ok in [("OBV rising", s["obv_rising"]),
                           ("CMF positive", s["cmf"] > 0),
                           ("above anchored VWAP", s["above_avwap"])] if ok]
    c["accum"] = (acc, ("Accumulation: " + ", ".join(lbl)) if lbl
                  else "No accumulation signature")

    if s["patterns"]:
        c["pattern"] = (1.0, "Pattern: " + ", ".join(s["patterns"]))
    return c


def horizon_scores(c):
    """Score all 4 horizons from the shared components."""
    out = {}
    for h, w in H_WEIGHTS.items():
        pts, br = 0.0, []
        for key, weight in w.items():
            frac, label = c.get(key, (0.0, key))
            got = frac * weight
            pts += got
            br.append({"label": label, "points": round(got, 1), "max": weight})
        if "pattern" in c:
            pts += 2
            br.append({"label": c["pattern"][1], "points": 2, "max": 2})
        out[h] = (min(round(pts), 100), br)
    return out


def resistance_info(s, cfg):
    tail = s["_df_tail"]
    lb = int(cfg["resistanceLookback"])
    res = float(tail["high"].tail(lb + 1).iloc[:-1].max())
    dist = (res - s["close"]) / s["close"] * 100
    return res, dist


def hard_filter_fails(s, cfg, rs_rank=50):
    """The user's core filters - still enforced for A-grade."""
    fails = []
    if not (cfg["minPrice"] <= s["close"] <= cfg["maxPrice"]):
        fails.append(f"price Rs{s['close']:.0f} outside {cfg['minPrice']}-{cfg['maxPrice']}")
    if cfg.get("trendEma", True) and not s["ema20"] > s["ema50"]:
        fails.append("EMA20 below EMA50")
    if cfg.get("requireEma200") and not s["ema50"] > s["ema200"]:
        fails.append("EMA50 below EMA200")
    if not (cfg["minRsi"] <= s["rsi"] <= cfg["maxRsi"]):
        fails.append(f"RSI {s['rsi']:.0f} outside {cfg['minRsi']}-{cfg['maxRsi']}")
    macd_ok = (s["macd"] > s["macd_signal"]
               or s["macd_hist"] > s["macd_hist_prev"])   # histogram turning up
    if not macd_ok:
        fails.append("MACD not bullish or improving")
    if not (s["avg_volume_20"] > 0
            and s["volume"] > cfg["minVolumeMultiplier"] * s["avg_volume_20"]):
        fails.append(f"volume below {cfg['minVolumeMultiplier']}x average")
    if s["delivery_pct"] < cfg["minDeliveryPct"]:
        fails.append(f"delivery {s['delivery_pct']:.0f}% below {cfg['minDeliveryPct']}%")
    if cfg.get("minAdx", 0) and s["adx"] < cfg["minAdx"]:
        fails.append(f"ADX {s['adx']:.0f} below {cfg['minAdx']}")
    if cfg.get("minRsRank", 0) and rs_rank < cfg["minRsRank"]:
        fails.append(f"RS Rank {rs_rank:.0f} below {cfg['minRsRank']}")
    if s.get("gap_pct", 0) > 5:
        fails.append(f"gapped up {s['gap_pct']:.1f}% at open - poor R:R, chase risk")
    return fails


# ---------------------------------------------------------------- AI
def gemini_rank(cands, mood, api_key):
    if not api_key:
        return
    payload = [{"symbol": c["symbol"], "sector": c["sector"],
                "breakout_score": c["breakoutScore"],
                "evidence": [b["label"] for b in c["scoreBreakdown"] if b["points"] > 0],
                "risk_reward": c["riskReward"]} for c in cands]
    prompt = (f"Rank these NSE pre-breakout swing candidates (5-10 day holds). "
              f"Market mood: {mood}. Return ONLY a JSON array of "
              '{"symbol","score" (0-100),"reason" (one short line)} - no fences.\n'
              + json.dumps(payload))
    try:
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               f"gemini-1.5-flash:generateContent?key={api_key}")
        r = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]},
                          timeout=60)
        r.raise_for_status()
        body = r.json()
        u = body.get("usageMetadata", {})
        if u:
            log(f"  Gemini tokens: {u.get('promptTokenCount',0)} in + "
                f"{u.get('candidatesTokenCount',0)} out = "
                f"{u.get('totalTokenCount',0)} total")
        gemini_rank.last_usage = u
        text = body["candidates"][0]["content"]["parts"][0]["text"].strip()
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        ai = {x["symbol"]: x for x in json.loads(text)}
        for c in cands:
            if c["symbol"] in ai:
                c["aiExplanation"] = ai[c["symbol"]]["reason"] + "\n" + c["aiExplanation"]
        log("  AI commentary applied (Gemini)")
    except Exception as e:
        log(f"  AI commentary skipped ({type(e).__name__})")


# ---------------------------------------------------------------- review
def review_open(db, conn):
    closed = 0
    for rec in db["recommendations"]:
        if rec.get("status") != "OPEN":
            continue
        px = pd.read_sql(
            "SELECT date, high, low, close FROM prices "
            "WHERE symbol=? AND date>? ORDER BY date",
            conn, params=(rec["symbol"], rec["date"]))
        if px.empty:
            continue
        entry = rec["closePrice"]
        rec["maxGainPct"] = round((px["high"].max()-entry)/entry*100, 2)
        rec["maxDrawdownPct"] = round((px["low"].min()-entry)/entry*100, 2)
        target = rec.get("target2") or rec["target"]
        for i, day in px.iterrows():
            if day["low"] <= rec["stopLoss"]:
                st, xp = "STOP_LOSS_HIT", rec["stopLoss"]
            elif day["high"] >= target:
                st, xp = "TARGET_HIT", target
            elif i + 1 >= rec.get("maxHoldDays", MAX_HOLD_DAYS):
                st, xp = "TIME_EXIT", day["close"]
            else:
                continue
            rec.update(status=st, exitPrice=round(float(xp), 2),
                       exitDate=day["date"],
                       pnlPct=round((xp-entry)/entry*100, 2), closedBy="auto")
            closed += 1
            break
    if closed:
        log(f"  auto-closed {closed} open recommendation(s)")


# ---------------------------------------------------------------- main
def main():
    log("SwingSense scanner v2 (breakout model) starting")
    db = read_db_json()
    cfg = db.get("config", {}).get("scanner", {})
    for k, v in DEFAULT_CFG.items():
        cfg.setdefault(k, v)
    for k, v in DEFAULT_CFG["minMovePct"].items():
        cfg["minMovePct"].setdefault(k, v)

    log("Step 1/6 syncing NSE data (first run downloads ~1 year, be patient)")
    universe = load_universe()
    conn = sync_history(set(universe))

    log("Step 2/6 computing indicators")
    hist = pd.read_sql("SELECT * FROM prices ORDER BY symbol, date", conn)
    if hist.empty:
        log("[ERROR] no price data available"); sys.exit(1)
    snaps = {}
    for sym, g in hist.groupby("symbol"):
        s = snapshot(g)
        if s:
            snaps[sym] = s
    log(f"  {len(snaps)} stocks with enough history")

    med_ret20 = float(np.median([s["ret20"] for s in snaps.values()])) if snaps else 0
    # RS Rating: weighted 3/6/12-month return, percentile-ranked across universe
    comp = {sym: 0.4*sn["ret63"] + 0.35*sn["ret126"] + 0.25*sn["ret252"]
            for sym, sn in snaps.items()}
    vals = np.array(sorted(comp.values()))
    rs_ranks = {sym: float((vals <= v).mean() * 100) for sym, v in comp.items()}
    above = sum(1 for s in snaps.values() if s["close"] > s["ema50"]) / max(len(snaps), 1)
    adv = sum(1 for s in snaps.values() if s["chg1"] > 0) / max(len(snaps), 1)
    mood_score = above*0.6 + adv*0.4
    mood = "Bullish" if mood_score > 0.58 else ("Bearish" if mood_score < 0.42 else "Neutral")
    log(f"Step 3/6 market mood: {mood} ({above*100:.0f}% above EMA50)")

    # sector strength: top 40% of sectors by average 20d return
    sec_ret = {}
    for sym, s in snaps.items():
        sec = universe.get(sym, {}).get("sector", "")
        sec_ret.setdefault(sec, []).append(s["ret20"])
    sec_avg = {k: float(np.mean(v)) for k, v in sec_ret.items() if len(v) >= 5}
    strong_cut = np.percentile(list(sec_avg.values()), 60) if sec_avg else 0
    strong_sectors = {k for k, v in sec_avg.items() if v >= strong_cut}
    top_sec = max(sec_avg, key=sec_avg.get) if sec_avg else ""
    weak_sec = min(sec_avg, key=sec_avg.get) if sec_avg else ""
    log(f"  top sector: {top_sec} / weak sector: {weak_sec}")

    log("Step 4/6 horizon scoring (2d / 1w / 2w / 1m)")
    today = dt.date.today().isoformat()
    scored = []
    for sym, s in snaps.items():
        if not (cfg["minPrice"] <= s["close"] <= cfg["maxPrice"]):
            continue
        rel = s["ret20"] - med_ret20
        sec = universe.get(sym, {}).get("sector", "")
        res, dist = resistance_info(s, cfg)
        rsr = rs_ranks.get(sym, 50)
        comps = components(s, cfg, rel, sec in strong_sectors, dist, rsr)
        hs = horizon_scores(comps)
        best_h = max(hs, key=lambda h: hs[h][0])   # single best horizon: no repeats
        score, breakdown = hs[best_h]
        fails = hard_filter_fails(s, cfg, rsr)
        scored.append(dict(sym=sym, s=s, score=score, breakdown=breakdown,
                           horizon=best_h, res=res, dist=dist, fails=fails,
                           rs_rank=rsr))
    log(f"  scored {len(scored)} in-band stocks across 4 horizons")

    def classify(x):
        s = x["s"]
        if x["dist"] <= 0 and x["vx"] >= 2 and s["chg1"] >= 2:
            return "2d"     # broke resistance today on big volume: momentum burst
        if 0 <= x["dist"] <= 1.5 and x["vx"] >= 1.5:
            return "1w"     # knocking on resistance with participation
        if x["rng_pct"] <= 12:
            return "2w"     # tight consolidation: classic swing breakout
        if (s["ema20"] > s["ema50"] > s["ema200"] and s["adx"] >= 22
                and x["rel"] > 0):
            return "1m"     # aligned trend + relative strength: position ride
        return "2w"

    def make_rec(x, grade):
        s, sym = x["s"], x["sym"]
        hz = HORIZONS[x["horizon"]]
        entry, a = s["close"], s["atr"]
        # tight base -> stop goes just under the consolidation low
        # (tighter risk is what makes breakout trades pay), else 2 x ATR
        tail = s["_df_tail"]
        cd = int(cfg["consolidationDays"])
        win = tail.tail(cd + 1).iloc[:-1]
        rng_pct = ((win["high"].max() - win["low"].min()) / entry * 100
                   if len(win) >= 5 else 99)
        atr_stop = entry - hz["stopATR"]*a
        base_stop = s["cons_low"] * 0.99
        stop = round(max(atr_stop, base_stop) if rng_pct <= 16 else atr_stop, 2)
        stop = min(stop, round(entry * 0.995, 2))          # never above ~entry
        t1 = round(entry + hz["t1ATR"]*a, 2)
        t2 = round(entry + hz["t2ATR"]*a, 2)
        rr = round((t1 - entry) / max(entry - stop, 0.01), 2)
        t1_pct = round((t1 - entry) / entry * 100, 1)
        t2_pct = round((t2 - entry) / entry * 100, 1)
        near_bo = 0 <= x["dist"] <= cfg["maxDistToBreakoutPct"]
        def add_days(d0, nn):
            d0 = dt.date.fromisoformat(d0)
            while nn > 0:
                d0 += dt.timedelta(days=1)
                if d0.weekday() < 5: nn -= 1
            return d0.isoformat()
        watch_until = add_days(today, hz["watch"]) if grade == "B" else None
        stars = min(5, max(1, int(x["score"] // 20) + (1 if x["score"] >= 90 else 0)))
        glabel = ("A+" if x["score"] >= 90 else "A" if x["score"] >= 80 else "B")
        vx_ = s["volume"] / max(s["avg_volume_20"], 1)
        d_ = x["dist"]
        pos_txt = (f"{-d_:.1f}% above its pivot" if d_ < 0
                   else f"{d_:.1f}% below its pivot Rs{x['res']:.1f}")
        base_txt = (f" after a {s['base_len']}-day base" if s["base_len"] >= 10 else "")
        align_txt = ("weekly and daily trends aligned" if s["weekly_up"]
                     else "daily uptrend")
        summary = (f"{glabel} ({x['score']}/100): trading {pos_txt}{base_txt}. "
                   f"Volume {vx_:.1f}x average (P{s['vol_pctile']:.0f}), "
                   f"delivery {s['delivery_pct']:.0f}%, RS Rank {x['rs_rank']:.0f}, "
                   f"{align_txt}. About {round((entry + hz['t2ATR']*a - entry)/entry*100,1)}% "
                   f"upside to T2 at {rr}:1 risk-reward over {hz['label']}.")
        evidence = [b["label"] for b in x["breakdown"] if b["points"] > 0]
        if near_bo:
            evidence.insert(0, f"Within {x['dist']:.1f}% of {cfg['resistanceLookback']}d resistance Rs{x['res']:.1f}")
        elif x["dist"] < 0:
            evidence.insert(0, f"Broke {cfg['resistanceLookback']}d resistance Rs{x['res']:.1f}")
        return {
            "id": f"{sym}-{today}", "date": today, "symbol": sym,
            "companyName": universe.get(sym, {}).get("companyName", sym),
            "sector": universe.get(sym, {}).get("sector", ""),
            "closePrice": round(entry, 2),
            "buyZone": f"{entry*0.99:.1f} - {entry*1.01:.1f}",
            "stopLoss": stop, "target": t1, "target1": t1, "target2": t2,
            "riskReward": rr,
            "target1Pct": t1_pct, "potentialPct": t2_pct,
            "resistance": round(x["res"], 2),
            "distToResistancePct": round(x["dist"], 2),
            "horizon": x["horizon"],
            "horizonLabel": hz["label"],
            "maxHoldDays": hz["hold"],
            "holdingPeriod": hz["label"],
            "breakoutScore": x["score"],
            "confidenceScore": x["score"],           # backward compat
            "stars": stars,
            "gradeLabel": glabel,
            "rsRank": round(x["rs_rank"], 0),
            "summary": summary,
            "scoreBreakdown": x["breakdown"],
            "riskRating": ("Low" if a/entry*100 < 2 else
                           "Medium" if a/entry*100 < 4 else "High"),
            "grade": grade,
            "failedFilters": x["fails"],
            "aiExplanation": "\n".join(evidence),
            "watchUntil": watch_until,
            "triggerAbove": round(max(x["res"], entry), 2),
            "invalidBelow": stop,
            "status": "OPEN" if grade == "A" else "WATCH",
            "marketMood": mood,
        }

    max_n = int(cfg["maxRecommendations"])
    if mood == "Bearish":
        max_n = max(3, max_n - 3)
        log("  bearish tape -> recommending fewer stocks")

    a_pool = [x for x in scored if not x["fails"]
              and x["score"] >= cfg["minBreakoutScore"]]
    recs = []
    for h in HORIZONS:
        pool_h = sorted([x for x in a_pool if x["horizon"] == h],
                        key=lambda x: x["score"], reverse=True)
        for x in pool_h[:max_n]:
            r = make_rec(x, "A")
            if (r["riskReward"] >= cfg["minRiskReward"]
                    and r["potentialPct"] >= cfg["minMovePct"][h]):
                recs.append(r)
    log(f"  {len(recs)} A-grade across horizons (score >= "
        f"{cfg['minBreakoutScore']}, all filters, R:R >= {cfg['minRiskReward']})")

    gemini_rank(recs, mood, os.environ.get("GEMINI_API_KEY")
                or db.get("config", {}).get("geminiApiKey", ""))

    if cfg["fillWithNearMisses"]:
        chosen = {r["symbol"] for r in recs}
        for h in HORIZONS:
            have_h = sum(1 for r in recs if r["horizon"] == h)
            pool_h = sorted([x for x in scored
                             if x["horizon"] == h and x["sym"] not in chosen
                             and x["score"] >= cfg["minBreakoutScore"]],
                            key=lambda x: x["score"], reverse=True)
            added = 0
            for x in pool_h:
                if added >= max(0, max_n - have_h):
                    break
                r = make_rec(x, "B")
                if r["potentialPct"] >= cfg["minMovePct"][h]:
                    recs.append(r)
                    chosen.add(x["sym"])
                    added += 1
        log(f"  watchlist fill: only score >= {cfg['minBreakoutScore']} "
            f"and per-horizon min move (total {len(recs)})")

    log("Step 5/6 saving recommendations")
    db = read_db_json()
    db["recommendations"] = [r for r in db["recommendations"]
                             if not (r["date"] == today and
                                     r.get("closedBy") != "manual")]
    kept_ids = {r["id"] for r in db["recommendations"]}
    db["recommendations"].extend(r for r in recs if r["id"] not in kept_ids)

    log("Step 6/6 reviewing open positions")
    review_open(db, conn)
    db["lastScan"] = {"date": today, "mood": mood,
                      "aiTokens": getattr(gemini_rank, "last_usage", None),
                      "passed": len(a_pool), "shown": len(recs),
                      "topSector": top_sec, "weakSector": weak_sec,
                      "at": dt.datetime.now().isoformat(timespec="seconds")}
    write_db_json(db)
    conn.close()

    a_grade = sum(1 for r in recs if r["grade"] == "A")
    log(f"Done. {a_grade} A-grade signal(s), {len(recs)-a_grade} watchlist.")
    if a_grade == 0:
        log("No A-grade swing opportunities identified today.")


if __name__ == "__main__":
    main()

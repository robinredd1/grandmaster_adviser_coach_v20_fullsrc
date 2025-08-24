import os, sys, time, math, asyncio, itertools, collections, json
from datetime import datetime, timezone, timedelta, date
from typing import List, Dict, Any, Tuple
import httpx
from config import *

FINNHUB_BASE = "https://finnhub.io/api/v1"
HEADERS_ALPACA = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_API_SECRET}

def write_json(path: str, payload: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)

class RateLimiter:
    def __init__(self, max_calls:int, period:float):
        self.max_calls=max_calls; self.period=period
        self.calls=collections.deque(); self.lock=asyncio.Lock()
    async def acquire(self):
        while True:
            async with self.lock:
                now=time.monotonic()
                while self.calls and now-self.calls[0]>self.period:
                    self.calls.popleft()
                if len(self.calls)<self.max_calls:
                    self.calls.append(now); return
                wait=self.period-(now-self.calls[0])+0.02
            await asyncio.sleep(max(wait,0.05))

async def fetch_symbols(client: httpx.AsyncClient) -> List[str]:
    r = await client.get(f"{FINNHUB_BASE}/stock/symbol", params={"exchange":"US","token":FINNHUB_API_KEY}, timeout=60.0)
    r.raise_for_status()
    return sorted({d.get("symbol") for d in r.json() if (d.get("symbol") or "").isupper()})

async def fh_quote(client: httpx.AsyncClient, limiter: RateLimiter, sym: str):
    await limiter.acquire()
    r = await client.get(f"{FINNHUB_BASE}/quote", params={"symbol":sym,"token":FINNHUB_API_KEY}, timeout=12.0)
    if r.status_code!=200: return None
    q=r.json()
    c=float(q.get("c") or 0); pc=float(q.get("pc") or 0); dp=float(q.get("dp") or 0.0)
    if c>0 and pc>0 and dp==0.0:
        dp = (c/pc - 1.0) * 100.0
    return {"c":c,"pc":pc,"dp":dp}

async def fh_candle_1m(client: httpx.AsyncClient, limiter: RateLimiter, sym: str, count:int=10):
    await limiter.acquire()
    r = await client.get(f"{FINNHUB_BASE}/stock/candle", params={"symbol":sym,"resolution":"1","count":count,"token":FINNHUB_API_KEY}, timeout=12.0)
    if r.status_code!=200: return None
    j=r.json()
    if j.get("s")!="ok": return None
    return j

async def fh_dollar_vol_5m(client: httpx.AsyncClient, limiter: RateLimiter, sym: str) -> float:
    await limiter.acquire()
    r = await client.get(f"{FINNHUB_BASE}/stock/candle", params={"symbol":sym,"resolution":"5","count":2,"token":FINNHUB_API_KEY}, timeout=12.0)
    if r.status_code!=200: return 0.0
    j=r.json()
    if j.get("s")!="ok": return 0.0
    try:
        v=float(j["v"][-1]); c=float(j["c"][-1])
        return v*c
    except Exception:
        return 0.0

async def fh_earnings_soon(client: httpx.AsyncClient, limiter: RateLimiter, sym:str)->bool:
    today=date.today()
    start=(today - timedelta(days=EARNINGS_BLACKOUT_DAYS)).isoformat()
    end=(today + timedelta(days=EARNINGS_BLACKOUT_DAYS)).isoformat()
    await limiter.acquire()
    r=await client.get(f"{FINNHUB_BASE}/calendar/earnings", params={"from":start,"to":end,"symbol":sym,"token":FINNHUB_API_KEY}, timeout=12.0)
    if r.status_code!=200: return False
    data=r.json() or {}
    return bool(data.get("earningsCalendar") or [])

def gap_pct(c, pc):
    if c and pc: return (c/pc - 1.0) * 100.0
    return 0.0

def buy_limit_from_last(last: float, bps:int) -> float:
    raw = last * (1.0 + bps/10000.0)
    return round(raw, 4 if last<1 else 2)

def size_by_risk(price: float, stop_pct: float) -> int:
    if price<=0 or stop_pct<=0: return 0
    risk_per_share = price * stop_pct
    if risk_per_share<=0: return 0
    qty = int(RISK_PER_TRADE_DOLLARS // risk_per_share)
    return max(qty, 1)

def size_by_dollars(price: float, dollars: float) -> int:
    if price<=0: return 0
    return max(int(dollars // price), 1)

def build_pick(sym: str, last: float, day: float, *, mode: str, stop_pct: float, trail_pct: float, limit_bps: int, fallback_dollars: float, dv5: float, r_mult: float=None) -> dict:
    limit = buy_limit_from_last(last, limit_bps)
    qty_risk = size_by_risk(last, stop_pct)
    qty_cash = size_by_dollars(last, fallback_dollars)
    qty = max(qty_risk, qty_cash) if not ALLOW_FRACTIONAL else round(max(fallback_dollars/last, qty_risk), 4)
    stop_price = round(last * (1.0 - stop_pct), 4 if last<1 else 2)
    target = round(last * (1.0 + (stop_pct * (r_mult or 1.5))), 4 if last<1 else 2)
    return {
        "symbol": sym, "last": round(last,4), "day_pct": round(day,2),
        "buy_limit": limit, "size_qty": str(qty),
        "stop_price": stop_price, "trail_pct": round(trail_pct*100,2),
        "first_target": target, "mode": mode, "dv5": int(dv5)
    }

async def scan_once():
    limiter=RateLimiter(FINNHUB_MAX_CALLS_PER_MIN, 60.0)
    picks_quick=[]; picks_core=[]
    async with httpx.AsyncClient() as ac:
        # account snapshot
        try:
            ar = await ac.get(ALPACA_BROKER_BASE+"/v2/account", headers=HEADERS_ALPACA, timeout=20.0)
            if ar.status_code==200:
                acc=ar.json()
                eq=float(acc.get("equity") or 0); last_eq=float(acc.get("last_equity") or 0)
                cash=float(acc.get("cash") or 0); bp=float(acc.get("buying_power") or 0)
                pnl=eq - last_eq
                write_json(STATE_ACCOUNT, {"ts": datetime.now().isoformat(timespec="seconds"), "equity":eq, "cash":cash, "buying_power":bp, "day_pnl":pnl})
        except Exception:
            pass

        # core leaders
        core_quotes = await asyncio.gather(*[fh_quote(ac, limiter, s) for s in CORE_SYMBOLS])
        core_scored = []
        for s,q in zip(CORE_SYMBOLS, core_quotes):
            if not q: continue
            last=q["c"]; day=q["dp"]
            if last<=0: continue
            if day >= CORE_MIN_DAY_PCT:
                dv5 = await fh_dollar_vol_5m(ac, limiter, s)
                if dv5 >= CORE_MIN_DOLLAR_VOL_5M:
                    pick = build_pick(s,last,day, mode="CORE", stop_pct=CORE_STOP_PCT, trail_pct=CORE_TRAIL_PCT, limit_bps=CORE_LIMIT_SLIPPAGE_BPS, fallback_dollars=DOLLARS_PER_CORE, dv5=dv5, r_mult=2.0)
                    core_scored.append((day, pick))
        core_scored.sort(key=lambda x: x[0], reverse=True)
        picks_core = [p for _,p in core_scored[:CORE_TAKE]]

        # quick wins
        syms = await fetch_symbols(ac)
        batch = syms[:SCAN_BATCH_SIZE]
        quotes = await asyncio.gather(*[fh_quote(ac, limiter, s) for s in batch])
        scored=[]
        for s,q in zip(batch, quotes):
            if not q: continue
            last=q["c"]; day=q["dp"]; pc=q["pc"]
            if last>=QW_MIN_PRICE and last<=QW_MAX_PRICE and day>=QW_MIN_DAY_PCT:
                if abs(gap_pct(last, pc)) <= MAX_GAP_PCT_ABS:
                    scored.append((s, day, last))
        scored.sort(key=lambda x: x[1], reverse=True)
        shortlist = scored[:QW_SHORTLIST_TOP_N]
        for s,day,last in shortlist:
            dv5 = await fh_dollar_vol_5m(ac, limiter, s)
            if dv5 < QW_MIN_DOLLAR_VOL_5M: continue
            c = await fh_candle_1m(ac, limiter, s, 10)
            if not c or not c.get("c"): continue
            clist = c["c"]
            if len(clist) < 3: continue
            slope_ok = clist[-1] >= clist[-3] or clist[-1] > clist[-2]
            if not slope_ok: continue
            if await fh_earnings_soon(ac, limiter, s): continue
            picks_quick.append(build_pick(s,last,day, mode="QUICK", stop_pct=QW_STOP_PCT, trail_pct=QW_TRAIL_PCT, limit_bps=QW_LIMIT_SLIPPAGE_BPS, fallback_dollars=DOLLARS_PER_QUICK, dv5=dv5, r_mult=QW_TARGET_R_MULT))
            if len(picks_quick) >= QW_TAKE: break

    payload = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "capital": ACCOUNT_CAPITAL_USD,
        "risk_per_trade": RISK_PER_TRADE_DOLLARS,
        "quick": picks_quick,
        "core": picks_core,
        "notes": "Premarket Quick Wins + Core. Suggestions only; you place orders. Stops/targets provided."
    }
    write_json(STATE_PICKS, payload)

async def main_loop():
    print("=== Grandmaster Advisor‑Coach v20 — Premarket Quick Wins + Core ===")
    while True:
        try:
            await scan_once()
        except Exception as e:
            print("[WARN] scan error:", e)
        time.sleep(BASE_SCAN_DELAY)

if __name__=="__main__":
    try:
        import asyncio; asyncio.run(main_loop())
    except KeyboardInterrupt:
        print("Exiting...")

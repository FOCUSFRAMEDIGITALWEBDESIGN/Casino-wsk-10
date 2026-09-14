"""Connected discovery and point-in-time ORB signals. No order submission here."""
import asyncio
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from statistics import median

from .config import number, symbol_name
from .strategy import NY, Signal, instant

D = Decimal


def clean_bars(raw, now, calendar):
    """One completed 5-minute slot per timestamp, valid OHLCV, RTH only."""
    rows = {}
    for bar in raw:
        stamp = instant(bar["t"])
        session = calendar.get(stamp.astimezone(NY).date().isoformat())
        if not session:
            continue
        op, cl = session
        if stamp < op or stamp + timedelta(minutes=5) > min(now, cl):
            continue
        if (stamp-op).total_seconds() % 300:
            continue
        o, h, l, c, v = [number(bar[k]) for k in ("o", "h", "l", "c", "v")]
        if min(o, h, l, c) <= 0 or v <= 0 or not l <= min(o, c) <= max(o, c) <= h:
            continue
        if "vw" not in bar or not l <= number(bar["vw"]) <= h:
            continue
        rows[stamp] = bar
    return sorted(rows.items())


def opening_stats(raw, now, calendar, minutes=15):
    rows = clean_bars(raw, now, calendar)
    sessions = defaultdict(dict)
    for t, b in rows:
        sessions[t.astimezone(NY).date().isoformat()][t] = b
    day = now.astimezone(NY).date().isoformat()
    if day not in calendar:
        return None
    volumes = []
    current = None
    for date in sorted(sessions):
        op, _ = calendar[date]
        stamps = [op + timedelta(minutes=i) for i in range(0, minutes, 5)]
        if not all(t in sessions[date] for t in stamps):
            continue
        window = [sessions[date][t] for t in stamps]
        volume = sum((number(b["v"]) for b in window), D(0))
        if date < day:
            volumes.append(volume)
        elif date == day:
            current = window, volume
    if current is None or len(volumes) < 14:
        return None
    window, volume = current
    base = median(volumes[-20:])
    if base <= 0:
        return None
    return {"rvol": volume/base, "high": max(number(b["h"]) for b in window),
            "low": min(number(b["l"]) for b in window), "rows": rows,
            "baseline_sessions": min(20, len(volumes))}


def orb_signal(symbol, raw, now, calendar, cfg):
    stat = opening_stats(raw, now, calendar, cfg.opening_minutes)
    if not stat:
        return None, "Eröffnungsfenster oder mindestens 14 Vergleichssitzungen fehlen."
    if stat["rvol"] < cfg.min_rvol:
        return None, f"RVOL {stat['rvol']:.2f} unter {cfg.min_rvol}."
    day = now.astimezone(NY).date().isoformat()
    op, cl = calendar[day]
    if not op + timedelta(minutes=cfg.opening_minutes+5) <= now <= op + timedelta(minutes=90):
        return None, "ORB-Einstiege nur nach bestätigtem Ausbruch in den ersten 90 Minuten."
    if now >= cl-timedelta(minutes=cfg.entry_cutoff_minutes):
        return None, "Börsenschluss zu nah."
    rows = stat["rows"]
    today = [(t,b) for t,b in rows if t >= op]
    if len(today) < cfg.opening_minutes//5+1 or len(rows) < 15:
        return None, "Indikatorhistorie fehlt."
    t, last = today[-1]
    # Require continuous current session; never bridge missing slots silently.
    if len(today) != int((t-op).total_seconds()/300)+1:
        return None, "Lücke in aktuellen IEX-Kerzen."
    if not 0 <= (now-(t+timedelta(minutes=5))).total_seconds() <= 90:
        return None, "Abgeschlossene Kerze veraltet."
    c, previous = number(last["c"]), number(today[-2][1]["c"])
    vol = sum((number(b["v"]) for _, b in today), D(0))
    vwap = sum((number(b["vw"])*number(b["v"]) for _,b in today), D(0))/vol
    side = None
    if previous <= stat["high"] < c and c > vwap:
        side = "buy"
    elif previous >= stat["low"] > c and c < vwap:
        side = "sell"
    if side is None:
        return None, f"Kein neuer ORB-Ausbruch mit VWAP · RVOL {stat['rvol']:.2f}."
    tr = []
    for (_,a), (_,b) in zip(rows[-15:-1], rows[-14:]):
        h, l, pc = number(b["h"]), number(b["l"]), number(a["c"])
        tr.append(max(h-l, abs(h-pc), abs(l-pc)))
    atr = sum(tr, D(0))/14
    distance = max(atr*D("1.5"), (stat["high"]-stat["low"])/2)
    if not D("0.15") <= distance/c*100 <= D("2"):
        return None, "Volatilitätsstop außerhalb 0,15–2 %."
    reason = (f"ORB {cfg.opening_minutes} Min · RVOL {stat['rvol']:.2f} "
              f"({stat['baseline_sessions']} Sitzungen) · VWAP {vwap:.2f} · ATR5m {atr:.3f} · IEX")
    return Signal(symbol, side, t.isoformat(), c, reason, distance, atr, stat["rvol"]), reason


class Scanner:
    def __init__(self, cfg, store, broker):
        self.cfg, self.store, self.broker = cfg, store, broker
        self.calendar = {}
        self.day = ""
        self.symbols = []
        self.history = {}
        self.summary = "Automatische Aktiensuche wartet auf Daten."
        self.ranking = []
        self.metrics = {}

    async def prepare(self, now, manual=()):
        day = now.astimezone(NY).date().isoformat()
        self.calendar = await self.broker.calendar(now)
        past = [d for d in sorted(self.calendar) if d < day]
        if not past:
            raise ValueError("Vergangene Handelssitzungen fehlen.")
        prior = past[-1]
        cache_key = f"scanner-v2.1:{self.cfg.feed}:{prior}:{self.cfg.min_dollar_volume}:{self.cfg.shortlist_size}"
        cache = self.store.get("scanner_daily")
        assets = await self.broker.assets()
        valid = []
        for a in assets:
            if (a.get("tradable") and a.get("status") == "active" and a.get("class") == "us_equity"
                    and a.get("exchange") in {"NYSE", "NASDAQ", "AMEX", "ARCA", "NYSEARCA", "BATS"}):
                try:
                    valid.append(symbol_name(a["symbol"]))
                except ValueError:
                    continue
        valid = sorted(set(valid))
        if not valid:
            raise ValueError("Keine börsennotierten handelbaren US-Aktien/ETFs geliefert.")
        if cache and cache.get("key") == cache_key:
            metrics = cache["metrics"]
        else:
            metrics = {}
            # All eligible asset symbols are evaluated; no alphabetical cutoff.
            start = self.calendar[past[max(0,len(past)-25)]][0]-timedelta(hours=12)
            end = self.calendar[prior][1]
            for i in range(0, len(valid), 100):
                self.summary = f"Liquiditätsprüfung: {i}/{len(valid)} Symbole · {self.cfg.feed.upper()}"
                batch = await self.broker.history(valid[i:i+100], start, end, "1Day")
                for symbol, bars in batch.items():
                    try:
                        rows = {}
                        for b in bars:
                            date = instant(b["t"]).astimezone(NY).date().isoformat()
                            if date in self.calendar and date <= prior:
                                if min(number(b[k]) for k in ("o","h","l","c")) > 0 and number(b["v"]) >= 0:
                                    rows[date] = b
                        if prior not in rows or len(rows) < 15:
                            continue
                        ordered = [rows[d] for d in sorted(rows)][-15:]
                        price = number(ordered[-1]["c"])
                        liquidity = sum((number(b["c"])*number(b["v"]) for b in ordered[-14:]),D(0))/14
                        atr = sum((max(number(b["h"])-number(b["l"]),
                                       abs(number(b["h"])-number(a["c"])),
                                       abs(number(b["l"])-number(a["c"]))) for a,b in zip(ordered,ordered[1:])),D(0))/14
                        if price >= 5 and liquidity >= self.cfg.min_dollar_volume and atr >= D("0.50"):
                            dates = sorted(rows)[-15:]
                            returns = {date: str(number(rows[date]["c"])/number(rows[prev]["c"])-1)
                                       for prev,date in zip(dates,dates[1:])}
                            metrics[symbol] = {"liquidity": str(liquidity), "atr": str(atr), "returns": returns}
                    except (KeyError, ValueError, ArithmeticError):
                        continue
                await asyncio.sleep(0)
            self.store.set("scanner_daily", {"key": cache_key, "metrics": metrics, "universe": len(valid)})
        valid_set = set(valid)
        ranked = sorted((s for s in metrics if s in valid_set), key=lambda s: (-number(metrics[s]["liquidity"]), s))
        # Manual additions still must pass the same asset/liquidity checks.
        selected = list(dict.fromkeys([s for s in manual if s in ranked]+ranked))[:self.cfg.shortlist_size]
        historical = {}
        for i in range(0,len(selected),20):
            self.summary = f"Lade RVOL-/Indikatorhistorie: {i}/{len(selected)} vorausgewählte Symbole."
            part = await self.broker.history(selected[i:i+20], now-timedelta(days=45), now)
            historical.update(part)
        self.symbols, self.history, self.metrics, self.day = selected, historical, metrics, day
        self.summary = f"{len(valid)} Börsensymbole geprüft; {len(ranked)} liquide; {len(selected)} in ORB-Beobachtung."

    async def run(self, now, manual=()):
        day = now.astimezone(NY).date().isoformat()
        if self.day != day:
            await self.prepare(now, manual)
        session = self.calendar.get(day)
        if not session or not session[0] <= now < session[1]:
            self.ranking = []
            return [], {}
        op, cl = session
        # Current session is reloaded for corrections; historical sessions stay cached.
        fresh = {}
        for i in range(0,len(self.symbols),20):
            fresh.update(await self.broker.history(self.symbols[i:i+20], op, now))
        signals, notes, ranking = [], {}, []
        for symbol in self.symbols:
            merged = {instant(b["t"]): b for b in self.history.get(symbol,[]) if instant(b["t"]) < op}
            merged.update({instant(b["t"]):b for b in fresh.get(symbol,[])})
            bars = list(merged.values())
            try:
                stat = opening_stats(bars, now, self.calendar, self.cfg.opening_minutes)
                if stat:
                    ranking.append({"symbol": symbol, "rvol": str(stat["rvol"])})
                signal, note = orb_signal(symbol, bars, now, self.calendar, self.cfg)
                notes[symbol] = note
                if signal:
                    signals.append(signal)
            except (KeyError, ValueError, ArithmeticError):
                notes[symbol] = "Ungültige/unvollständige Kursdaten; Symbol ausgelassen."
            await asyncio.sleep(0)
        self.ranking = sorted(ranking, key=lambda r: (-number(r["rvol"]), r["symbol"]))[:20]
        signals.sort(key=lambda s: (-s.rvol,s.symbol))
        self.summary = f"{len(self.symbols)} automatisch vorausgewählt · {len(ranking)} mit RVOL-Historie · {len(signals)} neue Ausbrüche."
        return signals, notes

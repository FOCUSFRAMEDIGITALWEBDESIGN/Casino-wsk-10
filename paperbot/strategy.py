from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
import hashlib
from statistics import mean
from zoneinfo import ZoneInfo
from .config import Settings, number

NY = ZoneInfo("America/New_York")


def instant(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Zeitstempel benötigt eine Zeitzone")
    return result.astimezone(timezone.utc)


def ema(values, period):
    result = [values[0]]
    alpha = 2 / (period + 1)
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


@dataclass(frozen=True)
class Signal:
    symbol: str
    side: str
    timestamp: str
    price: Decimal
    reason: str
    stop_distance: Decimal | None = None
    atr: Decimal | None = None
    rvol: Decimal = Decimal(0)

    @property
    def client_id(self):
        version = "orb-v2" if self.stop_distance is not None else "ema-v1"
        seed = f"{version}|{self.symbol}|{self.side}|{self.timestamp}".encode()
        return "pd-e-" + hashlib.sha256(seed).hexdigest()[:32]


def analyze(symbol, raw_bars, now, calendar):
    """Only completed regular-session bars; calendar includes early closes."""
    bars = {}
    for bar in raw_bars:
        stamp = instant(bar["t"])
        session = calendar.get(stamp.astimezone(NY).date().isoformat())
        if not session:
            continue
        opening, closing = session
        if opening <= stamp and stamp + timedelta(minutes=5) <= min(now, closing):
            if number(bar["c"]) > 0 and number(bar["v"]) >= 0:
                bars[stamp] = bar
    values = [bars[t] for t in sorted(bars)][-160:]
    if len(values) < 60:
        return None, "Zu wenig abgeschlossene 5-Minuten-Kerzen (mindestens 60)."
    last = values[-1]
    stamp = instant(last["t"])
    if stamp.astimezone(NY).date() != now.astimezone(NY).date() or (now - stamp).total_seconds() > 660:
        return None, "IEX-Kerzen fehlen oder sind veraltet."
    closes = [float(number(b["c"])) for b in values]
    fast, slow = ema(closes, 9), ema(closes, 21)
    changes = [b - a for a, b in zip(closes[-15:-1], closes[-14:])]
    gain = mean(max(x, 0) for x in changes)
    loss = mean(max(-x, 0) for x in changes)
    rsi = 50 if gain == loss == 0 else 100 if loss == 0 else 100 - 100 / (1 + gain / loss)
    baseline_volume = mean(float(number(b["v"])) for b in values[-21:-1])
    if baseline_volume <= 0 or float(number(last["v"])) < baseline_volume * 1.1:
        return None, f"Warte auf Volumenbestätigung · RSI {rsi:.0f}."
    side = None
    if fast[-2] <= slow[-2] and fast[-1] > slow[-1] and 45 <= rsi <= 75:
        side = "buy"
    elif fast[-2] >= slow[-2] and fast[-1] < slow[-1] and 25 <= rsi <= 55:
        side = "sell"
    if not side:
        return None, f"Kein EMA-9/21-Kreuz mit passendem RSI ({rsi:.0f})."
    reason = f"EMA 9/21 {'aufwärts' if side == 'buy' else 'abwärts'}, RSI {rsi:.0f}, Volumen ≥ 1,1 × 20-Kerzen-Mittel."
    return Signal(symbol, side, stamp.isoformat(), number(last["c"]), reason), reason


def make_entry(signal: Signal, quote, now, account, positions, open_entries, settings: Settings):
    age = (now - instant(quote["t"])).total_seconds()
    if not 0 <= age <= settings.quote_max_age:
        raise ValueError("Quote fehlt oder ist veraltet.")
    bid, ask = number(quote["bp"]), number(quote["ap"])
    if bid <= 0 or ask < bid or number(quote.get("bs", 0)) <= 0 or number(quote.get("as", 0)) <= 0:
        raise ValueError("Kein gültiger handelbarer Geld-/Briefkurs.")
    mid = (bid + ask) / 2
    if signal.atr is not None and abs(mid-signal.price) > signal.atr/2:
        raise ValueError("Kurs ist mehr als 0,5 ATR vom Signal entfernt.")
    if (ask - bid) / mid * 100 > settings.max_spread_pct:
        raise ValueError("Spread ist zu groß.")
    if abs(mid / signal.price - 1) * 100 > settings.max_drift_pct:
        raise ValueError("Kurs hat sich seit dem Signal zu stark bewegt.")
    if mid < 5:
        raise ValueError("Diese Strategie handelt nur Kurse ab 5 USD.")
    if signal.side == "sell" and not settings.allow_shorts:
        raise ValueError("Short-Trading ist deaktiviert.")
    sign = Decimal(1 if signal.side == "buy" else -1)
    rounding = ROUND_CEILING if signal.side == "buy" else ROUND_FLOOR
    reference = ask if signal.side == "buy" else bid
    entry = (reference * (1 + sign * settings.entry_slippage_pct / 100)).quantize(Decimal("0.01"), rounding=rounding)
    planned_distance = signal.stop_distance or entry * settings.stop_pct / 100
    stop = (entry - sign * planned_distance).quantize(Decimal("0.01"))
    distance = abs(entry - stop)
    if signal.stop_distance is not None and ((ask-bid)/distance > Decimal("0.1") or
            not Decimal("0.15") <= distance/entry*100 <= Decimal("2")):
        raise ValueError("Spread/Stopabstand nach Preisrundung unzulässig.")
    target = (entry + sign * distance * settings.reward_r).quantize(Decimal("0.01"))
    if min(entry, stop, target, distance) <= 0:
        raise ValueError("Ungültige Orderpreise.")
    if (signal.side == "buy" and stop >= bid - Decimal("0.01")) or (signal.side == "sell" and stop <= ask + Decimal("0.01")):
        raise ValueError("Stop liegt zu nah am aktuellen Kurs; Stop-/Slippage-Einstellungen prüfen.")
    equity, buying_power = number(account["equity"]), number(account["buying_power"])
    if min(equity, buying_power) <= 0:
        raise ValueError("Kein verfügbares Paperkapital.")
    occupied = {p["symbol"] for p in positions} | {o["symbol"] for o in open_entries}
    if signal.symbol in occupied or len(occupied) >= settings.max_positions:
        raise ValueError("Position oder Einstieg offen / Positionslimit erreicht.")
    gross = sum((abs(number(p["market_value"])) for p in positions), Decimal(0))
    # Conservative: count the full pending quantity, including partial fills.
    gross += sum((number(o["qty"]) * number(o["limit_price"]) for o in open_entries), Decimal(0))
    capital = min(equity * settings.position_pct / 100,
                  equity * settings.gross_pct / 100 - gross, buying_power * Decimal("0.95"))
    risk_per_share = distance + entry*settings.cost_buffer_pct/100
    qty = int(min(capital / entry, equity * settings.risk_pct / 100 / risk_per_share).to_integral_value(rounding=ROUND_FLOOR))
    if qty < 1:
        raise ValueError("Kein Platz im Kapital-/Risikolimit für eine ganze Aktie.")
    return {"symbol": signal.symbol, "side": signal.side, "qty": str(qty), "type": "limit",
            "limit_price": str(entry), "time_in_force": "gtc", "order_class": "bracket",
            "extended_hours": False, "client_order_id": signal.client_id,
            "stop_loss": {"stop_price": str(stop)}, "take_profit": {"limit_price": str(target)}}

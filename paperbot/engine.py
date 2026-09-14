import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json
import time
import uuid
from dataclasses import replace
from datetime import timedelta
from .scanner import Scanner
from .broker import BrokerError
from .config import number
from .strategy import NY, instant, make_entry

TERMINAL = {"filled", "canceled", "expired", "rejected", "replaced"}


def all_orders(order):
    yield order
    for child in order.get("legs") or []:
        yield from all_orders(child)


class Engine:
    def __init__(self, settings, store, broker):
        self.cfg, self.store, self.broker = settings, store, broker
        self.lock = asyncio.Lock()
        self.account = self.clock = None
        self.positions, self.open_orders = [], []
        self.notes = {}
        self.block_reason = "Warte auf ersten Broker-Abgleich."
        self.last_ok = 0.0
        self.last_scan = 0.0
        self.calendar, self.calendar_day = {}, ""
        self.initialized = False
        self.scanner = Scanner(settings, store, broker)
        self.candidates = []
        self.scan_ok = False
        self.scan_error = ""
        self.scanning = False
        if self.store.get("watchlist") is None:
            self.store.set("watchlist", list(settings.watchlist))

    @property
    def enabled(self):
        return self.store.get("enabled", False)

    @property
    def watchlist(self):
        return self.store.get("watchlist", [])

    @property
    def today(self):
        return instant(self.clock["timestamp"]).astimezone(NY).date().isoformat() if self.clock else datetime.now(NY).date().isoformat()

    def managed_symbols(self):
        return {r["symbol"] for r in self.store.intents(active=True) if r["kind"] == "entry"}

    def known_order_ids(self):
        ids = set()
        for row in self.store.intents():
            if row["snapshot"]:
                ids.update(o["id"] for o in all_orders(json.loads(row["snapshot"])))
        return ids

    def pnl(self):
        if not self.account:
            return Decimal(0), Decimal(0)
        baseline = number(self.store.session(self.today, self.account["last_equity"])["baseline"])
        change = number(self.account["equity"]) - baseline
        return change, change / baseline * 100 if baseline > 0 else Decimal(0)

    def summary(self):
        if not self.account or not self.clock:
            return "Warte auf Brokerdaten."
        pl, pct = self.pnl()
        stale = time.time() - self.last_ok > max(90, self.cfg.poll_seconds * 4)
        flags = "DATEN VERALTET" if stale else "Börse offen" if self.clock["is_open"] else "Börse geschlossen"
        next_time = instant(self.clock["next_close"] if self.clock["is_open"] else self.clock["next_open"])
        lines = [f"**PAPER · USD · {flags}**", f"Automatik: {'aktiv' if self.enabled else 'pausiert'}",
                 f"Kontowert: **{number(self.account['equity']):,.2f} USD**",
                 f"Tag seit vorherigem Börsenschluss: **{pl:+,.2f} USD ({pct:+.2f} %)**",
                 f"Positionen: {len(self.positions)} · Einstiegsversuche heute: {self.store.entries(self.today)}/{self.cfg.max_trades}",
                 f"{'Börsenschluss' if self.clock['is_open'] else 'Nächste Öffnung'}: <t:{int(next_time.timestamp())}:f>",
                 f"Neue Einstiege: {self.block_reason or 'freigegeben, warte auf Signal'}"]
        lines.append("Aktiensuche: " + self.scanner.summary)
        if self.scan_error:
            lines.append("Scannerfehler: " + self.scan_error)
        if self.notes:
            lines.append("\n**Letzter Scan**\n" + "\n".join(f"{s}: {n}" for s, n in list(self.notes.items())[:10]))
        return "\n".join(lines)

    async def observe(self, row, order):
        for item in all_orders(order):
            qty = number(item.get("filled_qty") or 0)
            status = item["status"]
            if qty > 0 or status in ("canceled", "rejected", "expired"):
                price = item.get("filled_avg_price") or "–"
                label = {"filled": "Ausgeführt", "partially_filled": "Teilausführung", "canceled": "Storniert",
                         "rejected": "Abgelehnt", "expired": "Abgelaufen"}.get(status, status)
                purpose = "Einstieg" if item["id"] == order["id"] and row["kind"] == "entry" else "Ausstieg"
                self.store.event(f"order:{item['id']}:{status}:{qty}", f"PAPER · {label} · {item['symbol']}",
                                 f"{purpose} · {item['side'].upper()} · Typ: {item['type']}\n"
                                 f"Bestätigt ausgeführt: {qty} Aktien · Ø {price} USD\nBrokerstatus: {status}\nOrder-ID: `{item['id']}`",
                                 0x2ECC71 if status == "filled" else 0xF39C12)
        status = order["status"]
        in_position = order["symbol"] in {p["symbol"] for p in self.positions}
        children = order.get("legs") or []
        finished = status in TERMINAL and (row["kind"] == "exit" or (
            not in_position and (number(order.get("filled_qty") or 0) == 0 or
                                 (children and all(o["status"] in TERMINAL for o in children)))))
        self.store.order(row["cid"], order, bool(finished))
        if row["kind"] == "entry" and number(order.get("filled_qty") or 0) > 0 and status != "filled":
            self.store.request_close(row["symbol"], "Teilgefüllter Einstieg wird kontrolliert geschlossen.")

    async def reconcile(self):
        for row in self.store.intents(active=True):
            try:
                if row["order_id"]:
                    order = await self.broker.order(row["order_id"])
                else:
                    known = await self.broker.by_client_id(row["cid"])
                    order = await self.broker.order(known["id"])
                await self.observe(row, order)
            except BrokerError as exc:
                if exc.status != 404:
                    raise
                self.store.event("unknown:" + row["cid"], "Orderstatus unklar – neue Einstiege gesperrt",
                                 f"Client-ID: `{row['cid']}`\nAutomatischer Abgleich läuft. Es wird keine zweite Order gesendet.\n"
                                 "Falls dauerhaft nicht auffindbar: /auftrag_pruefen. Beim Broker gegenprüfen.", 0xE74C3C)
                self.store.mark(row["cid"], "unknown")

    async def submit(self, payload, kind):
        cid, symbol = payload["client_order_id"], payload["symbol"]
        if not self.store.reserve(cid, symbol, kind, self.today, payload):
            return
        try:
            order = await self.broker.submit(payload)
            await self.observe(self.store.intent(cid), order)
            self.store.event("submitted:" + cid, f"PAPER · Order übermittelt · {symbol}",
                             f"{payload['side'].upper()} · {payload['qty']} Aktien · {payload['type']}\n"
                             + (f"Limit {payload['limit_price']} · Stop {payload['stop_loss']['stop_price']} · "
                                f"Ziel {payload['take_profit']['limit_price']} USD\n" if kind == "entry" else "")
                             + "Ausführung wird separat anhand der Brokerdaten gemeldet.\nClient-ID: `" + cid + "`")
        except BrokerError as exc:
            if exc.status in (400, 401, 403, 404, 422) and "client_order_id" not in str(exc).lower() and "unique" not in str(exc).lower():
                self.store.mark(cid, "rejected")
            self.store.event("submit-error:" + cid, "PAPER · Order nicht bestätigt", str(exc) + f"\nClient-ID: `{cid}`", 0xE74C3C)
            raise

    async def fresh(self):
        self.account, self.clock, self.positions, self.open_orders = await asyncio.gather(
            self.broker.account(), self.broker.clock(), self.broker.positions(), self.broker.orders())
        stamp = instant(self.clock["timestamp"])
        if abs((datetime.now(timezone.utc) - stamp).total_seconds()) > 90:
            raise ValueError("Broker-Uhr und Server-Uhr weichen zu stark ab.")
        if number(self.account["last_equity"]) <= 0:
            raise ValueError("Ungültiger vorheriger Schlusskontowert; Paperkonto prüfen.")
        number(self.account["equity"])
        account_id = self.store.get("account_id")
        if account_id and account_id != self.account["id"]:
            raise ValueError("Anderes Paperkonto erkannt. Passende Datenbank wiederherstellen oder separates DATA_DIR verwenden.")
        if not account_id:
            if await self.broker.orders("all"):
                raise ValueError("Datenbank ist neu, Paperkonto hat bereits Orders. Eigene Datenbank wiederherstellen oder neues Paperkonto verwenden.")
            self.store.set("account_id", self.account["id"])

    async def process_closes(self):
        if not self.clock["is_open"]:
            return
        for request in self.store.closes():
            symbol = request["symbol"]
            exits = [r for r in self.store.intents(active=True) if r["symbol"] == symbol and r["kind"] == "exit"]
            unknown = [r for r in self.store.intents(active=True) if r["symbol"] == symbol and r["state"] == "unknown"]
            if exits or unknown:
                continue
            orders = [o for o in await self.broker.orders() if o["symbol"] == symbol]
            if orders:
                for order in orders:
                    if order["status"] != "pending_cancel":
                        await self.broker.cancel(order["id"])
                continue
            positions = await self.broker.positions()
            position = next((p for p in positions if p["symbol"] == symbol), None)
            if position is None or number(position["qty"]) == 0:
                self.store.finish_close(symbol)
                self.store.event(f"flat:{symbol}:{request['created']}", f"PAPER · {symbol} geschlossen",
                                 request["reason"] + "\nBroker bestätigt: keine Position und keine offenen Orders.", 0x2ECC71)
                continue
            qty = number(position["qty"])
            payload = {"symbol": symbol, "qty": str(abs(qty)), "side": "sell" if qty > 0 else "buy",
                       "type": "market", "time_in_force": "day", "client_order_id": "pd-x-" + uuid.uuid4().hex}
            attempts = [r for r in self.store.intents() if r["kind"] == "exit" and r["symbol"] == symbol]
            if attempts and time.time() - attempts[-1]["created"] < 60:
                continue
            await self.submit(payload, "exit")

    def queue_managed_closes(self, reason):
        for symbol in self.managed_symbols():
            self.store.request_close(symbol, reason)

    def entry_block(self, notification_ok=True):
        session = self.store.session(self.today, self.account["last_equity"])
        if session["halted"]:
            return "Tagesverlustlimit erreicht; Sperre bis zum nächsten US-Kalendertag."
        if not self.enabled:
            return "Automatik pausiert (/start)."
        if not notification_ok:
            return "Discord-Meldungen derzeit nicht zustellbar."
        if not self.clock["is_open"]:
            return "Außerhalb der regulären Börsensitzung."
        if self.account.get("status") != "ACTIVE" or any(self.account.get(k) for k in ("trading_blocked", "account_blocked", "trade_suspended_by_user")):
            return "Brokerkonto für den Handel gesperrt."
        if self.account.get("currency", "USD") != "USD":
            return "Diese Version setzt ein USD-Paperkonto voraus."
        if any(r["state"] == "unknown" for r in self.store.intents(active=True)):
            return "Mindestens eine Order ist noch nicht eindeutig abgeglichen."
        if self.store.closes():
            return "Schließung/Stornierung noch in Arbeit."
        if self.store.entries(self.today) >= self.cfg.max_trades:
            return "Maximale Einstiegsversuche pro Tag erreicht."
        remaining = (instant(self.clock["next_close"]) - instant(self.clock["timestamp"])).total_seconds() / 60
        if remaining <= self.cfg.entry_cutoff_minutes:
            return "Keine neuen Einstiege kurz vor Börsenschluss."
        known = self.known_order_ids()
        if any(o["id"] not in known for o in self.open_orders) or any(p["symbol"] not in self.managed_symbols() for p in self.positions):
            return "Fremde Position/Order erkannt. Bitte eigenes Paperkonto nur für diesen Bot verwenden."
        return ""

    async def tick(self, notification_ok=True):
        async with self.lock:
            self.block_reason = "Broker-Abgleich läuft."
            await self.fresh()
            await self.reconcile()
            self.check_protection()
            now = instant(self.clock["timestamp"])
            self.store.session(self.today, self.account["last_equity"])
            _, pct = self.pnl()
            if pct <= -self.cfg.daily_loss_pct:
                self.store.halt(self.today)
                self.store.event("daily-halt:" + self.today, "PAPER · Tagesverlustlimit erreicht",
                                 f"Tagesänderung: {pct:.2f} %. Neue Einstiege sind gesperrt. Bot-Positionen werden zur Schließung vorgemerkt.", 0xE74C3C)
            if self.store.session(self.today, self.account["last_equity"])["halted"]:
                self.queue_managed_closes("Tagesverlustlimit erreicht.")
            remaining = (instant(self.clock["next_close"]) - now).total_seconds() / 60
            if self.clock["is_open"] and remaining <= self.cfg.close_before_minutes:
                self.queue_managed_closes("Position vor Börsenschluss schließen.")
            for row in self.store.intents(active=True):
                if row["kind"] == "entry" and row["snapshot"]:
                    order = json.loads(row["snapshot"])
                    if row["day"] != self.today:
                        self.store.request_close(row["symbol"], "Übernommene Position/Order aus vorheriger Sitzung schließen.")
                    if not self.enabled and order["status"] not in TERMINAL:
                        self.store.request_close(row["symbol"], "Pausierte Automatik: offenen Einstieg abbrechen.")
                    if order["status"] not in TERMINAL and time.time() - row["created"] >= self.cfg.entry_timeout:
                        self.store.request_close(row["symbol"], "Einstieg nach Zeitlimit stornieren; eventuelle Teilposition schließen.")
            await self.process_closes()
            self.positions, self.open_orders = await asyncio.gather(self.broker.positions(), self.broker.orders())
            self.last_ok = time.time()
            self.block_reason = self.entry_block(notification_ok)
            pl, _ = self.pnl()
            self.store.equity_point(now, self.account["equity"], self.account["cash"], pl)
            opened = bool(self.clock["is_open"])
            was_open = self.store.get("market_open")
            self.store.set("market_open", opened)
            if was_open != opened:
                self.store.event(f"market:{self.today}:{opened}", "US-Börse geöffnet" if opened else "US-Börse geschlossen", self.summary())
            if opened:
                self.store.set("observed_session", self.today)
            observed = self.store.get("observed_session")
            if not opened and observed and self.store.get("reported_session") != observed:
                self.store.event("day-report:" + observed, "PAPER · Bericht nach Börsenschluss",
                                 (self.summary() if observed == self.today else "Die vorherige Sitzung wurde nicht bis zum Ende beobachtet. Kein vollständiger Tagesbericht verfügbar. Aktuelle Werte mit /konto abrufen.")
                                 + ("\nNoch offene Positionen prüfen; Schließversuche laufen in der nächsten Sitzung weiter." if self.positions else ""))
                self.store.set("reported_session", observed)
            interval = self.cfg.status_minutes if opened else self.cfg.closed_status_minutes
            if time.time() - self.store.get("last_heartbeat", 0) >= interval * 60:
                self.store.event(f"heartbeat:{int(time.time() // 60)}", "PAPER · Regelmäßiger Status", self.summary())
                self.store.set("last_heartbeat", time.time())
            self.initialized = True
            if opened and not self.block_reason and self.scan_ok:
                await self.dispatch(notification_ok)

    async def prepare_scan(self):
        """Data work intentionally runs outside the order/position lock."""
        if self.scanning:
            return
        self.scanning = True
        try:
            clock = await self.broker.clock()
            now = instant(clock["timestamp"])
            if abs((datetime.now(timezone.utc)-now).total_seconds()) > 30:
                raise ValueError("Scanner-Brokerzeit ungültig.")
            candidates, notes = await self.scanner.run(now, self.watchlist)
            # Publishing is atomic on the event loop; old candidates expire independently.
            self.candidates, self.notes = candidates, notes
            self.scan_ok, self.scan_error = True, ""
            self.last_scan = time.time()
            self.store.set("scanner_ranking", {"at": now.isoformat(), "rows": self.scanner.ranking})
        except Exception as exc:
            self.candidates, self.scan_ok = [], False
            self.scan_error = str(exc) if isinstance(exc, (ValueError, BrokerError)) else type(exc).__name__
            raise
        finally:
            self.scanning = False

    def open_risk(self):
        """Conservative stop reserve incl. pending quantity and adverse moves."""
        total = Decimal(0)
        covered = set()
        by_symbol = {p["symbol"]: p for p in self.positions}
        for row in self.store.intents(active=True):
            if row["kind"] != "entry":
                continue
            payload = json.loads(row["payload"])
            symbol = row["symbol"]
            entry = number(payload["limit_price"])
            stop = number(payload["stop_loss"]["stop_price"])
            if row["snapshot"]:
                snapshot = json.loads(row["snapshot"])
                actual = [number(leg["stop_price"]) for leg in snapshot.get("legs") or []
                          if leg.get("stop_price") and leg.get("status") not in TERMINAL]
                if actual:
                    stop = min([stop]+actual) if payload["side"] == "buy" else max([stop]+actual)
            qty = number(payload["qty"])
            position = by_symbol.get(symbol)
            if position:
                qty = max(qty, abs(number(position["qty"])))
                price = number(position.get("current_price") or entry)
                # Worst of planned and current-to-stop risk, without netting.
                distance = max(abs(entry-stop),abs(price-stop))
            else:
                distance = abs(entry-stop)
            total += qty*(distance+entry*self.cfg.cost_buffer_pct/100)
            covered.add(symbol)
        if any(p["symbol"] not in covered for p in self.positions):
            raise ValueError("Offenes Positionsrisiko nicht eindeutig zuordenbar.")
        return total

    def check_protection(self):
        """A filled entry with no active broker stop must be flattened."""
        positions = {p["symbol"] for p in self.positions}
        for row in self.store.intents(active=True):
            if row["kind"] != "entry" or not row["snapshot"] or row["symbol"] not in positions:
                continue
            order = json.loads(row["snapshot"])
            if order.get("status") != "filled":
                continue
            stops = [leg for leg in order.get("legs") or [] if leg.get("type") in ("stop","stop_limit","trailing_stop")
                     and leg.get("status") not in TERMINAL | {"pending_cancel","done_for_day","suspended"}]
            if not stops:
                self.store.request_close(row["symbol"], "Gefüllte Position ohne bestätigten aktiven Stop schließen.")
                self.store.event("unprotected:"+row["cid"],"PAPER · Stop fehlt · "+row["symbol"],
                                 "Broker meldet keine aktive Stoporder. Kontrollierte Schließung angefordert.",0xE74C3C)

    def correlation_block(self, signal):
        """Small-sample concentration guard, not a forecast of diversification."""
        own = self.scanner.metrics.get(signal.symbol,{}).get("returns",{})
        exposures = {p["symbol"]: 1 if number(p["qty"]) > 0 else -1 for p in self.positions}
        for o in self.open_orders:
            if o.get("client_order_id", "").startswith("pd-e-"):
                exposures[o["symbol"]] = 1 if o["side"] == "buy" else -1
        direction = 1 if signal.side == "buy" else -1
        for symbol, side in exposures.items():
            other = self.scanner.metrics.get(symbol,{}).get("returns",{})
            dates = sorted(set(own) & set(other))
            if len(dates) < 10:
                return "Zu wenig gemeinsame Historie für die Konzentrationsprüfung."
            xs, ys = [number(own[d]) for d in dates], [number(other[d]) for d in dates]
            ax, ay = sum(xs)/len(xs), sum(ys)/len(ys)
            vx, vy = sum((x-ax)**2 for x in xs), sum((y-ay)**2 for y in ys)
            if min(vx,vy) <= 0:
                return "Korrelation bei unveränderter Historie nicht bestimmbar."
            corr = sum((x-ax)*(y-ay) for x,y in zip(xs,ys))/(vx*vy).sqrt()
            if corr*direction*side > Decimal("0.85"):
                return f"Ähnliche bestehende Exposition: {symbol} · Korrelation {corr:.2f}."
        return ""

    async def dispatch(self, notification_ok):
        """Called only under lock after position monitoring; at most one attempt/tick."""
        while self.candidates:
            signal = self.candidates.pop(0)
            if self.store.traded_symbol(self.today, signal.symbol):
                continue
            now = datetime.now(timezone.utc)
            age = (now-(instant(signal.timestamp)+timedelta(minutes=5))).total_seconds()
            if not 0 <= age <= 90:
                self.notes[signal.symbol] = "Signal beim Orderentscheid veraltet."
                continue
            try:
                asset = await self.broker.asset(signal.symbol)
                if asset.get("status") != "active" or not asset.get("tradable") or asset.get("class") != "us_equity":
                    raise ValueError("Aktie nicht handelbar.")
                if signal.side == "sell" and not all(asset.get(k) for k in ("shortable","easy_to_borrow","marginable")):
                    raise ValueError("Short nicht verfügbar.")
                quote = (await self.broker.quotes([signal.symbol])).get(signal.symbol)
                if not quote:
                    raise ValueError("Aktuelle IEX-Quote fehlt.")
                # Latest broker state after potentially slow market-data call.
                await self.fresh()
                self.block_reason = self.entry_block(notification_ok)
                if self.block_reason:
                    return
                concentration = self.correlation_block(signal)
                if concentration:
                    raise ValueError(concentration)
                now = datetime.now(timezone.utc)
                if not 0 <= (now-(instant(signal.timestamp)+timedelta(minutes=5))).total_seconds() <= 90:
                    raise ValueError("Signal inzwischen veraltet.")
                pl, pct = self.pnl()
                if pct <= -self.cfg.daily_loss_pct:
                    self.store.halt(self.today)
                    self.queue_managed_closes("Tagesverlustlimit erreicht.")
                    return
                equity = number(self.account["equity"])
                baseline = number(self.store.session(self.today,self.account["last_equity"])["baseline"])
                reserved = self.open_risk()
                remaining = min(equity*self.cfg.portfolio_risk_pct/100-reserved,
                                baseline*self.cfg.daily_loss_pct/100-max(Decimal(0),-pl)-reserved)
                if remaining <= 0:
                    raise ValueError("Gemeinsames Risikobudget ausgeschöpft.")
                cfg = replace(self.cfg, risk_pct=min(self.cfg.risk_pct,remaining/equity*100))
                pending = [o for o in self.open_orders if o.get("client_order_id", "").startswith("pd-e-")]
                payload = make_entry(signal,quote,now,self.account,self.positions,pending,cfg)
            except (ValueError, KeyError) as exc:
                self.notes[signal.symbol] = str(exc)
                continue
            self.store.event("signal:"+signal.client_id,"PAPER · Ausgewählt · "+signal.symbol,signal.reason)
            await self.submit(payload,"entry")
            self.notes[signal.symbol] = "Paperorder gesendet; Brokerfüllung wird separat gemeldet."
            return

    async def set_enabled(self, enabled):
        async with self.lock:
            if enabled and (not self.initialized or time.time() - self.last_ok > 90):
                raise ValueError("Erst aktuellen Broker-Abgleich abwarten. /status zeigt den Stand.")
            if enabled and self.store.session(self.today, self.account["last_equity"])["halted"]:
                raise ValueError("Tagesverlustsperre bleibt für heute aktiv.")
            self.store.set("enabled", enabled)
            if not enabled:
                for row in self.store.intents(active=True):
                    if row["kind"] == "entry" and row["snapshot"] and json.loads(row["snapshot"])["status"] not in TERMINAL:
                        self.store.request_close(row["symbol"], "Offenen Einstieg wegen /pause abbrechen.")
            self.block_reason = self.entry_block()

    async def close_position(self, symbol=None, emergency=False):
        async with self.lock:
            if emergency:
                self.store.set("enabled", False)
                self.queue_managed_closes("Notstopp per Discord.")
            positions, orders = await asyncio.gather(self.broker.positions(), self.broker.orders())
            symbols = {p["symbol"] for p in positions} | {o["symbol"] for o in orders}
            targets = symbols if symbol is None else {symbol} & symbols
            if not targets:
                return "Keine passende Position oder offene Order gefunden."
            for target in targets:
                self.store.request_close(target, "Manuelle Schließung per Discord." if not emergency else "Notstopp per Discord.")
            return "Schließung vorgemerkt: " + ", ".join(sorted(targets)) + ". Ausführung folgt während der regulären Börsenzeit; Brokerbestätigung abwarten."

    async def inspect_unknown(self, cid, discard=False):
        async with self.lock:
            row = self.store.intent(cid)
            if not row or row["state"] != "unknown":
                raise ValueError("Keine ungeklärte Order mit dieser Client-ID.")
            try:
                order = await self.broker.by_client_id(cid)
            except BrokerError as exc:
                if exc.status != 404:
                    raise
                if not discard:
                    return "Broker meldet 404: nicht gefunden. Weiter automatisch prüfen oder nach mindestens 5 Minuten mit verwerfen:true freigeben."
                if time.time() - row["created"] < 300:
                    raise ValueError("Mindestens 5 Minuten für den Abgleich warten.")
                positions, orders = await asyncio.gather(self.broker.positions(), self.broker.orders())
                if any(x["symbol"] == row["symbol"] for x in positions + orders):
                    raise ValueError("Für das Symbol existieren Positionen/Orders. Keine Freigabe möglich.")
                self.store.mark(cid, "discarded")
                self.store.event("discard:" + cid, "Orderversuch manuell freigegeben", f"Client-ID: `{cid}`. Kein erneutes Senden dieses Versuchs.")
                return "Nicht auffindbaren Versuch freigegeben; er zählt weiterhin zum Tageslimit."
            await self.observe(row, await self.broker.order(order["id"]))
            return "Order gefunden und abgeglichen."

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from paperbot.broker import AlpacaPaper, BrokerError
from paperbot.config import Settings, PAPER_URL, DATA_URL, number, symbol_name
from paperbot.engine import Engine
from paperbot.store import Store
from paperbot.strategy import NY, Signal, analyze, make_entry


def settings():
    return Settings(token="test-token", key="test-key", secret="test-secret")


def account():
    return {"id": "paper-account", "equity": "25000", "last_equity": "25000", "cash": "25000",
            "buying_power": "100000", "currency": "USD", "status": "ACTIVE"}


def position(symbol="AAPL", qty="10"):
    return {"symbol": symbol, "qty": qty, "market_value": str(abs(Decimal(qty)) * 100),
            "avg_entry_price": "100", "current_price": "100", "unrealized_pl": "0", "unrealized_plpc": "0", "side": "long"}


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.signal = Signal("AAPL", "buy", (self.now - timedelta(minutes=5)).isoformat(), Decimal("100"), "test")
        self.quote = {"t": self.now.isoformat(), "bp": 99.99, "ap": 100.01, "bs": 100, "as": 100}

    def order(self, **kwargs):
        return make_entry(kwargs.get("signal", self.signal), kwargs.get("quote", self.quote), self.now,
                          kwargs.get("account", account()), kwargs.get("positions", []),
                          kwargs.get("open_entries", []), kwargs.get("cfg", settings()))

    def test_sizing_respects_position_and_equity_risk(self):
        order = self.order()
        notional = Decimal(order["qty"]) * Decimal(order["limit_price"])
        risk = Decimal(order["qty"]) * abs(Decimal(order["limit_price"]) - Decimal(order["stop_loss"]["stop_price"]))
        self.assertLessEqual(notional, 5000)
        self.assertLessEqual(risk, 250)
        self.assertEqual(order["order_class"], "bracket")
        self.assertFalse(order["extended_hours"])
        self.assertEqual(order["time_in_force"], "gtc")

    def test_long_and_short_bracket_price_ordering(self):
        for side in ("buy", "sell"):
            order = self.order(signal=replace(self.signal, side=side))
            prices = [Decimal(order["stop_loss"]["stop_price"]), Decimal(order["limit_price"]), Decimal(order["take_profit"]["limit_price"])]
            self.assertEqual(prices, sorted(prices, reverse=side == "sell"))

    def test_no_short_when_disabled(self):
        with self.assertRaisesRegex(ValueError, "Short"):
            self.order(signal=replace(self.signal, side="sell"), cfg=replace(settings(), allow_shorts=False))

    def test_stale_and_future_quote_rejected(self):
        for seconds in (-121, 10):
            with self.assertRaises(ValueError):
                self.order(quote={**self.quote, "t": (self.now + timedelta(seconds=seconds)).isoformat()})

    def test_invalid_wide_empty_or_drifted_quotes_rejected(self):
        for edit in ({"bp": 0}, {"ap": 99}, {"ap": 105}, {"bp": 110, "ap": 110.02}, {"bs": 0}, {"ap": "NaN"}):
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                self.order(quote={**self.quote, **edit})

    def test_capital_and_pending_orders_limit(self):
        pending = [{"symbol": "MSFT", "qty": "100", "limit_price": "100"}]
        with self.assertRaises(ValueError):
            self.order(positions=[position("NVDA", "50")], open_entries=pending)
        with self.assertRaises(ValueError):
            self.order(positions=[position("AAPL")])
        with self.assertRaises(ValueError):
            self.order(account={**account(), "buying_power": "0"})

    def test_negative_short_value_counts_as_gross_exposure(self):
        with self.assertRaises(ValueError):
            self.order(positions=[{**position("MSFT", "-150"), "market_value": "-15000"}])

    def test_signal_identity_is_stable_and_direction_specific(self):
        self.assertEqual(self.signal.client_id, replace(self.signal).client_id)
        self.assertNotEqual(self.signal.client_id, replace(self.signal, side="sell").client_id)
        self.assertLessEqual(len(self.signal.client_id), 48)


class StrategyTests(unittest.TestCase):
    def fixture(self):
        day = datetime(2026, 9, 10, 9, 30, tzinfo=NY)
        previous = day - timedelta(days=1)
        # Constant history then an EMA crossover with moderate RSI and a volume spike.
        prices = [100] * 70 + [99.5, 99.7, 99.4, 99.6, 99.3, 99.5, 99.2, 99.4, 99.1, 99.3, 99.0, 99.2, 99.5, 99.8, 100.8]
        bars = []
        for i, price in enumerate(prices):
            stamp = previous + timedelta(minutes=5*i) if i < 78 else day + timedelta(minutes=5*(i-78))
            bars.append({"t": stamp.isoformat(), "c": price, "v": 500 if i == len(prices)-1 else 100})
        calendar = {d.date().isoformat(): (d.astimezone(timezone.utc), (d + timedelta(hours=6, minutes=30)).astimezone(timezone.utc)) for d in (previous, day)}
        now = day + timedelta(minutes=35, seconds=10)
        return bars, now, calendar

    def test_real_fixture_emits_long_signal(self):
        bars, now, cal = self.fixture()
        signal, reason = analyze("AAPL", bars, now, cal)
        self.assertIsNotNone(signal, reason)
        self.assertEqual(signal.side, "buy")

    def test_unfinished_candle_cannot_change_signal(self):
        bars, now, cal = self.fixture()
        base = analyze("AAPL", bars, now, cal)
        bars.append({"t": (now - timedelta(seconds=10)).isoformat(), "c": 100000, "v": 999999})
        self.assertEqual(base, analyze("AAPL", bars, now, cal))

    def test_stale_signals_blocked(self):
        bars, now, cal = self.fixture()
        signal, reason = analyze("AAPL", bars, now + timedelta(minutes=20), cal)
        self.assertIsNone(signal)
        self.assertIn("veraltet", reason)

    def test_early_close_aftermarket_bars_ignored(self):
        bars, now, cal = self.fixture()
        current = now.astimezone(NY).date().isoformat()
        opening, _ = cal[current]
        cal[current] = (opening, opening + timedelta(minutes=30))
        before = analyze("AAPL", bars[:-1], now, cal)
        self.assertEqual(before, analyze("AAPL", bars, now, cal))


class PersistenceTests(unittest.TestCase):
    def test_restart_preserves_limits_pause_and_daily_halt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.sqlite"
            db = Store(path)
            db.set("enabled", True)
            db.session("2026-09-11", "25000")
            db.halt("2026-09-11")
            self.assertTrue(db.reserve("id1", "AAPL", "entry", "2026-09-11", {}))
            self.assertFalse(db.reserve("id1", "AAPL", "entry", "2026-09-11", {}))
            db.close()
            db = Store(path)
            self.assertTrue(db.get("enabled"))
            session = db.session("2026-09-11", "99999")
            self.assertEqual(session["baseline"], "25000")
            self.assertEqual(session["halted"], 1)
            self.assertEqual(db.entries("2026-09-11"), 1)
            self.assertTrue(db.traded_symbol("2026-09-11", "AAPL"))
            self.assertFalse(db.session("2026-09-12", "24000")["halted"])
            self.assertEqual(db.entries("2026-09-12"), 0)
            db.close()

    def test_outbox_deduplication_and_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Store(Path(tmp) / "x.db")
            db.event("one", "Test", "Body")
            db.event("one", "Test", "Body")
            rows = db.pending_events()
            self.assertEqual(len(rows), 1)
            db.acknowledge(rows[0]["id"])
            self.assertEqual(db.pending_events(), [])
            db.close()

    def test_settings_reject_live_endpoint_and_bad_numbers(self):
        with patch.dict("os.environ", {"APCA_API_BASE_URL": "https://api.alpaca.markets"}):
            with self.assertRaisesRegex(ValueError, "ausschließlich"):
                Settings.from_env()
        for value in ("NaN", "Infinity", "bad"):
            with self.assertRaises(ValueError):
                number(value)
        for value in ("AAPL/USD", "<@123>", "../orders", "A;DROP"):
            with self.assertRaises(ValueError):
                symbol_name(value)


class FakeBroker:
    def __init__(self):
        self.acct = account()
        self.pos = []
        self.order_map = {}
        self.submitted, self.cancels = [], []
        self.unknown_submit = False
        self.error_submit = None
        self.cancel_immediate = False
        self.closed = False
        self.remaining = 180

    async def account(self):
        return deepcopy(self.acct)

    async def clock(self):
        now = datetime.now(timezone.utc)
        return {"timestamp": now.isoformat(), "is_open": not self.closed,
                "next_open": (now + timedelta(days=1)).isoformat(), "next_close": (now + timedelta(minutes=self.remaining)).isoformat()}

    async def positions(self):
        return deepcopy(self.pos)

    async def orders(self, status="open"):
        terminal = {"filled", "canceled", "rejected", "expired"}
        return deepcopy([o for o in self.order_map.values() if status == "all" or o["status"] not in terminal])

    async def order(self, oid):
        if oid not in self.order_map:
            raise BrokerError(404)
        return deepcopy(self.order_map[oid])

    async def by_client_id(self, cid):
        for o in self.order_map.values():
            if o["client_order_id"] == cid:
                return deepcopy(o)
        raise BrokerError(404)

    async def submit(self, payload):
        self.submitted.append(deepcopy(payload))
        order = {**payload, "id": "o" + str(len(self.submitted)), "status": "new", "filled_qty": "0", "filled_avg_price": None}
        if self.error_submit:
            raise self.error_submit
        self.order_map[order["id"]] = order
        if self.unknown_submit:
            raise BrokerError(0, "simulated timeout after acceptance")
        return deepcopy(order)

    async def cancel(self, oid):
        self.cancels.append(oid)
        if self.cancel_immediate:
            self.order_map[oid]["status"] = "canceled"

    async def calendar(self, now):
        return {}

    async def bars(self, symbols, now):
        return {s: [] for s in symbols}


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Store(Path(self.temp.name) / "x.db")
        self.broker = FakeBroker()
        self.engine = Engine(settings(), self.db, self.broker)
        await self.engine.fresh()
        self.engine.last_scan = time.time()

    async def asyncTearDown(self):
        self.db.close()
        self.temp.cleanup()

    def payload(self):
        return {"symbol": "AAPL", "side": "buy", "qty": "10", "type": "limit", "limit_price": "100",
                "client_order_id": "pd-e-unit", "stop_loss": {"stop_price": "99"}, "take_profit": {"limit_price": "102"}}

    async def add_filled_entry(self):
        await self.engine.submit(self.payload(), "entry")
        order = self.broker.order_map["o1"]
        order.update(status="filled", filled_qty="10", filled_avg_price="100")
        order["legs"] = [{"id": "sl", "client_order_id": "child", "symbol": "AAPL", "side": "sell", "type": "stop", "qty": "10", "status": "new", "filled_qty": "0"}]
        self.broker.pos = [position()]
        self.engine.positions = deepcopy(self.broker.pos)
        await self.engine.reconcile()

    async def test_timeout_after_acceptance_recovers_without_second_order(self):
        self.broker.unknown_submit = True
        with self.assertRaises(BrokerError):
            await self.engine.submit(self.payload(), "entry")
        self.assertEqual(self.db.intent("pd-e-unit")["state"], "unknown")
        await self.engine.reconcile()
        await self.engine.submit(self.payload(), "entry")
        self.assertEqual(len(self.broker.submitted), 1)
        self.assertEqual(self.db.intent("pd-e-unit")["state"], "active")

    async def test_unknown_not_found_blocks_next_entries(self):
        self.db.set("enabled", True)
        self.db.reserve("unknown", "AAPL", "entry", self.engine.today, {})
        await self.engine.reconcile()
        self.assertIn("nicht eindeutig", self.engine.entry_block())

    async def test_known_rejection_not_treated_as_fill(self):
        self.broker.error_submit = BrokerError(422, "insufficient buying power")
        with self.assertRaises(BrokerError):
            await self.engine.submit(self.payload(), "entry")
        self.assertEqual(self.db.intent("pd-e-unit")["state"], "rejected")
        self.assertFalse(any("Ausgeführt" in r["title"] for r in self.db.events()))

    async def test_pause_does_not_remove_filled_bracket(self):
        await self.add_filled_entry()
        await self.engine.set_enabled(False)
        self.assertEqual(self.db.closes(), [])
        self.assertEqual(self.broker.cancels, [])

    async def test_partial_entry_requests_close(self):
        await self.engine.submit(self.payload(), "entry")
        self.broker.order_map["o1"].update(status="partially_filled", filled_qty="3", filled_avg_price="100")
        self.broker.pos = [position(qty="3")]
        self.engine.positions = deepcopy(self.broker.pos)
        await self.engine.reconcile()
        self.assertEqual(self.db.closes()[0]["symbol"], "AAPL")

    async def test_close_waits_for_cancellation_then_reads_actual_quantity(self):
        await self.engine.submit(self.payload(), "entry")
        self.db.request_close("AAPL", "test")
        self.broker.pos = [position(qty="7")]
        await self.engine.process_closes()
        self.assertEqual(self.broker.cancels, ["o1"])
        self.assertEqual(len(self.broker.submitted), 1)
        await self.engine.process_closes()
        self.assertEqual(len(self.broker.submitted), 1, "Unconfirmed cancellation must not produce a sell")
        self.broker.order_map["o1"]["status"] = "canceled"
        self.broker.pos = [position(qty="4")]
        await self.engine.process_closes()
        self.assertEqual(self.broker.submitted[-1]["qty"], "4")
        self.assertEqual(self.broker.submitted[-1]["side"], "sell")
        await self.engine.process_closes()
        self.assertEqual(len(self.broker.submitted), 2, "Pending exit must not be submitted again or canceled")

    async def test_short_exit_buys_to_cover(self):
        self.db.request_close("AAPL", "test")
        self.broker.pos = [position(qty="-7")]
        await self.engine.process_closes()
        self.assertEqual(self.broker.submitted[0]["side"], "buy")
        self.assertEqual(self.broker.submitted[0]["qty"], "7")

    async def test_daily_halt_survives_rebound_and_start_cannot_override(self):
        self.db.set("enabled", True)
        self.broker.acct["equity"] = "24250"
        await self.engine.tick()
        self.assertTrue(self.db.session(self.engine.today, "25000")["halted"])
        self.broker.acct["equity"] = "24900"
        await self.engine.tick()
        with self.assertRaisesRegex(ValueError, "Tagesverlust"):
            await self.engine.set_enabled(True)

    async def test_eod_uses_broker_close_and_runs_while_paused(self):
        await self.add_filled_entry()
        self.db.set("enabled", False)
        self.broker.remaining = 9
        await self.engine.tick()
        self.assertTrue(self.db.closes())
        self.assertTrue(any(x["kind"] == "exit" for x in self.db.intents()))

    async def test_depleted_equity_still_reaches_emergency_loss_guard(self):
        await self.add_filled_entry()
        self.db.set("enabled", True)
        self.broker.acct["equity"] = "-1"
        await self.engine.tick()
        self.assertTrue(self.db.session(self.engine.today, "25000")["halted"])
        self.assertTrue(self.db.closes())

    async def test_no_market_exit_when_market_closed(self):
        self.broker.closed = True
        self.engine.clock = await self.broker.clock()
        self.db.request_close("AAPL", "test")
        self.broker.pos = [position()]
        await self.engine.process_closes()
        self.assertEqual(self.broker.submitted, [])
        self.assertTrue(self.db.closes())

    async def test_foreign_holdings_and_discord_outage_block_entries(self):
        self.db.set("enabled", True)
        self.engine.positions = [position("TSLA")]
        self.assertIn("Fremde", self.engine.entry_block())
        self.engine.positions = []
        self.assertIn("Discord", self.engine.entry_block(False))

    async def test_daily_attempt_limit_blocks_fourth(self):
        self.db.set("enabled", True)
        for i in range(3):
            self.db.reserve(str(i), f"T{i}", "entry", self.engine.today, {})
            self.db.mark(str(i), "rejected")
        self.assertIn("Maximale", self.engine.entry_block())

    async def test_account_switch_and_db_reset_fail_closed(self):
        self.broker.acct["id"] = "other-account"
        with self.assertRaisesRegex(ValueError, "Anderes Paperkonto"):
            await self.engine.fresh()
        self.broker.acct["id"] = "paper-account"
        await self.engine.submit(self.payload(), "entry")
        self.db.set("account_id", None)
        with self.assertRaisesRegex(ValueError, "Datenbank ist neu"):
            await self.engine.fresh()


class RequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_client_uses_only_paper_host_and_never_retries_post(self):
        class BrokenSession:
            def __init__(self):
                self.calls = []
            def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs))
                raise asyncio.TimeoutError()
        session = BrokenSession()
        broker = AlpacaPaper(settings(), session)
        with self.assertRaises(BrokerError):
            await broker.submit({"client_order_id": "unique"})
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][1], PAPER_URL + "/v2/orders")
        self.assertFalse(session.calls[0][2]["allow_redirects"])

    async def test_bar_pagination_merges_all_symbols(self):
        class Pages(AlpacaPaper):
            def __init__(self):
                self.page = 0
            async def request(self, method, path, **kwargs):
                self.page += 1
                self.assert_feed = kwargs["params"]["feed"]
                return {"bars": {"AAPL": [{"c": 1}]} if self.page == 1 else {"MSFT": [{"c": 2}]},
                        "next_page_token": "second" if self.page == 1 else None}
        broker = Pages()
        result = await broker.bars(["AAPL", "MSFT"], datetime.now(timezone.utc))
        self.assertEqual(result["MSFT"], [{"c": 2}])
        self.assertEqual(broker.page, 2)
        self.assertEqual(broker.assert_feed, "iex")


if __name__ == "__main__":
    unittest.main()

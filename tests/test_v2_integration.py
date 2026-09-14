"""Synthetic endpoint fixtures; no secrets, network, Discord login or real orders."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, AsyncMock

from paperbot.config import Settings
from paperbot.engine import Engine
from paperbot.scanner import orb_signal, opening_stats, clean_bars
from paperbot.broker import AlpacaPaper, BrokerError
from paperbot.store import Store
from paperbot.discord_app import GuardedTree
from test_core import FakeBroker, position

UTC = timezone.utc
NOW = datetime(2026, 9, 11, 13, 50, 5, tzinfo=UTC)
CFG = Settings(token="fixture",key="fixture",secret="fixture")


class MarketBroker(FakeBroker):
    def __init__(self):
        super().__init__()
        self.now = NOW
        self.cal = {}
        self.prices = {"AAA": [], "BBB": []}
        self.daily = []
        self.history_calls = []
        self.assets_calls = 0
        for age in range(38,-1,-1):
            date = NOW.date()-timedelta(days=age)
            if date.weekday() >= 5:
                continue
            op = datetime.combine(date,datetime.min.time(),tzinfo=UTC)+timedelta(hours=13,minutes=30)
            cl = op+timedelta(hours=6,minutes=30)
            self.cal[date.isoformat()] = op,cl
            if date < NOW.date():
                self.daily.append({"t":(op-timedelta(hours=9,minutes=30)).isoformat(),
                                   "o":99.8,"h":101,"l":99,"c":100,"v":2000000})
            for symbol in self.prices:
                for i in range(4 if date==NOW.date() else 20):
                    b = {"t":(op+timedelta(minutes=5*i)).isoformat(),
                         "o":99.8,"h":100,"l":99.5,"c":99.9,"v":1000,"vw":99.8}
                    if date == NOW.date():
                        b["v"] = 3000 if symbol=="AAA" else 4000
                        if i==3:
                            b.update(o=99.9,h=100.3,l=99.8,c=100.2,vw=100.1)
                    self.prices[symbol].append(b)

    async def clock(self):
        return {"timestamp":self.now.isoformat(),"is_open":True,
                "next_open":(self.now+timedelta(days=1)).isoformat(),
                "next_close":self.cal[NOW.date().isoformat()][1].isoformat()}

    async def assets(self):
        self.assets_calls += 1
        return [dict(symbol=s,status="active",tradable=True,exchange="NASDAQ",**{"class":"us_equity"})
                for s in ("AAA","BBB","ILLIQ")]

    async def history(self,symbols,start,end,timeframe="5Min"):
        self.history_calls.append((tuple(symbols),timeframe))
        if timeframe=="1Day":
            return {s:deepcopy(self.daily) if s!="ILLIQ" else [dict(b,v=1) for b in self.daily] for s in symbols}
        return {s:[deepcopy(b) for b in self.prices.get(s,[]) if start<=datetime.fromisoformat(b["t"])<=end]
                for s in symbols}

    async def calendar(self,now):
        return self.cal

    async def asset(self,symbol):
        return {"symbol":symbol,"status":"active","tradable":True,"class":"us_equity",
                "shortable":True,"easy_to_borrow":True,"marginable":True}

    async def quotes(self,symbols):
        return {s:{"t":self.now.isoformat(),"bp":100.19,"ap":100.21,"bs":100,"as":100} for s in symbols}


class V2IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name)/"journal.sqlite")
        self.broker = MarketBroker()
        self.engine = Engine(CFG,self.store,self.broker)
        self.clock = patch("paperbot.engine.datetime",wraps=datetime)
        self.dt = self.clock.start()
        self.dt.now.return_value = NOW

    async def asyncTearDown(self):
        self.clock.stop()
        self.store.close()
        self.tmp.cleanup()

    async def ready(self):
        await self.engine.fresh()
        self.store.set("enabled",True)
        await self.engine.prepare_scan()

    async def test_universe_to_order_to_confirmed_fill_and_dedup(self):
        await self.ready()
        self.assertEqual(set(self.engine.scanner.symbols),{"AAA","BBB"})
        self.assertEqual(self.engine.candidates[0].symbol,"BBB")
        self.assertFalse(self.broker.submitted)  # scan alone never trades
        await self.engine.tick(True)
        self.assertEqual(len(self.broker.submitted),1)
        payload = self.broker.submitted[0]
        self.assertEqual(payload["symbol"],"BBB")
        self.assertEqual(payload["order_class"],"bracket")
        self.assertFalse(payload["extended_hours"])
        self.assertLess(Decimal(payload["stop_loss"]["stop_price"]),Decimal(payload["limit_price"]))
        self.broker.pos = [position("BBB",payload["qty"])]
        order = self.broker.order_map["o1"]
        order.update(status="filled",filled_qty=payload["qty"],filled_avg_price="100.21")
        order["legs"] = [{"id":"sl","symbol":"BBB","side":"sell","type":"stop","status":"new","filled_qty":"0"}]
        self.engine.candidates = []
        await self.engine.tick(True)
        self.assertTrue(any("Ausgeführt" in e["title"] for e in self.store.events(50)))
        await self.engine.prepare_scan()
        # Remove the other symbol to prove the filled signal is not submitted twice.
        self.engine.candidates = [s for s in self.engine.candidates if s.symbol=="BBB"]
        await self.engine.tick(True)
        self.assertEqual(len(self.broker.submitted),1)

    async def test_no_notification_or_pause_means_no_order(self):
        await self.ready()
        await self.engine.tick(False)
        self.assertFalse(self.broker.submitted)
        self.store.set("enabled",False)
        await self.engine.tick(True)
        self.assertFalse(self.broker.submitted)

    async def test_future_and_stale_quotes_never_submit(self):
        await self.ready()
        for seconds in (1,-11):
            async def bad(symbols):
                return {s:{"t":(NOW+timedelta(seconds=seconds)).isoformat(),"bp":100.19,"ap":100.21,"bs":1,"as":1} for s in symbols}
            self.broker.quotes = bad
            await self.engine.prepare_scan()
            await self.engine.tick(True)
        self.assertFalse(self.broker.submitted)

    async def test_daily_remaining_risk_limits_new_position(self):
        await self.ready()
        self.broker.acct["equity"] = "24255"  # $745 loss, $5 remaining daily budget
        await self.engine.tick(True)
        self.assertEqual(len(self.broker.submitted),1)
        p = self.broker.submitted[0]
        cost = abs(Decimal(p["limit_price"])-Decimal(p["stop_loss"]["stop_price"])) + Decimal(p["limit_price"])*CFG.cost_buffer_pct/100
        self.assertLessEqual(Decimal(p["qty"])*cost,5)

    async def test_slow_scanner_does_not_hold_monitor_lock(self):
        await self.ready()
        waiting = asyncio.Event()
        release = asyncio.Event()
        async def slow(now, manual):
            waiting.set()
            await release.wait()
            return [], {}
        self.engine.scanner.run = slow
        task = asyncio.create_task(self.engine.prepare_scan())
        await waiting.wait()
        self.store.set("enabled",False)
        try:
            await asyncio.wait_for(self.engine.tick(True),timeout=0.5)
            self.assertGreater(self.engine.last_ok,0)
        finally:
            release.set()
            await task

    async def test_scan_failure_blocks_entries_but_monitor_works(self):
        await self.ready()
        self.engine.scanner.run = AsyncMock(side_effect=BrokerError(403,"data denied"))
        with self.assertRaises(BrokerError):
            await self.engine.prepare_scan()
        await self.engine.tick(True)
        self.assertFalse(self.engine.scan_ok)
        self.assertFalse(self.broker.submitted)
        self.assertGreater(self.engine.last_ok,0)

    async def test_daily_liquidity_cache_survives_new_scanner(self):
        await self.ready()
        self.broker.history_calls.clear()
        another = Engine(CFG,self.store,self.broker)
        await another.prepare_scan()
        self.assertFalse(any(tf=="1Day" for _,tf in self.broker.history_calls))
        self.assertEqual(set(another.scanner.symbols),{"AAA","BBB"})

    async def test_missing_stop_requests_close_and_blocks_new_entry(self):
        await self.ready()
        await self.engine.tick(True)
        p = self.broker.submitted[0]
        self.broker.order_map["o1"].update(status="filled",filled_qty=p["qty"],filled_avg_price="100.2",legs=[])
        self.broker.pos = [position("BBB",p["qty"])]
        await self.engine.tick(True)
        self.assertTrue(any("Stop fehlt" in e["title"] for e in self.store.events(50)))
        self.assertTrue(any(o["side"]=="sell" and o["symbol"]=="BBB" for o in self.broker.submitted))
        self.assertFalse(any(o["symbol"]=="AAA" for o in self.broker.submitted))

    async def test_correlated_exposure_blocks_second_position(self):
        await self.ready()
        series = {str(i):str(Decimal(i)/1000) for i in range(14)}
        self.engine.scanner.metrics["AAA"]["returns"] = series
        self.engine.scanner.metrics["BBB"]["returns"] = series
        signal = next(s for s in self.engine.candidates if s.symbol=="AAA")
        self.engine.positions = [position("BBB","10")]
        self.assertIn("Exposition", self.engine.correlation_block(signal))


class DataCausalityTests(unittest.TestCase):
    def setUp(self):
        self.broker = MarketBroker()
        self.rows = self.broker.prices["BBB"]

    def test_unfinished_future_bars_cannot_change_rvol_or_signal(self):
        before, _ = orb_signal("BBB",self.rows,NOW,self.broker.cal,CFG)
        self.assertIsNotNone(before)
        future = dict(self.rows[-1], t=(NOW+timedelta(minutes=5)).isoformat(),c=999,v=10**9)
        after, _ = orb_signal("BBB",self.rows+[future],NOW,self.broker.cal,CFG)
        self.assertEqual(before,after)

    def test_missing_current_slot_blocks_signal(self):
        op = self.broker.cal[NOW.date().isoformat()][0]
        rows = [b for b in self.rows if b["t"]!=(op+timedelta(minutes=5)).isoformat()]
        signal, _ = orb_signal("BBB",rows,NOW,self.broker.cal,CFG)
        self.assertIsNone(signal)

    def test_current_day_is_not_its_own_volume_baseline(self):
        stat = opening_stats(self.rows,NOW,self.broker.cal)
        self.assertEqual(stat["rvol"],Decimal(4))

    def test_stale_closed_bar_cannot_trigger(self):
        signal, _ = orb_signal("BBB",self.rows,NOW+timedelta(seconds=91),self.broker.cal,CFG)
        self.assertIsNone(signal)


class HistoryPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_paginates_and_rejects_repeated_token(self):
        client = AlpacaPaper(CFG,None)
        client.request = AsyncMock(side_effect=[{"bars":{"A":[{"c":1}]},"next_page_token":"x"},
                                               {"bars":{"B":[{"c":2}]},"next_page_token":None}])
        result = await client.history(["A","B"],NOW-timedelta(days=1),NOW)
        self.assertEqual(result,{"A":[{"c":1}],"B":[{"c":2}]})
        client.request = AsyncMock(return_value={"bars":{},"next_page_token":"x"})
        with self.assertRaises(BrokerError):
            await client.history(["A"],NOW-timedelta(days=1),NOW)


class AccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_stranger_cannot_claim_bot(self):
        from types import SimpleNamespace
        store = SimpleNamespace(get=lambda key:None)
        tree = SimpleNamespace(client=SimpleNamespace(authorized_ids={42},store=store))
        interaction = SimpleNamespace(guild_id=1,user=SimpleNamespace(id=13),
                                      response=SimpleNamespace(send_message=AsyncMock()))
        self.assertFalse(await GuardedTree.interaction_check(tree,interaction))
        interaction.user.id = 42
        self.assertTrue(await GuardedTree.interaction_check(tree,interaction))


if __name__ == "__main__":
    unittest.main()

import contextlib
import io
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
import bot

NOW = 1_789_387_200.0
MINT = 'A' * 44
POOL = 'B' * 44
FX = bot.D('1.10')


def pair(mint=MINT, price='0.001', pool=POOL):
    return {'chainId': 'solana', 'baseToken': {'address': mint, 'symbol': 'MEME'},
            'quoteToken': {'address': bot.SOL}, 'pairAddress': pool, 'priceUsd': price,
            'liquidity': {'usd': 200_000}, 'pairCreatedAt': (NOW - 7200) * 1000,
            'volume': {'h1': 50_000}, 'txns': {'m5': {'buys': 30, 'sells': 20}},
            'priceChange': {'m5': 3}}


def mint_reply():
    return {'result': {'value': {'owner': bot.TOKEN_PROGRAM, 'executable': False,
            'data': {'parsed': {'type': 'mint', 'info': {'isInitialized': True,
            'mintAuthority': None, 'freezeAuthority': None, 'supply': '1000000'}}}}}}


class MoneyTests(unittest.TestCase):
    def test_live_mode_cannot_be_enabled(self):
        with patch.dict('os.environ', {'TRADING_MODE': 'live'}), \
                patch('sys.argv', ['bot.py', 'run']), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exc:
                bot.main()
        self.assertEqual(exc.exception.code, 2)

    def test_second_runner_is_locked_out(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bot.lock'
            with bot.process_lock(path):
                with self.assertRaises(RuntimeError):
                    with bot.process_lock(path):
                        self.fail('Second process lock must fail')
            with bot.process_lock(path):
                pass

    def test_total_buy_cost_exactly_twenty(self):
        for price in ('0.00000001', '0.005', '15'):
            for fx in ('0.9', '1.1', '1.4'):
                qty = bot.buy_quantity(price, fx)
                total = qty * bot.D(price) * (1 + bot.SLIPPAGE) / bot.D(fx) * (1 + bot.FEE) + bot.NETWORK_EUR
                self.assertLess(abs(total - bot.STAKE), bot.D('1e-24'))

    def test_flat_price_roundtrip_loses_modelled_costs(self):
        qty = bot.buy_quantity('0.01', FX)
        proceeds = bot.liquidation_eur(qty, '0.01', FX)
        self.assertTrue(bot.D('19') < proceeds < bot.STAKE)

    def test_fx_direction_and_zero_floor(self):
        qty = bot.buy_quantity('.01', '1.1')
        self.assertLess(bot.liquidation_eur(qty, '.01', '1.2'), bot.liquidation_eur(qty, '.01', '1.1'))
        self.assertEqual(bot.liquidation_eur(qty, '.000000001', FX), 0)

    def test_bad_numbers_rejected(self):
        for value in ('NaN', 'Infinity', '-Infinity', None, 'oops'):
            with self.assertRaises(ValueError):
                bot.number(value)
        for price in ('0', '-1'):
            with self.assertRaises(ValueError):
                bot.buy_quantity(price, FX)


class DataTests(unittest.TestCase):
    def test_discovery_uses_solana_meme_description_and_deduplicates(self):
        class ProfileHttp:
            def json(self, url):
                return [
                    {'chainId': 'solana', 'tokenAddress': MINT, 'description': 'A meme coin'},
                    {'chainId': 'solana', 'tokenAddress': MINT, 'description': 'Memecoin'},
                    {'chainId': 'ethereum', 'tokenAddress': 'C' * 44, 'description': 'meme'},
                    {'chainId': 'solana', 'tokenAddress': 'D' * 44, 'description': 'Lending protocol'},
                    {'chainId': 'solana', 'tokenAddress': '../bad', 'description': 'meme'}]
        self.assertEqual(bot.Market(ProfileHttp()).discover(), [MINT])

    def test_fx_namespaced_xml_and_age(self):
        raw = b'<Envelope xmlns="urn:ecb"><Cube><Cube time="2026-09-11"><Cube currency="USD" rate="1.1"/></Cube></Cube></Envelope>'
        self.assertEqual(bot.parse_fx(raw, date(2026, 9, 14)), (FX, '2026-09-11'))
        for today in (date(2026, 9, 10), date(2026, 9, 17)):
            with self.assertRaises(ValueError):
                bot.parse_fx(raw, today)

    def test_only_revoked_legacy_mint_allowed(self):
        self.assertTrue(bot.mint_ok(mint_reply()))
        for field in ('freezeAuthority', 'mintAuthority'):
            r = mint_reply()
            r['result']['value']['data']['parsed']['info'][field] = MINT
            self.assertFalse(bot.mint_ok(r))
            del r['result']['value']['data']['parsed']['info'][field]
            self.assertFalse(bot.mint_ok(r))
        r = mint_reply()
        r['result']['value']['owner'] = 'Token2022'
        self.assertFalse(bot.mint_ok(r))
        self.assertFalse(bot.mint_ok({'result': {'value': None}}))

    def test_pair_identity_and_missing_fields(self):
        self.assertTrue(bot.valid_pair(pair(), MINT))
        self.assertFalse(bot.valid_pair(pair(), 'C' * 44))
        for field in ('priceUsd', 'liquidity', 'baseToken', 'quoteToken'):
            p = pair()
            del p[field]
            self.assertFalse(bot.valid_pair(p, MINT))
        p = pair()
        p['quoteToken']['address'] = MINT
        self.assertFalse(bot.valid_pair(p, MINT))

    def test_liquidity_and_sell_activity_filters(self):
        self.assertTrue(bot.eligible(pair(), NOW, FX))
        p = pair()
        p['liquidity']['usd'] = 99_999
        self.assertFalse(bot.eligible(p, NOW, FX))
        p = pair()
        p['txns']['m5']['sells'] = 0
        self.assertFalse(bot.eligible(p, NOW, FX))
        p = pair()
        p['pairCreatedAt'] = (NOW + 1) * 1000
        self.assertFalse(bot.eligible(p, NOW, FX))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'bot.sqlite3'
        self.s = bot.Store(self.path)
        self.s.start_day(NOW)

    def tearDown(self):
        self.s.db.close()
        self.temp.cleanup()

    def buy(self, mint=MINT):
        return self.s.buy(pair(mint), FX, NOW)

    def test_restart_preserves_cash_and_prevents_duplicate(self):
        self.assertTrue(self.buy())
        self.s.db.close()
        self.s = bot.Store(self.path)
        self.assertEqual(bot.number(self.s.get('cash')), 180)
        self.assertFalse(self.buy())
        self.assertEqual(len(self.s.positions()), 1)

    def test_three_position_limit(self):
        for char in 'ACD':
            self.assertTrue(self.buy(char * 44))
        self.assertFalse(self.buy('E' * 44))
        self.assertEqual(bot.number(self.s.get('cash')), 140)

    def test_cash_never_negative_or_partial_buy(self):
        self.s.put('cash', '19.99')
        self.assertFalse(self.buy())
        self.assertEqual(self.s.get('cash'), '19.99')

    def test_pause_still_allows_stop_and_no_double_exit(self):
        self.buy()
        p = self.s.positions()[0]
        self.s.put('paused', '1')
        self.assertFalse(self.buy('C' * 44))
        self.s.mark_or_close(p['id'], pair(price='.0005'), FX, NOW + 15)
        cash = self.s.get('cash')
        self.s.mark_or_close(p['id'], pair(price='.0005'), FX, NOW + 16)
        self.assertEqual(self.s.get('cash'), cash)
        self.assertFalse(self.s.positions())
        self.assertLess(bot.number(cash), 197)

    def test_profit_does_not_increase_next_stake(self):
        self.buy()
        self.s.mark_or_close(self.s.positions()[0]['id'], pair(price='.002'), FX, NOW + 1)
        before = bot.number(self.s.get('cash'))
        self.assertFalse(self.buy())
        self.assertTrue(self.buy('C' * 44))
        self.assertEqual(before - bot.number(self.s.get('cash')), bot.STAKE)

    def test_daily_buy_limit_and_cooldown(self):
        for char in 'ACDEF':
            self.assertTrue(self.buy(char * 44))
            self.s.put('exit_all', '1')
            p = self.s.positions()[0]
            self.s.mark_or_close(p['id'], pair(char * 44), FX, NOW + 1)
            self.s.put('exit_all', '0')
        self.assertFalse(self.buy('G' * 44))
        self.assertFalse(self.buy(MINT))
        self.assertEqual(self.s.db.execute('SELECT buys FROM days').fetchone()[0], 5)

    def test_daily_loss_lock_survives_restart_and_resume(self):
        for char in 'AC':
            self.assertTrue(self.buy(char * 44))
        for p in self.s.positions():
            self.s.mark_or_close(p['id'], pair(p['mint'], '.0001'), FX, NOW + 1)
        with self.s.tx():
            self.assertFalse(self.s.risk(NOW + 2))
        self.s.db.close()
        self.s = bot.Store(self.path)
        self.s.put('paused', '0')
        self.assertFalse(self.buy('D' * 44))
        self.s.start_day(NOW + 86400)
        self.assertTrue(self.s.buy(pair('D' * 44), FX, NOW + 86400))

    def test_missing_mark_blocks_buys_without_fictitious_sale(self):
        self.buy()
        p = self.s.positions()[0]
        self.s.unpriced(p['id'], NOW + 1)
        self.assertFalse(self.buy('C' * 44))
        self.assertIsNone(self.s.status(NOW + 2)['equity_eur'])
        self.assertEqual(bot.number(self.s.get('cash')), 180)
        self.assertEqual(len(self.s.positions()), 1)
        self.s.mark_or_close(p['id'], pair(), FX, NOW + 3)
        self.assertTrue(self.s.risk(NOW + 3))

    def test_day_start_with_unpriced_position_halts_entries(self):
        self.buy()
        self.s.unpriced(self.s.positions()[0]['id'], NOW + 1)
        self.s.start_day(NOW + 86400)
        self.assertEqual(self.s.db.execute('SELECT halted FROM days WHERE day=?',
                         (bot.utc_day(NOW + 86400),)).fetchone()[0], 1)

    def test_too_little_exit_liquidity_does_not_credit_cash(self):
        self.buy()
        p = pair(price='.0005')
        p['liquidity']['usd'] = 1
        with self.assertRaises(ValueError):
            self.s.mark_or_close(self.s.positions()[0]['id'], p, FX, NOW + 1)
        self.assertEqual(bot.number(self.s.get('cash')), 180)
        self.assertEqual(len(self.s.positions()), 1)

    def test_transaction_rollback(self):
        with self.assertRaises(RuntimeError):
            with self.s.tx():
                self.s.put('cash', '0')
                raise RuntimeError('simulate failure')
        self.assertEqual(bot.number(self.s.get('cash')), 200)

    def test_concurrent_connections_cannot_duplicate_mint(self):
        other = bot.Store(self.path)
        try:
            self.assertTrue(self.buy())
            self.assertFalse(other.buy(pair(), FX, NOW))
            self.assertEqual(bot.number(other.get('cash')), 180)
        finally:
            other.db.close()


class FakeHttp:
    def request(self, url):
        dated = bot.utc_day(NOW)
        return f'<Envelope><Cube time="{dated}"><Cube currency="USD" rate="1.1"/></Cube></Envelope>'.encode()


class FakeMarket:
    def __init__(self):
        self.price = '.001'
        self.safe = True
        self.fail = False

    def discover(self):
        return [MINT]

    def pair(self, mint, pair_id=None):
        if self.fail:
            raise RuntimeError('offline')
        return pair(mint, self.price)

    def check_mint(self, mint):
        return self.safe


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.s = bot.Store(':memory:')
        self.market = FakeMarket()
        self.now = NOW
        self.e = bot.Engine(self.s, self.market, FakeHttp(), clock=lambda: self.now)
        self.env = patch.dict('os.environ', {'WATCH_MINTS': ''})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.s.db.close()

    def test_single_candidate_warms_up_then_buys_and_exits(self):
        self.e.tick()
        for offset in (15, 30, 45):
            self.now = NOW + offset
            self.e.tick()
            self.assertFalse(self.s.positions())
        self.now = NOW + 60
        self.market.price = '.00102'
        self.e.tick()
        self.assertEqual(len(self.s.positions()), 1)
        self.assertEqual(bot.number(self.s.get('cash')), 180)
        self.now += 15
        self.market.price = '.0015'
        self.e.tick()
        self.assertFalse(self.s.positions())
        self.assertGreater(bot.number(self.s.get('cash')), 200)

    def test_rpc_rejection_prevents_entry(self):
        self.e.tick()
        self.now += 60
        self.market.price = '.00102'
        self.market.safe = False
        self.e.tick()
        self.assertFalse(self.s.positions())

    def test_outage_preserves_open_position_and_recovers(self):
        self.e.tick()
        self.now += 60
        self.market.price = '.00102'
        self.e.tick()
        self.market.fail = True
        self.now += 15
        self.e.tick()
        self.assertTrue(self.s.status(self.now)['valuation_incomplete'])
        self.assertEqual(len(self.s.positions()), 1)
        self.market.fail = False
        self.market.price = '.0015'
        self.now += 15
        self.e.tick()
        self.assertFalse(self.s.positions())

    def test_manual_close_is_executed_while_paused(self):
        self.e.tick()
        self.now += 60
        self.market.price = '.00102'
        self.e.tick()
        self.s.put('paused', '1')
        self.s.put('exit_all', '1')
        self.now += 15
        self.e.tick()
        self.assertFalse(self.s.positions())
        self.assertEqual(self.s.get('exit_all'), '0')
        self.assertEqual(self.s.get('paused'), '1')


if __name__ == '__main__':
    unittest.main()

"""Independent Solana paper trader. No wallet, signing or live-order capability."""
from __future__ import annotations

import argparse
import contextlib
import csv
import json
import logging
import os
from pathlib import Path
import re
import signal
import sqlite3
import threading
import time
import urllib.request
import urllib.error
from urllib.parse import urlparse
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

D = Decimal
STAKE = D('20.00')
INITIAL_CASH = D('200.00')
FEE = D('0.005')
NETWORK_EUR = D('0.10')
SLIPPAGE = D('0.01')
MAX_POSITIONS = 3
MAX_DAILY_BUYS = 5
MAX_DAILY_LOSS = D('20')
TOKEN_PROGRAM = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
SOL = 'So11111111111111111111111111111111111111112'
USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
DEX = 'https://api.dexscreener.com'
ECB = 'https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml'
LOG = logging.getLogger('memecoin-paper')


def number(value):
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('Ungültige Zahl') from None
    if not result.is_finite():
        raise ValueError('Nicht-endliche Zahl')
    return result


def address(value):
    return isinstance(value, str) and re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{32,44}', value) is not None


def utc_day(now):
    return datetime.fromtimestamp(now, timezone.utc).date().isoformat()


def buy_quantity(price_usd, usd_per_eur):
    price, fx = number(price_usd), number(usd_per_eur)
    if price <= 0 or fx <= 0:
        raise ValueError('Preis/Kurs muss positiv sein')
    return (STAKE - NETWORK_EUR) / (1 + FEE) * fx / (price * (1 + SLIPPAGE))


def liquidation_eur(quantity, price_usd, usd_per_eur):
    qty, price, fx = map(number, (quantity, price_usd, usd_per_eur))
    if qty < 0 or price <= 0 or fx <= 0:
        raise ValueError('Ungültige Bewertung')
    return max(D(0), qty * price * (1 - SLIPPAGE) / fx * (1 - FEE) - NETWORK_EUR)


class Http:
    """Bounded requests; exceptions deliberately omit URLs and credentials."""
    def __init__(self):
        self.last = 0.0

    def request(self, url, payload=None):
        if urlparse(url).scheme != 'https':
            raise ValueError('Nur HTTPS erlaubt')
        time.sleep(max(0, .3 - (time.monotonic() - self.last)))
        self.last = time.monotonic()
        body = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(url, data=body, headers={
            'User-Agent': 'MemecoinPaperBot/1.0', 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=6) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError('Antwort zu groß')
            return raw
        except urllib.error.HTTPError as exc:
            error = RuntimeError('Datenabruf fehlgeschlagen')
            error.http_status = exc.code
            raise error from None
        except Exception:
            raise RuntimeError('Datenabruf fehlgeschlagen') from None

    def json(self, url, payload=None):
        return json.loads(self.request(url, payload))


def parse_fx(raw, today):
    root = ET.fromstring(raw)
    for cube in root.iter():
        if 'time' not in cube.attrib:
            continue
        dated = date.fromisoformat(cube.attrib['time'])
        if not 0 <= (today - dated).days <= 5:
            raise ValueError('EZB-Kurs veraltet oder zukünftig')
        for item in cube:
            if item.attrib.get('currency') == 'USD':
                rate = number(item.attrib['rate'])
                if rate <= 0:
                    raise ValueError('Ungültiger EZB-Kurs')
                return rate, dated.isoformat()
    raise ValueError('USD-Kurs fehlt')


def mint_ok(reply):
    try:
        value = reply['result']['value']
        parsed = value['data']['parsed']
        info = parsed['info']
        return (value['owner'] == TOKEN_PROGRAM and value['executable'] is False
                and parsed['type'] == 'mint' and info['isInitialized'] is True
                and 'mintAuthority' in info and info['mintAuthority'] is None
                and 'freezeAuthority' in info and info['freezeAuthority'] is None
                and number(info['supply']) > 0)
    except (KeyError, TypeError, ValueError):
        return False


def valid_pair(pair, mint):
    try:
        return (pair['chainId'] == 'solana' and pair['baseToken']['address'] == mint
                and address(pair['pairAddress'])
                and pair['quoteToken']['address'] in (SOL, USDC)
                and number(pair['priceUsd']) > 0 and number(pair['liquidity']['usd']) > 0)
    except (KeyError, TypeError, ValueError):
        return False


def eligible(pair, now, fx):
    """Experimental entry filters; no profitability or sellability guarantee."""
    try:
        liq = number(pair['liquidity']['usd'])
        age = number(now) - number(pair['pairCreatedAt']) / 1000
        trades = pair['txns']['m5']
        buys, sells = number(trades['buys']), number(trades['sells'])
        return (liq >= 100_000 and 3600 <= age <= 30 * 86400
                and number(pair['volume']['h1']) >= 25_000
                and buys >= 10 and sells >= 5 and buys >= sells
                and D('0.5') <= number(pair['priceChange']['m5']) <= 10
                and STAKE * fx <= liq * D('0.0005'))
    except (KeyError, TypeError, ValueError):
        return False


class Market:
    def __init__(self, http):
        self.http = http
        self.rpc = os.getenv('SOLANA_RPC_URL', 'https://api.mainnet-beta.solana.com')

    def discover(self):
        data = self.http.json(DEX + '/token-profiles/latest/v1')
        if not isinstance(data, list):
            raise ValueError('Profilantwort ist keine Liste')
        return list(dict.fromkeys(p['tokenAddress'] for p in data
                    if isinstance(p, dict) and p.get('chainId') == 'solana'
                    and re.search(r'\bmeme(?:coins?|tokens?)?\b', str(p.get('description', '')), re.I)
                    and address(p.get('tokenAddress')) and p['tokenAddress'] not in (SOL, USDC)))[:24]

    def pair(self, mint, pair_id=None):
        if not address(mint) or (pair_id is not None and not address(pair_id)):
            raise ValueError('Ungültige Adresse')
        if pair_id:
            reply = self.http.json(DEX + '/latest/dex/pairs/solana/' + pair_id)
            pairs = reply.get('pairs') or []
        else:
            pairs = self.http.json(DEX + '/token-pairs/v1/solana/' + mint)
        if not isinstance(pairs, list):
            raise ValueError('Poolliste fehlt')
        valid = [p for p in pairs if valid_pair(p, mint)
                 and (pair_id is None or p['pairAddress'] == pair_id)]
        if not valid:
            raise ValueError('Kein bewertbarer Pool')
        return max(valid, key=lambda p: number(p['liquidity']['usd']))

    def check_mint(self, mint):
        return mint_ok(self.http.json(self.rpc, {
            'jsonrpc': '2.0', 'id': 1, 'method': 'getAccountInfo',
            'params': [mint, {'encoding': 'jsonParsed', 'commitment': 'finalized'}]}))


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY, mint TEXT NOT NULL, pair TEXT NOT NULL, symbol TEXT NOT NULL,
                qty TEXT NOT NULL, entry_price TEXT NOT NULL, entry_fx TEXT NOT NULL,
                opened REAL NOT NULL, closed REAL, proceeds TEXT, pnl TEXT,
                mark TEXT, marked REAL, error TEXT, reason TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS one_open_mint ON positions(mint) WHERE closed IS NULL;
            CREATE TABLE IF NOT EXISTS days (
                day TEXT PRIMARY KEY, baseline TEXT NOT NULL, buys INTEGER NOT NULL DEFAULT 0,
                halted INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, ts REAL NOT NULL, message TEXT NOT NULL,
                sent INTEGER NOT NULL DEFAULT 0);
        ''')
        with self.tx():
            self.db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', ('cash', str(INITIAL_CASH)))
            self.db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', ('paused', '0'))
            self.db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', ('exit_all', '0'))

    @contextlib.contextmanager
    def tx(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row[0] if row else default

    def put(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, str(value)))

    def positions(self):
        return self.db.execute('SELECT * FROM positions WHERE closed IS NULL ORDER BY id').fetchall()

    def event(self, now, message):
        self.db.execute('INSERT INTO events(ts,message) VALUES (?,?)', (now, message))
        LOG.info(message)

    def equity(self):
        return number(self.get('cash')) + sum((number(p['mark'] or '0') for p in self.positions()), D(0))

    def start_day(self, now):
        with self.tx():
            incomplete = any(p['error'] or p['marked'] is None or not 0 <= now - p['marked'] <= 120
                             for p in self.positions())
            self.db.execute('INSERT OR IGNORE INTO days(day,baseline,halted) VALUES (?,?,?)',
                            (utc_day(now), str(self.equity()), int(incomplete)))

    def risk(self, now):
        day = self.db.execute('SELECT * FROM days WHERE day=?', (utc_day(now),)).fetchone()
        if day is None:
            return False
        if any(p['error'] or p['marked'] is None or not 0 <= now - p['marked'] <= 120
               for p in self.positions()):
            return False
        loss = number(day['baseline']) - self.equity()
        if loss >= MAX_DAILY_LOSS:
            if not day['halted']:
                self.db.execute('UPDATE days SET halted=1 WHERE day=?', (utc_day(now),))
                self.event(now, 'PAPER: Tagesverlustgrenze erreicht; Käufe bis zum nächsten UTC-Tag gesperrt.')
            return False
        if day['halted'] or day['buys'] >= MAX_DAILY_BUYS:
            return False
        return True

    def buy(self, pair, fx, now):
        mint = pair['baseToken']['address']
        with self.tx():
            if self.get('paused') == '1' or self.get('exit_all') == '1' or not self.risk(now):
                return False
            if len(self.positions()) >= MAX_POSITIONS or number(self.get('cash')) < STAKE:
                return False
            if self.db.execute('SELECT 1 FROM positions WHERE mint=? AND '
                               '(closed IS NULL OR opened>?)', (mint, now - 86400)).fetchone():
                return False
            qty = buy_quantity(pair['priceUsd'], fx)
            mark = liquidation_eur(qty, pair['priceUsd'], fx)
            symbol = re.sub(r'[^\w.-]', '', str(pair['baseToken'].get('symbol', '?')))[:24] or '?'
            self.db.execute('''INSERT INTO positions
                (mint,pair,symbol,qty,entry_price,entry_fx,opened,mark,marked)
                VALUES (?,?,?,?,?,?,?,?,?)''',
                (mint, pair['pairAddress'], symbol, str(qty), str(pair['priceUsd']),
                 str(fx), now, str(mark), now))
            self.put('cash', number(self.get('cash')) - STAKE)
            self.db.execute('UPDATE days SET buys=buys+1 WHERE day=?', (utc_day(now),))
            self.event(now, f'PAPER KAUF {symbol}: Gesamtbelastung 20,00 EUR; Mint {mint}')
            return True

    def mark_or_close(self, position_id, pair, fx, now):
        with self.tx():
            p = self.db.execute('SELECT * FROM positions WHERE id=? AND closed IS NULL',
                                (position_id,)).fetchone()
            if p is None:
                return
            if pair['pairAddress'] != p['pair'] or not valid_pair(pair, p['mint']):
                raise ValueError('Abweichender Pool')
            proceeds = liquidation_eur(p['qty'], pair['priceUsd'], fx)
            if proceeds * fx > number(pair['liquidity']['usd']) * D('0.001'):
                raise ValueError('Liquidität für Modell unzureichend')
            self.db.execute('UPDATE positions SET mark=?,marked=?,error=NULL WHERE id=?',
                            (str(proceeds), now, position_id))
            reason = None
            if self.get('exit_all') == '1':
                reason = 'manuell'
            elif proceeds <= STAKE * D('.85'):
                reason = 'Stop bei Nettoverlust >=15%'
            elif proceeds >= STAKE * D('1.30'):
                reason = 'Gewinnziel netto >=30%'
            elif now - p['opened'] >= 4 * 3600:
                reason = 'Haltedauer 4 Stunden'
            if reason:
                self.db.execute('UPDATE positions SET closed=?,proceeds=?,pnl=?,reason=? WHERE id=?',
                                (now, str(proceeds), str(proceeds - STAKE), reason, position_id))
                self.put('cash', number(self.get('cash')) + proceeds)
                self.event(now, f'PAPER VERKAUF {p["symbol"]}: {proceeds:.2f} EUR; '
                           f'Ergebnis {proceeds - STAKE:+.2f} EUR; {reason}')

    def unpriced(self, position_id, now):
        with self.tx():
            p = self.db.execute('SELECT * FROM positions WHERE id=? AND closed IS NULL',
                                (position_id,)).fetchone()
            if p:
                self.db.execute('UPDATE positions SET mark=NULL,error=? WHERE id=?',
                                ('Keine zuverlässige Modellbewertung', position_id))
                if not p['error']:
                    self.event(now, f'PAPER {p["symbol"]}: Bewertung fehlt; Position bleibt offen, Käufe gesperrt.')

    def status(self, now):
        positions = [dict(p) for p in self.positions()]
        incomplete = any(p['error'] or p['marked'] is None or not 0 <= now - p['marked'] <= 120
                         for p in positions)
        return {'mode': 'PAPER_ONLY', 'stake_eur_including_buy_costs': str(STAKE),
                'cash_eur': self.get('cash'), 'paused': self.get('paused') == '1',
                'equity_eur': None if incomplete else str(self.equity()),
                'valuation_incomplete': incomplete, 'fx_date': self.get('fx_date'),
                'last_cycle': self.get('last_cycle'), 'scanner': self.get('scanner', 'Noch nicht gestartet'),
                'today': [dict(r) for r in self.db.execute('SELECT * FROM days WHERE day=?', (utc_day(now),))],
                'positions': positions}


class Engine:
    def __init__(self, store, market, http, clock=time.time):
        self.store, self.market, self.http, self.clock = store, market, http, clock
        self.candidates, self.observations = [], {}
        self.last_profiles, self.last_fx, self.cursor = 0, 0, 0

    def fx(self, now):
        if now - self.last_fx >= 3600 or not self.store.get('fx'):
            rate, dated = parse_fx(self.http.request(ECB), datetime.fromtimestamp(now, timezone.utc).date())
            with self.store.tx():
                self.store.put('fx', rate)
                self.store.put('fx_date', dated)
            self.last_fx = now
        dated = date.fromisoformat(self.store.get('fx_date'))
        age = (datetime.fromtimestamp(now, timezone.utc).date() - dated).days
        if not 0 <= age <= 5:
            raise ValueError('FX veraltet')
        return number(self.store.get('fx'))

    def tick(self):
        now = self.clock()
        self.store.start_day(now)
        try:
            fx = self.fx(now)
        except Exception:
            self.store.put('scanner', 'Pausiert: aktueller EZB-Referenzkurs fehlt')
            for p in self.store.positions():
                self.store.unpriced(p['id'], now)
            return
        for p in self.store.positions():
            try:
                pair = self.market.pair(p['mint'], p['pair'])
                self.store.mark_or_close(p['id'], pair, fx, self.clock())
            except Exception:
                self.store.unpriced(p['id'], self.clock())
        with self.store.tx():
            allowed = self.store.risk(self.clock())
            if self.store.get('exit_all') == '1' and not self.store.positions():
                self.store.put('exit_all', '0')
        self.store.put('last_cycle', datetime.fromtimestamp(self.clock(), timezone.utc).isoformat())
        if not allowed or self.store.get('paused') == '1':
            self.store.put('scanner', 'Neue Käufe pausiert oder durch Limits/Daten gesperrt')
            return
        try:
            self.scan(fx)
        except Exception:
            self.store.put('scanner', 'Kandidat übersprungen: Daten-/RPC-Fehler')

    def scan(self, fx):
        now = self.clock()
        if now - self.last_profiles >= 300 or not self.candidates:
            profiles = self.market.discover()
            manual = [x.strip() for x in os.getenv('WATCH_MINTS', '').split(',') if address(x.strip())]
            self.candidates = list(dict.fromkeys(manual + profiles))[:24]
            self.last_profiles = now
            self.observations = {k: v for k, v in self.observations.items() if now - v[0] <= 900}
        if not self.candidates:
            self.store.put('scanner', 'Keine Solana-Kandidaten im Profilfeed')
            return
        mint = self.candidates[self.cursor % len(self.candidates)]
        self.cursor += 1
        pair = self.market.pair(mint)
        now = self.clock()
        prior = self.observations.get(mint)
        if prior is None or prior[1] != pair['pairAddress'] or now - prior[0] >= 60:
            self.observations[mint] = (now, pair['pairAddress'], number(pair['priceUsd']))
        self.store.put('scanner', f'{len(self.candidates)} Kandidaten; zuletzt {mint}')
        if not eligible(pair, now, fx) or prior is None:
            return
        elapsed = now - prior[0]
        growth = number(pair['priceUsd']) / prior[2] - 1
        if prior[1] != pair['pairAddress'] or not 60 <= elapsed <= 900 or not D('.005') <= growth <= D('.08'):
            return
        if not self.market.check_mint(mint):
            self.store.put('scanner', f'Mint-Prüfung nicht bestanden: {mint}')
            return
        fresh = self.market.pair(mint, pair['pairAddress'])
        if not eligible(fresh, self.clock(), fx):
            return
        if abs(number(fresh['priceUsd']) / number(pair['priceUsd']) - 1) > D('.02'):
            return
        self.store.buy(fresh, fx, self.clock())


def deliver_one(store, http):
    webhook = os.getenv('DISCORD_WEBHOOK_URL', '')
    if not webhook:
        return
    parsed = urlparse(webhook)
    if parsed.scheme != 'https' or parsed.hostname != 'discord.com' or not parsed.path.startswith('/api/webhooks/'):
        raise ValueError('Discord-Webhook muss von discord.com stammen')
    event = store.db.execute('SELECT * FROM events WHERE sent=0 ORDER BY id LIMIT 1').fetchone()
    if event:
        http.request(webhook, {'content': f'[Ereignis {event["id"]}] {event["message"]}',
                               'allowed_mentions': {'parse': []}})
        store.db.execute('UPDATE events SET sent=1 WHERE id=?', (event['id'],))


@contextlib.contextmanager
def process_lock(path):
    with open(path, 'a+b') as handle:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            handle.write(b'0')
            handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError('Bot läuft bereits für dieses Datenverzeichnis') from None
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError('Bot läuft bereits für dieses Datenverzeichnis') from None
        yield


def main():
    parser = argparse.ArgumentParser(description='Solana Memecoin PAPER-Bot, fest 20 EUR je Kauf')
    parser.add_argument('command', choices=['run', 'once', 'status', 'pause', 'resume', 'close-all', 'export'])
    parser.add_argument('--data-dir', default=os.getenv('DATA_DIR', './data'))
    parser.add_argument('--output', default='trades.csv', help='CSV-Ziel für export')
    args = parser.parse_args()
    if os.getenv('TRADING_MODE', 'paper').lower() != 'paper':
        parser.error('Diese Version unterstützt ausschließlich Paper-Trading.')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    directory = Path(args.data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    store = Store(directory / 'memecoin-paper.sqlite3')
    try:
        if args.command == 'status':
            print(json.dumps(store.status(time.time()), indent=2, ensure_ascii=False))
        elif args.command in ('pause', 'resume', 'close-all'):
            with store.tx():
                store.put('paused', '0' if args.command == 'resume' else '1')
                if args.command == 'close-all':
                    store.put('exit_all', '1')
                store.event(time.time(), f'PAPER Steuerung: {args.command}')
            print('Gespeichert. Positionsüberwachung/Verkäufe benötigen einen laufenden run-Prozess.')
        elif args.command == 'export':
            rows = store.db.execute('SELECT * FROM positions ORDER BY id')
            with open(args.output, 'w', newline='', encoding='utf-8') as out:
                writer = csv.writer(out)
                writer.writerow([c[0] for c in rows.description])
                writer.writerows(rows)
            print(args.output)
        else:
            with process_lock(directory / 'bot.lock'):
                http = Http()
                engine = Engine(store, Market(http), http)
                stop = threading.Event()
                signal.signal(signal.SIGINT, lambda *_: stop.set())
                signal.signal(signal.SIGTERM, lambda *_: stop.set())
                LOG.info('PAPER ONLY | 20,00 EUR pro Kauf | keine Wallet verbunden')
                while not stop.is_set():
                    started = time.monotonic()
                    engine.tick()
                    try:
                        deliver_one(store, http)
                    except Exception:
                        LOG.warning('Discord-Zustellung fehlgeschlagen; Ereignis bleibt gespeichert.')
                    if args.command == 'once':
                        print(json.dumps(store.status(time.time()), indent=2, ensure_ascii=False))
                        break
                    stop.wait(max(1, 15 - (time.monotonic() - started)))
    finally:
        store.db.close()


if __name__ == '__main__':
    main()

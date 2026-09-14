"""Railway entry point: persistent-volume guard, tests, probes and Discord Gateway."""
import logging
import os
from pathlib import Path
import subprocess
import sys
import bot


def check_volume():
    if not os.getenv('RAILWAY_PROJECT_ID'):
        return
    target = Path(os.getenv('DATA_DIR', '/data')).resolve()
    mount = os.getenv('RAILWAY_VOLUME_MOUNT_PATH')
    if not mount or target != Path(mount).resolve() or not os.path.ismount(target):
        raise RuntimeError('Persistentes Railway-Volume fehlt; Start abgebrochen.')


def probe_sources(http, market):
    checks = {}
    try:
        _, dated = bot.parse_fx(http.request(bot.ECB), __import__('datetime').datetime.now(__import__('datetime').timezone.utc).date())
        checks['ECB'] = 'OK ' + dated
    except Exception as exc:
        checks['ECB'] = 'UNAVAILABLE ' + type(exc).__name__ + ' HTTP=' + str(getattr(exc, 'http_status', 'n/a'))
    try:
        checks['DEX_PROFILES'] = 'OK candidates=' + str(len(market.discover()))
    except Exception as exc:
        checks['DEX_PROFILES'] = 'UNAVAILABLE ' + type(exc).__name__ + ' HTTP=' + str(getattr(exc, 'http_status', 'n/a'))
    try:
        p = market.pair(bot.SOL)
        market.pair(bot.SOL, p['pairAddress'])
        checks['DEX_POOLS'] = 'OK'
    except Exception as exc:
        checks['DEX_POOLS'] = 'UNAVAILABLE ' + type(exc).__name__ + ' HTTP=' + str(getattr(exc, 'http_status', 'n/a'))
    try:
        reply = http.json(market.rpc, {
            'jsonrpc': '2.0', 'id': 1, 'method': 'getAccountInfo',
            'params': [bot.SOL, {'encoding': 'jsonParsed', 'commitment': 'finalized'}]})
        if reply['result']['value']['data']['parsed']['type'] != 'mint':
            raise ValueError('Missing mint')
        checks['SOLANA_RPC'] = 'OK'
    except Exception as exc:
        checks['SOLANA_RPC'] = 'UNAVAILABLE ' + type(exc).__name__ + ' HTTP=' + str(getattr(exc, 'http_status', 'n/a'))
    for name, status in checks.items():
        bot.LOG.info('STARTUP DATA %s %s', name, status)


def run_tests():
    subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s',
                    str(Path(__file__).parent / 'tests')], check=True)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    if len(sys.argv) > 1 and sys.argv[1] in ('run', 'once'):
        run_tests()
        check_volume()
        bot.LOG.info('VOLUME CHECK OK | Discord gateway configured=%s',
                     bool(os.getenv('DISCORD_TOKEN')))
        http = bot.Http()
        probe_sources(http, bot.Market(http))
    if os.getenv('DISCORD_TOKEN', '').strip():
        from discord_gateway import run_gateway
        run_gateway()
    else:
        bot.main()

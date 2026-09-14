import io
import unittest
import urllib.error
from unittest.mock import patch
import bot


def limited(header=None):
    return urllib.error.HTTPError('https://api.dexscreener.com/test', 429, 'rate limited',
                                  {} if header is None else {'Retry-After': header}, io.BytesIO())


class RateLimitTests(unittest.TestCase):
    def test_numeric_retry_after_stops_same_host_requests(self):
        h = bot.Http()
        with patch('bot.time.monotonic', return_value=1000), \
                patch('bot.time.sleep'), patch('bot.urllib.request.urlopen', side_effect=limited('120')) as request:
            with self.assertRaises(RuntimeError):
                h.request(bot.DEX + '/test')
            with self.assertRaises(RuntimeError) as exc:
                h.request(bot.DEX + '/other')
            self.assertEqual(exc.exception.http_status, 429)
            self.assertEqual(request.call_count, 1)
            self.assertEqual(h.blocked_until['api.dexscreener.com'], 1120)

    def test_missing_retry_after_uses_exponential_backoff(self):
        h = bot.Http()
        with patch('bot.time.monotonic', return_value=1000), patch('bot.time.sleep'), \
                patch('bot.urllib.request.urlopen', side_effect=limited()):
            with self.assertRaises(RuntimeError):
                h.request(bot.DEX + '/test')
        self.assertEqual(h.blocked_until['api.dexscreener.com'], 1060)
        with patch('bot.time.monotonic', return_value=1061), patch('bot.time.sleep'), \
                patch('bot.urllib.request.urlopen', side_effect=limited()):
            with self.assertRaises(RuntimeError):
                h.request(bot.DEX + '/test')
        self.assertEqual(h.blocked_until['api.dexscreener.com'], 1181)

    def test_http_date_retry_after(self):
        h = bot.Http()
        epoch = 1789387200  # Fixed date; compare timestamps rather than local time.
        from email.utils import formatdate
        with patch('bot.time.monotonic', return_value=1000), patch('bot.time.time', return_value=epoch), \
                patch('bot.time.sleep'), patch('bot.urllib.request.urlopen', side_effect=limited(formatdate(epoch + 180, usegmt=True))):
            with self.assertRaises(RuntimeError):
                h.request(bot.DEX + '/test')
        self.assertEqual(h.blocked_until['api.dexscreener.com'], 1180)

    def test_other_hosts_are_not_blocked(self):
        h = bot.Http()
        h.blocked_until['api.dexscreener.com'] = 2000
        with patch('bot.time.monotonic', return_value=1000), patch('bot.time.sleep'), \
                patch('bot.urllib.request.urlopen') as request:
            request.return_value.__enter__.return_value.read.return_value = b'ok'
            self.assertEqual(h.request(bot.ECB), b'ok')
            self.assertEqual(request.call_count, 1)

    def test_success_after_wait_resets_failures(self):
        h = bot.Http()
        h.blocked_until['api.dexscreener.com'] = 999
        h.failures['api.dexscreener.com'] = 2
        with patch('bot.time.monotonic', return_value=1000), patch('bot.time.sleep'), \
                patch('bot.urllib.request.urlopen') as request:
            request.return_value.__enter__.return_value.read.return_value = b'[]'
            self.assertEqual(h.request(bot.DEX + '/test'), b'[]')
        self.assertNotIn('api.dexscreener.com', h.failures)

import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
import aiohttp
from .config import PAPER_URL, DATA_URL
from .strategy import NY


class BrokerError(RuntimeError):
    def __init__(self, status, message="Broker nicht erreichbar", code=None):
        self.status, self.code = status, code
        super().__init__(f"Alpaca HTTP {status}: {message}")


class AlpacaPaper:
    """No live URL argument, no redirects and no automatic POST retries."""
    def __init__(self, settings, session):
        self.session = session
        self.headers = {"APCA-API-KEY-ID": settings.key, "APCA-API-SECRET-KEY": settings.secret}
        self.secrets = (settings.key, settings.secret)

    async def request(self, method, path, *, data_api=False, params=None, body=None):
        if not path.startswith("/v2/") or ".." in path:
            raise ValueError("Ungültiger API-Pfad")
        url = (DATA_URL if data_api else PAPER_URL) + path
        for attempt in range(3 if method == "GET" else 1):
            delay = 1 + attempt
            try:
                async with self.session.request(method, url, headers=self.headers, params=params,
                                                json=body, allow_redirects=False,
                                                timeout=aiohttp.ClientTimeout(total=15)) as response:
                    if response.status == 204:
                        return None
                    try:
                        result = await response.json(content_type=None)
                    except (ValueError, aiohttp.ClientError):
                        result = {}
                    if 200 <= response.status < 300:
                        return result
                    message = str(result.get("message", "Anfrage abgelehnt")) if isinstance(result, dict) else "Anfrage abgelehnt"
                    for secret in self.secrets:
                        message = message.replace(secret, "[entfernt]")
                    error = BrokerError(response.status, message[:200], result.get("code") if isinstance(result, dict) else None)
                    if method != "GET" or attempt == 2 or (response.status != 429 and response.status < 500):
                        raise error
                    try:
                        delay = min(10, max(delay, float(response.headers.get("Retry-After", delay))))
                    except ValueError:
                        pass
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if method != "GET" or attempt == 2:
                    raise BrokerError(0, "Netzwerkfehler; Orderstatus muss abgeglichen werden") from exc
            await asyncio.sleep(delay)
        raise BrokerError(0)

    async def account(self):
        return await self.request("GET", "/v2/account")

    async def clock(self):
        return await self.request("GET", "/v2/clock")

    async def positions(self):
        return await self.request("GET", "/v2/positions")

    async def orders(self, status="open"):
        result = await self.request("GET", "/v2/orders", params={"status": status, "limit": 500, "nested": "false"})
        if len(result) == 500:
            raise BrokerError(0, "Zu viele Orders für dieses Bot-Konto. Eigenes Paperkonto verwenden.")
        return result

    async def order(self, order_id):
        return await self.request("GET", "/v2/orders/" + quote(order_id, safe=""), params={"nested": "true"})

    async def by_client_id(self, cid):
        return await self.request("GET", "/v2/orders:by_client_order_id", params={"client_order_id": cid})

    async def submit(self, payload):
        return await self.request("POST", "/v2/orders", body=payload)

    async def cancel(self, order_id):
        try:
            await self.request("DELETE", "/v2/orders/" + quote(order_id, safe=""))
        except BrokerError as exc:
            if exc.status not in (404, 422):
                raise
        # 204/404/422 does not prove cancellation. Engine re-reads orders/position.

    async def asset(self, symbol):
        return await self.request("GET", "/v2/assets/" + quote(symbol, safe=""))

    async def quotes(self, symbols):
        result = await self.request("GET", "/v2/stocks/quotes/latest", data_api=True,
                                    params={"symbols": ",".join(symbols), "feed": "iex"})
        return result.get("quotes", {})

    async def calendar(self, now):
        rows = await self.request("GET", "/v2/calendar", params={
            "start": (now - timedelta(days=10)).date().isoformat(), "end": now.date().isoformat()})
        return {r["date"]: tuple(datetime.fromisoformat(r["date"] + "T" + r[key]).replace(tzinfo=NY).astimezone(timezone.utc)
                                  for key in ("open", "close")) for r in rows}

    async def bars(self, symbols, now):
        params = {"symbols": ",".join(symbols), "timeframe": "5Min", "feed": "iex", "adjustment": "split",
                  "start": (now - timedelta(days=10)).isoformat(), "end": now.isoformat(),
                  "limit": 10000, "sort": "asc"}
        result = {s: [] for s in symbols}
        for _ in range(20):
            page = await self.request("GET", "/v2/stocks/bars", data_api=True, params=params)
            for symbol, bars in (page.get("bars") or {}).items():
                result.setdefault(symbol, []).extend(bars)
            token = page.get("next_page_token")
            if not token:
                return result
            params["page_token"] = token
        raise BrokerError(0, "Kursdaten-Paginierung unvollständig; dieser Scan wird verworfen")

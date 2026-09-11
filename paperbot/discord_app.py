import asyncio
from datetime import datetime, timezone
import io
import logging
import time
import uuid
import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from .broker import AlpacaPaper, BrokerError
from .config import number, symbol_name
from .engine import Engine

log = logging.getLogger("paperbot")


class GuardedTree(app_commands.CommandTree):
    async def interaction_check(self, interaction):
        # Beim ersten Befehl wird der Bediener automatisch als Besitzer gespeichert.
        # Danach darf nur dieser Discord-Account den Bot steuern.
        owner_id = self.client.store.get("discord_owner_id")
        if owner_id is None:
            self.client.store.set("discord_owner_id", int(interaction.user.id))
            owner_id = int(interaction.user.id)
        if int(interaction.user.id) != int(owner_id):
            await interaction.response.send_message("Dieser Bot wurde bereits von einem anderen Discord-Account übernommen.", ephemeral=True)
            return False
        return True

    async def on_error(self, interaction, error):
        original = getattr(error, "original", error)
        if isinstance(original, (ValueError, BrokerError)):
            message = str(original)
        else:
            message = "Befehl fehlgeschlagen. Bot-Konsole prüfen. Der Brokerstatus ist maßgeblich."
            log.error("Discord command failed: %s", type(original).__name__)
        if interaction.response.is_done():
            await interaction.followup.send(message[:1800], ephemeral=True)
        else:
            await interaction.response.send_message(message[:1800], ephemeral=True)


class PaperDiscord(discord.Client):
    def __init__(self, cfg, store):
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.cfg, self.store = cfg, store
        self.tree = GuardedTree(self)
        self.http_session = None
        self.engine = None
        self.channel = None
        self.boot_id = uuid.uuid4().hex
        self.last_delivery = 0.0
        self.register_commands()

    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession()
        self.engine = Engine(self.cfg, self.store, AlpacaPaper(self.cfg, self.http_session))
        await self.tree.sync()
        self.poll.change_interval(seconds=self.cfg.poll_seconds)
        self.poll.start()
        self.deliver.start()

    async def on_ready(self):
        log.info("Discord verbunden als %s", self.user)
        await self.change_presence(activity=discord.Game(name="Papertrading | /hilfe"))
        self.store.event("boot:" + self.boot_id, "Paper Trader ist online",
                         "US-Aktien · Alpaca Paper · Kontowährung USD\n"
                         f"Automatik: {'aktiv (gespeichert)' if self.engine.enabled else 'pausiert – /start zum Aktivieren'}\n"
                         "Befehle: /hilfe · /status · /konto · /pause · /notstopp\nErster Kurs-/Broker-Abgleich läuft.")

    async def resolve_channel(self):
        channel_id = self.store.get("discord_channel_id")
        if not channel_id:
            raise ValueError("Noch kein Meldekanal gesetzt. Im gewünschten Discord-Kanal einmal /start ausführen.")
        channel = self.get_channel(int(channel_id)) or await self.fetch_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            raise ValueError("Der gespeicherte Meldekanal ist nicht mehr verfügbar.")
        member = channel.guild.me
        perms = channel.permissions_for(member) if member else None
        if not perms or not (perms.view_channel and perms.send_messages and perms.embed_links):
            raise ValueError("Bot benötigt im Meldekanal Kanal ansehen, Nachrichten senden und Links einbetten.")
        self.channel = channel
        return channel

    def notification_ok(self):
        pending = self.store.pending_events(1)
        queue_stuck = bool(pending and time.time() - pending[0]["created"] > 300)
        return self.is_ready() and self.channel is not None and self.last_delivery > 0 and not queue_stuck

    @tasks.loop(seconds=15)
    async def poll(self):
        try:
            await self.engine.tick(self.notification_ok())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            detail = str(exc) if isinstance(exc, (ValueError, BrokerError)) else type(exc).__name__
            self.engine.block_reason = "Daten-/Brokerfehler: " + detail
            log.error("Trading cycle paused: %s", detail)
            self.store.event(f"engine-error:{type(exc).__name__}:{int(time.time() // 900)}",
                             "PAPER · Handelszyklus unterbrochen", detail + "\nNeue Einstiege warten auf einen erfolgreichen Abgleich. Vorhandene Brokerorders bleiben bestehen.", 0xE74C3C)

    @poll.before_loop
    async def before_poll(self):
        await self.wait_until_ready()

    @tasks.loop(seconds=3)
    async def deliver(self):
        if not self.is_ready():
            return
        try:
            channel = await self.resolve_channel()
            for item in self.store.pending_events(5):
                embed = discord.Embed(title=item["title"][:256], description=item["body"], color=item["color"],
                                      timestamp=datetime.fromtimestamp(item["created"], timezone.utc))
                embed.set_footer(text="PAPER • USD • IEX-Daten • /hilfe")
                await channel.send(embed=embed)
                self.store.acknowledge(item["id"])
                self.last_delivery = time.time()
        except (discord.HTTPException, ValueError) as exc:
            self.channel = None
            log.warning("Meldung nicht zugestellt (%s); nächster Versuch folgt.", type(exc).__name__)

    @deliver.before_loop
    async def before_deliver(self):
        await self.wait_until_ready()

    async def close(self):
        running = []
        for loop in (self.poll, self.deliver):
            task = loop.get_task()
            loop.cancel()
            if task:
                running.append(task)
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        if self.http_session:
            await self.http_session.close()
        await super().close()

    def register_commands(self):
        tree = self.tree

        async def reply(interaction, title, body):
            embed = discord.Embed(title=title, description=body[:4000], color=0x3498DB)
            embed.set_footer(text="PAPER • USD • keine Echtgeldorders")
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)

        @tree.command(name="hilfe", description="Befehle und Funktionsweise anzeigen")
        async def help_command(interaction: discord.Interaction):
            await reply(interaction, "Paper Trader · Hilfe",
                        "**Ansehen**\n/status – Markt, Kontostand, letzter Scan\n/konto – USD-Kontowerte\n"
                        "/positionen – offene Positionen\n/watchlist – beobachtete Symbole\n/verlauf – letzte Meldungen\n"
                        "/export – Konto-/Orderdaten als CSV\n\n**Steuern**\n/start – automatische Paper-Einstiege aktivieren\n"
                        "/pause – neue und noch ungefüllte Einstiege stoppen; gefüllte Positionen weiter überwachen\n"
                        "/schliessen symbol:AAPL – Position/Order schließen\n/notstopp bestaetigen:True – pausieren und alle Positionen/Orders des Paperkontos schließen\n"
                        "/watch_add und /watch_remove – Watchlist ändern\n/auftrag_pruefen – unklaren Orderversuch abgleichen\n\n"
                        "EMA-9/21-Kreuz + RSI + Volumen, abgeschlossene 5-Minuten-Kerzen, IEX-Feed. "
                        "Handel während regulärer US-Sitzungen. Meldungen laufen rund um die Uhr. "
                        "Keine Gewinngarantie; Simulationsergebnisse können vom echten Handel abweichen.")

        @tree.command(name="status", description="Handelsstatus, Tagesergebnis und letzten Markt-Scan anzeigen")
        async def status(interaction: discord.Interaction):
            await reply(interaction, "Paper Trader · Status", self.engine.summary() +
                        f"\nLetzter erfolgreicher Abgleich: {('<t:' + str(int(self.engine.last_ok)) + ':R>') if self.engine.last_ok else 'noch keiner'}")

        @tree.command(name="konto", description="Aktuellen Paper-Kontostand beim Broker abrufen")
        async def account(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            data = await self.engine.broker.account()
            await reply(interaction, "Alpaca Paper · USD", "\n".join([
                f"Kontowert: **{number(data['equity']):,.2f} USD**", f"Cash: {number(data['cash']):,.2f} USD",
                f"Broker-Kaufkraft: {number(data['buying_power']):,.2f} USD (kann simulierten Kredit enthalten)",
                f"Vorheriger Schlusskontowert: {number(data['last_equity']):,.2f} USD",
                f"Status: {data['status']}\nDer Bot begrenzt sein Bruttoengagement auf {self.cfg.gross_pct} % des Kontowerts."]))

        @tree.command(name="positionen", description="Offene Paper-Positionen mit unrealisiertem Ergebnis")
        async def positions(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            rows = await self.engine.broker.positions()
            body = "\n\n".join(f"**{p['symbol']} · {p['side'].upper()} · {p['qty']} Aktien**\n"
                               f"Einstieg {number(p['avg_entry_price']):.2f} → aktuell {number(p['current_price']):.2f} USD\n"
                               f"Unrealisiert: {number(p['unrealized_pl']):+.2f} USD ({number(p['unrealized_plpc'])*100:+.2f} %)" for p in rows)
            await reply(interaction, "Offene Paper-Positionen", body or "Keine offenen Positionen.")

        @tree.command(name="start", description="Automatische Paper-Einstiege aktivieren")
        async def start(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            if interaction.channel_id is None:
                raise ValueError("/start muss in einem Server-Textkanal ausgeführt werden.")
            self.store.set("discord_channel_id", int(interaction.channel_id))
            self.channel = None
            await self.resolve_channel()
            await self.engine.set_enabled(True)
            self.store.event("manual-start:" + uuid.uuid4().hex, "PAPER · Automatik aktiviert", "Der Bot wartet auf ein gültiges Signal. Tages-, Zeit- und Risikolimits bleiben aktiv.")
            await reply(interaction, "Automatik aktiviert", self.engine.summary())

        @tree.command(name="pause", description="Neue Einstiege stoppen; gefüllte Positionen weiter überwachen")
        async def pause(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            await self.engine.set_enabled(False)
            self.store.event("manual-pause:" + uuid.uuid4().hex, "PAPER · Automatik pausiert", "Offene Einstiege werden abgebrochen. Gefüllte Positionen behalten ihre Broker-Ausgänge und werden weiter überwacht.")
            await reply(interaction, "Automatik pausiert", "Keine neuen Einstiege. Noch offene Einstiege werden storniert, eventuelle Teilfüllungen geschlossen. Gefüllte Positionen behalten Stop und Ziel; die Schließung vor Börsenschluss bleibt aktiv.")

        @tree.command(name="schliessen", description="Eine Paper-Position oder offene Order kontrolliert schließen")
        @app_commands.describe(symbol="US-Symbol, z. B. AAPL")
        async def close_symbol(interaction: discord.Interaction, symbol: str):
            await interaction.response.defer(ephemeral=True)
            result = await self.engine.close_position(symbol_name(symbol))
            await reply(interaction, "Schließung", result)

        @tree.command(name="notstopp", description="Pausieren und ALLE Positionen/Orders dieses Paperkontos schließen")
        @app_commands.describe(bestaetigen="True: betrifft alle Positionen und Orders des verbundenen Paperkontos")
        async def emergency(interaction: discord.Interaction, bestaetigen: bool):
            if not bestaetigen:
                await reply(interaction, "Keine Änderung", "Mit bestaetigen:True pausierst du den Bot und veranlasst die Schließung aller Positionen und Orders auf diesem Paperkonto.")
                return
            await interaction.response.defer(ephemeral=True)
            result = await self.engine.close_position(emergency=True)
            self.store.event("emergency:" + uuid.uuid4().hex, "PAPER · Notstopp angefordert", result, 0xE74C3C)
            await reply(interaction, "Automatik pausiert · Notstopp", result)

        @tree.command(name="watchlist", description="Beobachtete US-Symbole anzeigen")
        async def watchlist(interaction: discord.Interaction):
            await reply(interaction, "Watchlist", ", ".join(self.engine.watchlist) or "Leer. /watch_add nutzen.")

        @tree.command(name="watch_add", description="Ein US-Symbol zur Watchlist hinzufügen")
        async def watch_add(interaction: discord.Interaction, symbol: str):
            await interaction.response.defer(ephemeral=True)
            symbol = symbol_name(symbol)
            asset = await self.engine.broker.asset(symbol)
            if not asset.get("tradable") or asset.get("class") != "us_equity" or asset.get("status") != "active":
                raise ValueError("Dieses Symbol ist keine aktive, handelbare US-Aktie/ETF.")
            async with self.engine.lock:
                watch = self.engine.watchlist
                if symbol not in watch:
                    if len(watch) >= 20:
                        raise ValueError("Maximal 20 Symbole möglich.")
                    watch.append(symbol)
                    self.store.set("watchlist", watch)
            await reply(interaction, "Watchlist gespeichert", ", ".join(self.engine.watchlist))

        @tree.command(name="watch_remove", description="Symbol aus Beobachtung nehmen; vorhandene Position bleibt überwacht")
        async def watch_remove(interaction: discord.Interaction, symbol: str):
            await interaction.response.defer(ephemeral=True)
            symbol = symbol_name(symbol)
            async with self.engine.lock:
                self.store.set("watchlist", [s for s in self.engine.watchlist if s != symbol])
            await reply(interaction, "Watchlist gespeichert", ", ".join(self.engine.watchlist) or "Leer.")

        @tree.command(name="verlauf", description="Letzte Bot-Meldungen anzeigen")
        async def history(interaction: discord.Interaction):
            await reply(interaction, "Letzte Ereignisse", "\n\n".join(
                f"<t:{int(e['created'])}:t> **{e['title']}**\n{e['body'][:220]}" for e in self.store.events(10)) or "Noch keine Ereignisse.")

        @tree.command(name="export", description="Kontoentwicklung, Orders oder Meldungen als CSV herunterladen")
        @app_commands.choices(daten=[app_commands.Choice(name="Kontoentwicklung", value="konto"),
                                   app_commands.Choice(name="Orders", value="orders"),
                                   app_commands.Choice(name="Ereignisse", value="ereignisse")])
        async def export(interaction: discord.Interaction, daten: app_commands.Choice[str]):
            content = self.store.export_csv(daten.value)
            await interaction.response.send_message(file=discord.File(io.BytesIO(content), filename=f"paper-{daten.value}.csv"), ephemeral=True)

        @tree.command(name="auftrag_pruefen", description="Unklare Order abgleichen; nach 5 Min. ohne Brokerposition ggf. freigeben")
        @app_commands.describe(client_id="Client-ID aus der Bot-Fehlermeldung", verwerfen="Nur bei Broker-404 ohne Position/Orders nach mindestens 5 Minuten")
        async def check_order(interaction: discord.Interaction, client_id: str, verwerfen: bool = False):
            await interaction.response.defer(ephemeral=True)
            result = await self.engine.inspect_unknown(client_id.strip(), verwerfen)
            await reply(interaction, "Order-Abgleich", result)

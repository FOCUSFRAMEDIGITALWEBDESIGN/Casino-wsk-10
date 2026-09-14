"""Discord Gateway interface for the independent Solana paper bot."""
from __future__ import annotations
import asyncio
import os
from pathlib import Path
import time
import logging
import discord
from discord import app_commands
import bot

LOG = logging.getLogger("memecoin-paper.discord")


def _ids(raw):
    out = set()
    for value in (raw or "").split(","):
        try:
            if value.strip():
                out.add(int(value.strip()))
        except ValueError:
            continue
    return out


def _clip(text, length=1900):
    text = str(text)
    return text if len(text) <= length else text[:length - 3] + "..."


class PaperTree(app_commands.CommandTree):
    def __init__(self, client):
        super().__init__(client)
        self.client = client

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in self.client.authorized_ids:
            await interaction.response.send_message(
                "Dieser Bot ist nur für den eingerichteten Besitzer freigeschaltet.",
                ephemeral=True)
            return False
        configured = self.client.store.get("discord_guild_id")
        if configured and interaction.guild_id and str(interaction.guild_id) != configured:
            await interaction.response.send_message(
                "Bitte den eingerichteten Discord-Server verwenden.", ephemeral=True)
            return False
        return True


class GatewayBot(discord.Client):
    def __init__(self, store):
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none())
        self.store = store
        self.tree = PaperTree(self)
        # Keep the custom market client separate from discord.Client.http.
        # discord.py owns self.http and needs its static_login/close methods.
        self.market_http = bot.Http()
        self.market = bot.Market(self.market_http)
        self.engine = bot.Engine(store, self.market, self.market_http)
        self.authorized_ids = _ids(os.getenv("DISCORD_OWNER_IDS"))
        self.ticker_task = None
        self.channel = None
        self.synced = False
        self.register_commands()

    def register_commands(self):
        @self.tree.command(name="hilfe", description="Zeigt die Memecoin-Paper-Befehle")
        async def help_command(interaction: discord.Interaction):
            await interaction.response.send_message(
                "Memecoin-Paper-Bot\n"
                "/start Bot aktivieren und diesen Kanal speichern\n"
                "/status Kontostand, Limits und Datenquellen\n"
                "/positionen offene Paper-Positionen\n"
                "/scanner Suchstatus anzeigen\n"
                "/pause neue Käufe pausieren\n"
                "/schliessen alle Positionen schließen lassen und Käufe pausieren\n"
                "/verlauf abgeschlossene Paper-Trades", ephemeral=True)

        @self.tree.command(name="start", description="Bot aktivieren und diesen Kanal speichern")
        async def start_command(interaction: discord.Interaction):
            with self.store.tx():
                self.store.put("paused", "0")
                self.store.put("exit_all", "0")
                if interaction.guild_id:
                    self.store.put("discord_guild_id", interaction.guild_id)
                self.store.put("discord_channel_id", interaction.channel_id)
            self.channel = interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None
            await interaction.response.send_message(
                "Memecoin-Paper-Bot aktiviert. Pro Kauf bleiben es exakt 20,00 EUR; "
                "echte Wallet-Transaktionen sind nicht verbunden.")
            await self.deliver_events()

        @self.tree.command(name="pause", description="Neue Käufe pausieren")
        async def pause_command(interaction: discord.Interaction):
            self.store.put("paused", "1")
            await interaction.response.send_message(
                "Neue Käufe pausiert. Offene Paper-Positionen werden weiter überwacht.",
                ephemeral=True)

        @self.tree.command(name="status", description="Kontostand und Botstatus anzeigen")
        async def status_command(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            status = self.store.status(time.time())
            lines = [
                "Memecoin-Paper-Bot",
                f"Modus: {status['mode']}",
                f"Virtuelles Guthaben: {status['cash_eur']} EUR",
                f"Einsatz je Kauf: {status['stake_eur_including_buy_costs']} EUR",
                f"Bewertetes Eigenkapital: {status['equity_eur'] if status['equity_eur'] is not None else 'unvollständig'}",
                f"Offene Positionen: {len(status['positions'])}/3",
                f"Scanner: {status['scanner']}",
                f"Käufe pausiert: {'ja' if status['paused'] else 'nein'}",
                f"FX-Referenz: {status['fx_date'] or 'noch nicht vorhanden'}",
            ]
            await interaction.followup.send(_clip("\n".join(lines)), ephemeral=True)

        @self.tree.command(name="positionen", description="Offene Paper-Positionen anzeigen")
        async def positions_command(interaction: discord.Interaction):
            rows = self.store.positions()
            if not rows:
                await interaction.response.send_message("Keine offenen Paper-Positionen.", ephemeral=True)
                return
            lines = ["Offene Paper-Positionen"]
            for row in rows:
                mark = row["mark"] if row["mark"] is not None else "unbewertet"
                lines.append(f"{row['symbol']} · Einsatz 20,00 EUR · Wert {mark} EUR")
            await interaction.response.send_message(_clip("\n".join(lines)), ephemeral=True)

        @self.tree.command(name="scanner", description="Automatische Kandidatensuche anzeigen")
        async def scanner_command(interaction: discord.Interaction):
            await interaction.response.send_message(
                f"Scanner: {self.store.get('scanner', 'Noch nicht gestartet')}\n"
                "Neue Käufe erfolgen nur nach Markt-, Liquiditäts- und Mint-Prüfungen.",
                ephemeral=True)

        @self.tree.command(name="schliessen", description="Alle offenen Positionen schließen lassen")
        async def close_command(interaction: discord.Interaction):
            with self.store.tx():
                self.store.put("paused", "1")
                self.store.put("exit_all", "1")
                self.store.event(time.time(), "PAPER Steuerung: /schliessen")
            await interaction.response.send_message(
                "Schließung angefordert. Der laufende Bot versucht verfügbare Positionen "
                "mit aktuellen Modelldaten zu schließen; neue Käufe bleiben pausiert.")

        @self.tree.command(name="verlauf", description="Letzte abgeschlossene Paper-Trades anzeigen")
        async def history_command(interaction: discord.Interaction):
            rows = self.store.db.execute(
                "SELECT symbol,proceeds,pnl,reason,closed FROM positions "
                "WHERE closed IS NOT NULL ORDER BY closed DESC LIMIT 10").fetchall()
            if not rows:
                await interaction.response.send_message("Noch keine abgeschlossenen Paper-Trades.", ephemeral=True)
                return
            lines = ["Letzte Paper-Trades"]
            for row in rows:
                lines.append(f"{row['symbol']} · Ergebnis {row['pnl']} EUR · {row['reason'] or '—'}")
            await interaction.response.send_message(_clip("\n".join(lines)), ephemeral=True)

    async def setup_hook(self):
        info = await self.application_info()
        if not self.authorized_ids:
            owner = getattr(info, "owner", None)
            if owner:
                self.authorized_ids.add(owner.id)
        guild_raw = os.getenv("DISCORD_GUILD_ID", "").strip()
        if guild_raw:
            try:
                guild = discord.Object(id=int(guild_raw))
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            except (ValueError, discord.HTTPException):
                LOG.exception("Discord-Guild-Synchronisierung fehlgeschlagen")
                raise
        else:
            await self.tree.sync()
        self.synced = True
        self.ticker_task = asyncio.create_task(self.ticker(), name="memecoin-paper-ticker")

    async def on_ready(self):
        LOG.info("Discord Gateway verbunden als %s; Befehle synchronisiert=%s", self.user, self.synced)
        channel_id = self.store.get("discord_channel_id")
        if channel_id:
            try:
                channel = self.get_channel(int(channel_id)) or await self.fetch_channel(int(channel_id))
                if isinstance(channel, discord.TextChannel):
                    self.channel = channel
            except (ValueError, discord.HTTPException, discord.Forbidden):
                LOG.warning("Gespeicherter Discord-Kanal ist nicht erreichbar.")

    async def ticker(self):
        await self.wait_until_ready()
        while not self.is_closed():
            started = time.monotonic()
            try:
                await asyncio.to_thread(self.engine.tick)
                await self.deliver_events()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Paper-Trading-Zyklus fehlgeschlagen")
            await asyncio.sleep(max(1, 15 - (time.monotonic() - started)))

    async def deliver_events(self):
        if self.channel is None:
            channel_id = self.store.get("discord_channel_id")
            if channel_id:
                try:
                    channel = self.get_channel(int(channel_id)) or await self.fetch_channel(int(channel_id))
                    if isinstance(channel, discord.TextChannel):
                        self.channel = channel
                except (ValueError, discord.HTTPException, discord.Forbidden):
                    return
        if self.channel is None:
            return
        while True:
            event = self.store.db.execute(
                "SELECT id,message FROM events WHERE sent=0 ORDER BY id LIMIT 1").fetchone()
            if event is None:
                return
            try:
                await self.channel.send(
                    f"Paper-Ereignis #{event['id']}\n{_clip(event['message'], 1750)}",
                    allowed_mentions=discord.AllowedMentions.none())
            except (discord.HTTPException, discord.Forbidden):
                return
            self.store.db.execute("UPDATE events SET sent=1 WHERE id=?", (event["id"],))


def run_gateway():
    token = (os.getenv("DISCORD_TOKEN", "").strip() or os.getenv("discord_token", "").strip())
    if not token:
        raise RuntimeError("DISCORD_TOKEN fehlt; Gateway-Bot kann nicht starten.")
    directory = Path(os.getenv("DATA_DIR", "./data"))
    directory.mkdir(parents=True, exist_ok=True)
    store = bot.Store(directory / "memecoin-paper.sqlite3")
    try:
        with bot.process_lock(directory / "bot.lock"):
            client = GatewayBot(store)
            client.run(token, log_handler=None)
    finally:
        store.db.close()

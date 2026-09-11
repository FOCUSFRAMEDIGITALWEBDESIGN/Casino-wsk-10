import os
import re
import ipaddress
import socket
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
MAX_REDIRECTS = 10
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=12)
USER_AGENT = "DiscordLinkResolver/1.0"

BLOCKED_BYPASS_DOMAINS = {
    "linkvertise.com",
    "link-to.net",
    "linkvertise.net",
}

SHORTENER_DOMAINS = {
    "bit.ly",
    "tinyurl.com",
    "t.co",
    "is.gd",
    "cutt.ly",
    "rb.gy",
    "shorturl.at",
    "rebrand.ly",
}


def normalize_url(raw: str) -> str:
    raw = raw.strip().strip("<>")
    if not re.match(r"^https?://", raw, re.IGNORECASE):
        raw = "https://" + raw
    return raw


def hostname_matches(hostname: str, domains: set[str]) -> bool:
    hostname = (hostname or "").lower().rstrip(".")
    return any(hostname == d or hostname.endswith("." + d) for d in domains)


async def is_public_hostname(hostname: str) -> bool:
    if not hostname:
        return False

    lowered = hostname.lower()
    if lowered in {"localhost", "localhost.localdomain"} or lowered.endswith(".local"):
        return False

    loop = __import__("asyncio").get_running_loop()
    try:
        infos = await loop.getaddrinfo(hostname, None)
    except Exception:
        return False

    for info in infos:
        ip_text = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError:
            continue

        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False

    return True


async def resolve_redirects(url: str):
    history = []
    current = url
    headers = {"User-Agent": USER_AGENT}

    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT, headers=headers) as session:
        for _ in range(MAX_REDIRECTS + 1):
            parsed = urlparse(current)

            if parsed.scheme not in {"http", "https"}:
                return {"ok": False, "error": "Nur http/https-Links werden unterstützt.", "history": history}

            host = parsed.hostname or ""

            if hostname_matches(host, BLOCKED_BYPASS_DOMAINS):
                return {
                    "ok": False,
                    "blocked": True,
                    "error": "Linkvertise erkannt. Der Bot umgeht keine Werbe- oder Freischaltmechanismen.",
                    "history": history,
                    "final_url": current,
                }

            if not await is_public_hostname(host):
                return {"ok": False, "error": "Dieser Host ist aus Sicherheitsgründen nicht erlaubt.", "history": history}

            try:
                async with session.get(current, allow_redirects=False, max_redirects=0) as resp:
                    status = resp.status
                    history.append((status, current))

                    if status in {301, 302, 303, 307, 308}:
                        location = resp.headers.get("Location")
                        if not location:
                            return {"ok": False, "error": "Redirect ohne Ziel-URL erhalten.", "history": history}

                        current = str(resp.url.join(aiohttp.client_reqrep.URL(location)))
                        continue

                    return {
                        "ok": True,
                        "final_url": current,
                        "status": status,
                        "content_type": resp.headers.get("Content-Type", "unbekannt"),
                        "content_length": resp.headers.get("Content-Length", "unbekannt"),
                        "history": history,
                    }

            except aiohttp.TooManyRedirects:
                return {"ok": False, "error": "Zu viele Weiterleitungen.", "history": history}
            except aiohttp.ClientError as exc:
                return {"ok": False, "error": f"Netzwerkfehler: {type(exc).__name__}", "history": history}

    return {"ok": False, "error": f"Mehr als {MAX_REDIRECTS} Weiterleitungen.", "history": history}


intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"Eingeloggt als {bot.user} | {len(synced)} Slash-Commands synchronisiert.")
    except Exception as exc:
        print("Slash-Command-Sync fehlgeschlagen:", exc)


@bot.tree.command(name="resolve", description="Löst normale HTTP-Weiterleitungen eines Links auf.")
@app_commands.describe(url="Der Link, den du prüfen möchtest")
async def resolve(interaction: discord.Interaction, url: str):
    await interaction.response.defer(ephemeral=False)

    normalized = normalize_url(url)
    parsed = urlparse(normalized)
    host = parsed.hostname or ""

    if hostname_matches(host, BLOCKED_BYPASS_DOMAINS):
        embed = discord.Embed(
            title="Linkvertise erkannt",
            description="Ich kann diesen Link erkennen, aber keine Werbe-, Freischalt- oder Schutzmechanismen von Linkvertise umgehen.",
            color=discord.Color.orange(),
        )
        embed.add_field(name="URL", value=f"`{normalized[:1000]}`", inline=False)
        await interaction.followup.send(embed=embed)
        return

    result = await resolve_redirects(normalized)

    if not result.get("ok"):
        color = discord.Color.orange() if result.get("blocked") else discord.Color.red()
        embed = discord.Embed(
            title="Link konnte nicht aufgelöst werden",
            description=result.get("error", "Unbekannter Fehler"),
            color=color,
        )
        if result.get("final_url"):
            embed.add_field(name="Letzte URL", value=result["final_url"][:1000], inline=False)
        await interaction.followup.send(embed=embed)
        return

    final_url = result["final_url"]
    final_host = urlparse(final_url).hostname or ""

    embed = discord.Embed(title="Link aufgelöst", color=discord.Color.green())
    embed.add_field(name="Start", value=normalized[:1000], inline=False)
    embed.add_field(name="Ziel", value=final_url[:1000], inline=False)
    embed.add_field(name="HTTP-Status", value=str(result["status"]), inline=True)
    embed.add_field(name="Content-Type", value=result["content_type"][:100], inline=True)
    embed.add_field(name="Redirects", value=str(max(0, len(result["history"]) - 1)), inline=True)

    if hostname_matches(parsed.hostname or "", SHORTENER_DOMAINS):
        embed.set_footer(text="Kurzlink erkannt und normale Redirects wurden verfolgt.")
    elif final_host != (parsed.hostname or ""):
        embed.set_footer(text="Der Ziel-Host unterscheidet sich vom Start-Host.")

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="linkinfo", description="Zeigt Basisinformationen zu einer URL.")
@app_commands.describe(url="URL zum Prüfen")
async def linkinfo(interaction: discord.Interaction, url: str):
    normalized = normalize_url(url)
    parsed = urlparse(normalized)
    host = parsed.hostname or "unbekannt"
    scheme = parsed.scheme or "unbekannt"

    embed = discord.Embed(title="Link-Info", color=discord.Color.blurple())
    embed.add_field(name="Host", value=host, inline=False)
    embed.add_field(name="Schema", value=scheme, inline=True)
    embed.add_field(name="Bekannter Kurzlink-Dienst", value="Ja" if hostname_matches(host, SHORTENER_DOMAINS) else "Nein", inline=True)
    embed.add_field(name="Linkvertise/gesperrter Bypass-Dienst", value="Ja" if hostname_matches(host, BLOCKED_BYPASS_DOMAINS) else "Nein", inline=True)
    await interaction.response.send_message(embed=embed)


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN fehlt. Lege ihn als Umgebungsvariable an.")
    bot.run(TOKEN)

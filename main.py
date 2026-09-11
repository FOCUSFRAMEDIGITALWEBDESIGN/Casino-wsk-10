"""Sparked Host entry point: python main.py (Python 3.11+)."""
import argparse
import asyncio
import importlib.metadata
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import sys

ROOT = Path(__file__).resolve().parent


def main():
    os.chdir(ROOT)
    parser = argparse.ArgumentParser(description="Discord Paper Trader – ausschließlich Alpaca Paper")
    parser.add_argument("--check", action="store_true", help="Konfiguration/Abhängigkeiten lokal prüfen; keine Verbindung")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        raise SystemExit("Python 3.11 oder neuer erforderlich; empfohlen 3.12.")
    try:
        from dotenv import load_dotenv
        from paperbot.config import Settings
        load_dotenv(ROOT / ".env", override=False)
        cfg = Settings.from_env()
        for package in ("discord.py", "aiohttp", "python-dotenv"):
            print(f"{package}: {importlib.metadata.version(package)}")
    except (ImportError, ValueError, importlib.metadata.PackageNotFoundError) as exc:
        raise SystemExit(f"Startprüfung fehlgeschlagen: {exc}\nPakete aus requirements.txt installieren und .env prüfen.")
    if args.check:
        print("Konfiguration gültig. Paper-Endpunkt fest eingestellt. Keine Netzwerkverbindung hergestellt.")
        return
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    lock_file = (cfg.data_dir / "bot.lock").open("a+")
    # Sparked Host runs Linux. Prevent two local processes from sharing the journal.
    if sys.platform != "win32":
        import fcntl
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Es läuft bereits ein Bot mit diesem DATA_DIR.")
    else:
        import msvcrt
        lock_file.seek(0)
        lock_file.write("0")
        lock_file.flush()
        lock_file.seek(0)
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise SystemExit("Es läuft bereits ein Bot mit diesem DATA_DIR.")
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), RotatingFileHandler(log_dir / "bot.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")])
    from paperbot.store import Store
    from paperbot.discord_app import PaperDiscord
    store = Store(cfg.data_dir / "paperbot.sqlite3")
    bot = PaperDiscord(cfg, store)

    async def run():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.close()))
            except NotImplementedError:
                pass
        async with bot:
            await bot.start(cfg.token, reconnect=True)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    finally:
        store.close()
        lock_file.close()


if __name__ == "__main__":
    main()

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
import os
import re

PAPER_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"


def resolve_data_dir() -> Path:
    mount = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    data_dir = Path(os.getenv("DATA_DIR", mount or "data"))
    if os.getenv("RAILWAY_PROJECT_ID") or os.getenv("RAILWAY_SERVICE_ID"):
        if not mount:
            raise ValueError("Railway: persistentes Volume fehlt. Volume unter /data anbinden und neu deployen.")
        mount_path = Path(mount).resolve()
        if not Path(mount).is_absolute() or not data_dir.resolve().is_relative_to(mount_path):
            raise ValueError("Railway: DATA_DIR muss innerhalb des angebundenen Volumes liegen.")
    return data_dir


def number(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Ungültige Zahl") from exc
    if not result.is_finite():
        raise ValueError("Zahlen müssen endlich sein")
    return result


def symbol_name(value: str) -> str:
    value = value.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", value):
        raise ValueError("Bitte ein US-Aktiensymbol wie AAPL eingeben.")
    return value


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    key: str = field(repr=False)
    secret: str = field(repr=False)
    watchlist: tuple[str, ...] = ("AAPL", "MSFT", "NVDA", "SPY", "QQQ")
    allow_shorts: bool = True
    risk_pct: Decimal = Decimal("1")
    stop_pct: Decimal = Decimal("1")
    reward_r: Decimal = Decimal("2")
    daily_loss_pct: Decimal = Decimal("3")
    position_pct: Decimal = Decimal("20")
    gross_pct: Decimal = Decimal("60")
    max_positions: int = 3
    max_trades: int = 3
    entry_slippage_pct: Decimal = Decimal("0.10")
    max_spread_pct: Decimal = Decimal("0.25")
    max_drift_pct: Decimal = Decimal("0.50")
    entry_timeout: int = 120
    quote_max_age: int = 120
    scan_seconds: int = 60
    poll_seconds: int = 15
    status_minutes: int = 30
    closed_status_minutes: int = 180
    entry_cutoff_minutes: int = 30
    close_before_minutes: int = 10
    data_dir: Path = Path("data")

    @classmethod
    def from_env(cls):
        def required(name):
            value = os.getenv(name, "").strip()
            if not value:
                raise ValueError(f"{name} fehlt. Bitte .env vervollständigen.")
            return value

        for name in ("APCA_API_BASE_URL", "ALPACA_BASE_URL"):
            if os.getenv(name, PAPER_URL).rstrip("/") != PAPER_URL:
                raise ValueError(f"{name}: Diese Anwendung unterstützt ausschließlich Alpaca Paper.")
        raw_bool = os.getenv("ALLOW_SHORTS", "true").lower()
        if raw_bool not in ("true", "false"):
            raise ValueError("ALLOW_SHORTS muss true oder false sein.")
        decimal_fields = {
            "risk_pct": ("RISK_PER_TRADE_PCT", "1", 5),
            "stop_pct": ("STOP_LOSS_PCT", "1", 10),
            "reward_r": ("TAKE_PROFIT_R", "2", 10),
            "daily_loss_pct": ("DAILY_LOSS_LIMIT_PCT", "3", 20),
            "position_pct": ("MAX_POSITION_PCT", "20", 100),
            "gross_pct": ("MAX_GROSS_EXPOSURE_PCT", "60", 100),
            "entry_slippage_pct": ("ENTRY_SLIPPAGE_PCT", "0.10", 1),
            "max_spread_pct": ("MAX_SPREAD_PCT", "0.25", 2),
            "max_drift_pct": ("MAX_SIGNAL_DRIFT_PCT", "0.50", 2),
        }
        kwargs = {}
        for field_name, (env, default, maximum) in decimal_fields.items():
            value = number(os.getenv(env, default))
            if not 0 < value <= maximum:
                raise ValueError(f"{env} muss größer 0 und höchstens {maximum} sein.")
            kwargs[field_name] = value
        integer_fields = {
            "max_positions": ("MAX_POSITIONS", 3, 1, 10),
            "max_trades": ("MAX_TRADES_PER_DAY", 3, 1, 20),
            "entry_timeout": ("ENTRY_TIMEOUT_SECONDS", 120, 30, 300),
            "quote_max_age": ("QUOTE_MAX_AGE_SECONDS", 120, 10, 120),
            "scan_seconds": ("SCAN_INTERVAL_SECONDS", 60, 30, 300),
            "poll_seconds": ("POLL_INTERVAL_SECONDS", 15, 10, 60),
            "status_minutes": ("STATUS_INTERVAL_MINUTES", 30, 1, 1440),
            "closed_status_minutes": ("CLOSED_STATUS_INTERVAL_MINUTES", 180, 1, 1440),
            "entry_cutoff_minutes": ("ENTRY_CUTOFF_MINUTES", 30, 15, 120),
            "close_before_minutes": ("CLOSE_BEFORE_MINUTES", 10, 5, 30),
        }
        for field_name, (env, default, minimum, maximum) in integer_fields.items():
            value = int(os.getenv(env, str(default)))
            if not minimum <= value <= maximum:
                raise ValueError(f"{env}: zulässig sind {minimum} bis {maximum}.")
            kwargs[field_name] = value
        watchlist = tuple(dict.fromkeys(symbol_name(s) for s in os.getenv(
            "WATCHLIST", "AAPL,MSFT,NVDA,AMZN,META,GOOGL,AMD,TSLA,SPY,QQQ").split(",") if s.strip()))
        if not 1 <= len(watchlist) <= 20:
            raise ValueError("WATCHLIST benötigt 1 bis 20 Symbole.")
        if kwargs["close_before_minutes"] >= kwargs["entry_cutoff_minutes"]:
            raise ValueError("ENTRY_CUTOFF_MINUTES muss größer als CLOSE_BEFORE_MINUTES sein.")

        # Nur zwei Secrets nötig: DISCORD_TOKEN und ALPACA_KEY.
        # ALPACA_KEY enthält Alpaca Key-ID und Secret im Format KEY_ID:SECRET_KEY.
        alpaca = required("ALPACA_KEY")
        if ":" not in alpaca:
            raise ValueError("ALPACA_KEY muss im Format KEY_ID:SECRET_KEY eingetragen werden.")
        key, secret = alpaca.split(":", 1)
        key, secret = key.strip(), secret.strip()
        if not key or not secret:
            raise ValueError("ALPACA_KEY muss Key-ID und Secret enthalten.")

        return cls(token=required("DISCORD_TOKEN"), key=key, secret=secret,
                   watchlist=watchlist, allow_shorts=raw_bool == "true",
                   data_dir=resolve_data_dir(), **kwargs)

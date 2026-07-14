"""Central configuration loader.

Reads every tunable value from the environment (.env via python-dotenv) and
exposes them as typed module-level constants. Cogs import from here only — no
cog is allowed to read os.getenv directly or hardcode an ID/threshold.
"""

import json
import os
import re

from dotenv import load_dotenv

load_dotenv()

# Matches an inline comment: a '#' at the start of the value or preceded by
# whitespace, through end of line. Lets users keep the explanatory comments that
# ship in .env.example without them leaking into the parsed value:
#   "123  # my id"        -> "123"
#   "        # (optional)" -> ""   (dotenv already trimmed the leading spaces)
_INLINE_COMMENT = re.compile(r"(?:^|\s)#.*$")


# --------------------------------------------------------------------------- #
# Typed env helpers
# --------------------------------------------------------------------------- #
def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    return _INLINE_COMMENT.sub("", value).strip()


def _str(key: str, default: str | None = None, required: bool = False) -> str | None:
    val = _clean(os.getenv(key, default))
    if required and (val is None or val == ""):
        raise RuntimeError(f"Missing required environment variable: {key}")
    return val


def _int(key: str, default: int | None = None, required: bool = False) -> int | None:
    raw = _clean(os.getenv(key))
    if raw is None or raw == "":
        if required:
            raise RuntimeError(f"Missing required environment variable: {key}")
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {key} must be an integer, got: {raw!r}") from exc


def _float(key: str, default: float) -> float:
    raw = _clean(os.getenv(key))
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {key} must be a float, got: {raw!r}") from exc


def _bool(key: str, default: bool = False) -> bool:
    raw = (_clean(os.getenv(key)) or str(default)).lower()
    return raw in ("1", "true", "yes", "on", "y")


def _int_list(key: str) -> list[int]:
    raw = _clean(os.getenv(key)) or ""
    if not raw:
        return []
    return [int(part.strip()) for part in raw.split(",") if part.strip()]


# ═══════════════════════════════════════════════════════════════════════════ #
#  EDIT YOUR SETTINGS HERE
#  ---------------------------------------------------------------------------
#  Only DISCORD_TOKEN is a real secret and must come from the environment
#  (.env locally, a Railway Variable in production). Everything else below is
#  just Discord IDs / numbers — not secret — so they're hardcoded here. Change
#  them by editing this file and committing. (Setting an env var of the same
#  name still overrides any value here, if you ever want to.)
# ═══════════════════════════════════════════════════════════════════════════ #

# Core ----------------------------------------------------------------------- #
DISCORD_TOKEN: str = _str("DISCORD_TOKEN", required=True)   # SECRET — env only, never commit
GUILD_ID: int = _int("GUILD_ID", 1506442466738438234)
DATABASE_PATH: str = _str("DATABASE_PATH", "data/store.db")
# Text prefix for owner-only prefix commands (mentioning the bot also works).
COMMAND_PREFIX: str = _str("COMMAND_PREFIX", "!")
# Your Discord user ID — excluded from the public credit leaderboard (you can
# grant yourself unlimited credits, so you'd always sit on top).
OWNER_USER_ID: int = _int("OWNER_USER_ID", 904399821580943420)

# Branding: logo shown on panels + link to the web store ('' hides the button).
LOGO_PATH: str = _str("LOGO_PATH", "Assets/NVS-logo.orig.png")
# Separate logo for the honeypot tracking embed only; every other panel keeps LOGO_PATH.
HONEYPOT_LOGO_PATH: str = _str("HONEYPOT_LOGO_PATH", "Assets/NightVoid.png")
WEB_STORE_URL: str | None = _str("WEB_STORE_URL", "https://night-void-store.web.app/")

# System 1 — Tickets --------------------------------------------------------- #
TICKET_USE_THREADS: bool = _bool("TICKET_USE_THREADS", False)
TICKET_PANEL_CHANNEL_ID: int | None = _int("TICKET_PANEL_CHANNEL_ID")
TICKET_CATEGORY_ID: int | None = _int("TICKET_CATEGORY_ID", 1526680229505011832)
TICKET_STAFF_ROLE_ID: int = _int("TICKET_STAFF_ROLE_ID", 1514779833253630083)
TICKET_LOG_CHANNEL_ID: int | None = _int("TICKET_LOG_CHANNEL_ID", 1506774357207285790)
# Auto-close: a ticket with no message for AUTOCLOSE_HOURS gets a warning ping;
# if it stays silent for GRACE_HOURS more, it is closed. 0 disables the system.
# Defaults: warn at 2h of silence, close at 5h total (2h + 3h grace).
TICKET_AUTOCLOSE_HOURS: int = _int("TICKET_AUTOCLOSE_HOURS", 2)
TICKET_AUTOCLOSE_GRACE_HOURS: int = _int("TICKET_AUTOCLOSE_GRACE_HOURS", 3)

# System 2 — Code redemption ------------------------------------------------- #
CODE_RATE_LIMIT_MAX: int = _int("CODE_RATE_LIMIT_MAX", 3)
CODE_RATE_LIMIT_WINDOW: int = _int("CODE_RATE_LIMIT_WINDOW", 60)
# Redeemed codes older than this many days are auto-deleted (housekeeping) so the
# /list-codes output doesn't grow without bound. Unused codes are never removed.
CODE_REDEEMED_RETENTION_DAYS: int = _int("CODE_REDEEMED_RETENTION_DAYS", 4)

# System 3 — Reviews --------------------------------------------------------- #
# VERIFIED_BUYER_ROLE_ID is no longer used (the buyer gate was removed); kept
# for compatibility. Set it only if you re-add a role requirement.
VERIFIED_BUYER_ROLE_ID: int = _int("VERIFIED_BUYER_ROLE_ID", 0)
REVIEWS_CHANNEL_ID: int = _int("REVIEWS_CHANNEL_ID", 1521288084589641871)
# No longer used: the review DM buttons are now persistent (they survive bot
# restarts instead of expiring). Kept for compatibility.
REVIEW_DM_TIMEOUT: int = _int("REVIEW_DM_TIMEOUT", 86400)

# System 4 — Announcements --------------------------------------------------- #
ADMIN_ROLE_ID: int = _int("ADMIN_ROLE_ID", 1506758959179108412)
ANNOUNCEMENTS_CHANNEL_ID: int = _int("ANNOUNCEMENTS_CHANNEL_ID", 1506741646169997452)

# --------------------------------------------------------------------------- #
# System 5 — Credits (message + voice)
# --------------------------------------------------------------------------- #
CREDIT_MSG_MIN: int = _int("CREDIT_MSG_MIN", 8)
CREDIT_MSG_MAX: int = _int("CREDIT_MSG_MAX", 40)
CREDIT_MSG_COOLDOWN: int = _int("CREDIT_MSG_COOLDOWN", 210)
CREDIT_MSG_MAX_PER_MIN: int = _int("CREDIT_MSG_MAX_PER_MIN", 4)
CREDIT_IGNORED_CHANNELS: list[int] = _int_list("CREDIT_IGNORED_CHANNELS")
# Exponential decay factor for the weighted random award. Larger => the high
# end of the range becomes exponentially rarer.
CREDIT_WEIGHT_LAMBDA: float = _float("CREDIT_WEIGHT_LAMBDA", 0.09)

CREDIT_VOICE_AMOUNT: int = _int("CREDIT_VOICE_AMOUNT", 3)
CREDIT_VOICE_INTERVAL: int = _int("CREDIT_VOICE_INTERVAL", 180)
AFK_CHANNEL_ID: int | None = _int("AFK_CHANNEL_ID", 1506746120091734096)

# --------------------------------------------------------------------------- #
# System 5b — Credit transfers (/pay)
# --------------------------------------------------------------------------- #
# Fee (percent) burned on every transfer — recipient gets amount minus the fee.
PAY_FEE_PERCENT: int = _int("PAY_FEE_PERCENT", 5)
PAY_MIN_AMOUNT: int = _int("PAY_MIN_AMOUNT", 100)
# Max total a user may SEND per rolling 24h (anti alt-account funnelling).
PAY_DAILY_CAP: int = _int("PAY_DAILY_CAP", 50_000)

# --------------------------------------------------------------------------- #
# System 6 — Role shop
# --------------------------------------------------------------------------- #
SHOP_CONFIG_PATH: str = _str("SHOP_CONFIG_PATH", "shop_config.json")


def load_shop_items() -> list[dict]:
    """Load the role-shop catalogue from the JSON config file.

    Returns a list of {role_id:int, name:str, cost:int, description:str}.

    NOTE: this is only the *initial seed*. On first boot the shop copies these
    into the `shop_items` DB table (see cogs/shop.py), and after that the
    database is the source of truth — manage the live shop with /shop-add and
    /shop-remove. Editing this file later has no effect on a running store.
    """
    if not os.path.exists(SHOP_CONFIG_PATH):
        return []
    with open(SHOP_CONFIG_PATH, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    items: list[dict] = []
    for entry in raw:
        items.append(
            {
                "role_id": int(entry["role_id"]),
                "name": str(entry["name"]),
                "cost": int(entry["cost"]),
                "description": str(entry.get("description", "")),
            }
        )
    return items


SHOP_ITEMS: list[dict] = load_shop_items()

# --------------------------------------------------------------------------- #
# System 7 — Minor services
# --------------------------------------------------------------------------- #
MINOR_SERVICES_COST: int = _int("MINOR_SERVICES_COST", 200_000)
STAFF_SERVICES_CHANNEL_ID: int = _int("STAFF_SERVICES_CHANNEL_ID", 1521206296962007182)

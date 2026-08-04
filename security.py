"""Detection logic for the anti-raid / anti-spam protections.

Deliberately free of Discord calls: every decision here is a pure function over
plain values, so the rules that can ban someone are testable without a network.
The cog does the talking; this module decides.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone

# What tripped a protection.
REASON_HONEYPOT = "honeypot"
REASON_NEW_ACCOUNT = "new_account"
REASON_RAID = "raid"
REASON_SCAM = "scam"

REASON_LABELS = {
    REASON_HONEYPOT: "Posted in the honeypot channel",
    REASON_NEW_ACCOUNT: "Brand-new account joined",
    REASON_RAID: "Mass joins detected",
    REASON_SCAM: "Suspected scam link",
}

# What to do about it.
ACTION_BAN = "ban"
ACTION_KICK = "kick"
ACTION_TIMEOUT = "timeout"
ACTION_ALERT = "alert"
ACTION_NONE = "none"

DEFAULT_ACTION = ACTION_BAN
DEFAULT_MIN_ACCOUNT_AGE_DAYS = 7
DEFAULT_RAID_JOIN_COUNT = 10
DEFAULT_RAID_WINDOW_SECONDS = 60


@dataclass(frozen=True)
class Decision:
    """What happened, and what should be done about it."""

    reason: str
    detail: str
    intended: str
    watch_only: bool

    @property
    def action(self) -> str:
        """The action to actually take now — nothing while in watch mode."""
        return ACTION_NONE if self.watch_only else self.intended

    @property
    def label(self) -> str:
        return REASON_LABELS.get(self.reason, self.reason)


# --------------------------------------------------------------------------
# who is never touched
# --------------------------------------------------------------------------

def is_exempt(member, me_id: int | None = None) -> bool:
    """Staff and the bot itself are never actioned, whatever they do.

    Checked before any protection runs. Without this, a misconfigured honeypot
    could ban the admin who set it up, which is exactly the failure that makes
    people distrust automated moderation.
    """
    if me_id is not None and member.id == me_id:
        return True

    guild = getattr(member, "guild", None)
    if guild is not None and getattr(guild, "owner_id", None) == member.id:
        return True

    perms = getattr(member, "guild_permissions", None)
    if perms is None:
        return False
    return bool(
        getattr(perms, "administrator", False)
        or getattr(perms, "manage_guild", False)
        or getattr(perms, "ban_members", False)
        or getattr(perms, "manage_messages", False)
    )


# --------------------------------------------------------------------------
# account age
# --------------------------------------------------------------------------

def account_age_days(created_at: datetime | None) -> float | None:
    if created_at is None:
        return None
    return (datetime.now(timezone.utc) - created_at).total_seconds() / 86400


def check_account_age(created_at: datetime | None, min_days: int) -> str | None:
    """Detail string if the account is younger than allowed, else None."""
    if not min_days:
        return None
    age = account_age_days(created_at)
    if age is None or age >= min_days:
        return None
    if age < 1:
        readable = f"{int(age * 24)} hours old"
    else:
        readable = f"{int(age)} days old"
    return f"Account is {readable} (limit is {min_days} days)"


# --------------------------------------------------------------------------
# raid detection
# --------------------------------------------------------------------------

class RaidTracker:
    """Rolling count of joins per guild.

    In memory on purpose — the window is a minute and the host restarts the bot
    hours apart, so persisting it would buy nothing.
    """

    def __init__(self) -> None:
        self._joins: dict[int, deque[float]] = defaultdict(deque)

    def record(self, guild_id: int, window_seconds: int, when: float | None = None) -> int:
        now = time.time() if when is None else when
        joins = self._joins[guild_id]
        joins.append(now)

        cutoff = now - window_seconds
        while joins and joins[0] < cutoff:
            joins.popleft()
        return len(joins)

    def reset(self, guild_id: int) -> None:
        self._joins.pop(guild_id, None)


# --------------------------------------------------------------------------
# scam links
# --------------------------------------------------------------------------

# Domains that legitimately contain a brand name. Anything here is never flagged.
ALLOWED_DOMAINS = {
    "discord.com", "discord.gg", "discord.gift", "discord.media", "discord.dev",
    "discordapp.com", "discordapp.net", "discordstatus.com", "discordjs.guide",
    "discord.co", "discordresources.com",
    "steamcommunity.com", "steampowered.com", "steamstatic.com",
    "minecraft.net", "mojang.com",
    # Ordinary words that happen to sit one letter from a brand name.
    "discard.com", "discards.com",
}

# Long, distinctive names — safe to match anywhere inside a domain.
IMPERSONATED_BRANDS = ("discord", "steamcommunity", "steampowered")

# Short words that must appear as a whole token ("discord-nitro", "nitro.gg") and
# not merely inside a longer one, or nitrous.io gets someone banned.
TOKEN_BRANDS = ("nitro",)

# Look-alike tricks: 0 for o, 1/l for i, and so on.
CONFUSABLES = str.maketrans({
    "0": "o", "1": "i", "l": "i", "3": "e", "4": "a",
    "5": "s", "7": "t", "$": "s", "@": "a", "!": "i", "|": "i",
})

# A TLD list rather than "any dotted word", so ordinary prose like "e.g." and
# "1.5 blocks" is never mistaken for a link.
_TLDS = (
    "com|net|org|gg|gift|io|co|me|ru|su|site|xyz|info|top|shop|store|online|link|"
    "click|fun|icu|cyou|monster|tk|ml|ga|cf|pw|app|dev|guide|live|life|vip|cc|biz"
)
DOMAIN_RE = re.compile(
    rf"(?:https?://)?(?:www\.)?((?:[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?\.)+(?:{_TLDS}))\b",
    re.IGNORECASE,
)


def extract_domains(text: str) -> list[str]:
    return [match.group(1).lower().rstrip(".") for match in DOMAIN_RE.finditer(text or "")]


def normalise_domain(domain: str) -> str:
    """Strip the tricks a look-alike domain uses to read like the real thing."""
    return domain.translate(CONFUSABLES).replace("-", "").replace("_", "")


def osa_distance(a: str, b: str) -> int:
    """Edit distance counting a swap of two neighbouring letters as one change.

    Plain Levenshtein scores `discrod` against `discord` as 2 — the same as two
    unrelated edits — which is why transposed look-alikes slip past a distance-1
    check. This counts that swap as the single trick it actually is.
    """
    if a == b:
        return 0
    previous = list(range(len(b) + 1))
    before_previous = [0] * (len(b) + 1)

    for i, ch_a in enumerate(a, start=1):
        current = [i] + [0] * len(b)
        for j, ch_b in enumerate(b, start=1):
            cost = 0 if ch_a == ch_b else 1
            current[j] = min(
                previous[j] + 1,        # deletion
                current[j - 1] + 1,     # insertion
                previous[j - 1] + cost,  # substitution
            )
            if i > 1 and j > 1 and ch_a == b[j - 2] and a[i - 2] == ch_b:
                current[j] = min(current[j], before_previous[j - 2] + cost)
        before_previous, previous = previous, current

    return previous[len(b)]


# Only brands long enough that a near-miss is unmistakably deliberate. Short words
# would collide with ordinary domains — "intro" is one swap from "nitro", and
# flagging intro.com would ban someone for sharing a perfectly normal link.
FUZZY_BRANDS = tuple(brand for brand in IMPERSONATED_BRANDS if len(brand) >= 7)


def find_scam(content: str) -> str | None:
    """Detail string if the message looks like a phishing link, else None.

    Works by brand impersonation rather than a blocklist of known-bad domains:
    a domain that reads like `discord` but isn't one of Discord's real domains is
    the actual signal, and it keeps working when the scammers register a new one.

    False positives here would ban real members, which is precisely why watch mode
    is the default — run it, read the log, and add anything wrong to ALLOWED_DOMAINS.
    """
    if not content:
        return None

    for domain in extract_domains(content):
        if domain in ALLOWED_DOMAINS:
            continue
        # Subdomains of an allowed domain are fine too (cdn.discord.com).
        if any(domain.endswith("." + allowed) for allowed in ALLOWED_DOMAINS):
            continue

        normalised = normalise_domain(domain)
        for brand in IMPERSONATED_BRANDS:
            if brand in normalised:
                return f"`{domain}` looks like a fake **{brand}** link"

        tokens = re.split(r"[.\-_]", domain.translate(CONFUSABLES))
        for brand in TOKEN_BRANDS:
            if brand in tokens:
                return f"`{domain}` looks like a fake **{brand}** link"

        # Near-misses: discrod.gg, dsicord.com — one swap or typo from the real name.
        for label in normalised.split(".")[:-1]:
            for brand in FUZZY_BRANDS:
                if abs(len(label) - len(brand)) <= 1 and osa_distance(label, brand) <= 1:
                    return f"`{domain}` is one character off **{brand}**"

    return None


# --------------------------------------------------------------------------
# putting it together
# --------------------------------------------------------------------------

def decide(reason: str, detail: str, watch_only: bool, action: str | None = None) -> Decision:
    return Decision(
        reason=reason,
        detail=detail,
        intended=action or DEFAULT_ACTION,
        watch_only=bool(watch_only),
    )

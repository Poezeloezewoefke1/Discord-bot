"""Application forms — the rules, with no Discord calls in them.

Replacing an application bot is mostly a question of *shape*: a position has a
handful of questions, somebody fills them in once, and staff say yes or no. The
awkward parts are all limits and edge cases — Discord only allows five inputs in
a form, labels are capped at 45 characters, and "can this person apply right
now?" has four separate reasons to say no. Those live here so they can be tested
without a gateway connection.

The cog does the talking to Discord.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

# Application lifecycle. A decision is final: there is no "un-deny", because the
# applicant has already been told. Staff who change their mind delete it and ask
# the person to apply again.
STATUS_PENDING = "pending"
STATUS_ACCEPTED = "accepted"
STATUS_DENIED = "denied"
STATUS_WITHDRAWN = "withdrawn"

DECIDED = (STATUS_ACCEPTED, STATUS_DENIED)

# Discord's limits, which shape the whole feature:
MAX_QUESTIONS = 5          # a modal takes at most five inputs, full stop
MAX_QUESTION_LABEL = 45    # TextInput label
MAX_PLACEHOLDER = 100      # TextInput placeholder
MAX_ANSWER = 1000          # what we let people type; the embed field caps at 1024
MAX_BUTTON_LABEL = 80
MAX_KEY = 20

# How long after being turned down before somebody may apply again. Without it a
# denial is followed by a fresh application thirty seconds later, forever.
DEFAULT_COOLDOWN_DAYS = 7

SHORT = False
LONG = True


def q(label: str, long: bool = SHORT, placeholder: str | None = None) -> dict:
    return {"label": label, "long": long, "placeholder": placeholder, "required": True}


# Used when somebody adds a position without saying what to ask. Better than an
# empty form, and they can replace it with /apply-form later.
GENERIC_QUESTIONS = [
    q("Your Minecraft username"),
    q("How old are you, and what timezone?", placeholder="e.g. 16, GMT+1"),
    q("Why are you a good fit for this?", LONG),
]

# The positions a Minecraft SMP actually recruits for. These are created on first
# setup so the server has something working immediately; every one of them can be
# edited or deleted with /apply-form.
# Every key here must equal slug(label). /apply-form derives the key from the
# label, so a mismatch would make editing "Script writer" create a second
# position instead of changing this one. A test pins them together.
KEY_BUILDER = "builder"
KEY_SCRIPTER = "script-writer"
KEY_STAFF = "staff-helper"

DEFAULT_FORMS = (
    {
        "key": KEY_BUILDER,
        "label": "Builder",
        "emoji": "🔨",
        "blurb": "Build the things the script writers dream up.",
        "questions": [
            q("Your Minecraft username", placeholder="Exactly as it appears in-game"),
            q("How old are you, and what timezone?", placeholder="e.g. 16, GMT+1"),
            q("What have you built before?", LONG, "Styles you're good at, biggest thing you've built"),
            q("Screenshots or a portfolio link", LONG, "Imgur, YouTube, Planet Minecraft — anything we can look at"),
            q("Why do you want to build for us?", LONG),
        ],
    },
    {
        "key": KEY_SCRIPTER,
        "label": "Script writer",
        "emoji": "📜",
        "blurb": "Write the events and storylines the server runs on.",
        "questions": [
            q("Your Minecraft username"),
            q("How old are you, and what timezone?", placeholder="e.g. 16, GMT+1"),
            q("What sort of things do you like writing?", LONG),
            q("Give us one event idea, in a few lines", LONG, "Doesn't have to be polished — show us how you think"),
            q("How much time can you give per week?", placeholder="Be honest — we'd rather know"),
        ],
    },
    {
        "key": KEY_STAFF,
        "label": "Staff / helper",
        "emoji": "🛡️",
        "blurb": "Keep chat friendly and help people out.",
        "questions": [
            q("Your Minecraft username"),
            q("How old are you, and what timezone?", placeholder="e.g. 16, GMT+1"),
            q("Have you been staff on a server before?", LONG, "Which server, what you did, why you left"),
            q("Someone is nasty in chat — what do you do?", LONG),
            q("How many hours a week can you be online?"),
        ],
    },
)


# --------------------------------------------------------------------------
# keys
# --------------------------------------------------------------------------

def slug(label: str) -> str:
    """A short id safe to embed in a button's custom_id.

    The custom_id is parsed back with a regex, so the key may only contain
    characters that regex allows — anything else and a button silently stops
    resolving after a restart.
    """
    cleaned = re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")
    return cleaned[:MAX_KEY].strip("-") or "form"


KEY_PATTERN = re.compile(r"^[a-z0-9-]{1,%d}$" % MAX_KEY)


def is_valid_key(key: str) -> bool:
    return bool(KEY_PATTERN.match(key or ""))


# --------------------------------------------------------------------------
# questions
# --------------------------------------------------------------------------

def normalise_questions(raw) -> list[dict]:
    """Accept plain strings or full dicts, return the canonical shape.

    /apply-form gives us five strings; DEFAULT_FORMS gives dicts with a style and
    a placeholder. Both end up here so the modal builder only has one shape to
    deal with.
    """
    # A dict here would iterate its *keys* and quietly turn {"a": ...} into a
    # question called "a", so anything that isn't a sequence is rejected outright.
    if isinstance(raw, (str, bytes, dict)):
        return []

    questions: list[dict] = []
    for item in raw or ():
        if isinstance(item, str):
            item = {"label": item}
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        if not label:
            continue
        placeholder = item.get("placeholder")
        questions.append(
            {
                "label": label[:MAX_QUESTION_LABEL],
                "long": bool(item.get("long", False)),
                "placeholder": (str(placeholder)[:MAX_PLACEHOLDER] if placeholder else None),
                "required": bool(item.get("required", True)),
            }
        )
    return questions


def validate_questions(raw) -> list[str]:
    """Problems with a proposed set of questions, phrased for whoever typed them.

    Returned rather than raised: /apply-form shows them all at once instead of
    making an admin discover them one at a time.
    """
    problems = []
    items = list(raw or ())
    if not items:
        problems.append("An application needs at least one question.")
    if len(items) > MAX_QUESTIONS:
        problems.append(
            f"Discord only allows {MAX_QUESTIONS} questions in one form — you gave {len(items)}."
        )
    for index, item in enumerate(items, start=1):
        label = item if isinstance(item, str) else str((item or {}).get("label") or "")
        label = label.strip()
        if not label:
            problems.append(f"Question {index} is empty.")
        elif len(label) > MAX_QUESTION_LABEL:
            problems.append(
                f"Question {index} is {len(label)} characters — Discord caps a question at "
                f"{MAX_QUESTION_LABEL}. Shorten it: “{label[:40]}…”"
            )
    return problems


def questions_to_json(raw) -> str:
    return json.dumps(normalise_questions(raw), ensure_ascii=False)


def questions_from_json(text: str | None) -> list[dict]:
    """Never raises. A corrupt row must not take the whole panel down with it."""
    try:
        return normalise_questions(json.loads(text or "[]"))
    except (ValueError, TypeError):
        return []


# --------------------------------------------------------------------------
# answers
# --------------------------------------------------------------------------

def answers_to_json(pairs) -> str:
    """`pairs` is (question, answer) — stored together so editing a form later
    doesn't rewrite what somebody already submitted."""
    return json.dumps(
        [{"q": str(question), "a": str(answer)} for question, answer in pairs],
        ensure_ascii=False,
    )


def answers_from_json(text: str | None) -> list[tuple[str, str]]:
    try:
        loaded = json.loads(text or "[]")
    except (ValueError, TypeError):
        return []
    out = []
    for item in loaded if isinstance(loaded, list) else ():
        if isinstance(item, dict):
            out.append((str(item.get("q", "")), str(item.get("a", ""))))
    return out


def summarise(answers, limit: int = 200) -> str:
    """One line for a list view — the first answer is almost always the username."""
    for _, answer in answers:
        text = " ".join(answer.split())
        if text:
            return text[:limit]
    return "—"


# --------------------------------------------------------------------------
# may this person apply?
# --------------------------------------------------------------------------

def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def reapply_at(decided_at: str | None, cooldown_days: int) -> datetime | None:
    """When a denied applicant may try again, or None if they already may."""
    decided = _parse(decided_at)
    if decided is None or cooldown_days <= 0:
        return None
    return decided + timedelta(days=cooldown_days)


def why_cannot_apply(
    form,
    *,
    pending=None,
    last_denial=None,
    cooldown_days: int = DEFAULT_COOLDOWN_DAYS,
    now: datetime | None = None,
    already_has_role: bool = False,
) -> str | None:
    """The reason this person can't apply for this position, or None if they can.

    Every refusal is checked here rather than at the point of clicking, so the
    button, the slash command and the tests all agree on the rules.
    """
    now = now or datetime.now(timezone.utc)

    if form is None:
        return "That position doesn't exist any more."
    if not form["is_open"]:
        return f"**{form['label']}** applications are closed right now. Watch for them reopening."
    if already_has_role:
        return f"You already have the **{form['label']}** role — no need to apply again."
    if pending is not None:
        return (
            f"You already have a **{form['label']}** application waiting for a decision.\n"
            "Staff will get to it — applying twice doesn't make it faster."
        )

    if last_denial is not None:
        available = reapply_at(last_denial["decided_at"], cooldown_days)
        if available is not None and available > now:
            stamp = int(available.timestamp())
            return (
                f"Your last **{form['label']}** application wasn't accepted. "
                f"You can apply again <t:{stamp}:R>."
            )
    return None


# --------------------------------------------------------------------------
# presentation helpers
# --------------------------------------------------------------------------

def status_face(status: str) -> tuple[str, str]:
    """(emoji, wording) for a status — used in the card, the list and the DM."""
    return {
        STATUS_PENDING: ("🕓", "Waiting for a decision"),
        STATUS_ACCEPTED: ("✅", "Accepted"),
        STATUS_DENIED: ("❌", "Not accepted"),
        STATUS_WITHDRAWN: ("↩️", "Withdrawn"),
    }.get(status, ("•", status))


def looks_like_emoji(value: str | None) -> bool:
    """Whether this is safe to hand to Discord as a button emoji.

    Admins type this field by hand. A plain word like `builder` is accepted by
    discord.py and then rejected by the API when the panel is sent, which loses
    the whole panel — so anything that isn't a custom emoji or a non-ASCII
    character is dropped here instead.
    """
    text = (value or "").strip()
    if not text:
        return False
    if re.fullmatch(r"<a?:[A-Za-z0-9_]+:\d+>", text):  # <:name:id> / <a:name:id>
        return True
    return len(text) <= 8 and any(ord(char) > 127 for char in text)


def form_label(form, fallback: str = "Application") -> str:
    if form is None:
        return fallback
    return (form["label"] or fallback)[:MAX_BUTTON_LABEL]

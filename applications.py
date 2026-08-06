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


# What separates a question that sorts applicants from one that doesn't:
#
#   * Ask for evidence, not opinions. "Show us three builds" tells you something.
#     "Are you a good builder?" tells you nothing — nobody says no.
#   * Ask about what they did, not what they would do. Past behaviour is hard to
#     invent on the spot; intentions are free.
#   * Ask something where a bad answer is visibly bad. "Would you abuse staff
#     perms?" has one obvious answer, so it sorts nobody. "A friend of yours
#     breaks a rule — what do you do?" genuinely splits the field.
#   * Ask about this server's actual work. Builders here hand off schematics, so
#     whether they know Litematica matters more than their favourite block.
#   * Be specific about availability. "Are you active?" gets "yes". Hours and a
#     timezone get an answer you can plan around.
#
# The label is capped at 45 characters, which is too short to carry nuance — so
# the nuance lives in the placeholder, which is 100 and shows in the box as grey
# hint text. That is where an applicant is told what a good answer looks like.

# Used when somebody adds a position without saying what to ask. Better than an
# empty form, and they can replace it with /apply-form later.
GENERIC_QUESTIONS = [
    q("Your Minecraft username", placeholder="Exactly as it appears in-game"),
    q("Age, timezone, hours a week you can give", placeholder="e.g. 16, GMT+1, about 6 hours — usually evenings"),
    q(
        "What have you actually done before?",
        LONG,
        "Real examples with links if you have them. What you did, not what you'd like to do.",
    ),
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
KEY_EDITOR = "video-editor"
KEY_PROMOTOR = "promotor"

DEFAULT_FORMS = (
    {
        "key": KEY_BUILDER,
        "label": "Builder",
        "emoji": "🔨",
        "blurb": "Build what the script writers describe. Bring pictures — we go on what you show us, not what you say.",
        "questions": [
            q("Your Minecraft username", placeholder="Exactly as it appears in-game"),
            q(
                "Show us your three best builds",
                LONG,
                "Screenshots, renders, a video — anything we can look at. No links, no application.",
            ),
            q(
                "Biggest thing you finished, and how long",
                LONG,
                "Finished, not started. Half-built projects are the thing we run into most.",
            ),
            q(
                "How well do you know WorldEdit + Litematica?",
                LONG,
                "We pass builds between people as schematics, so this matters. Be honest, we can teach.",
            ),
            q(
                "Age, timezone, hours a week you can build",
                placeholder="e.g. 16, GMT+1, around 6 hours — mostly evenings",
            ),
        ],
    },
    {
        "key": KEY_SCRIPTER,
        "label": "Script writer",
        "emoji": "📜",
        "blurb": "Describe the builds and write the events. Question 2 is the actual job — that's what we read first.",
        "questions": [
            q("Your Minecraft username", placeholder="Exactly as it appears in-game"),
            q(
                "Describe a build a builder could start today",
                LONG,
                "Size, theme, where it goes, what it's for. Enough that nobody has to ask you anything.",
            ),
            q(
                "One event idea for the server, in 3-4 lines",
                LONG,
                "What happens, what players actually do, and why they'd turn up for it.",
            ),
            q(
                "Anything you've written before?",
                LONG,
                "Lore, stories, YouTube scripts, D&D campaigns — links if you have them. 'No' is fine.",
            ),
            q(
                "Age, timezone, hours a week you can give",
                placeholder="e.g. 16, GMT+1, around 4 hours — mostly weekends",
            ),
        ],
    },
    {
        "key": KEY_STAFF,
        "label": "Staff / helper",
        "emoji": "🛡️",
        "blurb": "Keep chat friendly and help people out. We're looking at how you handle people, not how long you've played.",
        "questions": [
            q("Your Minecraft username", placeholder="Exactly as it appears in-game"),
            q(
                "A friend of yours breaks a rule. What now?",
                LONG,
                "Be honest. This is the question we care about most, and 'it depends' is a real answer.",
            ),
            q(
                "Two players start arguing in chat — what now?",
                LONG,
                "Walk us through it, from the first message you'd send to how it ends.",
            ),
            q(
                "Been staff somewhere before? What happened?",
                LONG,
                "Which server, what you did, why you left. 'Never' is a perfectly good answer.",
            ),
            q(
                "Age, timezone, when you're usually online",
                placeholder="e.g. 16, GMT+1, most evenings and all weekend",
            ),
        ],
    },
    {
        "key": KEY_EDITOR,
        "label": "Video editor",
        "emoji": "🎬",
        "blurb": "Cut the SMP footage into videos and shorts. Links to your work matter more than anything else here.",
        "questions": [
            q("Your Minecraft username", placeholder="Exactly as it appears in-game"),
            q(
                "Links to 2-3 edits you've made",
                LONG,
                "Anything we can watch. An edit we can't see doesn't count — no links, no application.",
            ),
            q(
                "What do you edit with, and for how long?",
                placeholder="Premiere, DaVinci, CapCut, After Effects… and roughly how long you've used it",
            ),
            q(
                "Raw footage in — how long until it's done?",
                LONG,
                "For a 10-minute video. We plan uploads around this, so a realistic answer helps you.",
            ),
            q(
                "Age, timezone, hours a week you can edit",
                placeholder="e.g. 16, GMT+1, around 8 hours — mostly evenings",
            ),
        ],
    },
    {
        "key": KEY_PROMOTOR,
        "label": "Promotor",
        "emoji": "📣",
        "blurb": "Get more people onto the server. A real plan beats a big following — tell us what you'd actually do.",
        "questions": [
            q("Your Minecraft username", placeholder="Exactly as it appears in-game"),
            q(
                "Your accounts, with follower counts",
                LONG,
                "TikTok, YouTube, Instagram — links and rough numbers. Small is fine, made up is not.",
            ),
            q(
                "What would your first week look like?",
                LONG,
                "Something specific we could actually check up on. 'I'll post about it' isn't a plan.",
            ),
            q(
                "How do you promote us without it being spam?",
                LONG,
                "Mass-DMs and posting in other servers' chats get us a bad name. What do you do instead?",
            ),
            q(
                "Age, timezone, hours a week you can give",
                placeholder="e.g. 16, GMT+1, around 5 hours — mostly evenings",
            ),
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


# --------------------------------------------------------------------------
# matching a position to a role that already exists on the server
# --------------------------------------------------------------------------

# Names to look for, best first. Servers name these roles differently, and
# asking an admin to type five role names when the roles are sitting right there
# is asking them to do the bot's job.
#
# Note what is deliberately *absent* from the staff list: "staff", "moderator"
# and "admin". Somebody accepted for a helper position should land on the
# bottom rung, not on whatever role happens to contain the word staff.
ROLE_HINTS = {
    KEY_BUILDER: ("builder", "builders", "build team"),
    KEY_SCRIPTER: ("script writer", "script writers", "scriptwriter", "scripter", "scripters"),
    KEY_STAFF: ("trainee staff", "trainee", "trial staff", "helper", "helpers", "trainee mod"),
    KEY_EDITOR: ("editor", "editors", "video editor", "video editors", "edit team"),
    KEY_PROMOTOR: ("promotor", "promotors", "promoter", "promoters", "promo team"),
}


def normalise_role_name(name: str) -> str:
    """Role names are full of decoration — 『🔨』Builder, ・Builder・, ✦ Builder ✦."""
    stripped = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    return re.sub(r"\s+", " ", stripped).strip()


def guess_role_id(key: str, roles) -> int | None:
    """Which existing role a position should hand out, or None if nothing fits.

    `roles` is a sequence of (id, name, assignable) — assignable is False for
    roles that must never be handed out from a *guess*, however well the name
    matches. An admin naming a role explicitly is a different thing entirely and
    doesn't come through here.
    """
    candidates = [
        (role_id, normalise_role_name(name))
        for role_id, name, assignable in roles
        if assignable and normalise_role_name(name)
    ]

    hints = ROLE_HINTS.get(key, ())
    for hint in hints:
        for role_id, name in candidates:
            if name == hint:
                return role_id

    # Nothing matched exactly. Fall back to a name that contains a hint, shortest
    # first — otherwise "Script Writer" loses to "Lead Script Writer", and
    # handing a new applicant the lead role is a worse mistake than no role.
    for hint in hints:
        matches = sorted(
            ((role_id, name) for role_id, name in candidates if hint in name),
            key=lambda pair: len(pair[1]),
        )
        if matches:
            return matches[0][0]
    return None


# A leading emoji on a label: a custom `<:name:id>`, or a run of characters from
# the emoji blocks (including variation selectors, zero-width joiners and skin
# tones, which are what make one emoji several codepoints).
#
# Latin letters and accented ones are deliberately outside every range here, so
# a Dutch or French position name keeps its first letter.
LEADING_EMOJI = re.compile(
    r"^\s*("
    r"<a?:[A-Za-z0-9_]+:\d+>"
    r"|[\U0001F000-\U0001FAFF←-⇿⌀-⏿①-➿"
    r"⬀-⯿️‍⃣]+"
    r")\s*"
)


def split_label_emoji(label: str) -> tuple[str, str | None]:
    """`"🎬 Video editor"` → `("Video editor", "🎬")`.

    Typing the emoji into the name is the obvious thing to do, and without this
    it ends up in both the button's icon *and* its text, so the panel shows
    every emoji twice. Returns the label unchanged when it doesn't start with
    one, and never returns an empty label.
    """
    text = (label or "").strip()
    match = LEADING_EMOJI.match(text)
    if not match:
        return text, None

    rest = text[match.end():].strip()
    if not rest:
        return text, None  # the name is *only* an emoji; leave it be

    return rest, match.group(1).strip()


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

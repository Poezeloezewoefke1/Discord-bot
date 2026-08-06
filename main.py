"""Start the bot.

    python main.py

Most hosting panels default their start command to `main.py`, so this exists to
be the file they look for. The bot itself is in `bot.py` and both entry points
run exactly the same code.

The only thing this adds is what happens when the imports fail. On a panel you
get a log window and nothing else, and a bare ModuleNotFoundError traceback
scrolling past is a poor way to be told "the dependencies weren't installed" —
especially since the usual cause is a Python version the library can't run on,
which the traceback doesn't mention at all.
"""

from __future__ import annotations

import sys

MINIMUM_PYTHON = (3, 11)


def _die(*lines: str) -> None:
    print("\n".join(("", *lines, "")), file=sys.stderr, flush=True)
    raise SystemExit(1)


if sys.version_info < MINIMUM_PYTHON:
    _die(
        f"This bot needs Python {'.'.join(map(str, MINIMUM_PYTHON))} or newer.",
        f"This is Python {'.'.join(map(str, sys.version_info[:3]))}.",
        "On a hosting panel, change the Python version in the deployment settings.",
    )

try:
    from bot import run
except ModuleNotFoundError as missing:
    name = getattr(missing, "name", "") or ""
    if name == "audioop":
        # Removed from the standard library in 3.13; discord.py imports it at
        # import time, so this is fatal rather than an inconvenience.
        _die(
            "discord.py needs the `audioop` module, which Python 3.13 removed.",
            "Fix it with:    pip install audioop-lts",
            "It's already in requirements.txt — so this really means the install",
            "step didn't run. Re-run:    pip install -r requirements.txt",
        )
    _die(
        f"A required package is missing: {name or missing}",
        "",
        "Install everything with:    pip install -r requirements.txt",
        "On a hosting panel, check the install/build command is set to that.",
    )


if __name__ == "__main__":
    run()

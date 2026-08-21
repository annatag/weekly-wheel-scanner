"""Deliver alerts somewhere you will actually see them.

A monitor that writes to a log file you have to remember to open is barely
better than not running one: the positions this tool was built for drifted
unwatched for two weeks precisely because nothing interrupted anyone.

Two channels, both optional and independent:

* a macOS notification banner, which needs nothing installed but only reaches
  you at the machine;
* an ntfy.sh push, which reaches a phone and needs no account — just a topic
  name you choose.

Delivery never raises. A monitor that crashes because a notification server
was briefly unreachable would lose the alert it was trying to deliver, which
is the opposite of the point.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

from .netio import ssl_context

NTFY_TOPIC_ENV = "WHEELSCAN_NTFY_TOPIC"
NTFY_SERVER_ENV = "WHEELSCAN_NTFY_SERVER"
DEFAULT_NTFY_SERVER = "https://ntfy.sh"

# ntfy priorities: 1 min .. 5 max. URGENT maps to 4 so it breaks through a
# phone's do-not-disturb only when something is genuinely in the money.
PRIORITY = {"URGENT": 4, "WARN": 3, "INFO": 2}


@dataclass
class NotifyConfig:
    banner: bool = True
    push: bool = True
    topic: str | None = None
    server: str = DEFAULT_NTFY_SERVER

    @classmethod
    def from_environment(cls, *, banner: bool = True, push: bool = True) -> "NotifyConfig":
        """Read the ntfy topic from the environment, then the Keychain.

        The topic is effectively a password — anyone who knows it can read
        your alerts — so it is stored the same way as the API keys rather
        than sitting in a plist or a shell profile.
        """
        topic = os.environ.get(NTFY_TOPIC_ENV)
        if not topic:
            try:
                from . import secrets as keychain

                topic = keychain.get(NTFY_TOPIC_ENV)
            except Exception:
                topic = None
        return cls(
            banner=banner,
            push=push,
            topic=topic,
            server=os.environ.get(NTFY_SERVER_ENV, DEFAULT_NTFY_SERVER).rstrip("/"),
        )


def banner_available() -> bool:
    return sys.platform == "darwin" and shutil.which("osascript") is not None


def _escape(text: str) -> str:
    """AppleScript string literal escaping."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def send_banner(title: str, message: str, subtitle: str = "") -> bool:
    """Post a macOS Notification Center banner. False if it could not."""
    if not banner_available():
        return False
    script = (
        f'display notification "{_escape(message)}" '
        f'with title "{_escape(title)}"'
    )
    if subtitle:
        script += f' subtitle "{_escape(subtitle)}"'
    try:
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, timeout=15
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# HTTP headers are latin-1, but the alert text is full of typographic
# characters: an em dash in the title, "·" between fields, "σ" in the cushion.
# Any one of them raises UnicodeEncodeError inside urllib and loses the
# notification. The body is fine — it is UTF-8 encoded bytes — so only header
# values need folding.
_HEADER_SUBSTITUTIONS = {
    "—": "-", "–": "-", "·": "|", "σ": "sigma",
    "→": "->", "≥": ">=", "≤": "<=", "‘": "'",
    "’": "'", "“": '"', "”": '"', "…": "...",
}


def header_safe(text: str) -> str:
    """Fold a header value to characters urllib can actually encode."""
    for source, replacement in _HEADER_SUBSTITUTIONS.items():
        text = text.replace(source, replacement)
    return text.encode("ascii", "replace").decode("ascii")


def send_push(
    message: str,
    *,
    title: str,
    topic: str,
    server: str = DEFAULT_NTFY_SERVER,
    priority: int = 3,
    tags: str = "chart_with_downwards_trend",
) -> bool:
    """POST to ntfy. False on any failure; never raises."""
    if not topic:
        return False
    request = urllib.request.Request(
        f"{server}/{topic}",
        data=message.encode("utf-8"),
        headers={
            "Title": header_safe(title),
            "Priority": str(priority),
            "Tags": header_safe(tags),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=15, context=ssl_context()
        ) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


# macOS shows roughly this much of a notification body in Alerts style before
# truncating. Enough for four or five positions stated tersely.
BANNER_BUDGET = 240


def compact(body: str) -> str:
    """Squeeze the summary into what a notification will actually display.

    Each line already repeats the position label that its message also names
    ("C $136P: C $136P is 4.2% in the money"), which wastes half the budget.
    """
    lines = []
    for line in body.split("\n"):
        label, _, message = line.partition(": ")
        if message.startswith(label):
            message = message[len(label):].lstrip()
        lines.append(f"{label} {message}" if message else line)

    out = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > BANNER_BUDGET:
            out.append(f"(+{len(lines) - len(out)} more)")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


def summarise(findings_by_position: list[tuple[str, list]]) -> tuple[str, str, str]:
    """Condense alerts into (title, subtitle, body) short enough for a banner."""
    urgent = sum(
        1 for _, findings in findings_by_position
        for f in findings if f.level == "URGENT"
    )
    warn = sum(
        1 for _, findings in findings_by_position
        for f in findings if f.level == "WARN"
    )

    parts = []
    if urgent:
        parts.append(f"{urgent} urgent")
    if warn:
        parts.append(f"{warn} warning{'s' if warn != 1 else ''}")
    subtitle = " · ".join(parts) if parts else "review"

    lines = []
    for label, findings in findings_by_position:
        worst = next(
            (f for f in findings if f.level == "URGENT"),
            findings[0] if findings else None,
        )
        if worst is not None:
            lines.append(f"{label}: {worst.message}")

    body = "\n".join(lines[:6])
    if len(lines) > 6:
        body += f"\n(+{len(lines) - 6} more)"
    return "Wheel positions", subtitle, body or "See the log for detail."


def dispatch(
    findings_by_position: list[tuple[str, list]],
    config: NotifyConfig,
) -> dict[str, bool]:
    """Send the summary through every enabled channel. Returns what succeeded."""
    if not findings_by_position:
        return {}

    title, subtitle, body = summarise(findings_by_position)
    worst = max(
        (f.level for _, findings in findings_by_position for f in findings),
        key=lambda level: PRIORITY.get(level, 0),
        default="INFO",
    )

    sent: dict[str, bool] = {}
    if config.banner:
        # Send the whole summary, not just the first line. Clicking "Show" on
        # an osascript notification opens Script Editor, because that is the
        # application the notification belongs to - there is no view of the
        # alert behind it. So the notification has to be the whole message.
        sent["banner"] = send_banner(title, compact(body), subtitle)
    if config.push and config.topic:
        sent["push"] = send_push(
            body,
            title=f"{title} — {subtitle}",
            topic=config.topic,
            server=config.server,
            priority=PRIORITY.get(worst, 3),
            tags="rotating_light" if worst == "URGENT" else "warning",
        )
    return sent

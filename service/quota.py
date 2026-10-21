"""Detection of Gemini's per-account quota-limit refusal.

When an account exhausts its quota, Gemini does not raise an error: it answers
with a plain-text refusal and no media, e.g.

    "I can create more images as soon as your limit resets. Check your usage in Settings."

The service treats such a reply as a quota hit and locks the whole account for
a fixed period (QUOTA_LOCK_SECONDS), so no further requests are sent to Gemini
on its behalf until then. Any reset time in the message is ignored: its time
zone is unknown, so a fixed lock is the predictable choice.
"""

import re

from gemini_webapi.types import ModelOutput

_LIMIT_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\blimit\s+resets?\b",
        r"\bcheck\s+your\s+usage\b",
        r"\breached\s+(?:your|the|my)\s+(?:\w+\s+){0,3}limit\b",
        r"\b(?:can't|can’t|cannot|can not|unable to)\s+(?:create|generate|make)\s+"
        r"(?:any\s+)?more\s+(?:images|pictures|videos)\b",
        r"\b(?:image|video)\s+(?:creation|generation)\s+limit\b",
    )
]


def is_quota_limited(output: ModelOutput) -> bool:
    """Whether a generation reply is Gemini's quota-limit refusal (text, no media)."""

    if output.images or output.videos or output.media:
        return False
    text = output.text or ""
    return any(p.search(text) for p in _LIMIT_PATTERNS)

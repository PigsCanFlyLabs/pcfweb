"""A tiny self-hosted captcha, used to gate the Discord invite.

Deliberately not a third-party captcha: this guards one low-value page, and
every hosted option costs either a script tag from another origin (which the
cookie banner would then have to speak for) or an API key that has to be
plumbed through the cluster. What actually needs stopping here is the drive-by
scraper that follows every link on the site and harvests the invite, and a
question whose answer only exists server-side does that.

The answer never reaches the browser -- the challenge is stored in the session
and the client only ever sees the question -- and it is consumed on the first
attempt, right or wrong, so a challenge can neither be replayed nor guessed at
repeatedly. Getting it wrong means asking for a fresh question.
"""

import secrets
import time

from typing import Dict, Optional, Tuple


# Where the pending challenge lives in the session. One at a time: a second
# GET replaces the first, so the question on screen is always the live one.
SESSION_KEY = "discord_captcha"

# Seconds a challenge stays answerable. Long enough to read a page and type a
# number, short enough that a harvested session cookie is not a standing pass.
TTL_SECONDS = 15 * 60

# Spelled out rather than rendered as digits so the question is not a regex
# away from being solved. Index is the value.
NUMBER_WORDS = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
]


def _random_operands() -> Tuple[int, int]:
    # 1..5 each, so the sum is 2..10 -- answerable in one's head, and always a
    # value NUMBER_WORDS can spell if someone answers in words.
    return secrets.randbelow(5) + 1, secrets.randbelow(5) + 1


def new_challenge(session) -> str:
    """Issue a challenge, store its answer in the session, return the question."""
    left, right = _random_operands()
    session[SESSION_KEY] = {
        "answer": left + right,
        "issued_at": time.time(),
    }
    # Sessions are only saved when Django notices a change; assigning a dict
    # to a new key counts, but be explicit so a future in-place edit here does
    # not silently stop persisting.
    session.modified = True
    return (f"What is {NUMBER_WORDS[left]} plus {NUMBER_WORDS[right]}? "
            "(so we know you are not a bot)")


def _parse(raw: str) -> Optional[int]:
    """Read an answer as either digits or a spelled-out number."""
    cleaned = raw.strip().lower()
    if not cleaned:
        return None
    if cleaned.isdigit():
        return int(cleaned)
    if cleaned in NUMBER_WORDS:
        return NUMBER_WORDS.index(cleaned)
    return None


def check_answer(session, raw: str) -> bool:
    """Consume the pending challenge and say whether `raw` answered it.

    Consuming it on a wrong answer too is the point: otherwise the same
    question can be answered over and over until 2..10 runs out, which is
    eight guesses.
    """
    challenge: Optional[Dict] = session.pop(SESSION_KEY, None)
    session.modified = True
    if not challenge:
        # No session cookie, an expired session, or a POST that never asked
        # for a question in the first place.
        return False
    if time.time() - challenge.get("issued_at", 0) > TTL_SECONDS:
        return False
    return _parse(raw) == challenge.get("answer")

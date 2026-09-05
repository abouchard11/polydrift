"""Cross-venue title matching.

Deliberately dumb: token Jaccard over normalised words, with a small
abbreviation table so "Fed" and "Federal Reserve" agree. A high score means
"these two questions use the same words", NOT "these two markets resolve the
same way". Resolution equivalence is a human judgement made in the review gate
after reading both venues' rules text, and no score here is allowed to short-cut
that.
"""
from __future__ import annotations

import re
from typing import Iterable

# Expansions applied to both sides before tokenising. Keys are matched as
# whole words, case-insensitively.
ABBREVIATIONS: dict[str, str] = {
    "u.s.": "united states",
    "us": "united states",
    "usa": "united states",
    "u.k.": "united kingdom",
    "uk": "united kingdom",
    "fed": "federal reserve",
    "fomc": "federal reserve",
    "pres": "president",
    "pm": "prime minister",
    "gop": "republican",
    "dem": "democratic",
    "dems": "democratic",
    "bps": "basis points",
    "bp": "basis points",
    "btc": "bitcoin",
    "eth": "ethereum",
    "sec": "securities and exchange commission",
    "nato": "north atlantic treaty organization",
    "eu": "european union",
    "un": "united nations",
    "govt": "government",
    "vs": "versus",
    "vs.": "versus",
}

# Words that carry no matching signal on prediction-market titles.
STOPWORDS = frozenset("""
a an and are as at be before by for from in into is it of on or than that the
this to will with who what which when where how does do did any yes no
""".split())

# Words that swing resolution meaning; kept OUT of the stopword list on purpose
# so that "before" vs "after" or "official" vs "de facto" still differ.
_TOKEN_RX = re.compile(r"[a-z0-9]+(?:\.[a-z0-9]+)*")


def normalise(text: str) -> str:
    text = (text or "").lower()
    # Expand abbreviations as whole words, longest key first.
    for key in sorted(ABBREVIATIONS, key=len, reverse=True):
        pattern = r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])"
        text = re.sub(pattern, ABBREVIATIONS[key], text)
    text = text.replace("&", " and ")
    text = re.sub(r"[‘’'`]", "", text)          # apostrophes
    text = re.sub(r"[^a-z0-9.\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def tokens(text: str) -> set[str]:
    out: set[str] = set()
    for raw in _TOKEN_RX.findall(normalise(text)):
        tok = raw.strip(".")
        if not tok or tok in STOPWORDS:
            continue
        # crude plural/possessive stem, enough for titles
        if len(tok) > 4 and tok.endswith("s") and not tok.endswith("ss"):
            tok = tok[:-1]
        out.add(tok)
    return out


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def title_score(poly_question: str, kalshi_title: str, kalshi_subtitle: str = "") -> float:
    """Kalshi's per-leg proposition is usually title + yes_sub_title
    ("Who will the next Pope be?" + "Pietro Parolin"). Score against the union
    so a leg matches a Polymarket question that names the candidate."""
    return jaccard(tokens(poly_question), tokens(f"{kalshi_title} {kalshi_subtitle}"))


def year_tokens(text: str) -> set[str]:
    return set(re.findall(r"\b(20[2-9][0-9])\b", text or ""))

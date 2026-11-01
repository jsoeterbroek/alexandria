"""Map Library of Congress classification codes to Kiwix shelf categories.

Project Gutenberg records carry LCC codes (``PR``, ``QA``, ``DG``) in
``dcterms:subject``. Kiwix's catalogue exposes a category filter fed by the
``_category`` tag of each ZIM, but libkiwix builds that filter as a *single*
Xapian term (``XC`` + the raw value), while indexing the category by splitting
it on whitespace. A value containing a space therefore lists in the dropdown
and returns nothing when selected, and a hyphen splits into two indexed terms
for the same reason. Every label below is therefore one token: lower-case
letters and underscores only.
"""

from __future__ import annotations

# Top-level LCC class letter -> shelf label. ``I``, ``O``, ``W``, ``X``, ``Y``
# are unused by the LCC schedule.
_CLASSES = {
    "A": "general",
    "B": "philosophy",
    "C": "history",
    "D": "history",
    "E": "history",
    "F": "history",
    "G": "geography",
    "H": "social_sciences",
    "J": "political_science",
    "K": "law",
    "L": "education",
    "M": "music",
    "N": "arts",
    "P": "literature",
    "Q": "science",
    "R": "medicine",
    "S": "agriculture",
    "T": "technology",
    "U": "military",
    "V": "naval",
    "Z": "bibliography",
}

# Subclasses of ``P`` (language and literature), which would otherwise all
# collapse into one bucket that is the bulk of a Gutenberg collection. A book
# that lands in ``P`` without a label here falls back to ``literature``.
_LITERATURE_SUBCLASSES = {
    "PA": "classical_literature",
    "PB": "celtic_literature",
    "PC": "romance_languages",
    "PD": "germanic_languages",
    "PE": "english_language",
    "PF": "german_language",
    "PG": "slavic_literature",
    "PH": "uralic_literature",
    "PJ": "middle_eastern_literature",
    "PK": "indic_literature",
    "PL": "eastern_literature",
    "PM": "indigenous_literature",
    "PN": "literary_criticism",
    "PQ": "romance_literature",
    "PR": "english_literature",
    "PS": "american_literature",
    "PT": "germanic_literature",
    "PZ": "childrens_literature",
}

_CATEGORY_TAG = "_category:"


def _letters(code: str) -> str:
    """Leading alphabetic part of an LCC code (``PR`` from ``PR4034``)."""
    letters = ""
    for char in code.strip():
        if not char.isalpha():
            break
        letters += char
    return letters.upper()


def label_for_code(code: str) -> str:
    """Return the shelf label for a single LCC code, or ``""`` when unknown."""
    letters = _letters(code)
    if not letters:
        return ""
    if letters[0] == "P":
        return _LITERATURE_SUBCLASSES.get(letters[:2], "literature")
    return _CLASSES.get(letters[0], "")


def category_for(codes: tuple[str, ...]) -> str:
    """Pick the shelf label from a book's LCC codes.

    Gutenberg lists the primary classification first (``PR`` before ``PZ``),
    so the first code that maps to a known label wins.
    """
    for code in codes:
        label = label_for_code(code)
        if label:
            return label
    return ""


def category_from_tags(tags: str) -> str:
    """Read the category out of a Kiwix tag string (``…;_category:x;…``)."""
    for tag in tags.split(";"):
        tag = tag.strip()
        if tag.lower().startswith(_CATEGORY_TAG):
            return tag[len(_CATEGORY_TAG) :].strip()
    return ""

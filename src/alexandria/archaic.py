"""Rewrite archaic English prose into present-day English.

The rewrite is deterministic and operates on text nodes only:

* second person — ``thou knowest`` → ``you know``, ``knowest thou`` → ``you know``,
  ``thou art`` → ``you are``;
* third person ``-eth`` verbs — ``he cometh`` → ``he comes``;
* archaic auxiliaries (``hath``, ``dost``, ``shalt``) and function words
  (``quoth``, ``verily``, ``whither``, ``aught``, ``naught``);
* a few fixed phrases (``in very sooth``, ``good morrow``, ``bade him adieu``).

Verb stems are validated against the book's own vocabulary, and a token is only
treated as an inflected verb when its lowercase spelling occurs somewhere in the
book — so ``best``, ``guest``, ``teeth`` and proper names such as ``Hareth``
survive. Markup and attributes are never touched: :func:`modernize_soup`
replaces text nodes in place.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Comment, NavigableString

# Words ending in -est/-eth that are nouns, ordinals or names rather than verbs.
_EST_WORDS = frozenset(
    {
        "arrest",
        "attest",
        "against",
        "amidst",
        "amongst",
        "behest",
        "best",
        "blest",
        "chest",
        "conquest",
        "contest",
        "detest",
        "digest",
        "earnest",
        "east",
        "first",
        "forest",
        "guest",
        "harvest",
        "honest",
        "infest",
        "interest",
        "invest",
        "jest",
        "just",
        "last",
        "least",
        "lest",
        "manifest",
        "modest",
        "molest",
        "most",
        "must",
        "priest",
        "protest",
        "quest",
        "request",
        "rest",
        "suggest",
        "tempest",
        "unrest",
        "west",
        "whilst",
    }
)
_ETH_WORDS = frozenset(
    {
        "ashtoreth",
        "beth",
        "eightieth",
        "elizabeth",
        "fiftieth",
        "fortieth",
        "hareth",
        "hazarmaveth",
        "nazareth",
        "ninetieth",
        "seventieth",
        "seth",
        "shibboleth",
        "sixtieth",
        "teeth",
        "thirtieth",
        "twentieth",
    }
)

# Second-person auxiliaries with their own rules; never stemmed as -est/-st verbs.
_SECOND_PERSON = frozenset(
    {
        "art",
        "canst",
        "couldst",
        "didst",
        "dost",
        "durst",
        "hast",
        "mayest",
        "mayst",
        "mightest",
        "mightst",
        "shalt",
        "shouldst",
        "wast",
        "wert",
        "wilt",
        "wouldst",
    }
)

# Second-person verb shapes: any "learnest", a past tense "learnedst", or one of
# the auxiliaries that lost the "e" ("thou sayst", "thou dost"). A bare "-st"
# word is otherwise modern ("trust", "first", "cast") and must stay.
_ST_VERBS = (
    "hast have dost do didst had hadst can canst may mayst could couldst would wouldst should shouldst "
    "wast were wert say sayst know knowst think thinkst seek seekst need needst bethink bethinkst dare "
    "durst might mightst list"
)
_SECOND_FORM = r"(?:[A-Za-z]+(?:est|edst)|" + "|".join(sorted(set(_ST_VERBS.split()), key=len, reverse=True)) + r")"

# Auxiliaries are verbs even though "shoulder"/"woulder"-style lookalikes exist.
_AUXILIARIES = frozenset(
    {
        "be",
        "can",
        "could",
        "dare",
        "do",
        "have",
        "may",
        "might",
        "must",
        "need",
        "ought",
        "shall",
        "should",
        "will",
        "would",
    }
)

# Archaic verb stems that no longer survive as English stems.
_STEM_ALIASES = {"wot": "know", "weet": "know", "wist": "knew", "behove": "behoove", "beseem": "befit"}

_VERB_SYNONYMS = frozenset(
    "go come see know love give take make bring seek speak say tell stand sit lie live die".split()  # noqa: SIM905
)

# Everyday verbs used to resolve an archaic form in text too short to contain the
# modern verb itself (a footnote, a test fixture). Only consulted when the book
# offers no evidence at all, so book vocabulary always wins.
_COMMON_VERBS = frozenset(
    """
    ail answer ask bear beat become begin behold believe belong bid bind bite blow break bring
    build burn buy call carry cast catch choose come command continue cry cut deal die do draw
    drink drive dwell eat endure enter fall fear feed feel fight find flee flow fly follow forget
    forgive forsake gain get give go grow guard happen hate have hear heat help hide hold hope
    keep kill kiss know lack lay lead learn leave let lie live look lose love make marry mean
    meet melt move need obey offer open pass pay pray prepare profit put raise reach read receive
    refuse remain remember reply require rest return ride rise rule run save say see seek seem
    sell send serve set shake shine show sing sink sit slay sleep smite speak spend stand steal
    strike take teach tear tell think throw touch turn understand wait wake walk want wear weep
    win wish wonder work write yield
    """.split()  # noqa: SIM905
)

# Common second-person forms whose stem is ambiguous; resolved by rule instead
# of by corpus evidence so short texts behave like whole books.
_IRREGULAR_SECOND = {
    "goest": "go",
    "doest": "do",
    "seest": "see",
    "liest": "lie",
    "woest": "woo",
    "saysest": "say",
    "sayst": "say",
    "knowest": "know",
    "knowst": "know",
    "comest": "come",
    "camest": "came",
    "gavest": "gave",
    "sawest": "saw",
    "madest": "made",
    "hadst": "had",
    "didst": "did",
    "wast": "were",
    "wert": "were",
}

# Single-word replacements that need no context.
_WORDS = {
    "thou": "you",
    "thee": "you",
    "thy": "your",
    "ye": "you",
    "thyself": "yourself",
    "thyselves": "yourselves",
    "thou'rt": "you're",
    "thou'lt": "you'll",
    "thou'st": "you've",
    "hath": "has",
    "doth": "does",
    "saith": "says",
    "sayth": "says",
    "quoth": "said",
    "quotha": "indeed",
    "hast": "have",
    "dost": "do",
    "didst": "did",
    "hadst": "had",
    "wast": "were",
    "wert": "were",
    "shalt": "shall",
    "canst": "can",
    "couldst": "could",
    "wouldst": "would",
    "shouldst": "should",
    "mayst": "may",
    "mayest": "may",
    "mightst": "might",
    "mightest": "might",
    "durst": "dared",
    "wottest": "know",
    "wot": "know",
    "wist": "knew",
    "verily": "truly",
    "sooth": "truth",
    "aught": "anything",
    "naught": "nothing",
    "nought": "nothing",
    "ere": "before",
    "betwixt": "between",
    "whilst": "while",
    "amongst": "among",
    "amidst": "amid",
    "whence": "from where",
    "whither": "where",
    "hither": "here",
    "thither": "there",
    "thence": "from there",
    "hitherward": "this way",
    "thitherward": "that way",
    "whithersoever": "wherever",
    "whoso": "whoever",
    "whomsoever": "whoever",
    "unto": "to",
    "oft": "often",
    "ofttimes": "often",
    "mayhap": "perhaps",
    "perchance": "perhaps",
    "peradventure": "perhaps",
    "belike": "perhaps",
    "prithee": "please",
    "methinks": "it seems to me",
    "methought": "it seemed to me",
    "meseems": "it seems to me",
    "meseemeth": "it seems to me",
    "methinketh": "it seems to me",
    "anon": "soon",
    "albeit": "although",
    "howbeit": "however",
    "sans": "without",
    "nigh": "near",
    "wellnigh": "nearly",
    "wrought": "worked",
    "spake": "spoke",
    "smote": "struck",
    "bade": "ordered",
    "hight": "named",
    "whilome": "formerly",
    "yclept": "called",
    "eftsoons": "soon",
    "enow": "enough",
    "fain": "gladly",
    "damsel": "young woman",
    "beldam": "old woman",
    "steed": "horse",
    "espy": "spy",
    "yea": "yes",
    "nay": "no",
    "aye": "yes",
    "morrow": "next day",
    "ne'er": "never",
    "e'er": "ever",
    "e'en": "even",
    "o'er": "over",
    "ta'en": "taken",
}

# Words that must stay archaic in a fixed collocation.
_WORD_GUARDS = {
    "wrought": re.compile(r"\s+(?:iron|steel|brass|gold|silver)\b", re.IGNORECASE),
    "nigh": re.compile(r"\s+(?:unto|upon)\b", re.IGNORECASE),
}

# Whitespace, plus the sentinel that stands in for a text-node boundary, so
# "thou <i>art</i>" is still treated as one construction.
_WS = r"(?:[ \t\n\r\f\v\x00\u00a0\u2007\u202f]+)"

# Words whose modern form depends on what follows: "mine eyes" -> "my eyes",
# but "the money is mine" stays.
_NOT_A_NOUN = (
    "of is was were be am are and or to for with from in on at by as than that this it he she they we "
    "you if but not will shall would could should may might must had has have does did doth hath"
)
_POSSESSIVE_TAIL = r"(?!(?:" + _NOT_A_NOUN.replace(" ", "|") + r")\b)(?=[A-Za-z])"
_MINE = re.compile(rf"\b(mine)({_WS})" + _POSSESSIVE_TAIL, re.IGNORECASE)
_THINE = re.compile(rf"\b(thine)({_WS})" + _POSSESSIVE_TAIL, re.IGNORECASE)
_THINE_ALONE = re.compile(r"\bthine\b", re.IGNORECASE)

# Second person: "thou knowest" / "knowest thou" / "thou who knowest".
_THOU_VERB = re.compile(rf"\b(thou)({_WS})({_SECOND_FORM})\b", re.IGNORECASE)
_VERB_THOU = re.compile(rf"\b({_SECOND_FORM})({_WS})(thou)\b", re.IGNORECASE)
_THOU_RELATIVE = re.compile(rf"\b(thou|ye)({_WS})(who|that|which)({_WS})({_SECOND_FORM})\b", re.IGNORECASE)
_RELATIVE_VERB = re.compile(
    rf"\b(who|that|which|what|how|why|where|whence|whither)({_WS})({_SECOND_FORM})\b", re.IGNORECASE
)
# Leftovers where the pronoun and verb sat in different text nodes.
_YOU_VERB = re.compile(rf"\b(you)({_WS})((?:[A-Za-z]+(?:est|edst|eth)))\b", re.IGNORECASE)
# Reverse order: "Fearedst not what ills..." — the pronoun came later in the line.
_VERB_YOU = re.compile(rf"\b((?:[A-Za-z]+(?:est|edst|eth)))({_WS})(you)\b", re.IGNORECASE)
_AUXILIARY_ST = re.compile(rf"\b(needst)({_WS})(must)\b", re.IGNORECASE)
_YOU_WEST = re.compile(rf"\b(you)({_WS})(west)\b", re.IGNORECASE)

# Third person "-eth".
_ETH_VERB = re.compile(r"\b([A-Za-z]+eth)\b", re.IGNORECASE)

# "art" and "wilt" are archaic only next to a second-person construction; as a
# noun/ordinary verb ("the art of war", "flowers wilt") they stay.
_ART_AFTER = re.compile(
    rf"\b(thou|who|that|which|what|how|why|where|whence|whither|such|so)({_WS})(art)\b", re.IGNORECASE
)
_ART_BEFORE = re.compile(rf"\b(art)({_WS})(thou)\b", re.IGNORECASE)
_WILT_AFTER = re.compile(rf"\b(thou|who|that|which)({_WS})(wilt)\b", re.IGNORECASE)
_WILT_BEFORE = re.compile(rf"\b(wilt)({_WS})(thou)\b", re.IGNORECASE)

# Phrase-level fixes; these run before the single-word table.
_PHRASES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bfrom\s+whence\b", re.IGNORECASE), "from where"),
    (re.compile(r"\bfor\s+aye\b", re.IGNORECASE), "forever"),
    (re.compile(r"\bgood[\s-]+morrow\b", re.IGNORECASE), "good morning"),
    (re.compile(r"\bto[\s-]morrow\b", re.IGNORECASE), "tomorrow"),
    (re.compile(r"\b(?:I\s+)?pray\s+thee\b", re.IGNORECASE), "please"),
    (re.compile(r"\bin\s+(?:very\s+|good\s+)?sooth\b", re.IGNORECASE), "in truth"),
    (re.compile(r"\b(?:say|says|sayst|sayest|said|saith)\s+sooth\b", re.IGNORECASE), "tell the truth"),
    (re.compile(r"\bfain\s+of\b", re.IGNORECASE), "fond of"),
    (re.compile(r"\bfain\s+to\b", re.IGNORECASE), "eager to"),
    (re.compile(r"\bbade\s+(him|her|them|me|us|you)\s+(adieu|farewell|goodbye)\b", re.IGNORECASE), r"said \2 to \1"),
    (re.compile(r"\bbade\s+(adieu|farewell|goodbye)\b", re.IGNORECASE), r"said \1"),
    (re.compile(r"\bnigh\s+(unto|upon)\b", re.IGNORECASE), r"\1"),
)
_APOSTROPHE_FORMS = {
    "'tis": "it is",
    "’tis": "it is",
    "'twas": "it was",
    "’twas": "it was",
    "'twere": "it were",
    "’twere": "it were",
    "'twill": "it will",
    "’twill": "it will",
}
_APOSTROPHE_RE = re.compile(r"(?<![\w'])(?:" + "|".join(map(re.escape, _APOSTROPHE_FORMS)) + r")\b", re.IGNORECASE)

_WORD_PATTERN = re.compile(
    r"\b(" + "|".join(sorted(map(re.escape, _WORDS), key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

_TOKEN = re.compile(r"[A-Za-z]+")


def _same_case(source: str, replacement: str) -> str:
    """Give ``replacement`` the capitalisation of the matched ``source``."""
    if source.isupper() and len(source) > 1:
        return replacement.upper()
    first = source.lstrip("'’")[:1]
    if first.isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def _candidates(word: str) -> list[str]:
    """Plausible modern stems for an archaic inflected verb, best first.

    ``cometh`` → ``come``; ``kisseth`` → ``kiss``; ``sitteth`` → ``sit``;
    ``lieth`` → ``lie``; ``relieth`` → ``rely``; ``seest`` → ``see``;
    ``camest`` → ``came``; ``knowest`` → ``know``.
    """
    lower = word.lower()
    if lower.endswith("eth"):
        stem = lower[:-3]
        cuts = [stem + "e", stem]
        if stem.endswith(("ch", "sh", "ss", "s", "z")):
            cuts.insert(0, stem)
        if stem.endswith("i"):
            cuts.insert(0, stem[:-1] + "y")
        if len(stem) > 2 and stem[-1] == stem[-2] and stem[-1] not in "aeiou":
            cuts.insert(0, stem[:-1])
    elif lower.endswith("est"):
        bare, stem = lower[:-2], lower[:-3]
        cuts = [bare, stem + "e", stem]
        if stem.endswith("i"):
            cuts.insert(0, stem[:-1] + "y")
        if len(stem) > 2 and stem[-1] == stem[-2] and stem[-1] not in "aeiou":
            cuts.insert(0, stem[:-1])
        if stem.endswith("o") and lower not in {"goest", "doest", "woest"}:
            cuts.insert(0, stem + "e")
    elif lower.endswith("st"):
        # "trust" is already modern; only strip the "-st" when the remainder is
        # long enough to be a verb stem ("sayst" → "say", "knowst" → "know").
        stem = lower[:-2]
        cuts = [stem, stem + "e"]
        if stem.endswith("i"):
            cuts.insert(0, stem[:-1] + "y")
    else:
        return []
    unique: list[str] = []
    for candidate in cuts:
        if len(candidate) < 2 or candidate in unique:
            continue
        unique.append(candidate)
    return unique


def _evidence(stem: str, vocabulary: dict[str, int]) -> int:
    """How often this book uses ``stem`` bare or inflected."""
    return sum(vocabulary.get(form, 0) for form in (stem, stem + "s", stem + "es", stem + "ed", stem + "ing"))


def _adjective(stem: str, vocabulary: dict[str, int]) -> bool:
    """True when ``stem`` is an adjective whose superlative ends in ``-est``.

    The comparative must occur ("fairer") and no finite or participle form may
    ("says", "said", "saying"), which keeps ``sayest`` a verb and ``fairest`` an
    adjective.
    """
    if stem in _AUXILIARIES or stem in _SECOND_PERSON:
        return False
    if not any(form in vocabulary for form in (stem + "er", stem[:-1] + "ier", stem + stem[-1] + "er")):
        return False
    return not any(form in vocabulary for form in (stem + "s", stem + "es", stem + "ed", stem + "ing"))


def _verb_stem(word: str, vocabulary: dict[str, int], *, anchored: bool = False, minimum: int = 1) -> str | None:
    """Return the modern stem of an archaic ``-est``/``-eth`` verb, if known.

    Candidates are scored by how often the book uses the corresponding verb, so
    ``cometh`` resolves to ``come`` rather than ``com`` and ``aileth`` through
    ``ails``. Without ``anchored`` (no pronoun next to the word) a comparative or
    other adjective evidence rejects the candidate, which keeps "the fairest"
    and "the meanest" adjectives. With ``anchored`` the pronoun settles it: "thou
    meanest" is a verb even though "meaner" also occurs.
    """
    irregular = _IRREGULAR_SECOND.get(word.lower())
    if irregular is not None:
        return irregular
    best: str | None = None
    best_score: tuple[int, int] = (-1, 0)
    for candidate in _candidates(word):
        alias = _STEM_ALIASES.get(candidate, candidate)
        known = alias in _COMMON_VERBS or alias in _VERB_SYNONYMS
        # Without a pronoun a two-letter stem is more likely a stray token
        # ("I've" → "ve") than a verb: "vest thou" must stay put. A verb that is
        # known to be common is exempt, so "he goeth" still becomes "he goes".
        if len(alias) < 3 and not (anchored or known):
            continue
        if not anchored and _adjective(alias, vocabulary):
            continue
        score = (_evidence(alias, vocabulary), 1 if known else 0)
        if score > best_score:
            best, best_score = alias, score
    if best_score[0] >= minimum or (best is not None and best in _STEM_ALIASES.values()):
        return best
    if best_score[1]:
        return best
    if anchored:
        # The pronoun proves this is a verb even when no candidate is used
        # anywhere else; take the longest plausible stem ("thou lollest" → "loll",
        # "you dourest" → "dour").
        for candidate in _candidates(word):
            alias = _STEM_ALIASES.get(candidate, candidate)
            if len(alias) >= 3:
                return alias
    return None


def _third_person(stem: str) -> str:
    if stem == "have":
        return "has"
    if stem == "be":
        return "is"
    if stem.endswith(("s", "x", "z", "ch", "sh", "o")):
        return stem + "es"
    if stem.endswith("y") and len(stem) > 1 and stem[-2] not in "aeiou":
        return stem[:-1] + "ies"
    return stem + "s"


_SENTENCE_START = re.compile(r"(?:^|[.!?\u2026][\"\u201c\u201d'\u2019]?\s+|^[\"\u201c'\u2019]\s*)$", re.MULTILINE)


class _Vocab:
    """Words of one book, used to tell real verb stems from lookalike nouns."""

    def __init__(self, text: str) -> None:
        tokens = _TOKEN.findall(text)
        self.words: dict[str, int] = {}
        for token in tokens:
            self.words[token.lower()] = self.words.get(token.lower(), 0) + 1
        self.lowercase = {token for token in tokens if token.islower()}
        # Capitalised forms that never appear lowercase are treated as names
        # unless every occurrence opens a sentence ("Kisseth her hand").
        self.names: set[str] = set()
        for match in _TOKEN.finditer(text):
            token = match.group(0)
            if token.islower() or token in self.lowercase:
                continue
            if not _SENTENCE_START.match(text[max(0, match.start() - 4) : match.start()]):
                self.names.add(token)

    def verb_stem(self, word: str, *, anchored: bool = False, minimum: int = 1) -> str | None:
        lower = word.lower()
        if lower in _WORDS:
            return None
        return _verb_stem(word, self.words, anchored=anchored, minimum=minimum)

    def seen(self, word: str) -> bool:
        lower = word.lower()
        return lower in self.lowercase or lower not in self.names


def _conjugate_second(match: re.Match[str], vocab: _Vocab, *, verb_first: bool) -> str:
    if verb_first:
        word, space, pronoun = match.group(1), match.group(2), match.group(3)
    else:
        pronoun, space, word = match.group(1), match.group(2), match.group(3)
    if word.lower() in _EST_WORDS:
        return match.group(0)
    # Only a pronoun in front proves an unfamiliar word is a verb; "past thou"
    # must not become "pae you", and "trust thou" must not become "tru you".
    stem = vocab.verb_stem(word, anchored=not verb_first, minimum=1 if not verb_first else 2)
    if stem is None:
        return match.group(0)
    modern = _same_case(word, stem)
    you = _same_case(pronoun, "you")
    return modern + space + you if verb_first else you + space + modern


def _conjugate_verb_first(match: re.Match[str], vocab: _Vocab) -> str:
    word, space, pronoun = match.group(1), match.group(2), match.group(3)
    if word.lower() in _EST_WORDS:
        return match.group(0)
    stem = vocab.verb_stem(word, anchored=True)
    if stem is None:
        return match.group(0)
    return _same_case(word, stem) + space + pronoun


def _conjugate_you(match: re.Match[str], vocab: _Vocab) -> str:
    """Fix an archaic verb that was left behind after its pronoun modernized."""
    pronoun, space, word = match.group(1), match.group(2), match.group(3)
    if word.lower() in _EST_WORDS:
        return match.group(0)
    stem = vocab.verb_stem(word, anchored=True)
    if stem is None:
        return match.group(0)
    return pronoun + space + _same_case(word, stem)


def _thou_relative(match: re.Match[str], vocab: _Vocab) -> str:
    pronoun, space1, relative, space2, word = match.groups()
    stem = None if word.lower() in _EST_WORDS else vocab.verb_stem(word, anchored=True)
    if stem is None:
        return match.group(0)
    you = _same_case(pronoun, "you")
    return you + space1 + relative + space2 + _same_case(word, stem)


def _relative(match: re.Match[str], vocab: _Vocab) -> str:
    relative, space, word = match.group(1), match.group(2), match.group(3)
    if word.lower() in _EST_WORDS or not vocab.seen(word):
        return match.group(0)
    stem = vocab.verb_stem(word)
    if stem is None:
        return match.group(0)
    return relative + space + _same_case(word, stem)


def _conjugate_third(match: re.Match[str], vocab: _Vocab) -> str:
    word = match.group(1)
    if word.lower() in _ETH_WORDS or not vocab.seen(word):
        return word
    stem = vocab.verb_stem(word)
    if stem is None:
        return word
    return _same_case(word, _third_person(stem))


def _possessive(match: re.Match[str], replacement: str) -> str:
    return _same_case(match.group(1), replacement) + match.group(2)


def _replace_word(match: re.Match[str]) -> str:
    source = match.group(1)
    guard = _WORD_GUARDS.get(source.lower())
    if guard is not None and guard.match(match.string, match.end()):
        return source
    return _same_case(source, _WORDS[source.lower()])


def _phrase(match: re.Match[str], replacement: str) -> str:
    """Expand ``replacement`` (which may use \\1 group refs) and match its case."""
    expanded = match.expand(replacement)
    if expanded[:1] == replacement[:1] and replacement[:1].isalpha():
        return _same_case(match.group(0), expanded)
    return expanded


def _apostrophe(match: re.Match[str]) -> str:
    source = match.group(0)
    return _same_case(source, _APOSTROPHE_FORMS[source.lower()])


def modernize_text(text: str, vocab: _Vocab | None = None) -> str:
    """Modernize archaic English in one plain-text string."""
    vocab = vocab if vocab is not None else _Vocab(text)

    text = _VERB_THOU.sub(lambda m: _conjugate_second(m, vocab, verb_first=True), text)
    text = _THOU_VERB.sub(lambda m: _conjugate_second(m, vocab, verb_first=False), text)
    text = _THOU_RELATIVE.sub(lambda m: _thou_relative(m, vocab), text)
    text = _RELATIVE_VERB.sub(lambda m: _relative(m, vocab), text)
    text = _ETH_VERB.sub(lambda m: _conjugate_third(m, vocab), text)

    text = _ART_AFTER.sub(lambda m: m.group(1) + m.group(2) + _same_case(m.group(3), "are"), text)
    text = _ART_BEFORE.sub(lambda m: _same_case(m.group(1), "are") + m.group(2) + _same_case(m.group(3), "you"), text)
    text = _WILT_AFTER.sub(lambda m: m.group(1) + m.group(2) + _same_case(m.group(3), "will"), text)
    text = _WILT_BEFORE.sub(lambda m: _same_case(m.group(1), "will") + m.group(2) + _same_case(m.group(3), "you"), text)

    for pattern, replacement in _PHRASES:
        text = pattern.sub(lambda m, r=replacement: _phrase(m, r), text)

    text = _MINE.sub(lambda m: _possessive(m, "my"), text)
    text = _THINE.sub(lambda m: _possessive(m, "your"), text)
    text = _THINE_ALONE.sub(lambda m: _same_case(m.group(0), "yours"), text)
    text = _APOSTROPHE_RE.sub(_apostrophe, text)
    text = _WORD_PATTERN.sub(_replace_word, text)

    # Last pass: pronouns that only became "you" in this call left their verb
    # archaic ("thou west" → "you west"), and a pronoun in front of an unusual
    # stem proves it is a verb even when the book never uses it elsewhere.
    for _ in range(2):
        text = _YOU_WEST.sub(lambda m: m.group(1) + m.group(2) + _same_case(m.group(3), "were"), text)
        text = _YOU_VERB.sub(lambda m: _conjugate_you(m, vocab), text)
        text = _VERB_YOU.sub(lambda m: _conjugate_verb_first(m, vocab), text)
        text = _AUXILIARY_ST.sub(lambda m: _same_case(m.group(1), "need") + m.group(2) + m.group(3), text)
    return text


def _modernizable(node: NavigableString) -> bool:
    return not isinstance(node, Comment) and not any(
        getattr(parent, "name", "") in {"script", "style", "head", "title", "meta"} for parent in node.parents
    )


_SENTINEL = "\x00"


def modernize_soup(soup: BeautifulSoup) -> None:
    """Rewrite archaic English in every text node of ``soup`` in place.

    Inline markup splits sentences ("thou <i>art</i>"), so the text nodes are
    joined with a sentinel byte, modernized as one stream and split back. The
    sentinel never survives in the document.
    """
    nodes = [node for node in soup.find_all(string=True) if _modernizable(node)]
    if not nodes:
        return
    stream = _SENTINEL.join(str(node) for node in nodes)
    vocab = _Vocab(stream)
    modern = modernize_text(stream, vocab)
    if modern == stream:
        return
    parts = modern.split(_SENTINEL)
    if len(parts) != len(nodes):
        # A rewrite added or removed a sentinel; fall back to per-node work.
        for node in nodes:
            source = str(node)
            updated = modernize_text(source, vocab)
            if updated != source:
                node.replace_with(NavigableString(updated))
        return
    for node, updated in zip(nodes, parts, strict=True):
        if updated != str(node):
            node.replace_with(NavigableString(updated))

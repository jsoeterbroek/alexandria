"""Convert non-HTML Gutenberg editions (PDF, TeX) into parseable HTML.

A few ebooks only ship as PDF or TeX (Tractatus Logico-Philosophicus, #5740,
is PDF + LaTeX only). Their text is extracted, split into blocks and rendered
with the same ``<div class="chapter">`` structure the HTML editions use, so the
rest of the pipeline is unchanged.
"""

from __future__ import annotations

import html as html_lib
import re
from pathlib import Path

_SECTION_RE = re.compile(
    r"^\s*(chapter|part|book|volume|section|act|scene|canto|letter|"
    r"prologue|epilogue|introduction|preface|appendix|contents|conclusion)\b",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"^\s*\d+(?:\.\d+)*\.?\s*$")
# "1.1 The world is …" opens a numbered proposition.
_PROPOSITION_RE = re.compile(r"^(\d+(?:\.\d+){0,3})\.?\s+(?=[A-Z\u00c0-\u024f\"(])")
_MARKER_RE = re.compile(r"^\s*\*\*\*\s*(START|END) OF (THE|THIS) PROJECT GUTENBERG", re.IGNORECASE)
# LaTeX setup that survives plain-text conversion (list definitions, lengths).
_NOISE_RE = re.compile(
    r"(leftmargin=|itemsep=|parsep=|listparindent=|\\newlist|\\setlist|"
    r"^\s*\[?-?\d*\]?\s*\w+enumerate\d)",
    re.IGNORECASE,
)
_NOISE_WORDS = {"empty", "roman", "arabic"}


def pdf_to_html(path: Path, title: str) -> str:
    """Extract a PDF's text and render it as HTML."""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = [(page.extract_text() or "") for page in reader.pages]
    return text_to_html("\n\n".join(pages), title)


def tex_to_html(path: Path, title: str) -> str:
    """Convert a Gutenberg LaTeX source to HTML text."""
    from pylatexenc.latex2text import LatexNodes2Text

    raw = _read_text(path)
    start = raw.find("\\begin{document}")
    body = raw[start:] if start >= 0 else raw
    end = body.find("\\end{document}")
    if end >= 0:
        body = body[:end]
    text = LatexNodes2Text(keep_comments=False).latex_to_text(body)
    return text_to_html(text, title)


def _read_text(path: Path) -> str:
    """Decode a source file, preferring UTF-8 (Gutenberg's TeX is often UTF-8)."""
    payload = path.read_bytes()
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def text_to_html(text: str, title: str) -> str:
    """Render plain text as HTML with one ``div.chapter`` per top-level section."""
    sections = _split(text)
    body: list[str] = [f"<h1>{html_lib.escape(title)}</h1>"]
    for heading, paragraphs in sections:
        body.append('<div class="chapter">')
        if heading:
            body.append(f"<h2>{html_lib.escape(heading)}</h2>")
        body.extend(f"<p>{html_lib.escape(paragraph)}</p>" for paragraph in paragraphs)
        body.append("</div>")
    return (
        '<!DOCTYPE html>\n<html><head><meta charset="utf-8">'
        f"<title>{html_lib.escape(title)}</title></head>\n<body>\n" + "\n".join(body) + "\n</body></html>\n"
    )


def _split(text: str) -> list[tuple[str, list[str]]]:
    """Group ``text`` into ``(heading, paragraphs)`` sections."""
    sections: list[tuple[str, list[str]]] = []
    paragraphs: list[str] = []
    heading = ""

    def flush() -> None:
        nonlocal paragraphs, heading
        if heading or paragraphs:
            sections.append((heading, paragraphs))
        paragraphs, heading = [], ""

    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        block = " ".join(line.strip() for line in block.splitlines()).strip()
        block = re.sub(r"^\[-?\d+\]\s*", "", block)
        if not block:
            continue
        if _MARKER_RE.match(block) or _NOISE_RE.search(block):
            continue
        if set(block.lower().split()) <= _NOISE_WORDS:
            continue
        # LaTeX bookmarks duplicate their label: "Front MatterFront Matter".
        half = len(block) // 2
        if len(block) > 6 and block[:half].strip() == block[half:].strip():
            block = block[:half].strip()
        if _is_heading(block):
            flush()
            heading = block
            continue
        if _NUMBER_RE.match(block):
            flush()
            heading = block.rstrip(".")
            continue
        proposition = _PROPOSITION_RE.match(block)
        if proposition:
            number = proposition.group(1)
            # Only top-level propositions ("1", "2", …) open a section; the
            # numbered sub-propositions stay in the body, prefixed by number.
            if "." in number:
                paragraphs.append(f"{number} {block[proposition.end() :]}")
                continue
            flush()
            heading = number
            block = block[proposition.end() :]
        paragraphs.append(block)

    flush()
    sections = _merge_small(sections)

    # Give numeric sections a readable title from their first sentence.
    titled: list[tuple[str, list[str]]] = []
    for heading, paragraphs in sections:
        if heading and re.fullmatch(r"\d+", heading) and paragraphs:
            lead = re.split(r"(?<=[.!?])\s|\[", paragraphs[0], maxsplit=1)[0]
            lead = lead[:70].rstrip(" .,;:")
            heading = f"{heading}. {lead}" if lead else heading
        titled.append((heading, paragraphs))
    return titled


def _merge_small(sections: list[tuple[str, list[str]]], minimum: int = 240) -> list[tuple[str, list[str]]]:
    """Fold scraps (title-page fragments, stray numbers) into the next section."""
    merged: list[tuple[str, list[str]]] = []
    pending: list[tuple[str, list[str]]] = []
    for heading, paragraphs in sections:
        size = len(heading) + sum(len(paragraph) for paragraph in paragraphs)
        if size < minimum:
            pending.append((heading, paragraphs))
            continue
        if pending:
            extra_headings = [text for text, _ in pending if text]
            extra_paragraphs = [paragraph for _, body in pending for paragraph in body]
            heading = heading or (extra_headings[0] if extra_headings else "")
            paragraphs = extra_paragraphs + paragraphs
            for text in extra_headings[1:]:
                paragraphs.insert(0, text)
            pending = []
        merged.append((heading, paragraphs))
    if pending:
        merged.extend(pending)
    return merged


def _is_heading(block: str) -> bool:
    if _SECTION_RE.match(block):
        return True
    letters = [character for character in block if character.isalpha()]
    if len(letters) < 3 or len(block) > 70:
        return False
    return all(character.isupper() for character in letters)

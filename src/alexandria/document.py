"""Convert Project Gutenberg HTML into ZIM-ready pages and media.

Gutenberg serves three HTML layouts:

* modern reflowable HTML with one ``<div class="chapter">`` per chapter;
* older HTML where chapters start at ``<h2>``/``<h3>`` headings;
* legacy files wrapped in ``*** START/END OF THE PROJECT GUTENBERG EBOOK ***``.

All three are handled: the book is sliced out of the boilerplate, split into
articles, intra-book links and images are rewritten to archive paths, and every
article is wrapped in a small self-contained reader.
"""

from __future__ import annotations

import html as html_lib
import posixpath
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from PIL import Image, ImageOps

from .archaic import modernize_soup
from .models import Asset, BookMeta, Page, TocEntry
from .util import url_cache_key

_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_SECTION_WORDS = (
    "chapter",
    "part",
    "book",
    "volume",
    "section",
    "act",
    "scene",
    "canto",
    "letter",
    "prologue",
    "epilogue",
    "introduction",
    "preface",
    "appendix",
    "conclusion",
    "afterword",
)
_SECTION_RE = re.compile(r"^\s*(" + "|".join(_SECTION_WORDS) + r")\b", re.IGNORECASE)
_ORDINALS = (
    "first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|nineth|tenth|"
    "eleventh|twelfth|thirteenth|fourteenth|fifteenth|sixteenth|seventeenth|"
    "eighteenth|nineteenth|twentieth|last|\\d+(?:st|nd|rd|th)"
)
# "THE FIRST BOOK", "Book the Second", "Part One": Gutenberg's older editions
# number their divisions with ordinals instead of numerals.
_ORDINAL_RE = re.compile(
    r"^\s*(?:(?:the|his|her|my|our|its)\s+)?(?:"
    + _ORDINALS
    + r")\s+(book|part|volume|section|canto|act|scene|letter)\b",
    re.IGNORECASE,
)
# A bare numeral heading ("I", "XIV.") is a chapter marker; the descriptive
# chapter title usually follows in the next heading (Candide).
_ROMAN_RE = re.compile(r"^\s*[IVXLCDM]+\.?\s*$", re.IGNORECASE)
# "CONTENTS", "CONTENTS.", "BOOKS", "Contents of the First Volume",
# "Table of Contents for Ebooks 731-736".
_CONTENTS_RE = re.compile(r"^\s*(?:table\s+of\s+)?(?:contents|books)\b", re.IGNORECASE)
_DEPTH = {"part": 0, "volume": 0, "book": 1, "section": 1}
_TOC_DEPTH = {
    "part": 0,
    "volume": 0,
    "book": 1,
    "section": 1,
    "act": 1,
    "scene": 1,
    "canto": 1,
    "letter": 1,
}
_SKIP_HEADINGS = {
    "contents",
    "table of contents",
    "illustrations",
    "list of illustrations",
}

# Upper bound for one ZIM article. Books served as a single giant <div> (the
# Chambers dictionary, the Tagalog dictionary, some bibles) are split into
# article-sized chunks instead of one multi-megabyte page.
MAX_ARTICLE_BYTES = 256 * 1024
# Chrome (head, nav, footer) added around every article; chunking keeps the
# rendered page within MAX_ARTICLE_BYTES by budgeting for it.
_PAGE_CHROME_BYTES = 2 * 1024

# Concurrent downloads per book. Illustrated ebooks reference thousands of
# images; fetching them serially takes minutes per book.
_IMAGE_WORKERS = 8

_IMAGE_SUFFIXES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}

INDEX_PATH = "index.html"
STYLESHEET_PATH = "style.css"
COVER_PATH = "media/cover.jpg"

VIEWER_CSS = """
:root { color-scheme: light dark; }
body { margin: 0 auto; max-width: 48rem; padding: 1.5rem 1.25rem 4rem;
       font-family: Georgia, "Times New Roman", serif; line-height: 1.55; }
nav.booknav { display: flex; gap: 1rem; flex-wrap: wrap; align-items: baseline;
              border-bottom: 1px solid #8884; padding-bottom: .6rem; margin-bottom: 1.5rem;
              font-family: system-ui, sans-serif; font-size: .85rem; }
nav.booknav a { text-decoration: none; }
nav.booknav .spacer { flex: 1; }
main img { max-width: 100%; height: auto; }
main h1, main h2, main h3, main h4, main h5, main h6 { line-height: 1.3; }
main p { margin: 0 0 .85em; }
main blockquote { margin: 1em 1.5em; }
main table { border-collapse: collapse; max-width: 100%; }
main td, main th { padding: .15em .5em; vertical-align: top; }
.toc { font-family: system-ui, sans-serif; font-size: .95rem; padding-left: 0; }
.toc li { list-style: none; margin: .25rem 0; }
.toc .depth-0 { font-weight: 700; margin-top: 1rem; }
.toc .depth-1 { font-weight: 600; padding-left: 1rem; }
.toc .depth-2 { padding-left: 2rem; }
.meta { font-family: system-ui, sans-serif; font-size: .9rem; }
.meta dt { font-weight: 600; }
.subjects { font-family: system-ui, sans-serif; font-size: .9rem; padding-left: 1.2rem; }
.cover { float: right; max-width: 180px; margin: 0 0 1rem 1.5rem; }
footer.bookfoot { margin-top: 3rem; border-top: 1px solid #8884; padding-top: .8rem;
                  font-family: system-ui, sans-serif; font-size: .8rem; }
"""


@dataclass
class Section:
    """One article of the book."""

    title: str
    nodes: list[Tag]
    path: str = ""
    depth: int = 2
    toc_title: str | None = None


@dataclass(frozen=True)
class ContentsEntry:
    """One line of the book's own table of contents."""

    title: str
    depth: int
    target: Tag
    division: bool = False


@dataclass
class ContentsBlock:
    """A contents page found in the book."""

    heading: str
    entries: list[ContentsEntry]


@dataclass
class ParsedBook:
    """Parsed book, ready to be written into a ZIM archive."""

    meta: BookMeta
    pages: list[Page] = field(default_factory=list)
    assets: list[Asset] = field(default_factory=list)
    stylesheet: str = ""
    cover: bytes | None = None
    favicon: bytes | None = None
    index_path: str = INDEX_PATH
    section_count: int = 0
    word_count: int = 0


def parse_book(
    document: str,
    meta: BookMeta,
    source_url: str,
    media_dir: Path,
    fetch,
    cover: Asset | None = None,
    modernize: bool = False,
) -> ParsedBook:
    """Parse Gutenberg HTML into pages, media and a table of contents.

    ``fetch(url, cache_key)`` returns bytes and raises on failure; images that
    cannot be downloaded are dropped instead of breaking the build. ``cover``
    is the ebook cover, stored under :data:`COVER_PATH` and reused when the book
    HTML references the same image. ``modernize`` rewrites archaic English
    ("thou knowest", "he cometh", "quoth") into present-day English.
    """
    soup = BeautifulSoup(document, "html.parser")
    if modernize:
        modernize_soup(soup)
    for tag in soup.find_all(["script", "noscript"]):
        tag.decompose()

    gutenberg_css = "\n".join(str(style) for style in soup.find_all("style"))
    for style in soup.find_all("style"):
        style.decompose()

    nodes = _content_nodes(soup)
    contents = _declared_contents(soup, nodes)
    sections = _split_sections(nodes, contents)
    if not sections:
        raise ValueError(f"no readable content found in {source_url}")
    _assign_paths(sections)
    anchors = _anchor_map(sections)
    seen: dict[str, str] = {cover.zim_path: cover.url} if cover else {}
    content_assets = _rewrite(sections, anchors, source_url, media_dir, fetch, seen)
    assets: dict[str, Asset] = {}
    for asset in ([cover] if cover else []) + content_assets:
        assets.setdefault(asset.zim_path, asset)

    toc = _collect_toc(sections, contents)
    pages = [
        Page(
            path=section.path,
            title=section.title,
            body=_render_section(section, meta, sections),
        )
        for section in sections
    ]
    pages.insert(0, render_index(meta, toc, cover_path=cover.zim_path if cover else None))

    words = sum(len(node.get_text(" ", strip=True).split()) for section in sections for node in section.nodes)
    return ParsedBook(
        meta=meta,
        pages=pages,
        assets=list(assets.values()),
        stylesheet=gutenberg_css + "\n" + VIEWER_CSS,
        cover=_cover_png(cover.filepath, 192) if cover else None,
        favicon=_cover_png(cover.filepath, 48) if cover else None,
        section_count=len(sections),
        word_count=words,
    )


# --------------------------------------------------------------------------- #
# Content extraction
# --------------------------------------------------------------------------- #


def _content_nodes(soup: BeautifulSoup) -> list[Tag]:
    """Top-level body nodes with Gutenberg header/footer boilerplate removed."""
    body = soup.body or soup
    nodes = [child for child in body.children if isinstance(child, Tag)]
    start, end = 0, len(nodes)

    for index, node in enumerate(nodes):
        identifier = node.get("id") or ""
        classes = node.get("class") or []
        if node.name == "footer" or identifier == "pg-footer":
            end = min(end, index)
            break
        if node.name == "header" or identifier == "pg-header" or "pgheader" in classes:
            start = max(start, index + 1)

    for index in range(start, end):
        text = nodes[index].get_text(" ", strip=True).upper()
        if "*** START OF" in text:
            start = index + 1
        elif "*** END OF" in text:
            end = index
            break

    selected: list[Tag] = []
    for node in nodes[start:end]:
        # Gutenberg wraps page markers ("[Pg 111]") in these divs, but the same
        # wrapper also holds display math in image form. Only drop text-only ones.
        if "pg_body_wrapper" in (node.get("class") or []) and not node.find("img"):
            continue
        text = node.get_text(" ", strip=True).upper()
        if node.name in _HEADINGS and "PROJECT GUTENBERG" in text and "LICENSE" in text:
            break
        if node.get_text(" ", strip=True) or node.find("img"):
            selected.append(node)
    return selected


def _heading_text(node: Tag) -> str:
    """Heading text without figure captions, footnote markers or inline images."""
    clone = BeautifulSoup(str(node), "html.parser").find(node.name)
    if clone is None:
        return ""
    for extra in clone.select("span.caption, img, a.fnanchor, a.footnote, .pagenum, span.pagenum"):
        extra.decompose()
    # Empty link targets carry no title; anchors that wrap the heading text
    # itself ("<a id=\"link1\">STAVE ONE.</a>") must survive.
    for anchor in clone.find_all("a", id=True):
        if not anchor.get_text(" ", strip=True):
            anchor.decompose()
    text = re.sub(r"\s+", " ", clone.get_text(" ", strip=True)).strip()
    # Trailing page markers such as "[2137]" carry no title value.
    return re.sub(r"\s*(\[\d+\]|\{\d+\})$", "", text).strip()


def _is_section_head(node: Tag) -> bool:
    if node.name not in _HEADINGS:
        return False
    text = _heading_text(node)
    if not text or text.lower() in _SKIP_HEADINGS:
        return False
    return bool(_SECTION_RE.match(text) or _ORDINAL_RE.match(text) or _ROMAN_RE.match(text))


def _is_chapter_div(node: Tag) -> bool:
    """True for Gutenberg's ``<div class="chapter">`` wrapper."""
    return node.name in ("div", "section") and "chapter" in (node.get("class") or [])


def _is_contents_heading(node: Tag) -> bool:
    return node.name in _HEADINGS and bool(_CONTENTS_RE.match(_heading_text(node)))


def _clean_label(text: str) -> str:
    text = re.sub(r"\[Pg[^\]]*\]", " ", text)
    text = re.sub(r"\bPage\s+[\divxlcdm]+\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"[\s.,;:—–-]+$", "", text)


_TOC_CELL_RE = re.compile(
    r"(?:[\divxlcdmIVXLCDM]+\.?|page\s+[\divxlcdmIVXLCDM]+|facing(?:\s+\d+)?|\[?\d+\]?)",
    re.IGNORECASE,
)


def _row_label(link: Tag) -> str:
    """Title from the table row holding a contents link (Candide, Gregory).

    Contents tables lay a row out as "I. | How Candide was brought up… | 1", and
    the link sometimes wraps only the page number. The title is the longest
    non-numeric cell; a chapter numeral beside it is kept so the index reads like
    the printed page ("I. How Candide…", not the bare body heading).
    """
    row = link.find_parent("tr")
    if row is None or len(row.find_all("a", href=True)) > 1:
        # A single table cell may hold a whole list of works, one per line.
        return _clean_label(link.get_text(" ", strip=True))
    cells = [_clean_label(cell.get_text(" ", strip=True)) for cell in row.find_all(["td", "th"])]
    cells = [cell for cell in cells if cell]
    titles = [cell for cell in cells if not _TOC_CELL_RE.fullmatch(cell)]
    if not titles:
        return _clean_label(link.get_text(" ", strip=True))
    title = max(titles, key=len)
    marker = next((cell for cell in cells if cell != title and re.match(r"^[IVXLCDM]+\.?$", cell)), "")
    return f"{marker} {title}" if marker else title


def _inline_tail(link: Tag) -> str:
    """Text after a link up to the next line break ("I. </a> THE THREE…")."""
    parts: list[str] = []
    for sibling in link.next_siblings:
        if isinstance(sibling, Tag) and sibling.name == "br":
            break
        parts.append(sibling.get_text(" ", strip=True) if isinstance(sibling, Tag) else str(sibling))
        if sum(len(part) for part in parts) > 300:
            break
    return _clean_label(" ".join(parts))


def _is_division_link(link: Tag, title: str) -> bool:
    """True when a contents line labels a part/volume rather than a chapter.

    Gutenberg's contents pages set part and volume lines in bold (``<b>VOLUME
    I.</b>``, ``<b>FIRST PART.</b>``) while chapter lines stay plain, so the
    markup itself tells the nesting levels apart.
    """
    if re.match(r"^\s*(?:part|volume)\b", title, re.IGNORECASE):
        return True
    return link.find_parent(["b", "strong"]) is not None or link.find(["b", "strong"]) is not None


def _entries_between(heading: Tag) -> tuple[list[tuple[str, str, int, bool]], set[int]]:
    """``(fragment, title, depth, division)`` for the links under ``heading``."""
    level = int(heading.name[1])
    found: list[tuple[str, str, int, bool]] = []
    region: set[int] = set()
    for element in heading.find_all_next():
        if not isinstance(element, Tag):
            continue
        if element.name in _HEADINGS and int(element.name[1]) <= level:
            break
        region.add(id(element))
        href = (element.get("href") or "").strip() if element.name == "a" else ""
        if not href.startswith("#"):
            continue
        fragment = href[1:]
        label = _row_label(element)
        if len(re.findall(r"[A-Za-z]", label)) < 3:
            # "I. </a> THE THREE METAMORPHOSES." puts the title outside the link.
            tail = _inline_tail(element)
            if tail:
                label = _clean_label(f"{label} {tail}" if label else tail)
        if not fragment or not label or _PLACEHOLDER_RE.match(label):
            continue
        if re.fullmatch(r"[IVXLCDM]+\.?", label):
            # "III. </a> BACKWORLDSMEN." on a separate line: the title follows.
            label = _clean_label(f"{label} {_inline_tail(element)}")
        found.append((fragment, label, _title_depth(label), _is_division_link(element, label)))
    return found, region


_PLACEHOLDER_RE = re.compile(r"^(?:ibid|page\s+[\divxlcdm]+|\d+|\[\d+\])$", re.IGNORECASE)


_MARKER_RE = re.compile(
    r"^\s*(?:(?:chapter|part|book|volume|section|canto|act|scene|letter|stave|prologue|"
    r"epilogue|appendix|preface|introduction|notes|index)\b\.?|[ivxlcdm]+\.|\d+\.)\s*",
    re.IGNORECASE,
)


def _norm_title(text: str) -> str:
    stripped = _MARKER_RE.sub("", text).strip()
    text = stripped or text  # "NOTES" is a title, not a "notes" marker
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _title_matches(heading: Tag, title: str) -> bool:
    text = _norm_title(_heading_text(heading))
    wanted = _norm_title(title)
    if not text or not wanted:
        return False
    return text == wanted or (len(wanted) > 6 and wanted in text) or (len(text) > 6 and text in wanted)


def _resolve_entry(
    fragment: str,
    title: str,
    window: tuple[int, int],
    positions: dict[int, int],
    anchors: dict[str, Tag],
) -> Tag | None:
    """Article start for one contents entry.

    Entries link either to the heading itself (modern Gutenberg) or to an
    invisible page anchor just above it, sometimes several paragraphs early
    (Gregory of Tours links to the chapter list that opens the page, not to
    the book text). Prefer the nearest heading that carries the entry's own
    title, else the first section-style heading before the next entry.
    """
    anchor = anchors.get(fragment)
    if anchor is None:
        return None
    for parent in anchor.parents:
        if parent.name in _HEADINGS:
            return parent
    # The anchor often sits below its chapter heading, inside the same wrapper
    # (a page marker under "<h2>NOTES</h2>").
    for parent in anchor.parents:
        if not _is_chapter_div(parent):
            continue
        heading = parent.find(_HEADINGS)
        if heading is not None and _title_matches(heading, title):
            return heading
        break
    fallback: Tag | None = None
    current = anchor
    while True:
        current = current.find_next(True)
        if current is None:
            break
        index = positions.get(id(current))
        if index is None or index >= window[1]:
            break
        if current.name in _HEADINGS:
            if _title_matches(current, title):
                return current
            if fallback is None and _is_section_head(current):
                fallback = current
    return fallback


def _declared_contents(soup: BeautifulSoup, nodes: list[Tag]) -> ContentsBlock | None:
    """Return the book's own table of contents when it can drive the split.

    A block qualifies when every entry resolves inside the body of the book and
    the entries run forward through the document. That keeps a "CONTENTS" list
    of front matter only, and the alphabetical index Gutenberg prints beside
    the real contents (Maupassant), from deciding where articles start.
    """
    order = {id(element): index for index, element in enumerate(soup.find_all(True))}
    anchors: dict[str, Tag] = {}
    for element in soup.find_all(True):
        for attribute in ("id", "name"):
            value = element.get(attribute)
            if value:
                anchors.setdefault(value, element)
    content = {id(node) for node in nodes}
    for node in nodes:
        content.update(id(element) for element in node.find_all(True))

    def in_body(element: Tag) -> bool:
        return id(element) in content or any(id(parent) in content for parent in element.parents)

    blocks: list[ContentsBlock] = []
    for heading in soup.find_all(_HEADINGS):
        if not _is_contents_heading(heading):
            continue
        raw, region = _entries_between(heading)
        if len(raw) < 3:
            continue
        bounds = [order.get(id(anchors[row[0]]), -1) for row in raw]
        if any(bound < 0 for bound in bounds):
            continue
        entries: list[ContentsEntry] = []
        for index, (fragment, title, depth, division) in enumerate(raw):
            window = (bounds[index], bounds[index + 1] if index + 1 < len(bounds) else len(order))
            target = _resolve_entry(fragment, title, window, order, anchors)
            # An entry whose heading is not where the list says belongs to the
            # preceding article; dropping it is safer than splitting mid-text.
            if target is None or not in_body(target) or any(entry.target is target for entry in entries):
                continue
            entries.append(ContentsEntry(title=title, depth=depth, target=target, division=division))
        if len(entries) < 3:
            continue
        positions = [order[id(entry.target)] for entry in entries]
        if any(later < earlier for earlier, later in zip(positions, positions[1:], strict=False)):
            continue
        # Links that all point inside the listing itself are an index of its own
        # page numbers, not a table of contents (Gutenberg prints an alphabetical
        # index beside Maupassant's real contents).
        if all(id(entry.target) in region for entry in entries):
            continue
        if positions[-1] < len(order) * 0.4:
            continue
        blocks.append(ContentsBlock(heading=_heading_text(heading), entries=entries))

    # A multi-volume ebook prints one contents page per volume (Don Quixote);
    # together they describe the book, and they must not overlap.
    blocks.sort(key=lambda block: order[id(block.entries[0].target)])
    merged: list[ContentsEntry] = []
    for block in blocks:
        first = order[id(block.entries[0].target)]
        if merged and first <= order[id(merged[-1].target)]:
            continue
        merged.extend(block.entries)
    if len(merged) < 3:
        return None
    seen: set[int] = set()
    unique: list[ContentsEntry] = []
    for entry in merged:
        if id(entry.target) in seen:
            continue
        seen.add(id(entry.target))
        unique.append(entry)
    return ContentsBlock(heading=blocks[0].heading if blocks else "", entries=_depth_entries(unique))


def _depth_entries(entries: list[ContentsEntry]) -> list[ContentsEntry]:
    """Assign nesting levels from the contents page itself.

    Part and volume lines are set in bold, chapter lines are not; a contents
    page with no such dividers (Candide, Gregory of Tours) is a flat list.
    """
    nested = any(entry.division for entry in entries)
    seen_division = False
    levelled: list[ContentsEntry] = []
    for entry in entries:
        seen_division = seen_division or entry.division
        depth = 2 if nested and seen_division and not entry.division else 1
        levelled.append(ContentsEntry(title=entry.title, depth=depth, target=entry.target, division=entry.division))
    return levelled


def _article_starts(nodes: list[Tag], contents: ContentsBlock | None) -> dict[int, Tag]:
    """Elements that start a new article, keyed by object id.

    The declared contents page names the articles when it describes the whole
    book, and structural boundaries are added for chapters it does not list
    (Gulliver lists only its voyages, Don Quixote only its second volume). Those
    extras must carry real prose: title pages, copyright notices and printed
    chapter lists between the contents page and the first chapter stay with the
    front matter instead of becoming stub articles.
    """
    if contents is not None:
        starts: dict[int, Tag] = {}
        declared = {id(entry.target) for entry in contents.entries}
        for entry in contents.entries:
            owner = _chapter_owner(entry.target, declared)
            starts[id(owner)] = owner
        first = _document_order(contents.entries[0].target)
        for node in _descendants(nodes):
            if not _is_chapter_div(node) or id(node) in starts:
                continue
            if _document_order(node) <= first:
                continue
            if not _opens_article(node):
                continue
            starts[id(node)] = node
        return starts
    starts = {
        id(node): node
        for node in _descendants(nodes)
        if _is_chapter_div(node) and not any(_is_chapter_div(child) for child in node.find_all(True))
    }
    starts = _expand_containers(starts)
    if starts:
        return starts
    return {id(node): node for node in _descendants(nodes) if _is_section_head(node) or _is_contents_heading(node)}


def _expand_containers(starts: dict[int, Tag]) -> dict[int, Tag]:
    """Replace a container that swallows several chapters by those chapters.

    Some ebooks wrap the entire book in a single ``div.chapter`` (Gregory of
    Tours); taking that wrapper as one article would collapse the book into a
    single page, so the section headings inside it become the boundaries.
    """
    expanded: dict[int, Tag] = {}
    for start in starts.values():
        headings = [heading for heading in start.find_all(_HEADINGS) if _is_section_head(heading)]
        if len(headings) > 1:
            expanded.update({id(heading): heading for heading in headings})
        else:
            expanded[id(start)] = start
    return expanded


def _document_order(node: Tag) -> int:
    """Position of ``node`` among its document's tags."""
    root = node
    while root.parent is not None:
        root = root.parent
    for index, element in enumerate(root.find_all(True)):
        if element is node:
            return index
    return -1


def _opens_article(node: Tag) -> bool:
    """True for a chapter wrapper with a heading and prose of its own."""
    heading = node.find(_HEADINGS)
    if heading is None or not _is_section_head(heading):
        return False
    if any(_is_contents_heading(child) for child in node.find_all(_HEADINGS)):
        return False
    if _is_listing(node):
        return False
    words = len(node.get_text(" ", strip=True).split())
    return words >= 40


def _is_listing(node: Tag) -> bool:
    """True for a wrapper that is a printed list of chapters, not prose."""
    lists = node.find_all(["ul", "ol"])
    if not lists:
        return False
    listed = sum(len(entry.get_text(" ", strip=True).split()) for entry in lists)
    total = len(node.get_text(" ", strip=True).split())
    return total > 0 and listed * 4 >= total * 3


def _chapter_owner(target: Tag, declared: set[int]) -> Tag:
    """Chapter wrapper that contains ``target`` and no other declared entry."""
    for parent in target.parents:
        if not _is_chapter_div(parent):
            continue
        if sum(1 for member in [parent, *parent.find_all(True)] if id(member) in declared) <= 1:
            return parent
    return target


def _descendants(nodes: list[Tag]):
    for node in nodes:
        yield node
        yield from node.find_all(True)


def _split_sections(nodes: list[Tag], contents: ContentsBlock | None = None) -> list[Section]:
    """Group body nodes into sections, keeping front matter separate.

    Article boundaries come from the book's own contents page when it has a
    usable one, else from ``div.chapter`` wrappers, else from section headings.
    Nested wrappers are descended into, so a volume-level container (Maupassant)
    or one wrapper around the whole book (Gregory of Tours) still yields one
    article per chapter instead of one article per container.
    """
    starts = _article_starts(nodes, contents)
    # A book with no headings at all (some dictionaries, Principia) still has to
    # become articles, so chunking treats the whole body as one long article.
    pieces = _segments(nodes, starts, None) if starts else [(True, list(nodes))]
    front: list[Tag] = []
    groups: list[list[Tag]] = []
    for opened, tags in pieces:
        if opened or groups:
            if opened:
                groups.append(tags)
            else:
                groups[-1].extend(tags)
        else:
            front.extend(tags)

    # Heading-only dividers ("PART I", "Book II") become part of the next
    # article so the archive has no empty pages, but their headings stay. A
    # wrapper with no text at all (Gutenberg leaves empty chapter divs behind)
    # is dropped rather than turned into an empty article.
    merged: list[list[Tag]] = []
    pending: list[Tag] = []
    for group in groups:
        if not any(node.get_text(" ", strip=True) or node.find("img") for node in group):
            continue
        if _is_divider(group):
            pending.extend(group)
            continue
        merged.append(pending + group)
        pending = []
    if pending:
        if merged:
            merged[-1].extend(pending)
        elif front:
            front.extend(pending)
        else:
            merged.append(pending)

    sections: list[Section] = []
    if front:
        sections.append(Section(title="Front matter", nodes=front, depth=1))
    for group in merged:
        entries = _contents_entries(group, contents)
        if entries:
            deepest = max(entries, key=lambda entry: entry.depth)
            base = deepest.title
            depth = deepest.depth
        else:
            base = _section_title(group)
            depth = _title_depth(base)
        if not any(node.get_text(" ", strip=True) for node in group) and not entries:
            continue  # cover-only wrapper: the cover is an illustration already
        for index, chunk in enumerate(_chunk_nodes(group)):
            title = base if entries else _chunk_title(chunk, base, index)
            sections.append(Section(title=title, nodes=chunk, depth=depth, toc_title=base))
    _normalise_depths(sections)
    return sections


def _normalise_depths(sections: list[Section]) -> None:
    """Shift article levels so the shallowest body article is level 1.

    A contents page that marks only chapters (Candide, Gregory of Tours) would
    otherwise indent every article one level under the front page.
    """
    body = [section.depth for section in sections if section.title != "Front matter"]
    if not body:
        return
    shift = min(body) - 1
    if not shift:
        return
    for section in sections:
        if section.title != "Front matter":
            section.depth = max(section.depth - shift, 1)


def _segments(nodes: list[Tag], starts: dict[int, Tag], wrapper: Tag | None) -> list[tuple[bool, list[Tag]]]:
    """Split ``nodes`` at the start elements, cloning the wrappers around them.

    Returns ``(opened, tags)`` pairs; ``opened`` is true when ``tags`` begin an
    article at a start element. Leading tags before the first start come back as
    a single unopened segment so they can become the front matter.
    """
    pieces: list[tuple[bool, list[Tag]]] = []
    current: list[Tag] = []

    def flush() -> None:
        if current:
            pieces.append((False, _wrap_all(wrapper, list(current))))
            current.clear()

    for node in nodes:
        started = id(node) in starts
        nested = _contains_start(node, starts)
        if not started and not nested:
            current.append(node)
            continue
        flush()
        if started and not nested:
            pieces.append((True, _wrap_all(wrapper, [node])))
            continue
        leading: list[Tag] = []
        rest: list[Tag] = []
        for child in _element_children(node):
            if rest or id(child) in starts or _contains_start(child, starts):
                rest.append(child)
            else:
                leading.append(child)
        if started:
            pieces.append((True, _wrap_all(wrapper, _wrap_all(node, leading))))
        elif leading:
            pieces.append((False, _wrap_all(wrapper, _wrap_all(node, leading))))
        for child_started, tags in _segments(rest, starts, node):
            pieces.append((child_started, _wrap_all(wrapper, tags)))
    flush()
    return [piece for piece in pieces if piece[1]]


def _contains_start(node: Tag, starts: dict[int, Tag]) -> bool:
    return any(id(child) in starts for child in node.find_all(True))


def _wrap_all(wrapper: Tag | None, children: list[Tag]) -> list[Tag]:
    if not children:
        return []
    if wrapper is None:
        return children
    clone = Tag(name=wrapper.name, attrs={key: value for key, value in wrapper.attrs.items() if key != "id"})
    for child in children:
        clone.append(child.extract())
    return [clone]


def _contents_entries(group: list[Tag], contents: ContentsBlock | None) -> list[ContentsEntry]:
    """Declared entries whose anchors fall inside ``group``, in book order."""
    if contents is None:
        return []
    members = {id(element) for node in group for element in [node, *node.find_all(True)]}
    return [entry for entry in contents.entries if id(entry.target) in members]


def _is_divider(group: list[Tag]) -> bool:
    """True for a heading-only wrapper such as ``<div class="chapter"><h2>PART I</h2></div>``."""
    if any(node.find("img") for node in group):
        return False
    for node in group:
        if node.name in _HEADINGS:
            continue
        clone = BeautifulSoup(str(node), "html.parser").find(node.name)
        if clone is None:
            continue
        for heading in clone.find_all(_HEADINGS):
            heading.decompose()
        if clone.get_text(" ", strip=True):
            return False
    return True


def _node_size(node: Tag) -> int:
    """UTF-8 size of a node's HTML, matching what lands in the archive."""
    return len(str(node).encode("utf-8"))


def _element_children(node: Tag) -> list[Tag]:
    return [child for child in node.children if isinstance(child, Tag)]


def _chunk_nodes(nodes: list[Tag]) -> list[list[Tag]]:
    """Split ``nodes`` into chunks of at most :data:`MAX_ARTICLE_BYTES` of HTML.

    A single oversized node is exploded into cloned wrappers, each holding one
    slice of its children, so dictionary and bible-style single-file books still
    yield many articles instead of one giant page.
    """
    budget = MAX_ARTICLE_BYTES - _PAGE_CHROME_BYTES
    chunks: list[list[Tag]] = []
    current: list[Tag] = []
    size = 0

    for node in nodes:
        node_size = _node_size(node)
        if node_size > budget:
            if current:
                chunks.append(current)
                current, size = [], 0
            chunks.extend(_explode(node))
            continue
        if current and size + node_size > budget:
            chunks.append(current)
            current, size = [], 0
        current.append(node)
        size += node_size

    if current:
        chunks.append(current)
    return chunks or [nodes]


def _explode(node: Tag) -> list[list[Tag]]:
    """Split one oversized node into cloned wrappers around slices of its children.

    Recurses when a single child is still too large (e.g. one dictionary letter
    inside an already oversized body ``<div>``).
    """
    children = _element_children(node)
    if not children:
        return [[node]]

    budget = MAX_ARTICLE_BYTES - _PAGE_CHROME_BYTES
    slices: list[list[Tag]] = []
    current: list[Tag] = []
    size = 0
    for child in children:
        child_size = _node_size(child)
        if current and size + child_size > budget:
            slices.append(current)
            current, size = [], 0
        current.append(child)
        size += child_size
    if current:
        slices.append(current)

    exploded: list[list[Tag]] = []
    for group in slices:
        if len(group) == 1 and _node_size(group[0]) > budget:
            exploded.extend(_explode(group[0]))
            continue
        wrapper = Tag(name=node.name, attrs=dict(node.attrs))
        for child in group:
            wrapper.append(child.extract())
        exploded.append([wrapper])
    return exploded


def _chunk_title(chunk: list[Tag], base: str, index: int) -> str:
    """Title for one chunk, taken from the heading(s) the chunk starts with.

    Dividers such as ``PART I`` precede the real chapter heading, so the deepest
    heading of the leading run of nodes wins; headings further down belong to
    later content in the same article and must not retitle it.
    """
    leading: list[Tag] = []
    for node in chunk:
        if node.name in _HEADINGS:
            leading.append(node)
            continue
        headings = _group_headings([node])
        if _is_divider([node]):
            leading.extend(headings)
            continue
        # The first real content node may itself open with a heading.
        leading.extend(headings)
        break

    titles = [(heading, _heading_text(heading)) for heading in leading]
    titles = [(heading, text) for heading, text in titles if text and text.lower() not in _SKIP_HEADINGS]
    if titles:
        # A wrapper's own heading ("HELL OR THE INFERNO") is often plain prose
        # while the real unit below it is a canto or chapter; that unit wins.
        section = [(heading, text) for heading, text in titles if _is_section_head(heading)]
        pool = section or titles
        deepest = max(_title_depth(text) for _heading, text in pool)
        for _heading, text in reversed(pool):
            if _title_depth(text) == deepest:
                return text

    text = re.sub(r"\s+", " ", " ".join(node.get_text(" ", strip=True) for node in chunk[:2])).strip()
    headword = re.split(r"[,;:.]|\s{2,}", text, maxsplit=1)[0].strip()
    if headword and len(headword) <= 60:
        return headword
    return f"{base} ({index + 1})" if index else base


def _group_headings(group: list[Tag]) -> list[Tag]:
    headings: list[Tag] = []
    for node in group:
        if node.name in _HEADINGS:
            headings.append(node)
        else:
            headings.extend(node.find_all(_HEADINGS))
    return headings


def _has_section_head(group: list[Tag]) -> bool:
    return any(_is_section_head(node) for node in group)


def _section_title(group: list[Tag]) -> str:
    headings = _group_headings(group)
    candidates = [node for node in headings if _is_section_head(node)] or headings
    for heading in reversed(candidates):
        text = _heading_text(heading)
        if text:
            return text
    text = group[0].get_text(" ", strip=True) if group else ""
    return re.sub(r"\s+", " ", text)[:80] or "Untitled"


def _title_depth(title: str) -> int:
    """Nesting level implied by a heading: parts/volumes above chapters."""
    match = _SECTION_RE.match(title)
    if match:
        return _DEPTH.get(match.group(1).lower(), 1)
    ordinal = _ORDINAL_RE.match(title)
    if ordinal:
        return _DEPTH.get(ordinal.group(1).lower(), 1)
    return 2


def _assign_paths(sections: list[Section]) -> None:
    counter = 0
    for section in sections:
        if section.title == "Front matter":
            section.path = "front-matter.html"
            continue
        counter += 1
        section.path = f"chapter/{counter:03d}.html"


def _anchor_map(sections: list[Section]) -> dict[str, str]:
    anchors: dict[str, str] = {}
    for section in sections:
        for node in section.nodes:
            for element in [node, *node.find_all(True)]:
                attributes = ("id", "name") if element.name == "a" else ("id",)
                for attribute in attributes:
                    value = element.get(attribute)
                    if value:
                        anchors.setdefault(value, section.path)
    return anchors


def _collect_toc(sections: list[Section], contents: ContentsBlock | None = None) -> list[TocEntry]:
    """Build the front page table of contents.

    With a declared contents page the index reproduces it entry for entry, so
    the reader sees the printed structure — volumes, parts, chapter numerals —
    even where the articles themselves are titled differently. Otherwise the
    index mirrors the section headings.
    """
    entries: list[TocEntry] = []
    owners = _section_owners(sections)
    heads: dict[str, Tag] = {}
    for section in sections:
        heads[section.path] = section.nodes[0] if section.nodes else None
    if contents is not None:
        for entry in contents.entries:
            path = owners.get(id(entry.target))
            if path is None:
                continue
            entries.append(TocEntry(depth=entry.depth, title=entry.title, href=_entry_href(path, entry.target, heads)))
        front = next((section for section in sections if section.title == "Front matter"), None)
        if front is not None:
            entries.insert(0, TocEntry(depth=0, title=front.title, href=front.path))
        return entries

    for section in sections:
        if section.title == "Front matter":
            entries.append(TocEntry(depth=0, title=section.title, href=section.path))
            continue
        headings = [heading for heading in _group_headings(section.nodes) if _is_section_head(heading)]
        if not headings:
            entries.append(TocEntry(depth=section.depth, title=section.title, href=section.path))
            continue
        for heading in headings:
            title = _heading_text(heading)
            anchor = _heading_anchor(heading)
            href = f"{section.path}#{anchor}" if anchor else section.path
            entries.append(TocEntry(depth=_title_depth(title), title=title, href=href))
    return _nest(entries)


def _entry_href(path: str, target: Tag, heads: dict[str, Tag]) -> str:
    """Link to the entry's own anchor unless it already opens the article."""
    first = heads.get(path)
    if first is None:
        return path
    if target is first or target in first.find_all(True):
        return path
    anchor = target.get("id")
    if anchor is None:
        link = target.find("a", id=True)
        anchor = link.get("id") if link else None
    return f"{path}#{anchor}" if anchor else path


def _heading_anchor(heading: Tag) -> str | None:
    if heading.get("id"):
        return heading.get("id")
    link = heading.find("a", id=True)
    return link.get("id") if link else None


def _section_owners(sections: list[Section]) -> dict[int, str]:
    """Map every element inside an article to that article's path."""
    owners: dict[int, str] = {}
    for section in sections:
        for node in section.nodes:
            owners[id(node)] = section.path
            for element in node.find_all(True):
                owners[id(element)] = section.path
    return owners


def _nest(entries: list[TocEntry]) -> list[TocEntry]:
    """Map heading depths onto consecutive levels starting at 1.

    Headline levels are only relative: a book's chapters may all be ``h2`` while
    its parts are ``h2`` as well (Treasure Island), and the index title can be an
    ``h1`` that would otherwise outrank them. Ranking the distinct levels keeps
    the nesting intact and leaves no gap for the front page to render.
    """
    body = {entry.depth for entry in entries if entry.title != "Front matter"}
    if not body:
        return entries
    levels = {depth: rank + 1 for rank, depth in enumerate(sorted(body))}
    nested: list[TocEntry] = []
    previous = 0
    for entry in entries:
        depth = 0 if entry.title == "Front matter" else min(levels[entry.depth], previous + 1)
        nested.append(TocEntry(depth=depth, title=entry.title, href=entry.href))
        previous = depth
    return nested


# --------------------------------------------------------------------------- #
# Rewriting links and media
# --------------------------------------------------------------------------- #


def _rewrite(
    sections: list[Section],
    anchors: dict[str, str],
    source_url: str,
    media_dir: Path,
    fetch,
    seen: dict[str, str],
) -> list[Asset]:
    assets: dict[str, Asset] = {}
    prefetched = _prefetch_images(sections, source_url, media_dir, fetch, seen)
    # Images first: links that wrap a figure must point at the stored copy.
    image_targets: dict[str, str] = {}
    for section in sections:
        for node in section.nodes:
            for image in node.find_all("img"):
                source = image.get("src")
                if not source:
                    image.decompose()
                    continue
                url = urljoin(source_url, source)
                asset = _fetch_image(url, media_dir, seen, prefetched)
                if asset is None:
                    image.decompose()
                    continue
                image_targets[url] = asset.zim_path
                image["src"] = _relative(asset.zim_path, section.path)
                for attribute in ("srcset", "id", "longdesc"):
                    image.attrs.pop(attribute, None)
                assets.setdefault(asset.zim_path, asset)

            for source_tag in node.find_all("source"):
                source_tag.attrs.pop("srcset", None)

    for section in sections:
        for node in section.nodes:
            for link in node.find_all("a"):
                href = link.get("href")
                if not href:
                    continue
                target = image_targets.get(urljoin(source_url, href.strip()))
                link["href"] = _relative(target, section.path) if target else _rewrite_href(href, section.path, anchors)
    return list(assets.values())


def _rewrite_href(href: str, current_path: str, anchors: dict[str, str]) -> str:
    href = html_lib.unescape(href.strip())
    if not href or href.startswith(("mailto:", "javascript:")):
        return href

    parsed = urlparse(href)
    fragment = parsed.fragment
    if fragment and (not parsed.netloc or parsed.netloc.endswith("gutenberg.org")):
        target = anchors.get(fragment)
        if target:
            return f"#{fragment}" if target == current_path else _relative(target, current_path) + f"#{fragment}"
    return href


def _relative(target: str, current: str) -> str:
    """URL of ``target`` relative to the entry ``current``."""
    if target == current:
        return posixpath.basename(target)
    return posixpath.relpath(target, posixpath.dirname(current) or ".")


def _image_urls(sections: list[Section], source_url: str) -> list[str]:
    """Every image URL in document order, de-duplicated."""
    urls: dict[str, None] = {}
    for section in sections:
        for node in section.nodes:
            for image in node.find_all("img"):
                source = image.get("src")
                if not source:
                    continue
                url = urljoin(source_url, source)
                parsed = urlparse(url)
                if Path(unquote(parsed.path)).suffix.lower() in _IMAGE_SUFFIXES:
                    urls[url] = None
    return list(urls)


def _prefetch_images(
    sections: list[Section], source_url: str, media_dir: Path, fetch, seen: dict[str, str]
) -> dict[str, Asset]:
    """Download every referenced image concurrently, returning ready assets.

    Illustrated ebooks reference thousands of images; fetching them one at a
    time dominates the build. Each worker stores its own payload, so peak memory
    stays at one image per worker instead of the whole book. Failures are
    omitted from the result and the rewriting pass drops those images.
    """
    # Several URLs can share a basename (and therefore a zim path). Only the
    # first claimant is fetched up front, so concurrent workers never write the
    # same file; the rest are fallbacks when that download fails.
    claimants: dict[str, list[str]] = {}
    for url in _image_urls(sections, source_url):
        zim_path = _zim_path_for(url)
        if zim_path is not None and zim_path not in seen and zim_path not in claimants:
            claimants[zim_path] = [url]
        elif zim_path in claimants and url not in claimants[zim_path]:
            claimants[zim_path].append(url)

    def download(item: tuple[str, list[str]]) -> tuple[str, Asset | None]:
        for url in item[1]:
            asset = _store_image(url, media_dir, fetch)
            if asset is not None:
                return item[0], asset
        return item[0], None

    if not claimants:
        return {}
    with ThreadPoolExecutor(max_workers=_IMAGE_WORKERS) as pool:
        results = pool.map(download, claimants.items())
        return {asset.url: asset for _zim_path, asset in results if asset is not None}


def _zim_path_for(url: str) -> str | None:
    name = Path(unquote(urlparse(url).path)).name
    if not name or Path(name).suffix.lower() not in _IMAGE_SUFFIXES:
        return None
    return "media/" + _safe_name(name)


def _store_image(url: str, media_dir: Path, fetch) -> Asset | None:
    """Download ``url`` and write it to its media path, one file per call."""
    zim_path = _zim_path_for(url)
    if zim_path is None:
        return None
    try:
        payload = fetch(url, url_cache_key(url))
    except Exception:  # noqa: BLE001 - optional media must not fail the build
        return None
    destination = media_dir / zim_path.removeprefix("media/")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    return Asset(url=url, zim_path=zim_path, mimetype=_IMAGE_SUFFIXES[destination.suffix], filepath=destination)


def _fetch_image(url: str, media_dir: Path, seen: dict[str, str], prefetched: dict[str, Asset]) -> Asset | None:
    parsed = urlparse(url)
    name = Path(unquote(parsed.path)).name
    suffix = Path(name).suffix.lower()
    if not name or suffix not in _IMAGE_SUFFIXES:
        return None

    zim_path = "media/" + _safe_name(name)
    if zim_path in seen:
        return (
            None
            if seen[zim_path] != url
            else Asset(
                url=url,
                zim_path=zim_path,
                mimetype=_IMAGE_SUFFIXES[suffix],
                filepath=media_dir / zim_path.removeprefix("media/"),
            )
        )

    asset = prefetched.get(url)
    if asset is None:
        return None
    seen[zim_path] = url
    return asset


def _safe_name(name: str) -> str:
    stem, suffix = Path(name).stem, Path(name).suffix.lower()
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.") or "image"
    return cleaned + suffix


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _render_section(section: Section, meta: BookMeta, sections: list[Section]) -> str:
    index = sections.index(section)
    previous_link = next_link = ""
    if index > 0:
        previous = sections[index - 1]
        href = _relative(previous.path, section.path)
        previous_link = f'<a rel="prev" href="{href}">&larr; {html_lib.escape(previous.title)}</a>'
    if index + 1 < len(sections):
        following = sections[index + 1]
        href = _relative(following.path, section.path)
        next_link = f'<a rel="next" href="{href}">{html_lib.escape(following.title)} &rarr;</a>'

    contents = _relative(INDEX_PATH, section.path)
    nav = (
        '<nav class="booknav">'
        f'<a href="{contents}">Contents</a><span class="spacer"></span>'
        f"{previous_link}{next_link}</nav>"
    )
    language = meta.languages[0] if meta.languages else "en"
    body = "".join(str(node) for node in section.nodes)
    footer = f"{meta.title} — Project Gutenberg ebook #{meta.book_id}."
    return f"""<!DOCTYPE html>
<html lang="{html_lib.escape(language)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_lib.escape(section.title)} — {html_lib.escape(meta.title)}</title>
<link rel="stylesheet" href="{_relative(STYLESHEET_PATH, section.path)}">
</head>
<body>
{nav}
<main id="content">
{body}
</main>
<footer class="bookfoot">
<p>{html_lib.escape(footer)}</p>
</footer>
</body>
</html>
"""


def render_index(meta: BookMeta, toc: list[TocEntry], cover_path: str | None) -> Page:
    """Front page with metadata and a nested table of contents."""
    entries = "\n".join(
        f'<li class="depth-{min(entry.depth, 2)}"><a href="{entry.href}">{html_lib.escape(entry.title)}</a></li>'
        for entry in toc
    )
    creators = html_lib.escape(", ".join(meta.creators) or "Unknown")
    title = html_lib.escape(meta.title)
    cover = f'<img class="cover" src="{cover_path}" alt="Cover of {title}">' if cover_path else ""
    subjects = "".join(f"<li>{html_lib.escape(subject)}</li>" for subject in meta.subjects)
    contributors = "".join(f"<li>{html_lib.escape(person)}</li>" for person in meta.contributors)
    source = html_lib.escape(meta.source_url)
    ebook = html_lib.escape(meta.book_id)

    contributor_block = f'<h2>Contributors</h2><ul class="subjects">{contributors}</ul>' if contributors else ""
    subject_block = f'<h2>Subjects</h2><ul class="subjects">{subjects}</ul>' if subjects else ""
    language = meta.languages[0] if meta.languages else "en"
    body = f"""<!DOCTYPE html>
<html lang="{html_lib.escape(language)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Project Gutenberg #{ebook}</title>
<link rel="stylesheet" href="{STYLESHEET_PATH}">
</head>
<body>
<main id="content">
<h1>{title}</h1>
{cover}
<p class="meta">by {creators}</p>
<dl class="meta">
<dt>Source</dt><dd><a href="{source}">gutenberg.org/ebooks/{ebook}</a></dd>
<dt>Released</dt><dd>{html_lib.escape(meta.issued)}</dd>
<dt>Language</dt><dd>{html_lib.escape(meta.language_label)}</dd>
<dt>Rights</dt><dd>{html_lib.escape(meta.rights)}</dd>
</dl>
{contributor_block}
{subject_block}
<h2>Contents</h2>
<ol class="toc">
{entries}
</ol>
</main>
<footer class="bookfoot">
<p>{html_lib.escape(meta.title)} — Project Gutenberg ebook #{ebook}.</p>
</footer>
</body>
</html>
"""
    return Page(path=INDEX_PATH, title=meta.title, body=body)


def _cover_png(path: Path, size: int) -> bytes | None:
    """Render the downloaded cover as a square PNG of ``size`` pixels."""
    if not path.is_file():
        return None
    try:
        with Image.open(path) as image:
            source = ImageOps.exif_transpose(image).convert("RGBA")
    except OSError:
        return None

    thumbnail = ImageOps.contain(source, (size, size), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(
        thumbnail,
        ((size - thumbnail.width) // 2, (size - thumbnail.height) // 2),
        thumbnail,
    )
    buffer = BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()

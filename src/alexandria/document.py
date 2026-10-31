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
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from PIL import Image, ImageOps

from .models import Asset, BookMeta, Page, TocEntry

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
_ROMAN_RE = re.compile(r"^\s*[IVXLCDM]+\.\s+\S", re.IGNORECASE)
_DEPTH = {"part": 0, "volume": 0, "book": 1, "section": 1}
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
) -> ParsedBook:
    """Parse Gutenberg HTML into pages, media and a table of contents.

    ``fetch(url, cache_key)`` returns bytes and raises on failure; images that
    cannot be downloaded are dropped instead of breaking the build. ``cover``
    is the ebook cover, stored under :data:`COVER_PATH` and reused when the book
    HTML references the same image.
    """
    soup = BeautifulSoup(document, "html.parser")
    for tag in soup.find_all(["script", "noscript"]):
        tag.decompose()

    gutenberg_css = "\n".join(str(style) for style in soup.find_all("style"))
    for style in soup.find_all("style"):
        style.decompose()

    sections = _split_sections(_content_nodes(soup))
    if not sections:
        raise ValueError(f"no readable content found in {source_url}")

    _assign_paths(sections)
    anchors = _anchor_map(sections)
    seen: dict[str, str] = {cover.zim_path: cover.url} if cover else {}
    content_assets = _rewrite(sections, anchors, source_url, media_dir, fetch, seen)
    assets: dict[str, Asset] = {}
    for asset in ([cover] if cover else []) + content_assets:
        assets.setdefault(asset.zim_path, asset)

    toc = _collect_toc(sections)
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
        if "pg_body_wrapper" in (node.get("class") or []):
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
    for anchor in clone.find_all("a", id=True):
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
    return bool(_SECTION_RE.match(text) or _ROMAN_RE.match(text))


def _split_sections(nodes: list[Tag]) -> list[Section]:
    """Group body nodes into sections, keeping front matter separate."""
    chapter_divs = sum(1 for node in nodes if "chapter" in (node.get("class") or []))
    groups: list[list[Tag]] = []
    front: list[Tag] = []

    if chapter_divs >= 2:
        for node in nodes:
            if "chapter" in (node.get("class") or []):
                groups.append([node])
            elif groups:
                groups[-1].append(node)
            else:
                front.append(node)
    else:
        current: list[Tag] = []
        started = False
        for node in nodes:
            if _is_section_head(node):
                if current:
                    groups.append(current)
                current = [node]
                started = True
            else:
                current.append(node)
        if current:
            groups.append(current)
        if started and groups and not _has_section_head(groups[0]):
            front, groups = groups[0], groups[1:]

    # Heading-only dividers ("PART I", "Book II") become part of the next
    # article so the archive has no empty pages, but their headings stay.
    merged: list[list[Tag]] = []
    pending: list[Tag] = []
    for group in groups:
        if _is_divider(group):
            pending.extend(group)
            continue
        merged.append(pending + group)
        pending = []
    if pending:
        if merged:
            merged[-1].extend(pending)
        else:
            merged.append(pending)

    sections: list[Section] = []
    if front:
        sections.append(Section(title="Front matter", nodes=front, depth=1))
    for group in merged:
        base = _section_title(group)
        depth = _title_depth(base)
        for index, chunk in enumerate(_chunk_nodes(group)):
            sections.append(Section(title=_chunk_title(chunk, base, index), nodes=chunk, depth=depth))
    return sections


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
        deepest = max(_title_depth(text) for _heading, text in titles)
        for _heading, text in reversed(titles):
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
    match = _SECTION_RE.match(title)
    if not match:
        return 2
    return _DEPTH.get(match.group(1).lower(), 1)


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


def _collect_toc(sections: list[Section]) -> list[TocEntry]:
    """Build a nested table of contents from the article headings.

    Part/book dividers contribute their own, shallower entries, so the index
    mirrors the structure of the book instead of a flat chapter list.
    """
    entries: list[TocEntry] = []
    for section in sections:
        headings = _group_headings(section.nodes)
        candidates = [heading for heading in headings if _is_section_head(heading)]
        if section.title == "Front matter" or not candidates:
            entries.append(TocEntry(depth=section.depth, title=section.title, href=section.path))
            continue
        for heading in candidates:
            title = _heading_text(heading)
            anchor = heading.get("id")
            if anchor is None:
                link = heading.find("a", id=True)
                anchor = link.get("id") if link else None
            href = f"{section.path}#{anchor}" if anchor else section.path
            entries.append(TocEntry(depth=_title_depth(title), title=title, href=href))
    return entries


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
                asset = _fetch_image(url, media_dir, fetch, seen)
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


def _fetch_image(url: str, media_dir: Path, fetch, seen: dict[str, str]) -> Asset | None:
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

    try:
        payload = fetch(url, Path(zim_path))
    except Exception:  # noqa: BLE001 - optional media must not fail the build
        return None
    target = media_dir / zim_path.removeprefix("media/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    seen[zim_path] = url
    return Asset(url=url, zim_path=zim_path, mimetype=_IMAGE_SUFFIXES[suffix], filepath=target)


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
    downloads = f"<dt>Downloads</dt><dd>{meta.downloads}</dd>" if meta.downloads else ""
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
{downloads}
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

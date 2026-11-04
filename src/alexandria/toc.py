"""Audit a book's table of contents against the ebook's own contents page.

Gutenberg's HTML disagrees wildly about how a book is structured: chapters may
be bare numerals, headings may be hidden, a volume may wrap dozens of stories,
and a printed contents page may sit in a table, in plain links, or beside an
alphabetical index. Alexandria therefore reproduces the printed contents page
when the ebook has one. This module checks the result:

* every entry on the front page points at an article that exists, and at an
  anchor inside it when the entry names one;
* no entry is a placeholder (``[Pg 12]``, ``ibid``, ``Untitled``) and no two
  entries are identical;
* the index covers every article after the front matter, once each;
* nesting never skips a level;
* the entries match the edition currently published on gutenberg.org, so a
  stale cached download cannot silently produce the wrong structure.
"""

from __future__ import annotations

import html as html_lib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from bs4 import BeautifulSoup
from libzim.reader import Archive

from .document import ContentsBlock, _content_nodes, _declared_contents
from .gutenberg import fetch_html
from .net import Fetcher

# Bare roman numerals ("I", "XIV.") are real chapter markers and stay; these are
# the labels that carry no title at all.
_PLACEHOLDER_TITLE_RE = re.compile(
    r"^(?:|untitled|contents|ibid|\[pg[^\]]*\]|page\s+\S+|\d+(?:\.\d+)*)$",
    re.IGNORECASE,
)
_TOC_ENTRY_RE = re.compile(
    r'<li class="depth-(\d)"><a href="([^"]+)">(.*?)</a></li>',
    re.DOTALL,
)
_ANCHOR_RE = re.compile(r'(?:id|name)="([^"]+)"')


@dataclass(frozen=True)
class TocIssue:
    """One problem found in a book's table of contents."""

    severity: str  # "error" or "warning"
    message: str


@dataclass(frozen=True)
class Entry:
    depth: int
    title: str
    href: str


def entries_from_archive(archive: Archive) -> list[Entry]:
    """Read the front page table of contents out of a built ZIM."""
    index = archive.get_entry_by_path("index.html")
    body = bytes(index.get_item().content).decode("utf-8")
    return [
        Entry(depth=int(depth), title=_unescape(title), href=href) for depth, href, title in _TOC_ENTRY_RE.findall(body)
    ]


def _unescape(text: str) -> str:
    return html_lib.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def audit_archive(archive: Archive) -> list[TocIssue]:
    """Check one archive's table of contents and the links it publishes."""
    entries = entries_from_archive(archive)
    issues: list[TocIssue] = []
    if not entries:
        return [TocIssue("error", "front page has no table of contents")]

    pages = dict(_html_entries(archive))
    seen: dict[tuple[str, str], int] = {}
    linked: set[str] = set()
    previous_depth = 0
    for entry in entries:
        page, _, fragment = entry.href.partition("#")
        if _PLACEHOLDER_TITLE_RE.match(entry.title):
            issues.append(TocIssue("error", f"placeholder title {entry.title!r} for {entry.href}"))
        if page not in pages:
            issues.append(TocIssue("error", f"{entry.href} points at a missing article"))
            continue
        linked.add(page)
        if fragment:
            body = bytes(pages[page].get_item().content).decode("utf-8")
            if fragment not in _ANCHOR_RE.findall(body):
                issues.append(TocIssue("error", f"{entry.href} points at a missing anchor"))
        key = (entry.title.lower(), entry.href)
        seen[key] = seen.get(key, 0) + 1
        if entry.depth > previous_depth + 1:
            issues.append(TocIssue("error", f"{entry.title!r} skips a nesting level (depth {entry.depth})"))
        previous_depth = entry.depth

    for (title, href), count in seen.items():
        if count > 1:
            issues.append(TocIssue("error", f"{title!r} listed {count} times ({href})"))

    for path, entry in pages.items():
        if path == "index.html" or path in linked:
            continue
        issues.append(TocIssue("error", f"article {path} ({entry.title!r}) is missing from the table of contents"))
    return issues


class _ReadableEntry(Protocol):
    path: str
    title: str
    is_redirect: bool

    def get_item(self) -> object: ...


def _html_entries(archive: Archive) -> Iterable[tuple[str, _ReadableEntry]]:
    for entry_id in range(archive.entry_count):
        entry = archive.get_entry_by_id(entry_id)
        if entry.is_redirect or not entry.path.endswith(".html"):
            continue
        yield entry.path, entry


def online_contents(book_id: str, fetcher: Fetcher) -> ContentsBlock | None:
    """The contents page of the edition currently published on gutenberg.org."""
    document, _url, _path = fetch_html(fetcher, book_id)
    soup = BeautifulSoup(document, "html.parser")
    for tag in soup.find_all(["script", "noscript", "style"]):
        tag.decompose()
    return _declared_contents(soup, _content_nodes(soup))


def compare_with_online(entries: list[Entry], contents: ContentsBlock | None) -> list[TocIssue]:
    """Compare a built table of contents with the printed one online."""
    if contents is None:
        return [TocIssue("warning", "the online edition has no table of contents to compare with")]

    expected = [_normalise(entry.title) for entry in contents.entries]
    # The front page also lists the front matter, which carries no contents entry.
    actual = [_normalise(entry.title) for entry in entries if _normalise(entry.title) != "front matter"]
    issues: list[TocIssue] = []
    for title in expected:
        if title not in actual:
            issues.append(TocIssue("error", f"online contents lists {title!r}, the ZIM does not"))
    for title in actual:
        if title not in expected:
            issues.append(TocIssue("error", f"the ZIM lists {title!r}, the online contents does not"))
    if not issues and actual != expected:
        issues.append(TocIssue("error", "table of contents order differs from the online edition"))
    return issues


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def audit_book(archive: Archive, contents: ContentsBlock | None) -> list[TocIssue]:
    """Structural audit of a built archive plus the online comparison."""
    return audit_archive(archive) + compare_with_online(entries_from_archive(archive), contents)

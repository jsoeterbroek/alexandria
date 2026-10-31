"""Plain data types shared by the conversion pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BookMeta:
    """Normalized metadata for one Project Gutenberg ebook."""

    book_id: str
    title: str
    creators: tuple[str, ...]
    contributors: tuple[str, ...]
    subjects: tuple[str, ...]
    languages: tuple[str, ...]
    language3: str
    language_label: str
    issued: str
    rights: str
    description: str
    source_url: str
    downloads: int | None = None


@dataclass(frozen=True)
class Asset:
    """A local file that becomes a ZIM media entry."""

    url: str
    zim_path: str
    mimetype: str
    filepath: Path


@dataclass(frozen=True)
class Page:
    """An article to write into the ZIM."""

    path: str
    title: str
    body: str
    mimetype: str = "text/html"


@dataclass(frozen=True)
class TocEntry:
    depth: int
    title: str
    href: str

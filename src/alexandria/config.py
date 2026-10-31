"""Descriptor loading for the Alexandria workflow.

The descriptor is a small YAML file listing the Gutenberg books that should end
up in the local ZIM collection::

    output: output
    cache: cache
    books:
      - id: 11
      - id: 28054
        title: The Brothers Karamazov

``modernize: true`` rewrites archaic English ("thou knowest", "he cometh",
"quoth", "hath") into present-day English before the book is split and packed.

An entry may also point at local files instead of the network::

    - id: 1342
      html: ~/Downloads/1342-h.htm
      rdf: ~/Downloads/pg1342.rdf
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when the descriptor cannot be understood."""


@dataclass(frozen=True)
class BookSpec:
    """One book from the descriptor."""

    book_id: str
    title: str | None = None
    html: Path | None = None
    rdf: Path | None = None
    language: str | None = None
    modernize: bool = False
    build: bool = True


@dataclass(frozen=True)
class Config:
    """Loaded descriptor."""

    books: tuple[BookSpec, ...]
    output: Path
    cache: Path
    source: Path


def _book_spec(raw: Any, index: int) -> BookSpec:
    if isinstance(raw, bool):
        raise ConfigError(f"book #{index + 1}: expected a book id, got {raw!r}")
    if isinstance(raw, int):
        raw = str(raw)
    if isinstance(raw, str):
        raw = {"id": raw}
    if not isinstance(raw, dict):
        raise ConfigError(f"book #{index + 1}: expected a mapping or an id, got {type(raw).__name__}")

    book_id = str(raw.get("id", "")).strip()
    if not book_id.isdigit():
        raise ConfigError(f"book #{index + 1}: 'id' must be a numeric Gutenberg id, got {book_id!r}")

    def optional_path(key: str) -> Path | None:
        value = raw.get(key)
        if value in (None, ""):
            return None
        return Path(str(value)).expanduser()

    title = raw.get("title")
    language = raw.get("language")
    modernize = raw.get("modernize", False)
    build = raw.get("build", True)
    if not isinstance(modernize, bool):
        raise ConfigError(f"book #{index + 1}: 'modernize' must be true or false, got {modernize!r}")
    if not isinstance(build, bool):
        raise ConfigError(f"book #{index + 1}: 'build' must be true or false, got {build!r}")
    return BookSpec(
        book_id=book_id,
        title=str(title).strip() if title else None,
        html=optional_path("html"),
        rdf=optional_path("rdf"),
        language=str(language).strip() if language else None,
        modernize=modernize,
        build=build,
    )


def load_config(path: Path) -> Config:
    """Read and validate a YAML descriptor."""
    path = path.expanduser()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"descriptor not found: {path}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read descriptor {path}: {exc}") from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc

    if data is None:
        raise ConfigError(f"descriptor is empty: {path}")
    if isinstance(data, list):
        data = {"books": data}
    if not isinstance(data, dict):
        raise ConfigError("descriptor must be a mapping with a 'books' list")

    raw_books = data.get("books")
    if not isinstance(raw_books, list) or not raw_books:
        raise ConfigError("descriptor must contain a non-empty 'books' list")

    books: list[BookSpec] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_books):
        spec = _book_spec(raw, index)
        if spec.book_id in seen:
            continue
        seen.add(spec.book_id)
        books.append(spec)
    if not books:
        raise ConfigError("descriptor does not contain any usable books")

    output = Path(str(data.get("output") or "output")).expanduser()
    cache = Path(str(data.get("cache") or "cache")).expanduser()
    return Config(
        books=tuple(books),
        output=output,
        cache=cache,
        source=path,
    )

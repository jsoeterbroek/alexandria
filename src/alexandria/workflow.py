"""End-to-end orchestration: descriptor in, ZIM out."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from libzim.reader import Archive

from .config import BookSpec, Config
from .document import COVER_PATH, parse_book
from .gutenberg import COVER_URL, fetch_html, fetch_metadata, iso639_3, language_label
from .library import LIBRARY_NAME, write_library
from .models import Asset, BookMeta
from .net import Fetcher
from .util import human_size, slugify
from .zim import build_zim


@dataclass
class BookReport:
    book_id: str
    title: str
    output: Path
    pages: int
    media: int
    words: int
    size: int

    def line(self) -> str:
        return (
            f"#{self.book_id} {self.title} -> {self.output} "
            f"({self.pages} pages, {self.media} media, {self.words} words, {human_size(self.size)})"
        )


def zim_filename(book_id: str, title: str) -> str:
    return f"gutenberg-{book_id}-{slugify(title, 50)}.zim"


class Workflow:
    """Converts every book listed in a descriptor into a ZIM file.

    A failing book is reported and skipped so one bad id does not abort the
    rest of the collection. Books whose ZIM already exists are skipped unless
    ``force`` is set.
    """

    def __init__(self, config: Config, refresh: bool = False, force: bool = False, log=print) -> None:
        self.config = config
        self.fetcher = Fetcher(cache_dir=config.cache, refresh=refresh)
        self.force = force
        self.log = log

    def run(self) -> tuple[list[BookReport], list[tuple[str, str]], list[str]]:
        self.config.output.mkdir(parents=True, exist_ok=True)
        self.config.cache.mkdir(parents=True, exist_ok=True)

        reports: list[BookReport] = []
        failures: list[tuple[str, str]] = []
        skipped: list[str] = []
        for spec in self.config.books:
            if not spec.build:
                self.log(f"[{spec.book_id}] skipped (build: false)")
                skipped.append(spec.book_id)
                continue
            existing = self.built_zim(spec)
            if existing is not None and not self.force:
                self.log(f"[{spec.book_id}] skipped (already built: {existing.name})")
                skipped.append(spec.book_id)
                continue
            try:
                reports.append(self.convert(spec))
            except Exception as exc:  # noqa: BLE001 - keep building the remaining books
                self.log(f"[{spec.book_id}] failed: {exc}")
                failures.append((spec.book_id, str(exc)))
        self.write_library()
        return reports, failures, skipped

    def built_zim(self, spec: BookSpec) -> Path | None:
        """Return the existing, readable ZIM for ``spec`` if there is one."""
        for candidate in sorted(self.config.output.glob(f"gutenberg-{spec.book_id}-*.zim")):
            try:
                Archive(candidate)
            except Exception:  # noqa: BLE001 - an unreadable file is rebuilt
                continue
            return candidate
        return None

    def write_library(self) -> Path:
        """Refresh ``<output>/library.xml`` from every ZIM in the output directory."""
        target = self.config.output / LIBRARY_NAME
        count = write_library(target, sorted(self.config.output.glob("*.zim")))
        self.log(f"library: {target} ({count} book(s))")
        return target

    def convert(self, spec: BookSpec) -> BookReport:
        self.log(f"[{spec.book_id}] fetching metadata")
        meta, _ = fetch_metadata(self.fetcher, spec.book_id, spec.rdf)
        meta = _apply_overrides(meta, spec)

        self.log(f"[{spec.book_id}] fetching HTML for {meta.title!r}")
        document, source_url, _ = fetch_html(self.fetcher, spec.book_id, spec.html)

        with tempfile.TemporaryDirectory(prefix=f"alexandria-{spec.book_id}-") as workdir:
            media_dir = Path(workdir)
            cover = self._cover(spec, media_dir)
            book = parse_book(document, meta, source_url, media_dir, self.fetcher.fetch_bytes, cover)
            target = self.config.output / zim_filename(spec.book_id, meta.title)
            self.log(f"[{spec.book_id}] building {target.name} ({book.section_count} sections)")
            result = build_zim(target, book)

        return BookReport(
            book_id=spec.book_id,
            title=meta.title,
            output=result.path,
            pages=result.pages,
            media=result.media,
            words=book.word_count,
            size=result.size,
        )

    def _cover(self, spec: BookSpec, media_dir: Path) -> Asset | None:
        """Download the cover art; books without one simply get no illustration."""
        url = COVER_URL.format(id=spec.book_id)
        try:
            payload = self.fetcher.fetch_bytes(url, Path("media") / "cover.jpg")
        except Exception:  # noqa: BLE001 - covers are optional
            return None
        target = media_dir / "cover.jpg"
        target.write_bytes(payload)
        return Asset(url=url, zim_path=COVER_PATH, mimetype="image/jpeg", filepath=target)


def _apply_overrides(meta: BookMeta, spec: BookSpec) -> BookMeta:
    changes: dict[str, object] = {}
    if spec.title:
        changes["title"] = spec.title
    if spec.language:
        code3 = iso639_3(spec.language)
        changes.update(
            languages=(spec.language,),
            language3=code3,
            language_label=language_label(code3, spec.language),
        )
    return replace(meta, **changes) if changes else meta

"""Write parsed book content into a ZIM archive with libzim."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from libzim.writer import Creator, FileProvider, Hint, Item, StringProvider

from .archive import open_archive
from .classification import category_for
from .document import ParsedBook
from .models import Asset, Page


@dataclass
class BuildResult:
    path: Path
    pages: int
    media: int
    size: int


class _PageItem(Item):
    def __init__(self, page: Page, front_article: bool = True) -> None:
        self._page = page
        self._front_article = front_article

    def get_path(self) -> str:
        return self._page.path

    def get_title(self) -> str:
        return self._page.title

    def get_mimetype(self) -> str:
        return self._page.mimetype

    def get_contentprovider(self) -> StringProvider:
        return StringProvider(self._page.body)

    def get_hints(self) -> dict[Hint, bool]:
        return {Hint.COMPRESS: True, Hint.FRONT_ARTICLE: self._front_article}


class _MediaItem(Item):
    def __init__(self, asset: Asset) -> None:
        self._asset = asset

    def get_path(self) -> str:
        return self._asset.zim_path

    def get_title(self) -> str:
        return Path(self._asset.zim_path).name

    def get_mimetype(self) -> str:
        return self._asset.mimetype

    def get_contentprovider(self) -> FileProvider:
        return FileProvider(self._asset.filepath)

    def get_hints(self) -> dict[Hint, bool]:
        return {Hint.COMPRESS: True}


def build_zim(target: Path, book: ParsedBook) -> BuildResult:
    """Write ``book`` to ``target``; the archive only appears once verified."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    partial.unlink(missing_ok=True)

    meta = book.meta
    creator = Creator(partial)
    creator.config_indexing(True, meta.language3)
    creator.config_compression("zstd")

    try:
        with creator:
            _add_metadata(creator, book)
            for page in book.pages:
                creator.add_item(_PageItem(page))
            for asset in book.assets:
                creator.add_item(_MediaItem(asset))
            creator.add_item(
                _PageItem(
                    Page(
                        path="style.css",
                        title="Stylesheet",
                        body=book.stylesheet,
                        mimetype="text/css",
                    ),
                    front_article=False,
                )
            )
            if book.cover:
                creator.add_illustration(book.cover, 192, 192, 1.0)
            if book.favicon:
                creator.add_illustration(book.favicon, 48, 48, 1.0)
            creator.set_mainpath(book.index_path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise

    verify_zim(partial)
    partial.replace(target)
    return BuildResult(
        path=target,
        pages=len(book.pages),
        media=len(book.assets),
        size=target.stat().st_size,
    )


def _add_metadata(creator: Creator, book: ParsedBook) -> None:
    meta = book.meta
    creator.add_metadata(
        "Name",
        f"{meta.title} — {', '.join(meta.creators)}" if meta.creators else meta.title,
    )
    creator.add_metadata("Title", meta.title)
    creator.add_metadata("Creator", "; ".join(meta.creators) or "Unknown")
    creator.add_metadata("Publisher", "Project Gutenberg")
    creator.add_metadata("Language", meta.language3)
    creator.add_metadata("Description", meta.description)
    creator.add_metadata("Date", meta.issued)
    creator.add_metadata("Scraper", "alexandria")
    creator.add_metadata("Source", meta.source_url)
    creator.add_metadata("Tags", tags_for_book(book))
    creator.add_metadata("License", "Public domain in the USA.")
    if meta.subjects:
        creator.add_metadata("Keywords", "; ".join(meta.subjects))
    if meta.contributors:
        creator.add_metadata("Contributor", "; ".join(meta.contributors))


def tags_for_book(book: ParsedBook) -> str:
    """Kiwix tag string for ``book``: shelf category first, then the flags.

    ``tags_for_book`` is the single source of truth for the tag list, so the
    ZIM metadata and the ``library.xml`` fallback cannot drift apart.
    """
    tags = ["_ftindex:yes", "_pictures:yes", "_videos:no", "_details:yes"]
    category = category_for(book.meta.lcc)
    if category:
        tags.insert(0, f"_category:{category}")
    return ";".join(tags)


def verify_zim(path: Path, main_path: str | None = None) -> None:
    """Reopen the archive and check its integrity, main entry and links."""
    archive = open_archive(path)
    if not archive.has_main_entry:
        raise RuntimeError(f"{path.name}: archive has no main entry")
    if main_path is not None and archive.main_entry.path != main_path and not archive.main_entry.is_redirect:
        raise RuntimeError(f"{path.name}: unexpected main entry {archive.main_entry.path!r}")
    if not archive.check():
        raise RuntimeError(f"{path.name}: archive checksum verification failed")

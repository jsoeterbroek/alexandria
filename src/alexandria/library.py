"""Write a Kiwix library file describing the ZIMs in the output directory.

The resulting XML is the format consumed by ``kiwix-serve --library``,
Kiwix Desktop and ``kiwix-manage`` itself
(see https://wiki.kiwix.org/wiki/FeedFormat)::

    <library version="20110515">
      <book id="…" path="gutenberg-11-….zim" title="…" category="literature" … />
    </library>

``category`` drives the catalogue's category dropdown. libkiwix reads it from
the attribute when present and otherwise derives it from a ``_category:`` tag,
so both are written and stay in sync.
"""

from __future__ import annotations

import base64
from pathlib import Path
from xml.etree import ElementTree as ET

from libzim.reader import Archive

from .archive import open_archive
from .classification import category_from_tags

LIBRARY_NAME = "library.xml"
LIBRARY_VERSION = "20110515"
FAVICON_SIZE = 48


def write_library(path: Path, zim_paths: list[Path]) -> int:
    """(Re)write ``path`` describing every readable ZIM in ``zim_paths``.

    Returns the number of books written. Entries are sorted by title so the
    file stays stable across runs; archives that cannot be opened are skipped.
    """
    root = ET.Element("library", {"version": LIBRARY_VERSION})
    entries: list[tuple[str, dict[str, str]]] = []

    for zim in zim_paths:
        if not zim.is_file():
            continue
        try:
            archive = open_archive(zim)
        except Exception:  # noqa: BLE001 - a corrupt file must not break the library
            continue
        attributes = _book_attributes(archive, zim, path.parent)
        if attributes is None:
            continue
        entries.append((attributes.get("title", zim.name), attributes))

    for _title, attributes in sorted(entries, key=lambda item: item[0].lower()):
        ET.SubElement(root, "book", attributes)

    ET.indent(root, space="  ")
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    partial.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n")
    partial.replace(path)
    return len(entries)


def _book_attributes(archive: Archive, zim: Path, base: Path) -> dict[str, str] | None:
    title = _metadata(archive, "Title") or zim.stem
    resolved_base = base.resolve()
    resolved_zim = zim.resolve()
    tags = _metadata(archive, "Tags") or _synthesized_tags(archive)
    attributes = {
        "id": str(archive.uuid),
        "path": resolved_zim.relative_to(resolved_base).as_posix()
        if resolved_zim.is_relative_to(resolved_base)
        else str(zim),
        "title": title,
        "description": _metadata(archive, "Description"),
        "language": _metadata(archive, "Language") or "und",
        "creator": _metadata(archive, "Creator") or "Unknown",
        "publisher": _metadata(archive, "Publisher") or "Project Gutenberg",
        "name": _metadata(archive, "Name") or title,
        "tags": tags,
        "category": category_from_tags(tags),
        "date": _metadata(archive, "Date"),
        "articleCount": str(archive.article_count),
        "mediaCount": str(archive.media_count),
        "size": str(max(1, archive.filesize // 1024)),
    }
    favicon = _favicon(archive)
    if favicon is not None:
        payload, mimetype = favicon
        attributes["favicon"] = base64.b64encode(payload).decode("ascii")
        attributes["faviconMimeType"] = mimetype
    return {key: value for key, value in attributes.items() if value}


def _metadata(archive: Archive, name: str) -> str:
    try:
        return archive.get_metadata(name).decode("utf-8", errors="replace").strip()
    except (KeyError, RuntimeError):
        return ""


def _favicon(archive: Archive) -> tuple[bytes, str] | None:
    try:
        item = archive.get_illustration_item(FAVICON_SIZE)
    except (KeyError, RuntimeError):
        return None
    return bytes(item.content), item.mimetype


def _synthesized_tags(archive: Archive) -> str:
    """Fallback tag string mirroring what ``kiwix-manage`` writes."""
    return ";".join(
        (
            f"_ftindex:{'yes' if archive.has_fulltext_index else 'no'}",
            f"_pictures:{'yes' if archive.media_count else 'no'}",
            "_videos:no",
            "_details:yes",
        )
    )

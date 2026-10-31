"""Project Gutenberg metadata and file discovery."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pycountry

from .models import BookMeta
from .net import Fetcher

_RDF_NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dc": "http://purl.org/dc/terms/",
    "pg": "http://www.gutenberg.org/2009/pgterms/",
}
_DC = "{http://purl.org/dc/terms/}"
_PG = "{http://www.gutenberg.org/2009/pgterms/}"
_RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
_MARC = "{http://id.loc.gov/vocabulary/relators/}"

RDF_URL = "https://www.gutenberg.org/cache/epub/{id}/pg{id}.rdf"
HTML_URLS = (
    "https://www.gutenberg.org/cache/epub/{id}/pg{id}-images.html",
    "https://www.gutenberg.org/cache/epub/{id}/pg{id}.html",
    "https://www.gutenberg.org/files/{id}/{id}-h/{id}-h.htm",
)
COVER_URL = "https://www.gutenberg.org/cache/epub/{id}/images/cover.jpg"


class GutenbergError(RuntimeError):
    """Raised when Gutenberg metadata or content is missing or unusable."""


def iso639_3(code: str) -> str:
    """Map a Gutenberg language code (``en``, ``en-US``) to ISO 639-3."""
    base = (code or "en").split("-")[0].strip().lower()
    language = pycountry.languages.get(alpha_2=base) or pycountry.languages.get(alpha_3=base)
    return getattr(language, "alpha_3", "und") if language else "und"


def language_label(code3: str, fallback: str = "") -> str:
    """Human-readable name for an ISO 639-3 code."""
    return getattr(pycountry.languages.get(alpha_3=code3), "name", fallback or code3)


def _texts(root: ET.Element, tag: str) -> list[str]:
    values: list[str] = []
    for element in root.findall(f".//{tag}", _RDF_NS):
        text = " ".join("".join(element.itertext()).split())
        if text:
            values.append(text)
    return values


def _agent_names(root: ET.Element, namespace: str, tag: str) -> list[str]:
    names: list[str] = []
    for agent in root.findall(f".//{namespace}{tag}//{_PG}agent", _RDF_NS):
        for element in agent.findall(_PG + "name"):
            text = " ".join("".join(element.itertext()).split())
            if text:
                names.append(text)
    return names


def load_rdf(path: Path, book_id: str) -> BookMeta:
    """Parse a Gutenberg RDF record into ``BookMeta``."""
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise GutenbergError(f"cannot parse RDF for ebook {book_id}: {exc}") from exc

    title = next(iter(_texts(root, _DC + "title")), "") or f"Project Gutenberg ebook {book_id}"
    languages = _texts(root, _DC + "language//" + _RDF + "value") or ["en"]
    subjects = tuple(dict.fromkeys(_texts(root, _DC + "subject//" + _RDF + "value")))
    issued = next(iter(_texts(root, _DC + "issued")), "")
    rights = next(iter(_texts(root, _DC + "rights")), "")

    creators = tuple(dict.fromkeys(_agent_names(root, _DC, "creator")))
    contributors: list[str] = []
    for role in ("trl", "edt", "ill", "ctb", "aui", "nrt"):
        for name in _agent_names(root, _MARC, role):
            if name not in creators and name not in contributors:
                contributors.append(name)

    downloads = None
    for element in root.findall(f".//{_PG}downloads", _RDF_NS):
        if element.text and element.text.strip().isdigit():
            downloads = int(element.text.strip())
            break

    code3 = iso639_3(languages[0])
    label = language_label(code3, languages[0])
    byline = ", ".join(creators) or "an unknown author"
    description = f"{title} by {byline}. Project Gutenberg ebook #{book_id}, released {issued}."
    if subjects:
        description += " Subjects: " + "; ".join(subjects) + "."
    return BookMeta(
        book_id=book_id,
        title=title,
        creators=creators,
        contributors=tuple(contributors),
        subjects=subjects,
        languages=tuple(languages),
        language3=code3,
        language_label=label,
        issued=issued,
        rights=rights or "Public domain in the USA.",
        description=description or f"Project Gutenberg ebook #{book_id}.",
        source_url=f"https://www.gutenberg.org/ebooks/{book_id}",
        downloads=downloads,
    )


def fetch_metadata(fetcher: Fetcher, book_id: str, override: Path | None = None) -> tuple[BookMeta, Path]:
    """Load RDF metadata for ``book_id``, from ``override`` or the cache."""
    if override is not None:
        path = override.expanduser()
        if not path.is_file():
            raise GutenbergError(f"local RDF file not found: {path}")
        return load_rdf(path, book_id), path
    url = RDF_URL.format(id=book_id)
    path = fetcher.fetch_file(url, Path("rdf") / f"pg{book_id}.rdf")
    return load_rdf(path, book_id), path


def fetch_html(fetcher: Fetcher, book_id: str, override: Path | None = None) -> tuple[str, str, Path]:
    """Return ``(html, source_url, path)`` for the best available edition."""
    if override is not None:
        path = override.expanduser()
        if not path.is_file():
            raise GutenbergError(f"local HTML file not found: {path}")
        return path.read_text(encoding="utf-8", errors="replace"), path.as_uri(), path

    errors: list[str] = []
    for template in HTML_URLS:
        url = template.format(id=book_id)
        try:
            path = fetcher.fetch_file(url, Path("html") / f"pg{book_id}.html")
        except Exception as exc:  # noqa: BLE001 - any failure means "try the next layout"
            errors.append(f"{url}: {exc}")
            continue
        return path.read_text(encoding="utf-8", errors="replace"), url, path
    raise GutenbergError(f"no HTML edition found for ebook {book_id}:\n  " + "\n  ".join(errors))

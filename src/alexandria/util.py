"""Small helpers shared across the workflow."""

from __future__ import annotations

import re
import unicodedata
from hashlib import sha256
from pathlib import Path
from urllib.parse import unquote, urlparse

_TRANSLITERATIONS = str.maketrans(
    {
        "Æ": "AE",
        "æ": "ae",
        "Œ": "OE",
        "œ": "oe",
        "Ø": "O",
        "ø": "o",
        "Đ": "D",
        "đ": "d",
        "Ł": "L",
        "ł": "l",
        "ß": "ss",
        "Þ": "Th",
        "þ": "th",
        "Ð": "D",
        "ð": "d",
    }
)


def slugify(text: str, max_length: int = 80) -> str:
    """Return a filesystem-friendly ASCII slug."""
    normalized = unicodedata.normalize("NFKD", text.translate(_TRANSLITERATIONS))
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:max_length].rstrip("-") or "book"


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def url_cache_key(url: str, prefix: str = "media", length: int = 16) -> Path:
    """Stable cache path for a remote file, unique per URL.

    Images referenced from different books frequently share a filename
    (``images/cover.jpg``), so the cache key includes a digest of the URL to
    keep them apart.
    """
    parsed = urlparse(url)
    name = Path(unquote(parsed.path)).name or "file"
    stem = slugify(Path(name).stem, 40)
    suffix = Path(name).suffix.lower()
    digest = sha256(url.encode("utf-8")).hexdigest()[:length]
    return Path(prefix) / f"{stem}-{digest}{suffix}"

"""Cached HTTP fetching with retries."""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

USER_AGENT = "alexandria/0.1 (personal Project Gutenberg ZIM collection)"


class FetchError(RuntimeError):
    """Raised when a remote resource cannot be retrieved."""


@dataclass
class Fetcher:
    """Downloads resources into a cache directory and reuses them afterwards."""

    cache_dir: Path
    refresh: bool = False
    timeout: float = 60.0
    retries: int = 3
    backoff: float = 1.5

    def fetch_file(self, url: str, cache_key: Path) -> Path:
        """Return the local path for ``url``, downloading it when needed."""
        target = self.cache_dir / cache_key
        if not self.refresh and target.is_file() and target.stat().st_size > 0:
            return target

        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = response.read()
                if not payload:
                    raise FetchError(f"empty response from {url}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                return target
            except (urllib.error.URLError, TimeoutError, OSError, FetchError) as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(self.backoff * attempt)

        raise FetchError(f"could not download {url}: {last_error}") from last_error

    def fetch_bytes(self, url: str, cache_key: Path) -> bytes:
        return self.fetch_file(url, cache_key).read_bytes()

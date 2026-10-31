"""Open ZIM archives for reading without libzim's unsolicited diagnostics.

libzim's reader prints ``No stemming for language '<code>'`` on file descriptor
2 for every archive whose full-text index language has no Xapian stemmer. Latin
(``lat``), Ancient Greek (``grc``) and Hebrew (``heb``) are examples: Xapian
ships no Snowball stemmer for them, so the index keeps unstemmed terms and
search still works. The notice is neither an error nor actionable, so it is
filtered here while everything else libzim writes is replayed untouched.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import IO

from libzim.reader import Archive

_NOTICE_PREFIX = b"No stemming for language '"


def _replay(captured: IO[bytes]) -> None:
    captured.seek(0)
    for line in captured:
        if not line.startswith(_NOTICE_PREFIX):
            sys.stderr.write(line.decode("utf-8", "replace"))
    sys.stderr.flush()


def open_archive(path: Path) -> Archive:
    """Open ``path`` with the missing-stemmer notice suppressed.

    Only the ``No stemming for language '<code>'`` line is dropped; any other
    diagnostic libzim emits while opening the archive reaches stderr unchanged.
    """
    sys.stderr.flush()
    saved = os.dup(2)
    with tempfile.TemporaryFile() as capture:
        os.dup2(capture.fileno(), 2)
        try:
            archive = Archive(path)
        finally:
            os.dup2(saved, 2)
            os.close(saved)
            _replay(capture)
    return archive

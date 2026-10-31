"""Command line interface for the Alexandria workflow."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from .config import ConfigError, load_config
from .library import LIBRARY_NAME, write_library
from .workflow import Workflow
from .zim import verify_zim

DEFAULT_DESCRIPTOR = Path("input/input.yaml")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alexandria",
        description="Convert Project Gutenberg ebooks into a personal ZIM collection.",
    )
    parser.add_argument(
        "descriptor",
        nargs="?",
        default=str(DEFAULT_DESCRIPTOR),
        help=f"YAML descriptor (default: {DEFAULT_DESCRIPTOR})",
    )
    parser.add_argument("-o", "--output", type=Path, help="override the descriptor's output directory")
    parser.add_argument("-c", "--cache", type=Path, help="override the descriptor's cache directory")
    parser.add_argument(
        "--only",
        action="append",
        metavar="ID",
        help="build only these book ids (repeatable)",
    )
    parser.add_argument("--refresh", action="store_true", help="re-download cached resources")
    parser.add_argument(
        "--force",
        action="store_true",
        help="rebuild books whose ZIM already exists",
    )
    parser.add_argument("--list", action="store_true", help="list the books in the descriptor and exit")
    parser.add_argument(
        "--library",
        nargs="?",
        const=True,
        type=Path,
        metavar="XML",
        help="(re)write output/library.xml from existing ZIM files and exit; optional alternative path",
    )
    parser.add_argument(
        "--verify",
        nargs="+",
        type=Path,
        metavar="ZIM",
        help="verify existing ZIM files and exit",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.verify:
        return _verify(args.verify)

    try:
        config = load_config(Path(args.descriptor))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.output:
        config = replace(config, output=args.output)
    if args.cache:
        config = replace(config, cache=args.cache)
    if args.only:
        wanted = {book_id.removeprefix("#") for book_id in args.only}
        missing = wanted - {book.book_id for book in config.books}
        if missing:
            print(
                f"error: book(s) not in descriptor: {', '.join(sorted(missing))}",
                file=sys.stderr,
            )
            return 2
        config = replace(
            config,
            books=tuple(
                replace(book, build=True) if book.book_id in wanted else book
                for book in config.books
                if book.book_id in wanted
            ),
        )

    if args.list:
        books = [book for book in config.books if book.build]
        width = max((len(book.book_id) for book in books), default=1)
        for book in books:
            title = f"  {book.title}" if book.title else ""
            print(f"{book.book_id:<{width}}{title}")
        return 0

    if args.library:
        target = config.output / LIBRARY_NAME if args.library is True else args.library
        count = write_library(target, sorted(config.output.glob("*.zim")))
        print(f"wrote {target} ({count} book(s))")
        return 0

    # An explicit --only request always rebuilds, even if the ZIM exists.
    force = args.force or bool(args.only)
    reports, failures, skipped = Workflow(config, refresh=args.refresh, force=force).run()

    for book_id, message in failures:
        print(f"error: #{book_id}: {message}", file=sys.stderr)
    if reports:
        print(f"\nBuilt {len(reports)} ZIM file(s) in {config.output}")
        for report in reports:
            print(f"  {report.line()}")
    if skipped:
        print(f"Skipped {len(skipped)} book(s); use --force to rebuild them.")
    return 1 if failures else 0


def _verify(paths: list[Path]) -> int:
    failures = 0
    for path in paths:
        try:
            verify_zim(path)
        except Exception as exc:  # noqa: BLE001 - report every failure and keep going
            failures += 1
            print(f"FAIL {path}: {exc}", file=sys.stderr)
        else:
            print(f"OK   {path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

# Alexandria

Build a personal [ZIM](https://wiki.openzim.org/wiki/ZIM_file_format) collection
from Project Gutenberg ebooks. Alexandria downloads the ebook HTML and RDF
metadata, splits it into chapters, rewrites links and images, and packs
everything into a searchable ZIM archive with [libzim](https://github.com/openzim/python-libzim).

## Usage

```bash
make               # help: every target
make build         # convert missing books (skips ZIMs that already exist)
make build_all     # rebuild every book, overwriting existing ZIMs
make only ID=11    # (re)build one book
make list          # books in the descriptor
make library       # refresh output/library.xml
make verify        # integrity-check output/*.zim
make test          # offline test suite
make check         # lint + format check + tests
```

Without make:

```bash
uv sync
uv run alexandria              # uses input/input.yaml
uv run alexandria --list
uv run alexandria --only 11 --only 28054
uv run alexandria --verify output/*.zim
```

ZIM files are written to `output/`, downloads are cached in `cache/`.

### Descriptor

`input/input.yaml` lists the books to convert — currently the Project Gutenberg
top 100 (`input/top_100.txt`), sorted by ebook id:

```yaml
output: output
cache: cache

books:
  - id: 10
    title: "The King James Version of the Bible"  # top-100 #59
  - id: 11
    title: "Alice's Adventures in Wonderland"  # top-100 #4
```

Each entry accepts optional keys:

| Key        | Meaning                                                                      |
|------------|------------------------------------------------------------------------------|
| `id`       | Gutenberg ebook number (required)                                            |
| `title`    | Display title; used by `make list` and as the archive title (RDF when absent) |
| `language` | Replace the RDF language (ISO 639-1 or 639-3)                                 |
| `html`     | Use a local HTML file instead of downloading the ebook                       |
| `rdf`      | Use a local RDF metadata file instead of downloading it                       |
| `build`    | `false` keeps the entry in the file but skips it (default `true`)             |

`build: false` entries are not built and are omitted from `--list` output:

```yaml
  - id: 145
    title: "Middlemarch"
    build: false
```

`make list` prints `id  title` for every buildable entry, using the descriptor
titles only — no network lookup.

`--only ID` (and `make only ID=…`) always builds that book: it overrides both
`build: false` and the skip-if-already-built rule.

CLI flags: `-o/--output`, `-c/--cache`, `--refresh` (ignore the cache),
`--force` (rebuild existing ZIMs), `--only ID`, `--list`, `--library [XML]`,
`--verify`.

### Incremental builds

`make build` skips a book when `output/gutenberg-<id>-*.zim` already exists and
opens cleanly, so a rerun only converts what is new (a book you disabled with
`build: false` and later re-enabled is built again). Unreadable or partial
archives are rebuilt instead of trusted. `make build_all` (`--force`) rebuilds
everything; `make refresh` also re-downloads the cached Gutenberg files.

## What ends up in a ZIM

- `index.html` — title page with metadata, subjects and a nested table of contents.
- `chapter/NNN.html` — one article per chapter; part/book dividers are merged
  into the following chapter so no article is heading-only.
- `media/*` — every referenced image, plus the Gutenberg cover.
- `style.css` — Gutenberg's own stylesheet plus a small reader stylesheet.

Metadata (`Title`, `Creator`, `Language`, `Description`, `Date`, `Keywords`,
`Source`, `License`, `Contributor`, `Tags`) comes from the RDF record. Full-text
and title indexes are built with the book's ISO 639-3 language; the cover is
stored as a ZIM illustration at 192 px plus a 48 px favicon, which Kiwix uses in
its catalogue.

The archive is written to a `.part` file and only renamed after
`libzim.Archive.check()` and a main-entry check pass, so a failed build never
replaces a good file.

## Library file

Every run rewrites `output/library.xml`, a Kiwix library file
([FeedFormat](https://wiki.kiwix.org/wiki/FeedFormat)) describing all ZIMs in
the output directory — sorted by title, one `<book>` element each with id,
title, description, language, creator, publisher, name, tags, date,
`articleCount`, `mediaCount`, `size` and the base64 favicon:

```xml
<library version="20110515">
  <book id="0c2d1f59-…" path="gutenberg-11-alice-s-adventures-in-wonderland.zim"
        title="Alice's Adventures in Wonderland" language="eng" … />
</library>
```

Point Kiwix at it directly:

```bash
make library                       # refresh it without rebuilding
kiwix-serve --library output/library.xml
uv run alexandria --library        # same thing from the CLI
```

Stale ZIMs are included as long as the files exist, so removing a book from the
descriptor and deleting its ZIM drops it from the library on the next run.
Unreadable files are skipped rather than aborting the write.

## Layout

| Module                      | Responsibility                                      |
|-----------------------------|-----------------------------------------------------|
| `alexandria/config.py`      | YAML descriptor loading and validation              |
| `alexandria/gutenberg.py`   | RDF metadata, HTML edition and cover discovery      |
| `alexandria/net.py`         | Cached HTTP fetching with retries                   |
| `alexandria/document.py`    | Gutenberg HTML → pages, media, table of contents    |
| `alexandria/zim.py`         | libzim writing, metadata, verification              |
| `alexandria/library.py`     | Kiwix `library.xml` generation                       |
| `alexandria/workflow.py`    | Per-book orchestration and error isolation          |
| `alexandria/cli.py`         | Command line interface                              |

## Tests

```bash
uv run python -m unittest discover -s tests
```

The suite parses synthetic Gutenberg HTML in all three layouts and validates
the built archive: main entry, metadata, full-text search, media and that every
internal link resolves.

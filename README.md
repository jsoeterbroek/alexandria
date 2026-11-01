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
| `modernize`| Rewrite archaic English ("thou knowest", "quoth") into modern English (default `false`) |
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

### Modern English

Some Gutenberg texts are written in archaic English — Burton's *Thousand
Nights and a Night* alone has thousands of "thou", "thee", "hath", "quoth",
"cometh" and "-est" verbs. Set `modernize: true` on such an entry and
`alexandria/archaic.py` rewrites the prose before the book is split:

```yaml
  - id: 3435
    title: "The Book of the Thousand Nights and a Night — Volume 01 (of 10)"
    modernize: true
```

The rewrite is rule-based and deterministic, not a machine translation:

- pronouns and agreement — `thou knowest` → `you know`, `knowest thou` →
  `know you`, `thou art` → `you are`, `thy`/`thine` → `your`;
- verbs — `he cometh` → `he comes`, `she kisseth` → `she kisses`;
- auxiliaries and function words — `hath` → `has`, `dost` → `do`, `shalt` →
  `shall`, `quoth` → `said`, `verily` → `truly`, `aught`/`naught` →
  `anything`/`nothing`, `whither` → `where`, `ere` → `before`, `whilst` →
  `while`;
- phrases — `in very sooth` → `in truth`, `good morrow` → `good morning`,
  `bade him adieu` → `said adieu to him`.

Verb stems are validated against the words the book actually uses, so modern
lookalikes stay put: `the best guest`, `his teeth`, `the twentieth night`, `the
fairest of women`, `the art of war`, `flowers wilt`, `we wrought iron`,
`the money is mine`. Markup, attributes, links and proper names are untouched;
only text nodes change. Spelling and dialect stay as printed.

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
and title indexes are built with the book's ISO 639-3 language. Each book's own
cover is stored as a ZIM illustration at 192 px plus a 48 px favicon, which
Kiwix uses in its catalogue.

The Library of Congress classification in the RDF record (`PR`, `QA`, `DG`, …)
becomes a Kiwix shelf category, written as `_category:<name>` in `Tags` and as
the `category` attribute in `library.xml`. Kiwix's viewer shows those in its
category dropdown, so a mixed collection can be narrowed to e.g. `science`
without a text search. The label is one word on purpose: libkiwix indexes the
category by splitting it on whitespace but filters by the raw string as a single
Xapian term, so `Natural science` would list in the dropdown and then match
nothing (a hyphen splits into two terms for the same reason). Every entry in the
dropdown is therefore guaranteed to match at least one book.

`alexandria/classification.py` labels a book from its primary LCC code — the
first one that maps, which is Gutenberg's main classification. Top-level classes
give `history`, `philosophy`, `science`, `political_science`, `technology` and
so on; `P` is further split by subclass so a fiction-heavy collection does not
collapse into one bucket:

| Code | Category               | Code | Category                  |
|------|------------------------|------|---------------------------|
| `PA` | `classical_literature` | `PN` | `literary_criticism`      |
| `PC` | `romance_languages`    | `PQ` | `romance_literature`      |
| `PE` | `english_language`     | `PR` | `english_literature`      |
| `PG` | `slavic_literature`    | `PS` | `american_literature`     |
| `PJ` | `middle_eastern_literature` | `PT` | `germanic_literature` |
| `PL` | `eastern_literature`   | `PZ` | `childrens_literature`    |

Any other `P*` code falls back to `literature`. Adding a label for a code the
collection uses is a one-line change; re-run `make build_all` afterwards, since
the category is baked into each ZIM's metadata.

Xapian has no stemmer for some languages (Latin, Ancient Greek, Hebrew, and
other classical or liturgical languages). Those archives are indexed unstemmed:
exact words are searchable, inflected forms are not conflated, and libzim's
"No stemming for language" notice is suppressed by
`alexandria/archive.py` when the archive is reopened for verification, library
generation and rebuild decisions.

Downloads are cached per URL (`cache/media/<name>-<digest>.<ext>`): Gutenberg
reuses filenames such as `images/cover.jpg` across books, so a filename-only
cache would hand one book another book's cover. Covers fall back to
`pg<id>.cover.medium.jpg` when `images/cover.jpg` does not exist. Cache files
are written via rename, and a book's images are fetched eight at a time —
illustrated titles such as *Calculus Made Easy* (#33283, 2 341 figures) would
otherwise spend minutes downloading one connection at a time.

Some ebooks have no HTML edition at all — Tractatus Logico-Philosophicus
(#5740) ships only as PDF and LaTeX. Those are converted to HTML by
`alexandria/formats.py` (pypdf / pylatexenc), split into sections on headings
and proposition numbers, and then follow the normal pipeline. Audio-only
ebooks (e.g. #26471, a LibriVox recording) are reported as unbuildable rather
than packed as a track list.

The archive is written to a `.part` file and only renamed after
`libzim.Archive.check()` and a main-entry check pass, so a failed build never
replaces a good file.

## Library file

Every run rewrites `output/library.xml`, a Kiwix library file
([FeedFormat](https://wiki.kiwix.org/wiki/FeedFormat)) describing all ZIMs in
the output directory — sorted by title, one `<book>` element each with id,
title, description, language, creator, publisher, name, tags, category, date,
`articleCount`, `mediaCount`, `size` and the base64 favicon:

```xml
<library version="20110515">
  <book id="0c2d1f59-…" path="gutenberg-11-alice-s-adventures-in-wonderland.zim"
        title="Alice's Adventures in Wonderland" language="eng" category="literature" … />
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
| `alexandria/gutenberg.py`   | RDF metadata, edition discovery, cover lookup        |
| `alexandria/net.py`         | Cached HTTP fetching with retries                   |
| `alexandria/document.py`    | Gutenberg HTML → pages, media, table of contents    |
| `alexandria/archaic.py`     | Archaic English → modern English rewrite            |
| `alexandria/zim.py`         | libzim writing, metadata, verification              |
| `alexandria/archive.py`     | Reading ZIMs without libzim's stray stderr notices  |
| `alexandria/classification.py` | LCC class → Kiwix shelf category               |
| `alexandria/formats.py`     | PDF/TeX editions converted to parseable HTML         |
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

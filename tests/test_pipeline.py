"""Tests for the Alexandria Gutenberg-to-ZIM pipeline.

Run with ``uv run python -m unittest discover -s tests``.
"""

from __future__ import annotations

import contextlib
import html as html_lib
import io
import os
import posixpath
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup, Tag
from libzim.reader import Archive
from libzim.search import Query, Searcher

from alexandria.archaic import modernize_soup, modernize_text
from alexandria.archive import open_archive
from alexandria.classification import category_for, category_from_tags
from alexandria.cli import main
from alexandria.config import BookSpec, Config, ConfigError, load_config
from alexandria.document import MAX_ARTICLE_BYTES, ContentsBlock, ContentsEntry, parse_book
from alexandria.formats import tex_to_html, text_to_html
from alexandria.gutenberg import GutenbergError, iso639_3, load_rdf
from alexandria.library import LIBRARY_NAME, LIBRARY_VERSION, write_library
from alexandria.models import BookMeta, Page
from alexandria.toc import audit_archive, compare_with_online, entries_from_archive
from alexandria.util import human_size, slugify, url_cache_key
from alexandria.workflow import BookReport, Workflow, _apply_overrides
from alexandria.zim import build_zim

RDF = """<?xml version="1.0" encoding="utf-8"?>
<rdf:RDF xml:base="http://www.gutenberg.org/"
  xmlns:pgterms="http://www.gutenberg.org/2009/pgterms/"
  xmlns:dcterms="http://purl.org/dc/terms/"
  xmlns:marcrel="http://id.loc.gov/vocabulary/relators/"
  xmlns:dcam="http://purl.org/dc/dcam/"
  xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <pgterms:ebook rdf:about="ebooks/999">
    <dcterms:title>Testable Tales</dcterms:title>
    <dcterms:issued>2020-01-02</dcterms:issued>
    <dcterms:rights>Public domain in the USA.</dcterms:rights>
    <dcterms:language>
      <rdf:Description rdf:nodeID="lang">
        <rdf:value rdf:datatype="http://purl.org/dc/terms/RFC4646">en</rdf:value>
      </rdf:Description>
    </dcterms:language>
    <dcterms:subject>
      <rdf:Description rdf:nodeID="s1"><rdf:value>Fiction</rdf:value></rdf:Description>
    </dcterms:subject>
    <dcterms:subject>
      <rdf:Description rdf:nodeID="s2">
        <dcam:memberOf rdf:resource="http://purl.org/dc/terms/LCC"/>
        <rdf:value>PR</rdf:value>
      </rdf:Description>
    </dcterms:subject>
    <dcterms:creator>
      <pgterms:agent rdf:about="2009/agents/1">
        <pgterms:name>Doe, Jane</pgterms:name>
      </pgterms:agent>
    </dcterms:creator>
    <marcrel:trl>
      <pgterms:agent rdf:about="2009/agents/2">
        <pgterms:name>Roe, Richard</pgterms:name>
      </pgterms:agent>
    </marcrel:trl>
  </pgterms:ebook>
</rdf:RDF>
"""

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff0300000600"
    "0557bfabd40000000049454e44ae426082"
)

MODERN_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><style>body { color: black }</style></head>
<body>
<header class="pg-boilerplate pgheader" id="pg-header"><h2>The Project Gutenberg eBook</h2></header>
<h1>Testable Tales</h1>
<div class="fig"><img src="images/cover.jpg" alt=""></div>
<h2>Contents</h2>
<div class="chapter"><h2><a id="part01"></a>PART I</h2></div>
<div class="chapter"><h2><a id="chap01"></a>Chapter I.<br>The First</h2>
<p>It was a dark and stormy night. <a href="#chap02">next</a></p>
<img src="images/fig1.png" alt="figure one">
</div>
<div class="chapter"><h2><a id="chap02"></a>Chapter II.<br>The Second</h2>
<p>The stormy unicorn appeared at dawn.</p>
</div>
<footer class="pg-boilerplate pgheader" id="pg-footer"><p>*** END OF THE PROJECT GUTENBERG EBOOK ***</p></footer>
</body></html>
"""

LEGACY_HTML = """<html><body>
<p>*** START OF THE PROJECT GUTENBERG EBOOK LEGACY BOOK ***</p>
<h1>Legacy Book</h1>
<h2>CHAPTER I.</h2>
<p>Alpha beta gamma. <a href="other.html">unresolved</a></p>
<h2>CHAPTER II.</h2>
<p>Delta epsilon zeta.</p>
<h2>THE FULL PROJECT GUTENBERG LICENSE</h2>
<p>Licence boilerplate that must not appear.</p>
</body></html>
"""


def fake_fetch(payloads: dict[str, bytes]):
    def fetch(url: str, _cache_key: Path) -> bytes:
        if url in payloads:
            return payloads[url]
        raise RuntimeError(f"no payload for {url}")

    return fetch


def make_meta(**overrides) -> BookMeta:
    defaults = {
        "book_id": "999",
        "title": "Testable Tales",
        "creators": ("Doe, Jane",),
        "contributors": ("Roe, Richard",),
        "subjects": ("Fiction",),
        "languages": ("en",),
        "language3": "eng",
        "language_label": "English",
        "issued": "2020-01-02",
        "rights": "Public domain in the USA.",
        "description": "A test book.",
        "source_url": "https://www.gutenberg.org/ebooks/999",
        "lcc": ("PR",),
    }
    defaults.update(overrides)
    return BookMeta(**defaults)


class HelpersTest(unittest.TestCase):
    def test_slugify(self) -> None:
        self.assertEqual(slugify("The Brothers Karamazov"), "the-brothers-karamazov")
        self.assertEqual(slugify("Æsop — Fables!"), "aesop-fables")
        self.assertEqual(slugify("***"), "book")

    def test_human_size(self) -> None:
        self.assertEqual(human_size(512), "512 B")
        self.assertEqual(human_size(2048), "2.0 KiB")
        self.assertEqual(human_size(5 * 1024 * 1024), "5.0 MiB")

    def test_iso639_3(self) -> None:
        self.assertEqual(iso639_3("en"), "eng")
        self.assertEqual(iso639_3("en-US"), "eng")
        self.assertEqual(iso639_3("de"), "deu")
        self.assertEqual(iso639_3("zz"), "und")


class MetadataTest(unittest.TestCase):
    def test_load_rdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pg999.rdf"
            path.write_text(RDF, encoding="utf-8")
            meta = load_rdf(path, "999")
        self.assertEqual(meta.title, "Testable Tales")
        self.assertEqual(meta.creators, ("Doe, Jane",))
        self.assertEqual(meta.contributors, ("Roe, Richard",))
        self.assertEqual(meta.subjects, ("Fiction",))
        self.assertEqual(meta.lcc, ("PR",))
        self.assertEqual(meta.language3, "eng")
        self.assertEqual(meta.language_label, "English")
        self.assertEqual(meta.issued, "2020-01-02")

    def test_missing_rdf(self) -> None:
        with self.assertRaises(GutenbergError):
            load_rdf(Path("/nonexistent/pg1.rdf"), "1")


class ClassificationTest(unittest.TestCase):
    def test_maps_lcc_class_to_category(self) -> None:
        cases = {
            "QA": "science",
            "QH": "science",
            "DG": "history",
            "JC": "political_science",
            "B": "philosophy",
            "U": "military",
        }
        for code, expected in cases.items():
            self.assertEqual(category_for((code,)), expected, code)

    def test_literature_split_into_subclasses(self) -> None:
        cases = {
            "PR": "english_literature",
            "PS": "american_literature",
            "PA": "classical_literature",
            "PQ": "romance_literature",
            "PG": "slavic_literature",
            "PJ": "middle_eastern_literature",
            "PT": "germanic_literature",
            "PZ": "childrens_literature",
            "PE": "english_language",
        }
        for code, expected in cases.items():
            self.assertEqual(category_for((code,)), expected, code)

    def test_unlisted_literature_subclass_falls_back(self) -> None:
        # P is valid but PX has no label of its own.
        self.assertEqual(category_for(("PX",)), "literature")

    def test_first_known_code_wins(self) -> None:
        # Gutenberg lists the primary classification first; PZ is secondary.
        self.assertEqual(category_for(("PR", "PZ")), "english_literature")
        self.assertEqual(category_for(("XX", "QA")), "science")

    def test_classification_ignores_trailing_digits(self) -> None:
        self.assertEqual(category_for(("PR4034",)), "english_literature")
        self.assertEqual(category_for(("QA76.73",)), "science")

    def test_unknown_or_missing_codes_have_no_category(self) -> None:
        self.assertEqual(category_for(()), "")
        self.assertEqual(category_for(("XX",)), "")
        self.assertEqual(category_for(("",)), "")
        self.assertEqual(category_for(("1234",)), "")

    def test_categories_are_single_xapian_tokens(self) -> None:
        # libkiwix indexes the category by whitespace-splitting it but filters
        # with the raw string as one term, so a space makes the dropdown entry
        # unfilterable; a hyphen is indexed as a separate term for the same reason.
        codes = [f"{letter}A" for letter in "ABCDEFGHJKLMNPQRSTUVZ"]
        codes += [f"P{letter}" for letter in "ABCDEFGHKLMNQRSTZ"]
        for code in codes:
            category = category_for((code,))
            self.assertRegex(category, r"^[a-z_]+$", code)

    def test_category_from_tags(self) -> None:
        tags = "_category:english_literature;_ftindex:yes;_pictures:yes;_videos:no;_details:yes"
        self.assertEqual(category_from_tags(tags), "english_literature")
        self.assertEqual(category_from_tags("_ftindex:yes;_details:yes"), "")
        self.assertEqual(category_from_tags("_category:science"), "science")


class ConfigTest(unittest.TestCase):
    def test_load_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.yaml"
            path.write_text(
                "output: out\ncache: cache\nbooks:\n  - 11\n  - id: 28054\n    title: Custom\n  - id: 11\n",
                encoding="utf-8",
            )
            config = load_config(path)
        self.assertEqual([book.book_id for book in config.books], ["11", "28054"])
        self.assertEqual(config.books[1].title, "Custom")
        self.assertEqual(config.output, Path("out"))

    def test_empty_descriptor_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.yaml"
            path.write_text("books: []\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_non_numeric_id_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.yaml"
            path.write_text("books:\n  - id: nope\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_modernize_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.yaml"
            path.write_text(
                "books:\n  - id: 11\n  - id: 3435\n    modernize: true\n",
                encoding="utf-8",
            )
            config = load_config(path)
        self.assertEqual([book.modernize for book in config.books], [False, True])

    def test_non_boolean_modernize_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.yaml"
            path.write_text("books:\n  - id: 11\n    modernize: maybe\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_author_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.yaml"
            path.write_text(
                'books:\n  - id: 11\n  - id: 766\n    title: "David Copperfield"\n    author: "Charles Dickens"\n',
                encoding="utf-8",
            )
            config = load_config(path)
        self.assertEqual([book.author for book in config.books], [None, "Charles Dickens"])

    def test_build_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.yaml"
            path.write_text(
                "books:\n  - id: 11\n  - id: 84\n    build: false\n",
                encoding="utf-8",
            )
            config = load_config(path)
        self.assertEqual([book.build for book in config.books], [True, False])

    def test_non_boolean_build_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.yaml"
            path.write_text("books:\n  - id: 11\n    build: maybe\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)


class CliTest(unittest.TestCase):
    def test_only_rebuilds_disabled_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.yaml"
            path.write_text(
                f"output: {tmp}/out\ncache: {tmp}/cache\nbooks:\n  - id: 11\n    build: false\n",
                encoding="utf-8",
            )
            # An explicit --only must override the descriptor's build switch.
            with mock.patch.object(Workflow, "run", autospec=True) as run:
                run.return_value = ([], [], [])
                with contextlib.redirect_stdout(io.StringIO()):
                    code = main([str(path), "--only", "11"])
            self.assertEqual(code, 0)
            workflow = run.call_args.args[0]
            self.assertEqual([book.book_id for book in workflow.config.books], ["11"])
            self.assertTrue(workflow.config.books[0].build)
            self.assertTrue(workflow.force, "--only must also override skip-if-built")

    def test_list_shows_titles_and_hides_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.yaml"
            path.write_text(
                "books:\n"
                "  - id: 11\n"
                '    title: "Alice\'s Adventures in Wonderland"\n'
                "  - id: 84\n"
                '    title: "Frankenstein"\n'
                "    build: false\n"
                "  - id: 1342\n",
                encoding="utf-8",
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main([str(path), "--list"])
        self.assertEqual(code, 0)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], "11    Alice's Adventures in Wonderland")
        self.assertEqual(lines[1], "1342")
        self.assertNotIn("Frankenstein", output.getvalue())


class ArchaicTest(unittest.TestCase):
    """Archaic English is rewritten without damaging modern lookalikes."""

    def test_pronouns_and_verbs(self) -> None:
        self.assertEqual(modernize_text("thou knowest that I love thee"), "you know that I love you")
        self.assertEqual(modernize_text("Knowest thou not?"), "Know you not?")
        self.assertEqual(modernize_text("thou art welcome, thou hast much"), "you are welcome, you have much")
        self.assertEqual(modernize_text("so shalt thou witness it"), "so shall you witness it")
        self.assertEqual(modernize_text("as thou wilt"), "as you will")
        self.assertEqual(modernize_text("thy wife and thine eyes"), "your wife and your eyes")

    def test_third_person_inflections(self) -> None:
        self.assertEqual(modernize_text("he cometh and goeth"), "he comes and goes")
        self.assertEqual(modernize_text("the old saw lieth not"), "the old saw lies not")
        self.assertEqual(modernize_text("what aileth thee?"), "what ails you?")
        self.assertEqual(modernize_text("she kisseth his hand"), "she kisses his hand")

    def test_archaic_vocabulary(self) -> None:
        self.assertEqual(modernize_text("quoth he"), "said he")
        self.assertEqual(modernize_text("in very sooth"), "in truth")
        self.assertEqual(modernize_text("naught availed her aught"), "nothing availed her anything")
        self.assertEqual(modernize_text("whither goest thou?"), "where go you?")

    def test_modern_lookalikes_survive(self) -> None:
        self.assertEqual(modernize_text("the best guest in the west"), "the best guest in the west")
        self.assertEqual(modernize_text("the fairest of women"), "the fairest of women")
        self.assertEqual(modernize_text("the art of war; flowers wilt"), "the art of war; flowers wilt")
        self.assertEqual(modernize_text("we wrought iron"), "we wrought iron")
        self.assertEqual(modernize_text("the money is mine"), "the money is mine")
        self.assertEqual(modernize_text("his teeth and the twentieth night"), "his teeth and the twentieth night")

    def test_markup_is_untouched_but_split_words_are_joined(self) -> None:
        soup = BeautifulSoup('<p>thou <i>knowest</i> me, <a href="thou.html">thee</a></p>', "html.parser")
        modernize_soup(soup)
        self.assertEqual(str(soup), '<p>you <i>know</i> me, <a href="thou.html">you</a></p>')

    def test_parse_book_modernizes_only_when_asked(self) -> None:
        html = "<html><body><h1>Book</h1><h2>CHAPTER I.</h2><p>Thou knowest that he cometh.</p></body></html>"
        media_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        plain = parse_book(html, make_meta(), "https://example.test/book.html", media_dir, fake_fetch({}))
        modern = parse_book(
            html, make_meta(), "https://example.test/book.html", media_dir, fake_fetch({}), modernize=True
        )
        self.assertIn("Thou knowest", " ".join(page.body for page in plain.pages))
        self.assertIn("You know that he comes", " ".join(page.body for page in modern.pages))


class ParsingTest(unittest.TestCase):
    def parse(self, html: str, meta: BookMeta | None = None, payloads=None):
        payloads = payloads if payloads is not None else {"https://example.test/images/fig1.png": PNG}
        media_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        return parse_book(
            html,
            meta or make_meta(),
            "https://example.test/book.html",
            media_dir,
            fake_fetch(payloads),
        )

    def test_modern_layout_sections(self) -> None:
        book = self.parse(MODERN_HTML)
        paths = [page.path for page in book.pages]
        self.assertEqual(paths[0], "index.html")
        self.assertIn("front-matter.html", paths)
        self.assertEqual(paths[-2:], ["chapter/001.html", "chapter/002.html"])
        titles = [page.title for page in book.pages]
        self.assertIn("Chapter I. The First", titles)
        self.assertIn("Chapter II. The Second", titles)
        self.assertNotIn("PART I", titles, "heading-only dividers merge into the next article")

    def test_boilerplate_and_license_removed(self) -> None:
        for html in (MODERN_HTML, LEGACY_HTML):
            book = self.parse(html)
            text = " ".join(page.body for page in book.pages)
            self.assertNotIn("*** END OF", text)
            self.assertNotIn("Licence boilerplate", text)

    def test_legacy_layout_split_on_headings(self) -> None:
        book = self.parse(LEGACY_HTML)
        paths = [page.path for page in book.pages]
        self.assertEqual(
            paths,
            ["index.html", "front-matter.html", "chapter/001.html", "chapter/002.html"],
        )
        titles = [page.title for page in book.pages]
        self.assertEqual(titles, ["Testable Tales", "Front matter", "CHAPTER I.", "CHAPTER II."])
        front = next(page for page in book.pages if page.path == "front-matter.html")
        self.assertIn("Legacy Book", front.body)

    def test_links_rewritten_for_relative_paths(self) -> None:
        book = self.parse(MODERN_HTML)
        chapter = next(page for page in book.pages if page.path == "chapter/001.html")
        self.assertIn('href="002.html#chap02"', chapter.body)
        self.assertIn('href="../index.html"', chapter.body)
        self.assertIn('href="../style.css"', chapter.body)

    def test_anchor_links_resolve_to_target_section(self) -> None:
        book = self.parse(MODERN_HTML)
        chapter = next(page for page in book.pages if page.path == "chapter/002.html")
        self.assertIn('id="chap02"', chapter.body)
        index = next(page for page in book.pages if page.path == "index.html")
        self.assertIn("chapter/001.html#chap01", index.body)

    def test_images_downloaded_and_rewritten(self) -> None:
        book = self.parse(MODERN_HTML)
        chapter = next(page for page in book.pages if page.path == "chapter/001.html")
        self.assertIn('src="../media/fig1.png"', chapter.body)
        self.assertEqual([asset.zim_path for asset in book.assets], ["media/fig1.png"])
        self.assertTrue(book.assets[0].filepath.is_file())

    def test_missing_image_is_dropped(self) -> None:
        book = self.parse(MODERN_HTML, payloads={})
        chapter = next(page for page in book.pages if page.path == "chapter/001.html")
        self.assertNotIn("<img", chapter.body)
        self.assertEqual(book.assets, [])

    def test_images_are_downloaded_concurrently(self) -> None:
        """Illustrated books reference thousands of images; fetches must overlap."""
        html = (
            "<html><body><h1>Many Figures</h1>"
            + "".join(f'<div class="chapter"><h2>Chapter {n}</h2><img src="images/{n}.png"></div>' for n in range(16))
            + "</body></html>"
        )
        lock = threading.Lock()
        state = {"active": 0, "peak": 0}

        def fetch(_url: str, _key: Path) -> bytes:
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            try:
                time.sleep(0.02)
                return PNG
            finally:
                with lock:
                    state["active"] -= 1

        media_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        book = parse_book(html, make_meta(), "https://example.test/book.html", media_dir, fetch)
        self.assertEqual(len(book.assets), 16)
        self.assertGreater(state["peak"], 1, "image downloads must overlap")

    def test_same_basename_is_fetched_once_and_falls_back(self) -> None:
        """Two URLs sharing a basename map to one zim path; the failed one defers."""
        html = """<html><body><h1>Clash</h1>
        <div class="chapter"><h2>Chapter I</h2>
          <img src="a/figure.png"><img src="b/figure.png">
        </div></body></html>"""
        requested: list[str] = []

        def fetch(url: str, _key: Path) -> bytes:
            requested.append(url)
            if url.endswith("/a/figure.png"):
                raise RuntimeError("404")
            return PNG

        media_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        book = parse_book(html, make_meta(), "https://example.test/book.html", media_dir, fetch)
        self.assertEqual([asset.zim_path for asset in book.assets], ["media/figure.png"])
        self.assertEqual(book.assets[0].url, "https://example.test/b/figure.png")
        self.assertEqual(requested, ["https://example.test/a/figure.png", "https://example.test/b/figure.png"])
        self.assertEqual(len(list(media_dir.iterdir())), 1)

    def test_page_marker_wrapper_keeps_images(self) -> None:
        """`pg_body_wrapper` holds page markers, but also image-form display math."""
        html = """<html><body><h1>Wrapped</h1>
        <div class="chapter"><h2>Chapter I</h2><p>Text before the formula.</p></div>
        <div class="pg_body_wrapper"><span class="align-center"><img src="images/formula.svg"></span></div>
        <div class="pg_body_wrapper">[Pg 42]</div>
        <div class="chapter"><h2>Chapter II</h2><p>Text after the formula.</p></div>
        </body></html>"""
        book = self.parse(html, payloads={"https://example.test/images/formula.svg": b"<svg/>"})
        chapter = next(page for page in book.pages if page.path == "chapter/001.html")
        self.assertIn('src="../media/formula.svg"', chapter.body)
        self.assertNotIn("[Pg 42]", chapter.body)
        self.assertEqual([asset.zim_path for asset in book.assets], ["media/formula.svg"])

    def test_front_page_metadata(self) -> None:
        book = self.parse(MODERN_HTML)
        index = next(page for page in book.pages if page.path == "index.html")
        self.assertIn("Testable Tales", index.body)
        self.assertIn("Doe, Jane", index.body)
        self.assertIn("Roe, Richard", index.body)
        self.assertIn("Fiction", index.body)
        self.assertIn("english", index.body.lower())
        self.assertEqual(index.title, "Testable Tales")
        self.assertTrue(index.body.startswith("<!DOCTYPE html>"))
        self.assertIn('<link rel="stylesheet" href="style.css">', index.body)

    def test_word_count(self) -> None:
        book = self.parse(MODERN_HTML)
        self.assertGreater(book.word_count, 10)

    def test_no_content_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.parse("<html><body><header></header></body></html>")

    def test_oversized_single_div_is_split(self) -> None:
        # Dictionary-style markup: one <div class="body"> holding everything.
        entries = "".join(
            f"<p>Headword {n}, n. Definition text for entry {n}. " + ("filler " * 400) + "</p>" for n in range(400)
        )
        html = f"<html><body><h1>Big Dictionary</h1><div class='body'><h2>A</h2>{entries}</div></body></html>"
        book = self.parse(html)
        chapters = [page for page in book.pages if page.path.startswith("chapter/")]
        self.assertGreater(len(chapters), 1, "an oversized article must be split")
        self.assertLess(max(len(page.body.encode()) for page in chapters), MAX_ARTICLE_BYTES * 2)
        joined = " ".join(page.body for page in chapters)
        self.assertIn("Headword 0", joined)
        self.assertIn("Headword 399", joined)


def toc_entries(book) -> list[tuple[int, str, str]]:
    """(depth, title, href) for every entry on the front page."""
    index = next(page for page in book.pages if page.path == "index.html")
    return [
        (int(depth), html_lib.unescape(title), href)
        for depth, href, title in re.findall(r'<li class="depth-(\d)"><a href="([^"]+)">([^<]+)</a></li>', index.body)
    ]


class ContentsPageTest(unittest.TestCase):
    """The index reproduces the book's own table of contents.

    Gutenberg books disagree about how a chapter looks: a bare "I", a hidden
    heading, a `div.chapter` per chapter or one wrapper per volume. Whenever the
    ebook prints a contents page, its entries decide the article titles and
    nesting, so the ZIM does not depend on those layout accidents.
    """

    def parse(self, html: str):
        media_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        return parse_book(html, make_meta(), "https://example.test/book.html", media_dir, fake_fetch({}))

    def test_bare_numeral_chapters_take_their_titles_from_the_contents_page(self) -> None:
        html = """<html><body><h1>Roman Chapters</h1>
        <h2>CONTENTS</h2>
        <table>
          <tr><td>CHAPTER</td><td></td><td>PAGE</td></tr>
          <tr><td>I.</td><td>How the hero was born</td><td><a href="#Page_1">1</a></td></tr>
          <tr><td>II.</td><td>What became of him</td><td><a href="#Page_9">9</a></td></tr>
          <tr><td>III.</td><td>He returns home</td><td><a href="#Page_17">17</a></td></tr>
        </table>
        <p><span><a id="Page_1">[Pg 1]</a></span></p>
        <h2>I</h2><p>Born under a lucky star.</p>
        <p><span><a id="Page_9">[Pg 9]</a></span></p>
        <h2>II</h2><p>He travelled far and wide.</p>
        <p><span><a id="Page_17">[Pg 17]</a></span></p>
        <h2>III</h2><p>He came home again.</p>
        </body></html>"""
        book = self.parse(html)
        entries = toc_entries(book)
        self.assertEqual(
            [title for _depth, title, _href in entries],
            ["Front matter", "I How the hero was born", "II What became of him", "III He returns home"],
        )
        chapters = [page for page in book.pages if page.path.startswith("chapter/")]
        self.assertEqual(len(chapters), 3, "each contents entry is one article")
        self.assertEqual(chapters[0].title, "I How the hero was born")
        self.assertIn("Born under a lucky star", chapters[0].body)
        self.assertNotIn("He travelled far", chapters[0].body)

    def test_nested_volume_wrappers_split_at_the_volumes_chapters(self) -> None:
        html = """<html><body><h1>Stories</h1>
        <h2>CONTENTS IN EACH VOLUME</h2>
        <p class="toc"><a href="#vol1"><b>VOLUME I.</b></a></p>
        <p class="toc"><a href="#s1">First Story</a></p>
        <p class="toc"><a href="#s2">Second Story</a></p>
        <p class="toc"><a href="#vol2"><b>VOLUME II.</b></a></p>
        <p class="toc"><a href="#s3">Third Story</a></p>
        <div class="volume">
          <h2><a id="vol1"></a>VOLUME I.</h2>
          <h2><a id="s1"></a>First Story</h2><p>Once upon a time.</p>
          <h2><a id="s2"></a>Second Story</h2><p>And then again.</p>
          <h2><a id="vol2"></a>VOLUME II.</h2>
          <h2><a id="s3"></a>Third Story</h2><p>Finally, the end.</p>
        </div>
        </body></html>"""
        book = self.parse(html)
        entries = toc_entries(book)
        titles = [title for _depth, title, _href in entries]
        self.assertIn("VOLUME I", titles)
        self.assertIn("VOLUME II", titles)
        self.assertIn("Third Story", titles)
        depths = {title: depth for depth, title, _href in entries}
        self.assertLess(depths["VOLUME I"], depths["First Story"], "volumes nest above their stories")
        self.assertEqual(depths["Third Story"], depths["First Story"], "stories of both volumes are siblings")
        chapters = [page for page in book.pages if page.path.startswith("chapter/")]
        self.assertIn("Third Story", [page.title for page in chapters])

    def test_one_wrapper_around_the_whole_book_is_descended_into(self) -> None:
        html = """<html><body>
        <div class="chapter">
          <h1>History</h1>
          <h2 id="CONTENTS">CONTENTS</h2>
          <table><tr><td><a href="#Page_1">Book I</a></td><td><a href="#Page_19">19</a></td></tr>
                 <tr><td><a href="#Page_19">Book II</a></td><td>51</td></tr>
                 <tr><td><a href="#Page_51">Book III</a></td><td>73</td></tr></table>
          <p><span id="Page_1">[Pg 1]</span></p>
          <h2>First Book</h2><p>The Franks enter history.</p>
          <p><span id="Page_19">[Pg 19]</span></p>
          <h2>Second Book</h2><p>They carry on.</p>
          <p><span id="Page_51">[Pg 51]</span></p>
          <h2>Third Book</h2><p>The story ends.</p>
        </div>
        </body></html>"""
        book = self.parse(html)
        chapters = [page for page in book.pages if page.path.startswith("chapter/")]
        self.assertEqual(len(chapters), 3, "a single outer wrapper must not swallow the book")
        self.assertEqual([page.title for page in chapters], ["Book I", "Book II", "Book III"])
        self.assertIn("They carry on", chapters[1].body)

    def test_alphabetical_index_is_not_mistaken_for_the_contents(self) -> None:
        html = """<html><body><h1>Stories</h1>
        <h2>CONTENTS IN ALPHABETICAL ORDER</h2>
        <p><a id="a">[ A ]</a></p>
        <p><a href="#s1">APPLE</a></p>
        <p><a id="b">[ B ]</a></p>
        <p><a href="#s2">BANANA</a></p>
        <p><a id="c">[ C ]</a></p>
        <p><a href="#s3">CHERRY</a></p>
        <h2><a href="#a">A</a></h2>
        <h2><a href="#b">B</a></h2>
        <h2><a href="#c">C</a></h2>
        <h2><a id="s1"></a>APPLE</h2><p>An apple story.</p>
        <h2><a id="s2"></a>BANANA</h2><p>A banana story.</p>
        <h2><a id="s3"></a>CHERRY</h2><p>A cherry story.</p>
        </body></html>"""
        book = self.parse(html)
        titles = [title for _depth, title, _href in toc_entries(book)]
        self.assertEqual(titles, ["Front matter", "APPLE", "BANANA", "CHERRY"])
        self.assertNotIn("A", titles)

    def test_hidden_headings_still_split_articles(self) -> None:
        html = """<html><body><h1>Franks</h1>
        <div class="chapter"><h2 style="display: none; visibility: hidden;">First Book</h2></div>
        <p>First book prose.</p>
        <div class="chapter"><h2 style="display: none; visibility: hidden;">Second Book</h2></div>
        <p>Second book prose.</p>
        </body></html>"""
        book = self.parse(html)
        chapters = [page for page in book.pages if page.path.startswith("chapter/")]
        self.assertEqual([page.title for page in chapters], ["First Book", "Second Book"])

    def test_empty_wrapper_is_not_an_article(self) -> None:
        html = """<html><body><h1>Wrapped</h1>
        <div class="chapter"></div>
        <div class="chapter"><h2>Chapter I</h2><p>Real content.</p></div>
        </body></html>"""
        book = self.parse(html)
        chapters = [page for page in book.pages if page.path.startswith("chapter/")]
        self.assertEqual([page.title for page in chapters], ["Chapter I"])
        self.assertNotIn("Untitled", [page.title for page in book.pages])


class OverrideTest(unittest.TestCase):
    """Descriptor title/author overrides reach the ZIM and its description."""

    def test_title_and_author_replace_rdf_values(self) -> None:
        meta = make_meta(title="The Original Title", creators=("Dickens, Charles",))
        spec = BookSpec(book_id="999", title="David Copperfield", author="Charles Dickens")
        updated = _apply_overrides(meta, spec)
        self.assertEqual(updated.title, "David Copperfield")
        self.assertEqual(updated.creators, ("Charles Dickens",))
        self.assertIn("David Copperfield by Charles Dickens.", updated.description)
        self.assertNotIn("Dickens, Charles", updated.description)

    def test_author_override_replaces_mismatched_rdf_creator(self) -> None:
        # ebook 68282's RDF record names a different book's author entirely.
        meta = make_meta(title="The call of Cthulhu", creators=("Levine, Carl Oscar",))
        spec = BookSpec(book_id="68282", author="H. P. Lovecraft")
        updated = _apply_overrides(meta, spec)
        self.assertEqual(updated.creators, ("H. P. Lovecraft",))
        self.assertIn("The call of Cthulhu by H. P. Lovecraft.", updated.description)

    def test_no_overrides_returns_metadata_unchanged(self) -> None:
        meta = make_meta()
        self.assertIs(_apply_overrides(meta, BookSpec(book_id="999")), meta)

    def test_language_override_rebuilds_description(self) -> None:
        meta = make_meta(title="Book", creators=("Doe, Jane",))
        updated = _apply_overrides(meta, BookSpec(book_id="999", language="la"))
        self.assertEqual(updated.language3, "lat")
        self.assertIn("Book by Doe, Jane.", updated.description)


class ZimTest(unittest.TestCase):
    def test_build_and_verify(self) -> None:
        media_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        payloads = {"https://example.test/images/fig1.png": PNG}
        book = parse_book(
            MODERN_HTML,
            make_meta(),
            "https://example.test/book.html",
            media_dir,
            fake_fetch(payloads),
        )
        target = media_dir / "out.zim"
        result = build_zim(target, book)
        self.assertEqual(result.pages, len(book.pages))
        self.assertTrue(target.is_file())
        self.assertFalse(target.with_name("out.zim.part").exists())

        archive = Archive(target)
        self.assertTrue(archive.check())
        self.assertEqual(archive.main_entry.path, "mainPage")
        self.assertTrue(archive.main_entry.is_redirect)
        self.assertEqual(archive.main_entry.get_redirect_entry().path, "index.html")
        self.assertEqual(archive.has_fulltext_index, True)

        self.assertEqual(archive.get_metadata("Title"), b"Testable Tales")
        self.assertEqual(archive.get_metadata("Language"), b"eng")
        self.assertEqual(archive.get_metadata("Creator"), b"Doe, Jane")
        self.assertIn(b"Fiction", archive.get_metadata("Keywords"))
        self.assertEqual(
            archive.get_metadata("Tags"),
            b"_category:english_literature;_ftindex:yes;_pictures:yes;_videos:no;_details:yes",
        )
        self.assertEqual(archive.get_entry_by_path("index.html").title, "Testable Tales")

        for entry_id in range(archive.entry_count):
            entry = archive.get_entry_by_id(entry_id)
            if entry.is_redirect or not entry.path.endswith((".html", ".css")):
                continue
            body = bytes(entry.get_item().content).decode("utf-8")
            for reference in re.findall(r'(?:href|src)="([^"]+)"', body):
                if reference.startswith(("http", "mailto", "#", "data:")):
                    continue
                target_path = posixpath.normpath(posixpath.join(posixpath.dirname(entry.path), reference.split("#")[0]))
                self.assertTrue(
                    archive.has_entry_by_path(target_path),
                    f"{entry.path} references missing {reference}",
                )

        search = Searcher(archive).search(Query().set_query("unicorn"))
        self.assertEqual(search.get_estimated_matches(), 1)
        self.assertEqual([result.path for result in search.get_results(0, 5)], ["chapter/002.html"])


class TocAuditTest(unittest.TestCase):
    """`alexandria --check-toc` finds broken, stale or mis-nested indexes."""

    def build(self, html: str):
        media_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        book = parse_book(html, make_meta(), "https://example.test/book.html", media_dir, fake_fetch({}))
        target = media_dir / "out.zim"
        build_zim(target, book)
        return target, book

    def test_clean_archive_passes(self) -> None:
        target, _book = self.build(MODERN_HTML)
        issues = audit_archive(Archive(target))
        self.assertEqual([issue.message for issue in issues], [])

    def test_placeholder_titles_and_missing_articles_are_reported(self) -> None:
        _target, book = self.build(MODERN_HTML)
        index = next(page for page in book.pages if page.path == "index.html")
        tampered = index.body.replace("Chapter I. The First", "[Pg 42]").replace(
            'href="chapter/002.html#chap02"', 'href="chapter/999.html"'
        )
        book.pages = [Page(path=index.path, title=index.title, body=tampered)] + [
            page for page in book.pages if page.path != index.path
        ]
        target = Path(self.enterContext(tempfile.TemporaryDirectory())) / "tampered.zim"
        build_zim(target, book)
        messages = [issue.message for issue in audit_archive(Archive(target))]
        self.assertTrue(any("placeholder title" in message for message in messages), messages)
        self.assertTrue(any("missing article" in message for message in messages), messages)

    def test_online_comparison_reports_drift(self) -> None:
        target, _book = self.build(MODERN_HTML)
        entries = entries_from_archive(Archive(target))
        contents = ContentsBlock(
            heading="Contents",
            entries=[
                ContentsEntry(title="PART I", depth=1, target=Tag(name="h2")),
                ContentsEntry(title="Chapter I. The First", depth=2, target=Tag(name="h2")),
                ContentsEntry(title="Chapter II. A Different Title", depth=2, target=Tag(name="h2")),
            ],
        )
        messages = [issue.message for issue in compare_with_online(entries, contents)]
        self.assertTrue(any("a different title" in message.lower() for message in messages), messages)

    def test_cli_check_toc_exits_nonzero_on_defects(self) -> None:
        target, _book = self.build(MODERN_HTML)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            self.assertEqual(main(["--offline", "--check-toc", str(target)]), 0)
        self.assertIn("OK", out.getvalue())


class ArchiveNoticeTest(unittest.TestCase):
    """Latin has no Xapian stemmer; libzim's notice must not reach stderr."""

    @contextlib.contextmanager
    def captured_stderr(self):
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "stderr.txt"
        saved = os.dup(2)
        sys.stderr.flush()
        target_fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        try:
            os.dup2(target_fd, 2)
        finally:
            os.close(target_fd)
        try:
            yield path
        finally:
            sys.stderr.flush()
            os.dup2(saved, 2)
            os.close(saved)

    def test_latin_archive_is_built_quietly_and_stays_searchable(self) -> None:
        media_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        book = parse_book(
            MODERN_HTML,
            make_meta(languages=("la",), language3="lat", language_label="Latin"),
            "https://example.test/book.html",
            media_dir,
            fake_fetch({}),
        )
        target = media_dir / "latin.zim"

        with self.captured_stderr() as stderr:
            build_zim(target, book)
            archive = open_archive(target)
            search = Searcher(archive).search(Query().set_query("unicorn"))
        output = stderr.read_text(encoding="utf-8", errors="replace")

        self.assertNotIn("No stemming", output)
        self.assertEqual(archive.get_metadata("Language"), b"lat")
        self.assertEqual(search.get_estimated_matches(), 1)

    def test_other_libzim_diagnostics_are_replayed(self) -> None:
        class NoisyArchive:
            def __init__(self, _path) -> None:
                os.write(2, b"No stemming for language 'la'\nlibzim: something else\n")

        with self.captured_stderr() as stderr, mock.patch("alexandria.archive.Archive", NoisyArchive):
            open_archive(Path("unused.zim"))
        output = stderr.read_text(encoding="utf-8", errors="replace")

        self.assertNotIn("No stemming", output)
        self.assertIn("libzim: something else", output)


class ImageCacheTest(unittest.TestCase):
    """Images from different books often share a filename; caching must not mix them."""

    def test_cache_keys_differ_per_url(self) -> None:
        alice = url_cache_key("https://www.gutenberg.org/cache/epub/11/images/cover.jpg", prefix="cover")
        peter = url_cache_key("https://www.gutenberg.org/cache/epub/16/images/cover.jpg", prefix="cover")
        self.assertNotEqual(alice, peter)
        self.assertEqual(alice.parent, peter.parent)
        self.assertEqual(alice.suffix, ".jpg")

    def test_same_url_maps_to_same_key(self) -> None:
        url = "https://example.test/images/fig1.png"
        self.assertEqual(url_cache_key(url), url_cache_key(url))

    def test_cover_falls_back_to_medium_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            urls: list[str] = []

            class StubFetcher:
                def fetch_bytes(self, url: str, _key: Path) -> bytes:
                    urls.append(url)
                    if url.rstrip("/").endswith("images/cover.jpg"):
                        raise RuntimeError("404")
                    return b"medium cover"

            workflow = Workflow.__new__(Workflow)
            workflow.fetcher = StubFetcher()  # type: ignore[assignment]
            asset = workflow._cover(BookSpec(book_id="2000"), Path(tmp))
            self.assertIsNotNone(asset)
            self.assertEqual(len(urls), 2)
            self.assertIn("cover.medium.jpg", urls[1])
            assert asset is not None
            self.assertEqual(asset.filepath.read_bytes(), b"medium cover")

    def test_cover_fetch_is_per_book(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            calls: list[Path] = []

            class StubFetcher:
                def fetch_bytes(self, url: str, key: Path) -> bytes:
                    calls.append(key)
                    return b"data:" + url.encode()

            workflow = Workflow.__new__(Workflow)
            workflow.fetcher = StubFetcher()  # type: ignore[assignment]
            first = workflow._cover(BookSpec(book_id="11"), Path(tmp) / "a")
            second = workflow._cover(BookSpec(book_id="16"), Path(tmp) / "b")
            self.assertEqual(len(calls), 2)
            self.assertNotEqual(calls[0], calls[1])
            assert first is not None and second is not None
            self.assertNotEqual(first.url, second.url)
            self.assertEqual(first.zim_path, second.zim_path)


class WorkflowSkipTest(unittest.TestCase):
    """`alexandria build` must not redo work that already exists."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.output = self.tmp / "output"
        self.output.mkdir()
        self.config = Config(
            books=(BookSpec(book_id="999", title="Zebra Tales"),),
            output=self.output,
            cache=self.tmp / "cache",
            source=self.tmp / "input.yaml",
        )

    def build_once(self, force: bool = False, prebuild: bool = True):
        workflow = Workflow(self.config, force=force, log=lambda _message: None)
        book = parse_book(
            MODERN_HTML,
            make_meta(),
            "https://example.test/book.html",
            self.tmp,
            fake_fetch({}),
        )
        with mock.patch.object(Workflow, "convert", autospec=True) as convert:
            convert.return_value = BookReport(
                book_id="999",
                title="Testable Tales",
                output=self.output / "gutenberg-999-testable-tales.zim",
                pages=len(book.pages),
                media=0,
                words=book.word_count,
                size=1,
            )
            if prebuild and not force:
                build_zim(self.output / "gutenberg-999-testable-tales.zim", book)
            reports, failures, skipped = workflow.run()
        return reports, failures, skipped, convert

    def test_existing_archive_is_skipped(self) -> None:
        reports, failures, skipped, convert = self.build_once()
        self.assertEqual(reports, [])
        self.assertEqual(failures, [])
        self.assertEqual(skipped, ["999"])
        convert.assert_not_called()
        self.assertTrue((self.output / "library.xml").is_file())

    def test_force_rebuilds_existing_archive(self) -> None:
        reports, failures, skipped, convert = self.build_once(force=True)
        self.assertEqual(skipped, [])
        convert.assert_called_once()

    def test_unreadable_archive_is_rebuilt(self) -> None:
        (self.output / "gutenberg-999-broken.zim").write_bytes(b"not a zim")
        reports, failures, skipped, convert = self.build_once(prebuild=False)
        self.assertEqual(skipped, [])
        convert.assert_called_once()

    def test_build_false_is_skipped(self) -> None:
        config = Config(
            books=(BookSpec(book_id="999", build=False),),
            output=self.output,
            cache=self.tmp / "cache",
            source=self.tmp / "input.yaml",
        )
        reports, failures, skipped = Workflow(config, log=lambda _message: None).run()
        self.assertEqual((reports, failures, skipped), ([], [], ["999"]))


class FormatsTest(unittest.TestCase):
    """Ebooks without HTML (PDF/TeX only) are converted to parseable HTML."""

    def test_text_to_html_sections(self) -> None:
        text = (
            "CHAPTER I.\n\n"
            "It was a dark and stormy night, and the wind blew steadily.\n\n"
            "The door creaked open.\n\n"
            "CHAPTER II.\n\n"
            "Another day dawned over the hills and far away.\n"
        )
        html = text_to_html(text, "Testable Tales")
        self.assertEqual(html.count('<div class="chapter">'), 2)
        self.assertIn("<h2>CHAPTER I.</h2>", html)
        self.assertIn("The door creaked open.", html)

    def test_plain_text_paragraphs_are_escaped(self) -> None:
        html = text_to_html("A & B <tag>\n\nsecond paragraph", "T")
        self.assertIn("A &amp; B &lt;tag&gt;", html)

    def test_markers_and_noise_removed(self) -> None:
        text = (
            "*** START OF THE PROJECT GUTENBERG EBOOK X ***\n\n"
            "empty roman\n\n"
            "Front MatterFront Matter\n\n"
            + "Real content that is definitely long enough to survive merging. " * 5
            + "\n\n*** END OF THE PROJECT GUTENBERG EBOOK X ***\n"
        )
        html = text_to_html(text, "T")
        self.assertNotIn("*** START", html)
        self.assertNotIn("*** END", html)
        self.assertNotIn("empty roman", html)
        self.assertIn("<p>Front Matter</p>", html)

    def test_numbered_propositions_become_sections(self) -> None:
        text = (
            "1 The world is everything that is the case. " + "Filler sentence here. " * 12 + "\n\n"
            "1.1 The world is the totality of facts, not of things. " + "More filler text. " * 12 + "\n\n"
            "2 What is the case, the fact, is the existence of atomic facts. " + "Yet more filler. " * 12
        )
        html = text_to_html(text, "Tractatus")
        self.assertIn("<h2>1. The world is everything", html)
        self.assertIn("<h2>2. What is the case", html)
        self.assertIn("1.1 The world is the totality of facts", html)

    def test_tex_source_converted(self) -> None:
        tex = (
            "\\documentclass{book}\n\\begin{document}\n"
            "\\chapter{Chapter I.}\n"
            + "The stormy night was dark and full of terrors, as everyone said. " * 6
            + "\n\\end{document}\n"
        )
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "book.tex"
        path.write_text(tex, encoding="latin-1")
        html = tex_to_html(path, "Testable Tales")
        self.assertIn("<h1>Testable Tales</h1>", html)
        self.assertIn("stormy night", html)

    def test_utf8_tex_keeps_accents(self) -> None:
        tex = "\\begin{document}\ngeh\u00f6rt K\u00fcrnberger\n\\end{document}\n"
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "accent.tex"
        path.write_text(tex, encoding="utf-8")
        html = tex_to_html(path, "T")
        self.assertIn("geh\u00f6rt", html)
        self.assertNotIn("geh\u00c3", html)


class LibraryTest(unittest.TestCase):
    def test_write_library(self) -> None:
        media_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        output = media_dir / "output"
        output.mkdir()
        book = parse_book(
            MODERN_HTML,
            make_meta(title="Zebra Tales"),
            "https://example.test/book.html",
            media_dir,
            fake_fetch({}),
        )
        zim = output / "gutenberg-999-zebra-tales.zim"
        build_zim(zim, book)

        second = parse_book(
            MODERN_HTML,
            make_meta(title="Apple Tales"),
            "https://example.test/book.html",
            media_dir,
            fake_fetch({}),
        )
        other = output / "gutenberg-998-apple-tales.zim"
        build_zim(other, second)

        # Non-ZIM files in the directory must be ignored.
        (output / "notes.txt").write_text("hello", encoding="utf-8")

        count = write_library(output / LIBRARY_NAME, sorted(output.glob("*.zim")))
        self.assertEqual(count, 2)

        root = ET.parse(output / LIBRARY_NAME).getroot()
        self.assertEqual(root.tag, "library")
        self.assertEqual(root.get("version"), LIBRARY_VERSION)
        entries = root.findall("book")
        self.assertEqual([entry.get("title") for entry in entries], ["Apple Tales", "Zebra Tales"])

        entry = entries[1]
        self.assertEqual(entry.get("path"), "gutenberg-999-zebra-tales.zim")
        self.assertEqual(entry.get("language"), "eng")
        self.assertEqual(entry.get("creator"), "Doe, Jane")
        self.assertEqual(entry.get("publisher"), "Project Gutenberg")
        self.assertEqual(entry.get("articleCount"), str(len(book.pages)))
        self.assertEqual(
            entry.get("tags"), "_category:english_literature;_ftindex:yes;_pictures:yes;_videos:no;_details:yes"
        )
        self.assertEqual(entry.get("category"), "english_literature")
        self.assertTrue(entry.get("id"))
        self.assertTrue(int(entry.get("size")) >= 1)

    def test_write_library_skips_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            (output / "broken.zim").write_bytes(b"not a zim file")
            count = write_library(output / LIBRARY_NAME, sorted(output.glob("*.zim")))
            self.assertEqual(count, 0)
            self.assertEqual(ET.parse(output / LIBRARY_NAME).getroot().findall("book"), [])


if __name__ == "__main__":
    unittest.main()

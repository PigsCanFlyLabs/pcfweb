"""Guards against Django's ``{# ... #}`` comment syntax being used across
more than one line.

Django's lexer matches comments with a pattern that does not span
newlines, so a ``{# ... #}`` opened on one line and closed on another is
not a comment at all: every line of it is emitted verbatim into the
rendered page. ``{% comment %}``/``{% endcomment %}`` is the multi-line
form."""

import os
import re
from pathlib import Path

from django.test import SimpleTestCase, TestCase

from main.models import Product


REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Django renders whatever it is handed, so an .html-only sweep is too
# narrow: google_products.xml is a template, and a text email body would
# be one too. Filter by suffix rather than naming the template dirs, so
# a template added outside main/templates is still covered.
TEMPLATE_SUFFIXES = {".html", ".htm", ".xml", ".txt", ".svg", ".json"}

# ``static`` and ``media`` hold collected and vendored assets -- Django's
# admin ships .txt licence files down there, and they are nobody's
# template.
SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".mypy_cache", "static", "media",
}

# One .html and one .xml, so a sweep that quietly stopped covering
# either fails instead of passing on the files it still reaches.
KNOWN_TEMPLATES = (
    "main/templates/single-product.html",
    "main/templates/google_products.xml",
)

# Deliberately not re.DOTALL -- this mirrors how Django itself matches a
# comment, which is exactly why an unclosed one leaks.
SINGLE_LINE_COMMENT = re.compile(r"\{#.*?#\}")

LATIN_FILLER = re.compile(
    r"\b("
    r"lorem|ipsum|dolor sit amet|consectetur|adipiscing|eiusmod|"
    r"incididunt|labore|aliqua|veniam|nostrud|ullamco|laboris|"
    r"commodo|consequat|duis aute|cupidatat|proident|"
    r"sunt in culpa|donec|nulla|vestibulum|curabitur|praesent|"
    r"aenean|etiam|phasellus"
    r")\b",
    re.IGNORECASE,
)


def find_templates():
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        # Pruned in place, so the walk never descends into them at all.
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.suffix in TEMPLATE_SUFFIXES:
                yield path


def find_multiline_comments(text):
    """Line numbers of every ``{#`` Django would not read as a comment."""
    # Blank out the real comments in place -- they never contain a
    # newline, so this keeps every remaining offset where it was.
    masked = SINGLE_LINE_COMMENT.sub(lambda m: " " * len(m.group()), text)
    return [
        text.count("\n", 0, match.start()) + 1
        for match in re.finditer(r"\{#", masked)
    ]


def find_latin_filler(text):
    """Line numbers containing classic theme filler copy."""
    return sorted({
        text.count("\n", 0, match.start()) + 1
        for match in LATIN_FILLER.finditer(text)
    })


class MultiLineTemplateCommentTest(SimpleTestCase):
    def test_sweep_reaches_known_templates(self):
        """A sweep that missed the tree would pass for the wrong reason.

        Counting files is not enough: a REPO_ROOT that resolved somewhere
        wrong could still turn up a couple of stray matches. Name real
        templates instead, so a misresolved root fails loudly."""
        swept = {
            str(path.relative_to(REPO_ROOT)) for path in find_templates()
        }

        for known in KNOWN_TEMPLATES:
            self.assertIn(known, swept, f"{known} is not being scanned")

    def test_no_multi_line_django_comments(self):
        offenders = []
        for path in find_templates():
            for lineno in find_multiline_comments(path.read_text()):
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{lineno}"
                )

        self.assertEqual(
            offenders,
            [],
            "{# ... #} is single-line only; these are emitted into the "
            "rendered page instead of being stripped. Use "
            "{% comment %}...{% endcomment %}: " + ", ".join(offenders),
        )

    def test_no_latin_filler_text(self):
        offenders = []
        swept = set()
        for path in find_templates():
            swept.add(str(path.relative_to(REPO_ROOT)))
            for lineno in find_latin_filler(path.read_text()):
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{lineno}"
                )

        for known in KNOWN_TEMPLATES:
            self.assertIn(known, swept, f"{known} is not being scanned")

        self.assertEqual(
            offenders,
            [],
            "Classic latin/theme filler text found in rendered templates: "
            + ", ".join(offenders),
        )


class ProductPageCommentLeakTest(TestCase):
    """The user-visible symptom, on the template that had the leak."""

    def test_product_page_does_not_render_comment_text(self):
        product = Product.objects.create(
            name="Widget",
            description="A widget.",
            external_product_id="prod_comment_leak",
            price=1000,
            cat=Product.Categories.ELECTRONICS,
        )

        response = self.client.get(f"/product/{product.pk}")

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "The quantity input is a real field")
        self.assertNotContains(response, "posted field wins")

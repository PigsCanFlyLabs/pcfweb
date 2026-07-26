"""Guards against Django's ``{# ... #}`` comment syntax being used across
more than one line.

Django's lexer matches comments with a pattern that does not span
newlines, so a ``{# ... #}`` opened on one line and closed on another is
not a comment at all: every line of it is emitted verbatim into the
rendered page. ``{% comment %}``/``{% endcomment %}`` is the multi-line
form."""

import re
from pathlib import Path

from django.test import SimpleTestCase, TestCase

from main.models import Product


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SKIP_DIRS = {".git", ".venv", "venv", "env", "node_modules", "__pycache__"}

# Deliberately not re.DOTALL -- this mirrors how Django itself matches a
# comment, which is exactly why an unclosed one leaks.
SINGLE_LINE_COMMENT = re.compile(r"\{#.*?#\}")


def find_templates():
    for path in sorted(REPO_ROOT.rglob("*.html")):
        if SKIP_DIRS.isdisjoint(path.relative_to(REPO_ROOT).parts):
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


class MultiLineTemplateCommentTest(SimpleTestCase):
    def test_sweep_finds_templates(self):
        """A sweep that matches nothing would pass for the wrong reason."""
        self.assertGreater(len(list(find_templates())), 1)

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

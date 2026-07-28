"""Tests for the "Distributed Computing 4 Kids (and Executives)" SKUs.

The three catalogue entries as the fixture defines them, plus the build
guard and packaging rules that get the e-book archive into the image
without letting it leak onto the web."""

import html as html_module
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from django.test import TestCase
from django.utils.html import escape

from main import digital
from main.models import Product
from main.tests.base import EBOOK_PK, EBOOK_STEM, REPO_ROOT


P1 = (
    "How do you solve the biggest problems of today? You harness the power "
    "of teamwork, with computers, friends, or garden gnomes. Distributed "
    "Computing 4 Kids and Executives teaches how to solve big problems with "
    "garden gnomes (and later computers). These techniques power modern AI, "
    "search, and recommendation systems."
)
P2 = (
    "This book is for kids interested in learning how to solve large "
    "problems, or executives looking to understand what their data science "
    "team is up to and why their cloud and AI bill keeps growing."
)
P3 = (
    "The book is in two parts going from garden gnomes in part one to "
    "computers in part two. The first part explains the concepts of "
    "distributed computing like how work can be split up and combined."
)
P4 = (
    "For readers looking to turn the concepts into reality the second part "
    "goes into actual code with Python and Apache Spark, a distributed "
    "computing framework."
)
P5 = (
    "Both kids and Executives can benefit from having helpers if they choose "
    "to pursue the second part, although they'll find them in different "
    "places. For kids a librarian or parent who understands computers can be "
    "a great helper. Executives should reach out to their data science group "
    "or experienced intern who may be able to help them connect to their "
    "actual business data."
)
P6 = (
    "Big Data and Large Language Models don't have to mean a big headache. "
    "Grab your helper, pour some tea, and get started!"
)
STANDARD_COPY = "\n\n".join((P1, P2, P3, P4, P5, P6))
STANDARD_PARAGRAPHS = (P1, P2, P3, P4, P5, P6)

# The Executive Edition keeps the new standard copy and appends the row's
# existing executive-only add-on text verbatim.
EXECUTIVE_ADD_ON = (
    'This special executive edition is the almost exact same content as '
    'regular Distributed Computing 4 Kids and Executives but it costs '
    '(roughly) $10.42 more with a different ISBN. You might be saying to '
    'yourself, "Holden, why would I want this special executive edition?" '
    'the answer is to be even more executive, and support creation of books '
    "like these. You're not just reading a book for kids and executives, "
    "you're reading the executive edition. The extra also helps keep a "
    "developer from turning to a life of enterprise support contracts."
)
EXECUTIVE_EDITION_COPY = f"{STANDARD_COPY}\n\n{EXECUTIVE_ADD_ON}"
EXECUTIVE_PARAGRAPHS = STANDARD_PARAGRAPHS + (EXECUTIVE_ADD_ON,)


class BookAssetGuardTest(TestCase):
    """Part 4: the build guard.

    Without it a build host that never ran `git lfs pull` ships ~130-byte
    pointer stubs, and customers get emailed a text file. The check is only
    worth anything if it actually fails the build, so run it for real.
    """

    GUARD = str(REPO_ROOT / "scripts" / "check-book-assets.sh")
    LFS_POINTER = (
        "version https://git-lfs.github.com/spec/v1\n"
        "oid sha256:1cbec737f863e4922cee63cc2ebbfaafcd1cff8b790d8cfd2e6a5d550b648afa\n"
        "size 27641344\n")

    def setUp(self):
        self.source = Path(tempfile.mkdtemp(prefix="pcfweb-src-")).resolve()
        self.dest = Path(tempfile.mkdtemp(prefix="pcfweb-dest-")).resolve()
        self.addCleanup(shutil.rmtree, self.source, True)
        self.addCleanup(shutil.rmtree, self.dest, True)

    def run_guard(self):
        return subprocess.run(
            [self.GUARD, str(self.source), str(self.dest)],
            capture_output=True, text=True)

    def write_real_archive(self, stem=EBOOK_STEM):
        # Padded past the one-megabyte floor, and a genuine ZIP.
        path = self.source / f"{stem}.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(f"{stem}.epub", "e" * (2 * 1024 * 1024))
            archive.writestr(f"{stem}.pdf", "%PDF-1.4" + "p" * 1024)
        return path

    def test_a_real_archive_passes_and_is_staged(self):
        self.write_real_archive()

        result = self.run_guard()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.dest / f"{EBOOK_STEM}.zip").is_file())

    def test_an_lfs_pointer_fails_the_build_loudly(self):
        (self.source / f"{EBOOK_STEM}.zip").write_text(self.LFS_POINTER)

        result = self.run_guard()

        self.assertEqual(result.returncode, 1)
        self.assertIn("Git LFS pointer file", result.stderr)
        self.assertIn("git lfs pull", result.stderr)
        self.assertFalse((self.dest / f"{EBOOK_STEM}.zip").exists())

    def test_a_file_that_is_not_a_zip_fails_the_build(self):
        (self.source / f"{EBOOK_STEM}.zip").write_bytes(b"x" * (2 * 1024 * 1024))

        result = self.run_guard()

        self.assertEqual(result.returncode, 1)
        self.assertIn("ZIP magic bytes", result.stderr)

    def test_an_implausibly_small_archive_fails_the_build(self):
        with zipfile.ZipFile(self.source / f"{EBOOK_STEM}.zip", "w") as archive:
            archive.writestr("tiny.txt", "not a book")

        result = self.run_guard()

        self.assertEqual(result.returncode, 1)
        self.assertIn("expected a real book archive", result.stderr)

    def test_an_empty_asset_directory_fails_the_build(self):
        result = self.run_guard()

        self.assertEqual(result.returncode, 1)
        self.assertIn("no book archives found", result.stderr)

    def test_one_bad_archive_stages_none_of_them(self):
        # A partial stage would put a good book and a stub in the same image.
        self.write_real_archive("good_book")
        (self.source / "bad_book.zip").write_text(self.LFS_POINTER)

        result = self.run_guard()

        self.assertEqual(result.returncode, 1)
        self.assertEqual(list(self.dest.glob("*.zip")), [])


class BookAssetPackagingTest(TestCase):
    """Part 4: the archives have to reach the image, and stay off the web."""

    ROOT = REPO_ROOT

    def test_the_dockerfile_copies_the_archives_in(self):
        dockerfile = (self.ROOT / "Dockerfile").read_text()
        self.assertIn("COPY --chown=www-data:www-data book-assets "
                      "/opt/app/book-assets", dockerfile)

    def test_the_archives_are_not_dockerignored(self):
        # Adding them here is the one-line change that silently ships an
        # image with no books in it.
        ignored = [line.strip() for line
                   in (self.ROOT / ".dockerignore").read_text().splitlines()]
        self.assertNotIn("book-assets", ignored)
        self.assertNotIn("book-assets/", ignored)

    def test_the_archives_are_gitignored(self):
        ignored = [line.strip() for line
                   in (self.ROOT / ".gitignore").read_text().splitlines()]
        self.assertIn("book-assets/", ignored)

    def test_the_asset_root_is_not_publicly_servable(self):
        # nginx serves /static and /media off disk (conf/nginx.default) and
        # nothing else, so the books must live outside both.
        from django.conf import settings as django_settings
        root = digital.asset_root()
        for public in (Path(django_settings.STATIC_ROOT).resolve(),
                       Path(django_settings.MEDIA_ROOT).resolve()):
            with self.subTest(public=str(public)):
                self.assertFalse(root.is_relative_to(public))

    def test_the_build_calls_the_guard_before_building_the_image(self):
        build = (self.ROOT / "build.sh").read_text()
        self.assertLess(build.index("check-book-assets.sh"),
                        build.index("docker buildx build"))


class DistributedComputing4KidsCatalogTest(TestCase):
    """Part 5: the three SKUs, as the fixture defines them."""

    fixtures = ["initial_products"]

    TITLE = "Distributed Computing 4 Kids (and Executives)"

    def rendered_page(self, pk):
        response = self.client.get(f"/product/{pk}")
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def rendered_description_paragraphs(self, pk):
        html = self.rendered_page(pk)
        match = re.search(
            r'<div class="product-description">(.*?)</div>', html, re.DOTALL)
        self.assertIsNotNone(match)
        assert match is not None  # for mypy
        return [
            html_module.unescape(text.strip())
            for text in re.findall(r"<p>(.*?)</p>", match.group(1), re.DOTALL)
        ]

    def test_all_three_skus_are_present(self):
        for pk, price in ((104, 2000), (105, 3042), (106, 1299)):
            with self.subTest(pk=pk):
                product = Product.objects.get(pk=pk)
                self.assertTrue(product.name.startswith(self.TITLE))
                self.assertEqual(product.price, price)
                self.assertEqual(product.cat, Product.Categories.BOOKS)
                # book_covers/ prefix, not the flat name this branch was
                # written against: PR #23 relocated every cover into
                # images/book_covers/ in the pcfweb-assets repo. The prefixed
                # path is the one that actually resolves there -- checked at
                # build time by scripts/check-product-images.sh, against the
                # real asset checkout rather than against this code.
                self.assertEqual(
                    product.image_name,
                    "book_covers/distributed_computing_4_kids.jpg")

    def test_the_printed_editions_are_physical_and_not_ours_to_send(self):
        for pk in (104, 105):
            with self.subTest(pk=pk):
                product = Product.objects.get(pk=pk)
                self.assertEqual(product.delivery_type,
                                 Product.DeliveryTypes.PHYSICAL)
                self.assertEqual(product.tax_code, Product.TaxTypes.BOOKS)
                self.assertFalse(product.sells_ebook)
                self.assertFalse(product.is_pwyw)
                self.assertTrue(product.is_physical_good())

    def test_the_ebook_is_digital_pwyw_and_ours_to_send(self):
        ebook = Product.objects.get(pk=EBOOK_PK)

        self.assertEqual(ebook.delivery_type, Product.DeliveryTypes.DIGITAL)
        self.assertEqual(ebook.tax_code, Product.TaxTypes.DIGITAL_BOOKS)
        self.assertEqual(ebook.tax_code, "txcd_10302000")
        self.assertTrue(ebook.sells_ebook)
        self.assertTrue(ebook.is_pwyw)
        self.assertTrue(ebook.is_digitally_fulfilled())
        self.assertFalse(ebook.is_physical_good())
        self.assertEqual(ebook.digital_asset_name, EBOOK_STEM)

    def test_the_ebook_does_not_reuse_the_physical_books_tax_code(self):
        self.assertNotEqual(
            Product.objects.get(pk=EBOOK_PK).tax_code,
            Product.objects.get(pk=104).tax_code)

    def test_the_asset_name_matches_the_naming_contract(self):
        self.assertRegex(
            Product.objects.get(pk=EBOOK_PK).digital_asset_name,
            digital.ASSET_NAME_PATTERN)

    def test_the_standard_print_edition_carries_its_real_isbn(self):
        standard = Product.objects.get(pk=104)

        self.assertEqual(standard.isbn, "9781960595997")
        # Now a real GTIN, so the feed identifies it by ISBN rather than
        # falling back to a made-up mpn.
        self.assertEqual(standard.get_gtin(), "9781960595997")

    def test_the_executive_edition_carries_its_own_isbn(self):
        executive = Product.objects.get(pk=105)

        self.assertEqual(executive.isbn, "9781960595003")
        self.assertEqual(executive.get_gtin(), "9781960595003")

    def test_the_two_print_editions_do_not_share_an_isbn(self):
        # The whole premise of the Executive Edition is a different number on
        # the same book, so a copy-paste of 104's ISBN onto 105 would make the
        # SKU pointless -- and would collide in the Merchant feed, where
        # get_gtin() is the product identifier.
        isbns = [Product.objects.get(pk=pk).isbn for pk in (104, 105, EBOOK_PK)]
        assigned = [isbn for isbn in isbns if isbn]

        self.assertEqual(len(assigned), 2)
        self.assertEqual(len(set(assigned)), len(assigned))

    def test_both_print_editions_offer_a_signature_and_the_ebook_does_not(self):
        # get_display_text() adds the note whenever isbn is set. That is right
        # for a printed book Holden can physically sign -- including the
        # Executive Edition, which is a print run like any other -- and is the
        # reason the e-book's ISBN must not be parked in `isbn`; see the
        # fixture comment and ebook_isbn.
        #
        # The note contains an apostrophe ("Holden's"), and get_display_text()
        # returns escaped markup, so the raw note is NOT a substring of it:
        # asserting `NOTE in get_display_text()` fails even when the note is
        # there. Escape it here rather than asserting on a conveniently
        # apostrophe-free fragment, which is how a vacuous assertion gets in.
        escaped_note = escape(Product.SIGNED_ON_REQUEST_NOTE)
        self.assertNotEqual(escaped_note, Product.SIGNED_ON_REQUEST_NOTE)
        for pk in (104, 105):
            with self.subTest(pk=pk):
                product = Product.objects.get(pk=pk)
                self.assertIn(escaped_note, product.get_display_text())
                # The feed is plain text, so there the note is unescaped.
                self.assertIn(Product.SIGNED_ON_REQUEST_NOTE,
                              product.get_feed_description())

        ebook = Product.objects.get(pk=EBOOK_PK)
        self.assertNotIn(escaped_note, ebook.get_display_text())
        self.assertNotIn(Product.SIGNED_ON_REQUEST_NOTE,
                         ebook.get_feed_description())

    def test_the_ebooks_isbn_is_left_unset_rather_than_placeheld(self):
        # A shared placeholder would emit Merchant-feed products with one id.
        # 106's belongs in ebook_isbn, not here.
        #
        # This test originally also asserted get_gtin() was None. That was a
        # true statement about a world where ebook_isbn did not yet exist --
        # the field arrives with the per-format identifier work -- not about
        # the property the test is named for. The e-book now has its own real
        # ISBN-13 in ebook_isbn, so it *should* carry a GTIN: an e-book with
        # an assigned ISBN is identified by it in the Merchant feed, and
        # get_gtin() resolving print_isbn or ebook_isbn or upc is what puts it
        # there. The invariant that matters -- 106 is not placeheld, and does
        # not borrow a sibling's number -- is asserted directly below instead,
        # which is strictly stronger than the None check it replaces.
        ebook = Product.objects.get(pk=EBOOK_PK)

        # Still nothing in the legacy print column, or in print_isbn: either
        # would put "available signed on request" on a download.
        self.assertFalse(ebook.isbn)
        self.assertFalse(ebook.print_isbn)

        # Its own number, not a placeholder and not a sibling's.
        self.assertEqual(ebook.ebook_isbn, "9781960595980")
        self.assertEqual(ebook.get_gtin(), "9781960595980")
        siblings = [Product.objects.get(pk=pk).get_gtin() for pk in (104, 105)]
        self.assertNotIn(ebook.get_gtin(), siblings)

    def test_the_new_skus_do_not_collide_in_the_merchant_feed(self):
        response = self.client.get("/google_products.xml")

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        ids = re.findall(r"<g:id>(\d+)</g:id>", body)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("104", ids)
        # Both print editions now have a real ISBN, so each is identified by
        # its own gtin; only the e-book still has none and falls back to mpn.
        self.assertIn("<g:gtin>9781960595997</g:gtin>", body)
        self.assertIn("<g:gtin>9781960595003</g:gtin>", body)
        # All three SKUs now carry a real ISBN of their own -- the e-book's in
        # ebook_isbn -- so none of them falls back to a synthesised mpn. The
        # feed template emits <g:mpn> only when get_gtin() is None, so PCF106
        # disappearing is the e-book gaining a genuine identifier, which is
        # better for the listing than the fallback it replaces.
        self.assertIn("<g:gtin>9781960595980</g:gtin>", body)
        mpns = re.findall(r"<g:mpn>(PCF\d+)</g:mpn>", body)
        self.assertNotIn("PCF106", mpns)
        # Whatever identifies each SKU, no two may share it.
        gtins = re.findall(r"<g:gtin>(\d+)</g:gtin>", body)
        self.assertEqual(len(gtins), len(set(gtins)))

    def test_the_new_books_are_not_attributed_to_oreilly(self):
        # get_brand() defaults the Books category to O'Reilly, which these
        # are not.
        for pk in (104, 105, EBOOK_PK):
            with self.subTest(pk=pk):
                self.assertEqual(
                    Product.objects.get(pk=pk).get_brand(),
                    "Pigs Can Fly Labs")

    def test_the_copy_is_the_owners_words(self):
        standard = Product.objects.get(pk=104)
        self.assertEqual(standard.description, STANDARD_COPY)

        executive = Product.objects.get(pk=105)
        self.assertEqual(executive.description, EXECUTIVE_EDITION_COPY)

        ebook = Product.objects.get(pk=EBOOK_PK)
        self.assertEqual(ebook.description, STANDARD_COPY)

    def test_the_executive_copy_survives_yaml_intact(self):
        # The copy carries two double quotes, two apostrophes, a literal $ and
        # a pair of parentheses, all of which are ways a YAML round-trip can
        # quietly mangle a string. Asserted on the model field rather than on
        # rendered HTML: the page escapes both the quotes and the apostrophes,
        # so an assertion there would be about the escaping, not the words.
        executive = Product.objects.get(pk=105)

        self.assertEqual(executive.description, EXECUTIVE_EDITION_COPY)
        self.assertIn('"Holden, why would I want this special executive '
                      'edition?"', executive.description)
        self.assertIn("(roughly) $10.42 more", executive.description)
        self.assertIn("almost exact same content", executive.description)
        self.assertIn("The extra also helps keep a developer from turning to "
                      "a life of enterprise support contracts.",
                      executive.description)
        self.assertEqual(executive.description.count('"'), 2)
        self.assertEqual(executive.description.count("'"), 4)

        rendered = str(executive.get_display_text())
        self.assertIn("&quot;Holden, why would I want this special executive "
                      "edition?&quot;", rendered)
        self.assertIn("You&#x27;re not just reading a book for kids and "
                      "executives", rendered)
        self.assertIn("<p>All of Holden&#x27;s books are available signed on "
                      "request</p>", rendered)

    def test_the_executive_premium_is_exactly_what_the_copy_claims(self):
        # The copy makes a factual claim about the two prices -- "(roughly)
        # $10.42 more" -- and nothing else in the suite stops one side of that
        # from moving without the other. test_all_three_skus_are_present pins
        # the literals, but literals are exactly what a repricing edits, and it
        # would go green again the moment both numbers were updated to a pair
        # the sentence no longer describes. So state the relationship instead:
        # the premium is the difference between the two rows, and the string
        # the copy has to carry is derived from that same number rather than
        # written out a second time. Reprice either edition alone and this
        # fails; reprice both and it still fails unless the sentence was
        # updated to match.
        standard = Product.objects.get(pk=104)
        executive = Product.objects.get(pk=105)

        premium = executive.price - standard.price

        self.assertEqual(premium, 1042)
        self.assertIn("$10.42", executive.description)
        self.assertIn(f"${premium // 100}.{premium % 100:02d} more",
                      executive.description)

    def test_every_sku_has_its_own_product_page(self):
        for pk in (104, 105, EBOOK_PK):
            with self.subTest(pk=pk):
                response = self.client.get(f"/product/{pk}")
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Distributed Computing 4 Kids")

    def test_the_standard_and_ebook_pages_render_six_description_paragraphs(self):
        for pk in (104, EBOOK_PK):
            with self.subTest(pk=pk):
                paragraphs = self.rendered_description_paragraphs(pk)

                self.assertEqual(paragraphs[:6], list(STANDARD_PARAGRAPHS))
                if pk == 104:
                    self.assertEqual(paragraphs[6], Product.SIGNED_ON_REQUEST_NOTE)
                    self.assertEqual(len(paragraphs), 7)
                else:
                    self.assertEqual(len(paragraphs), 6)

    def test_the_executive_page_renders_the_six_paragraphs_plus_the_add_on(self):
        paragraphs = self.rendered_description_paragraphs(105)

        self.assertEqual(paragraphs[:7], list(EXECUTIVE_PARAGRAPHS))
        self.assertEqual(paragraphs[7], Product.SIGNED_ON_REQUEST_NOTE)
        self.assertEqual(len(paragraphs), 8)

    def test_the_ebook_page_says_the_price_is_a_suggestion(self):
        response = self.client.get(f"/product/{EBOOK_PK}")

        self.assertContains(response, "Pay what you want")
        self.assertContains(response, "12.99")

    def test_no_retailer_links_are_claimed_yet(self):
        # Every alt link is driven by a field the fixture leaves unset:
        # amazon_link, bookshop_link and the rest are stored URLs, not
        # anything derived from the ISBN, so giving 104 an ISBN must not
        # conjure a retailer that does not stock the book.
        for pk in (104, 105, EBOOK_PK):
            with self.subTest(pk=pk):
                self.assertEqual(
                    Product.objects.get(pk=pk).get_alt_links(), [])


class OReillySafariLinkTest(TestCase):
    """The Safari trial link must track the publisher, not the ISBN.

    It used to be emitted for any product with an ISBN set, which was
    indistinguishable from "is an O'Reilly book" only for as long as every
    book in the catalogue was one. DC4K is self-published and is not on the
    platform, so the moment pk 104 got its ISBN that link became a false
    claim to a customer -- an offer of a free trial for a book the trial does
    not contain. on_oreilly_safari states the fact instead of inferring it.
    """

    fixtures = ["initial_products"]

    LABEL = "Read on O'Reilly Safari (free trial)"

    OREILLY_PKS = (100, 101, 102, 103)
    SELF_PUBLISHED_PKS = (104, 105, EBOOK_PK)

    def safari_url(self, pk):
        links = dict(Product.objects.get(pk=pk).get_alt_links())
        return links.get(self.LABEL)

    def test_the_oreilly_titles_keep_the_safari_link(self):
        for pk in self.OREILLY_PKS:
            with self.subTest(pk=pk):
                self.assertEqual(
                    self.safari_url(pk), Product.OREILLY_SAFARI_URL)

    def test_the_self_published_book_never_advertises_safari(self):
        for pk in self.SELF_PUBLISHED_PKS:
            with self.subTest(pk=pk):
                self.assertIsNone(self.safari_url(pk))

    def test_an_isbn_alone_does_not_produce_the_link(self):
        # The actual regression, now covering both print editions: each has a
        # real ISBN of its own and must still be Safari-free. Asserting on the
        # ISBN as well as the link keeps this from passing for the accidental
        # reason that the row has no ISBN to infer from -- which is exactly
        # what pk 105 used to be.
        for pk in (104, 105):
            with self.subTest(pk=pk):
                product = Product.objects.get(pk=pk)

                self.assertTrue(product.isbn)
                self.assertFalse(product.on_oreilly_safari)
                self.assertIsNone(self.safari_url(pk))

    def test_the_link_follows_the_flag_and_not_the_isbn(self):
        # Guards the guard, in both directions: an O'Reilly book stripped of
        # its ISBN keeps the link, and a self-published one flagged by hand
        # gains it. If get_alt_links() ever went back to reading `isbn` this
        # is what would fail.
        # Queryset updates rather than save(): fixture rows carry no
        # external_product_id, so Product.save() would call Stripe to mint
        # one.
        Product.objects.filter(pk=100).update(isbn="")
        self.assertEqual(self.safari_url(100), Product.OREILLY_SAFARI_URL)

        Product.objects.filter(pk=104).update(on_oreilly_safari=True)
        self.assertEqual(self.safari_url(104), Product.OREILLY_SAFARI_URL)

    def test_a_product_page_for_the_new_book_does_not_offer_safari(self):
        # Through the view, not just the model: this is what a customer sees.
        for pk in self.SELF_PUBLISHED_PKS:
            with self.subTest(pk=pk):
                response = self.client.get(f"/product/{pk}")
                self.assertNotContains(response, "O'Reilly Safari")
                self.assertNotContains(response, Product.OREILLY_SAFARI_URL)

    def test_a_product_page_for_an_oreilly_book_still_offers_safari(self):
        response = self.client.get("/product/100")

        self.assertContains(response, Product.OREILLY_SAFARI_URL)

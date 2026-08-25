"""Extra product images, and the command that grabs them off disk.

A product's primary image stays on Product; these are the additional ones
Google shows beside it in a listing. The failure modes worth pinning are not
"no image appears" -- that is visible -- but the two that are silent: an
image that duplicates the primary one (Google rejects the whole offer), and
an image that belongs to a different product (a misrepresented offer that
looks fine).
"""

import re
import tempfile
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree

from django.core.management import call_command
from django.test import TestCase, override_settings

from main.management.commands.grab_book_images import candidates_for
from main.models import Product, ProductImage


G = "{http://base.google.com/ns/1.0}"

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def make_product(**fields):
    with mock.patch("main.models.Payments") as payments:
        payments.create_product.return_value = "prod_img"
        defaults = {
            "name": "A Book",
            "description": "Plain prose.",
            "price": 1999,
            "cat": Product.Categories.BOOKS,
            "external_product_id": "prod_img",
            "image_name": "book_covers/a_book.jpg",
        }
        defaults.update(fields)
        return Product.objects.create(**defaults)


class AdditionalImageUrlsTest(TestCase):
    def test_extra_images_come_back_in_position_order(self):
        product = make_product()
        ProductImage.objects.create(
            product=product, image_name="book_covers/a_book_spread.jpg",
            position=2)
        ProductImage.objects.create(
            product=product, image_name="book_covers/a_book_back.jpg",
            position=1)

        urls = product.get_additional_image_urls()

        self.assertEqual(
            [Path(url).name for url in urls],
            ["a_book_back.jpg", "a_book_spread.jpg"])

    def test_the_primary_image_is_never_repeated(self):
        # Google rejects an offer whose additional_image_link repeats its
        # image_link, and attaching the product's own cover as an extra is
        # the first thing somebody filling in the admin would try.
        product = make_product()
        ProductImage.objects.create(
            product=product, image_name="book_covers/a_book.jpg")

        self.assertEqual(product.get_additional_image_urls(), [])

    def test_a_duplicate_extra_appears_once(self):
        product = make_product()
        for position in (1, 2):
            ProductImage.objects.create(
                product=product, image_name="book_covers/a_book_back.jpg",
                position=position)

        self.assertEqual(len(product.get_additional_image_urls()), 1)

    def test_a_row_that_resolves_to_nothing_is_dropped(self):
        # An empty <g:additional_image_link> is a malformed URL to Google,
        # which rejects the item rather than ignoring the field.
        product = make_product()
        ProductImage.objects.create(product=product, image_name="")

        self.assertEqual(product.get_additional_image_urls(), [])

    def test_no_more_than_googles_limit_are_offered(self):
        product = make_product()
        for index in range(15):
            ProductImage.objects.create(
                product=product,
                image_name=f"book_covers/a_book_{index}.jpg",
                position=index)

        self.assertEqual(
            len(product.get_additional_image_urls()),
            Product.MAX_ADDITIONAL_IMAGES)

    def test_one_products_extras_do_not_leak_onto_another(self):
        first = make_product()
        second = make_product(image_name="book_covers/other.jpg")
        ProductImage.objects.create(
            product=first, image_name="book_covers/a_book_back.jpg")

        self.assertEqual(second.get_additional_image_urls(), [])


@override_settings(THUMBNAIL_DEBUG=False)
class FeedAdditionalImageTest(TestCase):
    def feed_items(self):
        response = self.client.get("/google_products.xml")
        self.assertEqual(response.status_code, 200)
        return ElementTree.fromstring(response.content).findall(
            "./channel/item")

    def test_extra_images_reach_the_feed_as_absolute_urls(self):
        product = make_product()
        ProductImage.objects.create(
            product=product, image_name="book_covers/a_book_back.jpg")

        links = [element.text.strip() for element
                 in self.feed_items()[0].findall(f"{G}additional_image_link")]

        self.assertEqual(len(links), 1)
        # Relative would be rejected: Google fetches these itself, with no
        # page to resolve them against.
        self.assertTrue(links[0].startswith("https://www.pigscanfly.ca/"))
        self.assertIn("a_book_back.jpg", links[0])

    def test_a_product_with_no_extras_emits_no_such_field(self):
        make_product()

        self.assertEqual(
            self.feed_items()[0].findall(f"{G}additional_image_link"), [])


class GrabBookImagesTest(TestCase):
    """The command's two refusals, which are the whole reason it is not a loop.

    Both failures it guards against are silent: a Git LFS pointer has a real
    image name and a plausible size, and another edition's cover is a valid
    readable image of the wrong book.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.covers = self.root / "assets" / "images" / "book_covers"
        self.covers.mkdir(parents=True)

    def write_image(self, name):
        from PIL import Image

        path = self.covers / name
        Image.new("RGB", (10, 10)).save(path)
        return path

    def write_lfs_pointer(self, name):
        path = self.covers / name
        path.write_text(
            "version https://git-lfs.github.com/spec/v1\n"
            "oid sha256:" + "0" * 64 + "\nsize 1999869\n")
        return path

    def run_command(self, **kwargs):
        with override_settings(STATIC_ROOT=str(self.root)):
            call_command("grab_book_images", **kwargs)

    def test_it_attaches_a_sibling_image(self):
        product = make_product()
        self.write_image("a_book.jpg")
        self.write_image("a_book_back.jpg")

        self.run_command()

        self.assertEqual(
            [row.image_name for row in product.extra_images.all()],
            ["book_covers/a_book_back.jpg"])

    def test_it_refuses_another_products_cover(self):
        # high_performance_spark_2ed.jpg extends high_performance_spark's
        # stem while being the next edition's cover. Attaching it would put
        # the 2nd edition's artwork on the 1st edition's listing.
        first = make_product(image_name="book_covers/hps.jpg")
        make_product(image_name="book_covers/hps_2ed.jpg", name="HPS 2ed")
        self.write_image("hps.jpg")
        self.write_image("hps_2ed.jpg")

        self.run_command()

        self.assertEqual(list(first.extra_images.all()), [])

    def test_it_refuses_an_unmaterialised_lfs_pointer(self):
        # A pointer stub reaching the feed is a broken image URL sent to
        # Google, which is worse than sending none.
        product = make_product()
        self.write_image("a_book.jpg")
        self.write_lfs_pointer("a_book_back.jpg")

        self.run_command()

        self.assertEqual(list(product.extra_images.all()), [])

    def test_it_is_idempotent(self):
        product = make_product()
        self.write_image("a_book.jpg")
        self.write_image("a_book_back.jpg")

        self.run_command()
        self.run_command()

        self.assertEqual(product.extra_images.count(), 1)

    def test_a_dry_run_writes_nothing(self):
        product = make_product()
        self.write_image("a_book.jpg")
        self.write_image("a_book_back.jpg")

        self.run_command(dry_run=True)

        self.assertEqual(product.extra_images.count(), 0)

    def test_it_never_attaches_the_primary_image(self):
        product = make_product()
        self.write_image("a_book.jpg")

        self.run_command()

        self.assertEqual(list(product.extra_images.all()), [])

    def test_a_hand_added_image_survives_a_run(self):
        product = make_product()
        ProductImage.objects.create(
            product=product, image_name="book_covers/photographed_by_hand.jpg")
        self.write_image("a_book.jpg")

        self.run_command()

        self.assertEqual(product.extra_images.count(), 1)

    def test_an_absent_asset_tree_is_not_an_error(self):
        # CI and a fresh checkout both run without the sibling assets tree,
        # and failing there would fail the build over an enrichment.
        make_product()

        with override_settings(STATIC_ROOT=str(self.root / "nowhere")):
            call_command("grab_book_images")

    def test_seeded_alt_text_does_not_call_an_interior_page_a_cover(self):
        # The command cannot know whether a matched file is a back cover or
        # an interior illustration, so the seeded description must not claim
        # either. It used to say "<name> cover", which was a false statement
        # to a screen reader for every extra picture that is not one.
        product = make_product()
        self.write_image("a_book.jpg")
        self.write_image("a_book_back.jpg")

        self.run_command()

        row = product.extra_images.get()
        self.assertNotIn("cover", row.alt_text)
        self.assertIn(product.name, row.alt_text)

    def test_a_maximum_length_product_name_still_attaches(self):
        # Product.name allows 250 characters and the alt-text prefix adds
        # more, but alt_text is itself capped at 250. SQLite (these tests)
        # would silently store the over-long value; PostgreSQL rejects it,
        # and the exception would abort the run with every later product's
        # images left unattached. So pin the bound directly, and pin that a
        # product processed after the long-named one still gets its image.
        name_limit = Product._meta.get_field("name").max_length
        alt_limit = ProductImage._meta.get_field("alt_text").max_length
        long_named = make_product(
            name="A" * name_limit, image_name="book_covers/a_book.jpg")
        later = make_product(
            name="Later Book", image_name="book_covers/z_book.jpg")
        self.write_image("a_book.jpg")
        self.write_image("a_book_back.jpg")
        self.write_image("z_book.jpg")
        self.write_image("z_book_back.jpg")

        self.run_command()

        row = long_named.extra_images.get()
        self.assertLessEqual(len(row.alt_text), alt_limit)
        self.assertEqual(later.extra_images.count(), 1)

    def test_the_claimed_set_is_what_excludes_a_sibling(self):
        # Directly, so the exclusion cannot quietly become "nothing matched".
        self.write_image("hps.jpg")
        self.write_image("hps_2ed.jpg")
        root = self.root / "assets" / "images"

        without = candidates_for("book_covers/hps.jpg", root)
        with_claim = candidates_for(
            "book_covers/hps.jpg", root, {"book_covers/hps_2ed.jpg"})

        self.assertEqual([path.name for path in without], ["hps_2ed.jpg"])
        self.assertEqual(with_claim, [])


class StartServerGrabBookImagesTest(TestCase):
    """The wiring: the extra pictures only reach the feed if startup attaches
    them.

    Same argument as StartServerBookAssetCheckTest: the command is only
    useful if something actually runs it, and before this wiring a fresh
    database served a feed with no <g:additional_image_link> on any offer
    until somebody remembered a manual command.
    """

    def setUp(self):
        with open(REPO_ROOT / "scripts" / "start-server.sh") as fh:
            self.script = fh.read()

    def test_the_primary_attaches_extra_images(self):
        primary_block = re.search(
            r'if \[ -n "\$\{PRIMARY:-\}" \];.*?\nfi\n', self.script, re.S)

        self.assertIsNotNone(primary_block, "the PRIMARY block moved")
        self.assertIn("grab_book_images", primary_block.group(0))

    def test_it_runs_after_the_catalogue_is_seeded(self):
        # The command walks Product rows, so running it before seed_products
        # on a fresh database would attach nothing and report success.
        self.assertLess(
            self.script.index("seed_products"),
            self.script.index("grab_book_images"))

    def test_the_attach_cannot_take_the_pod_down(self):
        """`set -e` plus a bare call would turn a bad picture into an outage."""
        call = re.search(r"\./manage\.py grab_book_images.*?\n(.*\n)?",
                         self.script)

        self.assertIsNotNone(call)
        self.assertIn("||", call.group(0))

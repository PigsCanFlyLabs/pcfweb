"""Tests for the "Distributed Computing 4 Kids (and Executives)" SKUs.

The three catalogue entries as the fixture defines them, plus the build
guard and packaging rules that get the e-book archive into the image
without letting it leak onto the web."""

import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from django.test import TestCase

from main import digital
from main.models import Product
from main.tests.base import EBOOK_PK, EBOOK_STEM, REPO_ROOT


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

    def test_all_three_skus_are_present(self):
        for pk, price in ((104, 3000), (105, 4200), (106, 1500)):
            with self.subTest(pk=pk):
                product = Product.objects.get(pk=pk)
                self.assertTrue(product.name.startswith(self.TITLE))
                self.assertEqual(product.price, price)
                self.assertEqual(product.cat, Product.Categories.BOOKS)
                self.assertEqual(
                    product.image_name, "distributed_computing_4_kids.jpg")

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

    def test_the_isbns_are_left_unset_rather_than_placeheld(self):
        # A shared placeholder would emit three Merchant-feed products with
        # one id, and would advertise signed copies -- of a PDF, for 106.
        for pk in (104, 105, EBOOK_PK):
            with self.subTest(pk=pk):
                product = Product.objects.get(pk=pk)
                self.assertFalse(product.isbn)
                self.assertIsNone(product.get_gtin())
                self.assertNotIn("signed on request",
                                 product.get_display_text())

    def test_the_new_skus_do_not_collide_in_the_merchant_feed(self):
        response = self.client.get("/google_products.xml")

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        ids = re.findall(r"<g:id>(\d+)</g:id>", body)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("104", ids)
        # No gtin, so each falls back to a distinct mpn.
        mpns = re.findall(r"<g:mpn>(PCF\d+)</g:mpn>", body)
        self.assertEqual(sorted(mpns), ["PCF104", "PCF105", "PCF106"])

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
        self.assertIn("garden gnomes", standard.description)
        self.assertIn("Written and illustrated by Holden Karau",
                      standard.description)

        executive = Product.objects.get(pk=105)
        self.assertIn("a number you can expense", executive.description)
        self.assertIn("enterprise support contracts", executive.description)

        ebook = Product.objects.get(pk=EBOOK_PK)
        self.assertIn("garden gnomes", ebook.description)
        self.assertIn("DRM-free ZIP", ebook.description)
        self.assertIn("EPUB", ebook.description)

    def test_every_sku_has_its_own_product_page(self):
        for pk in (104, 105, EBOOK_PK):
            with self.subTest(pk=pk):
                response = self.client.get(f"/product/{pk}")
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Distributed Computing 4 Kids")

    def test_the_ebook_page_says_the_price_is_a_suggestion(self):
        response = self.client.get(f"/product/{EBOOK_PK}")

        self.assertContains(response, "Pay what you want")
        self.assertContains(response, "15.00")

    def test_no_retailer_links_are_claimed_yet(self):
        # Bookshop links are ISBN-derived and the site lists none.
        for pk in (104, 105, EBOOK_PK):
            with self.subTest(pk=pk):
                self.assertEqual(
                    Product.objects.get(pk=pk).get_alt_links(), [])

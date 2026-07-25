"""The Google Merchant product feed and the copy it shares with the site.

Google rejects an item outright on a malformed field, and the failure shows
up days later in Merchant Center rather than in any request, so the shapes
worth pinning are the ones a template change could quietly break.
"""

from unittest import mock
from xml.etree import ElementTree

from django.test import TestCase, override_settings

from main.models import Product


G = "{http://base.google.com/ns/1.0}"


@override_settings(THUMBNAIL_DEBUG=False)
class ProductFeedTest(TestCase):
    def make_product(self, **fields):
        with mock.patch("main.models.Payments") as payments:
            payments.create_product.return_value = "prod_feed"
            defaults = {
                "name": "A Book",
                "description": "Plain prose.",
                "price": 3999,
                "cat": Product.Categories.BOOKS,
                "external_product_id": "prod_feed",
            }
            defaults.update(fields)
            return Product.objects.create(**defaults)

    def feed_items(self):
        response = self.client.get("/google_products.xml")
        self.assertEqual(response.status_code, 200)
        # Parsing rather than substring-matching: a feed that is not
        # well-formed XML is rejected wholesale, so this asserts that too.
        root = ElementTree.fromstring(response.content)
        return root.findall("./channel/item")

    def test_preorder_price_is_a_bare_number(self):
        # get_display_price() renders "Pre-order: 39.99" for the product page.
        # Sending that as <g:price> makes Google reject the item.
        self.make_product(preorder_only=True)

        price = self.feed_items()[0].find(f"{G}price").text

        self.assertEqual(price.strip(), "39.99 USD")

    def test_price_is_a_bare_number_for_ordinary_products(self):
        self.make_product()

        price = self.feed_items()[0].find(f"{G}price").text

        self.assertEqual(price.strip(), "39.99 USD")

    def test_the_description_is_plain_text_not_escaped_markup(self):
        # get_display_text() returns markup for the HTML page; in XML that
        # arrives at Google as literal &lt;p&gt; in the listing copy.
        self.make_product(print_isbn="9781449358624")

        description = self.feed_items()[0].find(f"{G}description").text

        self.assertIn("Plain prose.", description)
        self.assertNotIn("<p>", description)
        self.assertNotIn("&lt;", description)
        self.assertIn("signed on request", description)

    def test_a_product_with_no_image_omits_the_image_link(self):
        # get_image_url() returns None, which used to render the string
        # "None" straight onto the end of the site URL.
        self.make_product(image_name="")

        item = self.feed_items()[0]

        self.assertIsNone(item.find(f"{G}image_link"))

    def test_a_product_with_an_image_still_has_an_absolute_image_link(self):
        self.make_product(image_name="learning_spark_1ed.jpg")

        link = self.feed_items()[0].find(f"{G}image_link").text

        self.assertTrue(link.strip().startswith("https://www.pigscanfly.ca/"))
        self.assertNotIn("None", link)


@override_settings(THUMBNAIL_DEBUG=False)
class ProductCopyEscapingTest(TestCase):
    """get_display_text() is rendered without an autoescape override, so it
    has to do its own escaping.

    THUMBNAIL_DEBUG is overridden for the same reason as everywhere else that
    renders a page: the image assets are gitignored and absent in CI.
    """

    def make_product(self, description, **fields):
        with mock.patch("main.models.Payments") as payments:
            payments.create_product.return_value = "prod_esc"
            return Product.objects.create(
                name="A Book", description=description, price=1000,
                cat=Product.Categories.BOOKS,
                external_product_id="prod_esc", **fields)

    def test_the_description_is_escaped(self):
        product = self.make_product("Angle < bracket & ampersand")

        rendered = str(product.get_display_text())

        self.assertNotIn("Angle < bracket", rendered)
        self.assertIn("&lt;", rendered)
        self.assertIn("&amp;", rendered)

    def test_markup_in_admin_copy_does_not_become_live_html(self):
        product = self.make_product("<script>alert(1)</script>")

        rendered = str(product.get_display_text())

        self.assertNotIn("<script>", rendered)

    def test_the_signed_note_is_still_real_markup_for_print_books(self):
        product = self.make_product("Prose.", print_isbn="9781449358624")

        rendered = str(product.get_display_text())

        self.assertIn("<p>", rendered)
        self.assertIn("signed on request", rendered)

    def test_ebook_isbn_does_not_offer_a_signed_copy(self):
        print_product = self.make_product("Print.", print_isbn="9781449358624")
        ebook_product = self.make_product("PDF.", ebook_isbn="9781449358624")

        print_rendered = str(print_product.get_display_text())
        ebook_rendered = str(ebook_product.get_display_text())

        # Anti-vacuity: this assertion would fail if the signed note stopped
        # being emitted for the print control case.
        self.assertIn("signed on request", print_rendered)
        self.assertIn("PDF.", ebook_rendered)
        self.assertNotIn("signed on request", ebook_rendered)

    def test_feed_signed_note_is_keyed_to_print_isbn(self):
        print_product = self.make_product("Print.", print_isbn="9781449358624")
        ebook_product = self.make_product("PDF.", ebook_isbn="9781449358624")

        print_description = print_product.get_feed_description()
        ebook_description = ebook_product.get_feed_description()

        # Anti-vacuity: the plain-text note is still present for print books.
        self.assertIn(Product.SIGNED_ON_REQUEST_NOTE, print_description)
        self.assertIn("PDF.", ebook_description)
        self.assertNotIn(Product.SIGNED_ON_REQUEST_NOTE, ebook_description)

    def test_gtin_prefers_offer_format_identifier(self):
        print_product = Product(
            print_isbn="9781449358624",
            ebook_isbn="9781449358625",
            upc="123456789012",
        )
        ebook_product = Product(ebook_isbn="9781449358625", upc="123456789012")
        upc_product = Product(upc="123456789012")

        self.assertEqual(print_product.get_gtin(), "9781449358624")
        self.assertEqual(ebook_product.get_gtin(), "9781449358625")
        self.assertEqual(upc_product.get_gtin(), "123456789012")

    def test_the_product_page_renders_it_without_an_autoescape_override(self):
        product = self.make_product("Angle < bracket")

        response = self.client.get(f"/product/{product.pk}")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Angle < bracket", response.content)
        self.assertIn(b"&lt;", response.content)

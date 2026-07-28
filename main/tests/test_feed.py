"""The Google Merchant product feed and the copy it shares with the site.

Google rejects an item outright on a malformed field, and the failure shows
up days later in Merchant Center rather than in any request, so the shapes
worth pinning are the ones a template change could quietly break.
"""

import html as html_module
import re
from unittest import mock
from xml.etree import ElementTree

from django.test import TestCase, override_settings

from main.models import Product


G = "{http://base.google.com/ns/1.0}"
EBOOK_DELIVERY_NOTE = (
    "Delivered by email as a DRM-free ZIP containing both the EPUB and the "
    "PDF."
)


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

    def test_a_digital_product_advertises_no_shipping(self):
        """A download is emailed, so the feed must not offer to post it.

        The feed excludes SERVICES and noorder rows, so a DIGITAL product --
        the pay-what-you-want e-book this release ships -- is included like
        any other book. The shipping blocks used to sit unguarded in the item
        loop, which told Google there was an SF local delivery, US and CA
        postage prices and a 1-21 day transit window for a file sent by email.
        """
        self.make_product(
            name="An E-book",
            delivery_type=Product.DeliveryTypes.DIGITAL,
            sells_ebook=True,
            digital_asset_name="an_ebook",
        )

        item = self.feed_items()[0]

        self.assertEqual(item.findall(f"{G}shipping"), [])
        # The handling times live only inside those blocks, so they go too --
        # a handling time is how long before the thing is *posted*.
        self.assertEqual(item.findall(f".//{G}min_handling_time"), [])
        self.assertEqual(item.findall(f".//{G}max_handling_time"), [])
        # Anti-vacuity: the item really is in the feed and really is the
        # digital one, rather than the feed being empty or filtered.
        self.assertEqual(item.find(f"{G}title").text.strip(), "An E-book")

    def test_a_physical_product_still_advertises_shipping(self):
        """The control for the test above: physical goods keep every block."""
        self.make_product(
            name="A Physical Book",
            delivery_type=Product.DeliveryTypes.PHYSICAL,
        )

        item = self.feed_items()[0]
        services = [s.find(f"{G}service").text.strip()
                    for s in item.findall(f"{G}shipping")]

        self.assertEqual(services, [
            "SF Local Delivery",
            "US Economy Shipping",
            "Faster US Shipping",
            "CA Economy Shipping",
        ])
        # Handling times come back with them.
        self.assertEqual(
            [t.text.strip() for t in item.findall(f".//{G}min_handling_time")],
            ["3"] * 4)

    def test_shipping_tracks_delivery_type_not_the_books_category(self):
        """Both SKUs are cat=BOOKS; only the fulfilment method differs.

        Guards against a fix that keyed the shipping blocks off the category
        instead. The DC4K print and e-book editions are both books, and the
        print one has to keep its shipping.
        """
        self.make_product(
            name="Print Edition",
            delivery_type=Product.DeliveryTypes.PHYSICAL)
        self.make_product(
            name="E-book Edition",
            external_product_id="prod_feed_ebook",
            delivery_type=Product.DeliveryTypes.DIGITAL,
            sells_ebook=True,
            digital_asset_name="an_ebook")

        by_title = {
            item.find(f"{G}title").text.strip(): item
            for item in self.feed_items()
        }

        self.assertEqual(len(by_title), 2)
        self.assertEqual(
            len(by_title["Print Edition"].findall(f"{G}shipping")), 4)
        self.assertEqual(by_title["E-book Edition"].findall(f"{G}shipping"), [])


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

    def description_html(self, response):
        html = response.content.decode()
        match = re.search(
            r'<div class="product-description">(.*?)</div>', html, re.DOTALL)
        self.assertIsNotNone(match)
        assert match is not None  # for mypy
        return match.group(1)

    def paragraph_texts(self, response):
        return [
            html_module.unescape(text.strip())
            for text in re.findall(
                r"<p>(.*?)</p>", self.description_html(response), re.DOTALL)
        ]

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

    def test_blank_lines_become_distinct_paragraphs(self):
        product = self.make_product("First paragraph.\n\nSecond paragraph.")

        rendered = str(product.get_display_text())

        self.assertIn("<p>First paragraph.</p>", rendered)
        self.assertIn("<p>Second paragraph.</p>", rendered)

    def test_a_single_paragraph_product_page_stays_a_single_description_block(self):
        product = self.make_product("Plain prose.", print_isbn="9781449358624")

        response = self.client.get(f"/product/{product.pk}")

        self.assertEqual(
            self.paragraph_texts(response),
            ["Plain prose.", Product.SIGNED_ON_REQUEST_NOTE])

    def test_product_page_paragraphization_still_escapes_apostrophes_and_tags(self):
        product = self.make_product(
            "O'Hara <tag>\n\nSecond <b>paragraph</b>.")

        response = self.client.get(f"/product/{product.pk}")
        html = self.description_html(response)

        self.assertIn("O&#x27;Hara &lt;tag&gt;", html)
        self.assertIn("Second &lt;b&gt;paragraph&lt;/b&gt;.", html)
        self.assertNotIn("<tag>", html)
        self.assertNotIn("<b>paragraph</b>", html)

    def test_ebook_isbn_does_not_offer_a_signed_copy(self):
        print_product = self.make_product("Print.", print_isbn="9781449358624")
        ebook_product = self.make_product(
            "PDF.",
            ebook_isbn="9781449358624",
            delivery_type=Product.DeliveryTypes.DIGITAL,
            sells_ebook=True,
            digital_asset_name="pdf_book",
        )

        print_rendered = str(print_product.get_display_text())
        ebook_rendered = str(ebook_product.get_display_text())

        # Anti-vacuity: this assertion would fail if the signed note stopped
        # being emitted for the print control case.
        self.assertIn("signed on request", print_rendered)
        self.assertIn("PDF.", ebook_rendered)
        self.assertNotIn("signed on request", ebook_rendered)
        self.assertIn(EBOOK_DELIVERY_NOTE, ebook_rendered)

    def test_a_physical_product_with_no_print_isbn_does_not_claim_digital_delivery(self):
        product = self.make_product(
            "Widget.",
            delivery_type=Product.DeliveryTypes.PHYSICAL,
        )

        rendered = str(product.get_display_text())
        feed_description = product.get_feed_description()

        self.assertIn("Widget.", rendered)
        self.assertIn("Widget.", feed_description)
        self.assertNotIn(EBOOK_DELIVERY_NOTE, rendered)
        self.assertNotIn(EBOOK_DELIVERY_NOTE, feed_description)

    def test_feed_signed_note_is_keyed_to_print_isbn(self):
        print_product = self.make_product("Print.", print_isbn="9781449358624")
        ebook_product = self.make_product(
            "PDF.",
            ebook_isbn="9781449358624",
            delivery_type=Product.DeliveryTypes.DIGITAL,
            sells_ebook=True,
            digital_asset_name="pdf_book",
        )

        print_description = print_product.get_feed_description()
        ebook_description = ebook_product.get_feed_description()

        # Anti-vacuity: the plain-text note is still present for print books.
        self.assertIn(Product.SIGNED_ON_REQUEST_NOTE, print_description)
        self.assertIn("PDF.", ebook_description)
        self.assertNotIn(Product.SIGNED_ON_REQUEST_NOTE, ebook_description)
        self.assertIn(EBOOK_DELIVERY_NOTE, ebook_description)

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

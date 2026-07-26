from django.test import SimpleTestCase

from main.models import Product


class ProductIdentifierTest(SimpleTestCase):
    def test_default_asin_help_text_warns_that_kindle_does_not_use_it(self):
        help_text = Product._meta.get_field("default_asin").help_text

        self.assertIn("Print/catalogue ASIN", help_text)
        self.assertIn("not used for Kindle", help_text)

    def test_explicit_amazon_links_take_precedence_over_asins(self):
        product = Product(
            amazon_link="https://example.com/print",
            amazon_in_link="https://example.in/print",
            kindle_link="https://example.com/kindle",
            print_asin="PRINTASIN",
            ebook_asin="EBOOKASIN",
            default_asin="DEFAULTASIN",
        )

        self.assertEqual(product.get_amazon_link(), "https://example.com/print")
        self.assertEqual(product.get_amazon_in_link(), "https://example.in/print")
        self.assertEqual(product.get_kindle_link(), "https://example.com/kindle")

    def test_asin_resolution_uses_format_specific_value_before_default(self):
        product = Product(
            print_asin="PRINTASIN",
            ebook_asin="EBOOKASIN",
            default_asin="DEFAULTASIN",
        )

        self.assertEqual(
            product.get_amazon_link(),
            "https://www.amazon.com/dp/PRINTASIN",
        )
        self.assertEqual(
            product.get_amazon_in_link(),
            "https://www.amazon.in/dp/PRINTASIN",
        )
        self.assertEqual(
            product.get_kindle_link(),
            "https://www.amazon.com/dp/EBOOKASIN",
        )

    def test_default_asin_fills_missing_print_asins_only(self):
        product = Product(default_asin="DEFAULTASIN")

        self.assertEqual(
            product.get_amazon_link(),
            "https://www.amazon.com/dp/DEFAULTASIN",
        )
        self.assertEqual(
            product.get_amazon_in_link(),
            "https://www.amazon.in/dp/DEFAULTASIN",
        )
        self.assertIsNone(product.get_kindle_link())

    def test_default_asin_alone_does_not_create_a_kindle_alt_link(self):
        print_product = Product(default_asin="DEFAULTASIN")
        ebook_product = Product(ebook_asin="EBOOKASIN")

        print_links = dict(print_product.get_alt_links())
        ebook_links = dict(ebook_product.get_alt_links())

        # Anti-vacuity: a real e-book ASIN still creates the Kindle link.
        self.assertEqual(
            ebook_links["Buy on Kindle (e-book)"],
            "https://www.amazon.com/dp/EBOOKASIN",
        )
        self.assertNotIn("Buy on Kindle (e-book)", print_links)

    def test_alt_links_use_derived_amazon_links(self):
        product = Product(print_asin="PRINTASIN", ebook_asin="EBOOKASIN")

        links = dict(product.get_alt_links(country="IN"))

        self.assertEqual(
            links["Buy on Amazon (print)"],
            "https://www.amazon.com/dp/PRINTASIN",
        )
        self.assertEqual(
            links["Buy on Amazon.in (print)"],
            "https://www.amazon.in/dp/PRINTASIN",
        )
        self.assertEqual(
            links["Buy on Kindle (e-book)"],
            "https://www.amazon.com/dp/EBOOKASIN",
        )

    def test_safari_alt_link_is_keyed_to_the_publisher_not_the_isbn(self):
        """Renamed from test_safari_alt_link_is_keyed_to_print_isbn.

        This branch keyed the Safari trial link to print_isbn, having inherited
        an older rule that keyed it to `isbn`. The DC4K branch replaced that
        inference outright with an explicit on_oreilly_safari flag, because the
        inference is wrong: DC4K is self-published, carries a real print ISBN,
        and is not on O'Reilly's platform, so an ISBN-keyed rule offers a
        Safari trial for a book that is not there. The two rules cannot both
        hold; the flag is the correct one and OReillySafariLinkTest in
        test_dc4k.py pins it.

        The distinction this test existed to defend -- a print title offers the
        Safari link and an e-book-only product does not -- is preserved, now
        stated against the publisher flag.
        """
        oreilly_print = Product(print_isbn="9781449358624",
                                on_oreilly_safari=True)
        ebook_product = Product(ebook_isbn="9781449358625",
                                on_oreilly_safari=False)
        # An ISBN alone must no longer be enough to conjure the link.
        self_published_print = Product(print_isbn="9781960595997",
                                       on_oreilly_safari=False)

        oreilly_labels = [label for label, _ in oreilly_print.get_alt_links()]
        ebook_labels = [label for label, _ in ebook_product.get_alt_links()]
        self_pub_labels = [label for label, _
                           in self_published_print.get_alt_links()]

        # Anti-vacuity: the Safari link still appears for the print control.
        self.assertIn("Read on O'Reilly Safari (free trial)", oreilly_labels)
        self.assertNotIn("Read on O'Reilly Safari (free trial)", ebook_labels)
        self.assertNotIn(
            "Read on O'Reilly Safari (free trial)", self_pub_labels)

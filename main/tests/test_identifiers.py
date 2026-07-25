from django.test import SimpleTestCase

from main.models import Product


class ProductIdentifierTest(SimpleTestCase):
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

    def test_default_asin_fills_missing_format_specific_asins(self):
        product = Product(default_asin="DEFAULTASIN")

        self.assertEqual(
            product.get_amazon_link(),
            "https://www.amazon.com/dp/DEFAULTASIN",
        )
        self.assertEqual(
            product.get_amazon_in_link(),
            "https://www.amazon.in/dp/DEFAULTASIN",
        )
        self.assertEqual(
            product.get_kindle_link(),
            "https://www.amazon.com/dp/DEFAULTASIN",
        )

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

    def test_safari_alt_link_is_keyed_to_print_isbn(self):
        print_product = Product(print_isbn="9781449358624")
        ebook_product = Product(ebook_isbn="9781449358625")

        print_labels = [label for label, _ in print_product.get_alt_links()]
        ebook_labels = [label for label, _ in ebook_product.get_alt_links()]

        # Anti-vacuity: the Safari link still appears for the print control.
        self.assertIn("Read on O'Reilly Safari (free trial)", print_labels)
        self.assertNotIn("Read on O'Reilly Safari (free trial)", ebook_labels)

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

    def test_safari_alt_link_is_keyed_to_the_oreilly_flag_not_an_isbn(self):
        """Safari link tracks the publisher, via the flag -- never an ISBN.

        This replaces an earlier `test_safari_alt_link_is_keyed_to_print_isbn`,
        which asserted the opposite rule. Keying off print_isbn was safe only
        while every print book in the catalogue was an O'Reilly title. The
        self-published DC4K print SKUs have real print ISBNs and are *not* on
        the platform, so the ISBN rule would advertise an O'Reilly free trial
        for a book that is not there -- a false claim to a customer.

        The flag is also the fail-safe direction: an unflagged new title loses
        a link, where the ISBN rule invents one.
        """
        safari_label = "Read on O'Reilly Safari (free trial)"

        flagged = Product(print_isbn="9781449358624", on_oreilly_safari=True)
        # The case the ISBN rule got wrong: a real print ISBN, not on Safari.
        self_published_print = Product(
            print_isbn="9781960595997", on_oreilly_safari=False)
        ebook_product = Product(
            ebook_isbn="9781960595980", on_oreilly_safari=False)

        def labels(product):
            return [label for label, _ in product.get_alt_links()]

        # Anti-vacuity: the Safari link still appears for the flagged control.
        self.assertIn(safari_label, labels(flagged))
        self.assertNotIn(safari_label, labels(self_published_print))
        self.assertNotIn(safari_label, labels(ebook_product))

    def test_safari_link_ignores_print_isbn_when_the_flag_is_unset(self):
        """The flag alone decides, so print_isbn cannot resurrect the link."""
        safari_label = "Read on O'Reilly Safari (free trial)"

        with_isbn = Product(print_isbn="9781960595997", on_oreilly_safari=False)
        without_isbn = Product(print_isbn=None, on_oreilly_safari=False)

        for product in (with_isbn, without_isbn):
            labels = [label for label, _ in product.get_alt_links()]
            self.assertNotIn(safari_label, labels)

        # And conversely: no print ISBN at all still gets the link when the
        # flag says the title is on the platform.
        flagged_without_isbn = Product(print_isbn=None, on_oreilly_safari=True)
        self.assertIn(
            safari_label,
            [label for label, _ in flagged_without_isbn.get_alt_links()])

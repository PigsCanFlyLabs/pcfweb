from django.test import SimpleTestCase, TestCase

from main.models import Product
from main.tests.base import EBOOK_PK


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

        # Anti-vacuity: a real e-book ASIN still creates the e-book link.
        self.assertEqual(
            ebook_links["Buy on Amazon (ebook)"],
            "https://www.amazon.com/dp/EBOOKASIN",
        )
        self.assertNotIn("Buy on Amazon (ebook)", print_links)

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
            links["Buy on Amazon (ebook)"],
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


class AmazonEbookLinkTest(TestCase):
    """The "Buy on Amazon (ebook)" button, end to end.

    Unlike the Safari link directly above it, this one is *not* gated on
    on_oreilly_safari. Safari needs a flag because its URL is a single
    affiliate constant shared by every title, so nothing in the row itself
    says whether the claim is true. An Amazon e-book link is per-title data:
    the e-book ASIN *is* the statement "this book is on Amazon at this
    address". Gating a correct, owner-entered ASIN behind a second publisher
    flag would silently swallow a true link, which is a worse failure than the
    one the flag exists to prevent.

    So the rule is presence, not publisher: no ebook_asin (and no explicit
    kindle_link), no button. Only the O'Reilly titles are expected to carry
    one, and the fixture gives none of the self-published SKUs a value -- see
    test_no_self_published_sku_offers_the_amazon_ebook_link.
    """

    fixtures = ["initial_products"]

    LABEL = "Buy on Amazon (ebook)"

    OREILLY_PK = 100
    SELF_PUBLISHED_PKS = (104, 105, EBOOK_PK)

    def ebook_url(self, pk):
        links = dict(Product.objects.get(pk=pk).get_alt_links())
        return links.get(self.LABEL)

    def set_ebook_asin(self, pk, asin):
        # Queryset update rather than save(): fixture rows carry no
        # external_product_id, so Product.save() would call Stripe to mint one.
        Product.objects.filter(pk=pk).update(ebook_asin=asin)

    def test_an_oreilly_book_with_an_asin_renders_the_link(self):
        self.set_ebook_asin(self.OREILLY_PK, "B0EBOOK100")

        response = self.client.get(f"/product/{self.OREILLY_PK}")

        self.assertContains(response, self.LABEL)
        self.assertContains(response, "https://www.amazon.com/dp/B0EBOOK100")

    def test_an_explicit_kindle_link_wins_over_the_derived_one(self):
        # The curated-URL escape hatch every other retailer link has: a
        # stored URL beats anything derived from an identifier.
        Product.objects.filter(pk=self.OREILLY_PK).update(
            kindle_link="https://www.amazon.com/dp/CURATED",
            ebook_asin="B0EBOOK100")

        self.assertEqual(
            self.ebook_url(self.OREILLY_PK),
            "https://www.amazon.com/dp/CURATED")

    def test_a_book_without_an_asin_renders_no_link_at_all(self):
        # The fixture ships no e-book ASINs, so pk 100 is the "not supplied
        # yet" case as-is. The point is that the absent value produces no
        # button rather than a button with a dead or empty href.
        response = self.client.get(f"/product/{self.OREILLY_PK}")

        self.assertIsNone(self.ebook_url(self.OREILLY_PK))
        self.assertNotContains(response, self.LABEL)
        self.assertNotContains(response, 'href=""')
        # Anti-vacuity: the page did render its other retailer buttons, so the
        # assertions above are about this link and not about an empty page.
        self.assertContains(response, "Buy on Amazon (print)")

    def test_no_self_published_sku_offers_the_amazon_ebook_link(self):
        # DC4K is the owner's own book: the print SKUs are not on Amazon as
        # e-books and the digital SKU is sold here directly, pay-what-you-want.
        # None of them carry an ebook_asin, so none of them get the button.
        for pk in self.SELF_PUBLISHED_PKS:
            with self.subTest(pk=pk):
                response = self.client.get(f"/product/{pk}")

                self.assertIsNone(self.ebook_url(pk))
                self.assertNotContains(response, self.LABEL)

    def test_the_ebook_isbn_alone_never_conjures_the_link(self):
        # pk 106 has a real ebook_isbn. An ISBN is not an ASIN and there is no
        # public mapping between them, so deriving an Amazon URL from one would
        # be a fabricated link pointing at whatever product happens to sit at
        # that address. Presence of the ISBN must change nothing.
        ebook = Product.objects.get(pk=EBOOK_PK)

        self.assertTrue(ebook.ebook_isbn)
        self.assertFalse(ebook.ebook_asin)
        self.assertIsNone(ebook.get_kindle_link())
        self.assertIsNone(self.ebook_url(EBOOK_PK))

    def test_a_print_asin_does_not_produce_an_ebook_link(self):
        # Guards the other direction of the same mistake: a print/catalogue
        # identifier must not be spent on an e-book link, or the button would
        # sell the paperback.
        self.assertIsNone(self.ebook_url(self.OREILLY_PK))

        Product.objects.filter(pk=self.OREILLY_PK).update(
            print_asin="PRINTASIN", default_asin="DEFAULTASIN")

        self.assertIsNone(self.ebook_url(self.OREILLY_PK))

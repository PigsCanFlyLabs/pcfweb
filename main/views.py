import json
import logging
import re
from datetime import timedelta
from urllib.parse import quote, urlparse

from typing import *
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import (
    FileResponse, Http404, HttpResponse, HttpResponseBadRequest,
    JsonResponse)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import NoReverseMatch, reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt

import stripe

from main import captcha, mailing
from main.digital import (
    BadSignature, DigitalAssetError, SignatureExpired, link_lifetime_days,
    open_asset, parse_download_token)
from main.forms import (
    MailingListImportForm, MailingListSendForm, MailingListSignupForm,
    PurchaseFeedbackForm)
from main.models import (
    PWYW_ROUND_DOWN_BELOW, Cart, CartProduct, EmailIdentity,
    MailingListMessage, Order, Product, PurchaseFeedback, PwywAmountError,
    SuppressedAddress, normalize_email_identity, parse_pwyw_amount,
    send_batch_size)
from main.payments import Payments
from main.socials import LIBERATED_BREAD_URL, follow_targets
from main.utils import (
    generate_username, get_country_code, get_storable_client_ip)
from pigscanfly.hostnames import ascii_lowercase

logger = logging.getLogger(__name__)


class CartQuantityOverflow(ValueError):
    """Combining valid cart quantities would exceed the storage column."""


def over_cache_limit(key: str, limit: int) -> bool:
    """Count one hit against an hourly bucket and say whether it is over.

    The rate-limiting primitive behind every open endpoint here (the mailing
    list signup, the post-checkout feedback form). Deliberately crude: the
    cache is per worker process, so the real ceiling is this times the worker
    count. That bounds a flood without needing a shared cache the site does
    not otherwise run.
    """
    try:
        count = cache.get_or_set(key, 0, 3600)
        # incr is atomic where the backend supports it; a missing key means
        # it expired between the two calls, which is a reset, not an error.
        count = cache.incr(key) if count is not None else 1
    except ValueError:
        cache.set(key, 1, 3600)
        count = 1
    return count > limit


# /healthz is served by main.middleware.HealthCheckMiddleware rather than a
# view here, so it can answer ahead of the HTTPS redirect, the ALLOWED_HOSTS
# check and the cookie-consent middleware's database query.


# The homepage features this book by title rather than by primary key.
# Fixture pks are seeded data: stable today, but a reseed or an admin edit can
# move them, and a hardcoded pk fails *silently* -- it keeps resolving, just to
# whatever row happens to hold that number later. Matching on the title the
# fixture gives all three SKUs means the worst case is a card that falls back
# to the books listing, which is visible rather than wrong.
FEATURED_BOOK_TITLE = "Distributed Computing 4 Kids (and Executives)"


def _integrity_error_text(error: IntegrityError) -> str:
    parts = [str(error)]
    cause = getattr(error, "__cause__", None)
    if cause is not None:
        parts.append(str(cause))
    return " ".join(parts)


def _constraint_name(error: IntegrityError) -> Optional[str]:
    cause = getattr(error, "__cause__", None)
    diag = getattr(cause, "diag", None)
    return getattr(diag, "constraint_name", None)


def _is_email_identity_conflict(error: IntegrityError) -> bool:
    if _constraint_name(error) == "unique_normalized_email":
        return True
    return "unique_normalized_email" in _integrity_error_text(error)


def _is_username_conflict(error: IntegrityError) -> bool:
    if _constraint_name(error) == "auth_user_username_key":
        return True
    text = _integrity_error_text(error)
    return ("auth_user.username" in text
            or "auth_user_username_key" in text)


def _create_signup_user(email: str, password: str) -> User:
    for _ in range(5):
        username = generate_username(email)
        try:
            with transaction.atomic():
                return User.objects.create_user(
                    email=email, username=username, password=password)
        except IntegrityError as error:
            if _is_username_conflict(error):
                continue
            raise
    raise IntegrityError("Could not allocate a unique username.")


# Create your views here.
class HomeView(View):
    def featured_book(self):
        """The standard print edition of the featured book, or None.

        Ordered by pk so the three SKUs resolve to the standard print edition
        rather than the Executive or e-book one; None when the catalogue has
        not been seeded, which the template handles.
        """
        return (Product.objects
                .filter(name__startswith=FEATURED_BOOK_TITLE,
                        cat=Product.Categories.BOOKS)
                .exclude(noorder=True)
                .order_by('pk')
                .first())

    def get(self, request):
        # collapse_format_groups() BEFORE the [:3] slice, not after. Sliced
        # first, the three DC4K SKUs fill all three carousel slots and
        # collapse to a single card -- so the homepage would show one book
        # where it means to show three, and the slice would be silently
        # doing the opposite of its job.
        #
        # The cost of that ordering is that the whole category is fetched to
        # render three cards, where the old code issued a LIMIT 3. Accepted
        # knowingly: the collapse cannot stop early and stay correct, because
        # a row anywhere later in the queryset can replace which member
        # represents one of the first three groups (format_order decides, and
        # release-date order does not deliver members together). The
        # catalogue is nine rows; if it ever grows to where this matters, the
        # fix belongs in collapse_format_groups(), not in a slice here.
        def carousel(cat):
            return (Product.objects.filter(cat=cat).exclude(noorder=True)
                    .order_by_release_date().collapse_format_groups()[:3])

        highlights = map(
            lambda cat: ((cat, cat.label), carousel(cat)),
            Product.Categories)
        # Only show categories with elements in them.
        highlights = list(filter(lambda x: len(x[1]) != 0, highlights))
        return render(
            request, 'index.html',
            context={
                'title': 'Pigs Can Fly Labs',
                'highlights': highlights,
                'featured_book': self.featured_book(),
                'liberated_bread_url': LIBERATED_BREAD_URL,
            })


class AboutView(View):
    def get(self, request):
        return render(request, 'about.html', context={'title': 'About Us'})



class FamilyView(View):
    def get(self, request):
        # The family is a mix of things, not all of them separate companies:
        # one is a sibling company with its own site, another is the same
        # company as Pigs Can Fly Labs but with its own site. The `kind` line
        # says which each one is so the page does not flatten that distinction.
        projects = [
            {
                "name": "Pigs Can Fly Labs",
                "kind": (
                    "The company behind this site and all of Holden's books!"
                ),
                "description": (
                    "The company behind this site — "
                    "value-priced queer and nerdy stuff."
                ),
                "url": None,
                "coming_soon": False,
            },
            {
                "name": "Fight Health Insurance",
                "kind": "Separate company and project",
                "description": (
                    "A separate company and project that helps people "
                    "appeal health insurance denials."
                ),
                "url": "https://www.fighthealthinsurance.com/",
                "coming_soon": False,
            },
            {
                "name": "Liberated Bread",
                "kind": "Same company, its own site",
                "description": (
                    "The same company as Pigs Can Fly Labs, with its own "
                    "site — not a separate company. Coming soon."
                ),
                "url": LIBERATED_BREAD_URL,
                "coming_soon": True,
            },
        ]
        return render(request, "family.html", context={
            "title": "Our Family of Projects",
            "projects": projects,
        })
class PrivacyView(View):
    def get(self, request):
        return render(request, 'privacy.html', context={'title': 'Privacy Policy'})

class TosView(View):
    def get(self, request):
        return render(request, 'tos.html', context={'title': 'TOS'})

class ReturnView(View):
    def get(self, request):
        return render(request, 'return.html', context={'title': 'TOS'})

class ContactView(View):
    def get(self, request):
        return render(request, 'contact.html', context={'title': 'Contact Us'})


class DiscordJoinView(View):
    """The captcha-gated door to the Discord invite.

    A Discord invite URL is a bearer token: whatever reads it can join, and
    every link-following crawler on the internet reads pages like this one. So
    the URL is never in the page as a URL. It lives as two halves (see
    Base.DISCORD_INVITE_PART_ONE/TWO), the halves are only put in a response
    once a captcha has been answered, and the joining happens in the visitor's
    browser.

    None of that is a wall -- a determined scraper runs JavaScript. It is a
    speed bump sized to the actual threat, and if the invite starts filling up
    with bots anyway the fallback is already here: unset the halves and this
    page becomes "e-mail us and we'll invite you", which is also what a
    misconfigured pair renders.
    """

    # The joined halves must look like this or the page will not offer them.
    # Two jobs: it catches a bad ConfigMap edit (a half dropped, an invite
    # pasted with a trailing newline) before it becomes a dead button, and it
    # keeps anything that is not an https discord.gg link -- a javascript:
    # URL, an attacker-chosen host -- from being handed to the browser to open.
    INVITE_PATTERN = re.compile(r"^https://discord\.gg/[A-Za-z0-9-]{2,64}$")

    # The name is bait for form-filling bots and deliberately looks worth
    # filling in; real visitors never see the field. See the template.
    HONEYPOT_FIELD = "email_confirm"

    def invite_parts(self, request) -> Optional[Tuple[str, str]]:
        """The two halves, or None when they don't make a usable invite."""
        first = settings.DISCORD_INVITE_PART_ONE  # type: ignore[misc]
        second = settings.DISCORD_INVITE_PART_TWO  # type: ignore[misc]
        if not first or not second:
            # Both halves have to be set, even though one of them could hold
            # a complete-looking URL on its own: an empty half is how a
            # dropped ConfigMap key looks, and there is no way to tell that
            # from a deliberate one-sided split. Fail to the e-mail page.
            logger.warning(
                "Only one half of the Discord invite is set; serving the "
                "e-mail fallback. Set both DISCORD_INVITE_PART_ONE and "
                "DISCORD_INVITE_PART_TWO.")
            return None
        if not self.INVITE_PATTERN.match(first + second):
            logger.warning(
                "The Discord invite halves do not join into a "
                "https://discord.gg/... link; serving the e-mail fallback.")
            return None
        return first, second

    def page(self, request, *, solved: bool, error: Optional[str] = None):
        parts = self.invite_parts(request)
        context = {
            "title": "Join our Discord",
            "support_email": settings.DISCORD_SUPPORT_EMAIL,  # type: ignore[misc]
            "invite_configured": parts is not None,
            "error": error,
        }
        if parts is None:
            # Nothing to gate, so don't make anyone answer a question first.
            return render(request, "discord.html", context=context)
        if solved:
            context["invite_part_one"], context["invite_part_two"] = parts
        else:
            context["challenge"] = captcha.new_challenge(request.session)
        context["solved"] = solved
        return render(request, "discord.html", context=context)

    def get(self, request):
        return self.page(request, solved=False)

    def post(self, request):
        # A filled honeypot is a bot; answer exactly as a wrong answer does so
        # it learns nothing about which field gave it away.
        if request.POST.get(self.HONEYPOT_FIELD, "").strip():
            return self.page(
                request, solved=False,
                error="That didn't match — here's a new question.")
        if not captcha.check_answer(request.session,
                                    request.POST.get("captcha_answer", "")):
            return self.page(
                request, solved=False,
                error=("That didn't match (or the question timed out) — "
                       "here's a new one."))
        return self.page(request, solved=True)


class ProductsView(View):
    def get(self, request, category=None):
        if "category" not in request.GET and category is None:
            return render(request, 'products.html', context={
                'title': 'Products',
                'type': 'producs',
                'products': (Product.objects.exclude(noorder=True)
                             .order_by_release_date()
                             .collapse_format_groups())
            })
        else:
            cat = category or request.GET["category"]
            try:
                cat_name = Product.Categories(cat).label
            except ValueError:
                # Categories are stored upper-cased ("B", "OE"); be forgiving
                # about the case in the URL, but 404 on anything else rather
                # than letting the ValueError become a 500.
                try:
                    cat = cat.upper()
                    cat_name = Product.Categories(cat).label
                except ValueError:
                    raise Http404(f"No such product category: {cat}")
            extra_style = None
            bg_img_name = f"assets/images/{cat_name}.jpg".lower()
            if finders.find(f"{bg_img_name}"):
                extra_style = f"background-image: url('/static/{bg_img_name}');"
            return render(request, 'products.html', context={
                'title': f'Products - {cat_name}',
                'type': cat_name,
                'products': (Product.objects.filter(cat=cat)
                             .exclude(noorder=True)
                             .order_by_release_date()
                             .collapse_format_groups()),
                'extra_style': extra_style
            })


class ServicesView(View):
    """A curated page, not a product listing.

    This used to render products.html from
    ``Product.objects.filter(cat=SERVICES).exclude(noorder=True)``. With the
    FMT2 network services retired there is nothing left in that queryset, and
    the things the owner does want listed are a poor fit for Product rows:
    saving one auto-creates a Stripe product, wants a tax code, puts the row
    in the Google Merchant feed and runs it past the stock gate -- all wrong
    for "email us about consulting".

    ``noorder=True`` cannot express "listed but not buyable" here either,
    because the old queryset explicitly *excluded* noorder rows, so anything
    marked that way vanished from the page instead of appearing unbuyable.

    So this follows FamilyView: a hand-written list of dicts rendered by a
    template. Two of these -- Liberated Bread and Fight Health Insurance --
    also appear on /family, which is deliberate. /family says who they are;
    this page says what they offer, so the copy is different on purpose.
    """

    # Where consulting enquiries go. The owner asked for email with the
    # project described, rather than the generic contact form.
    CONSULTING_EMAIL = "holden@pigscanfly.ca"

    # Stated once for the consulting group rather than repeated on each card:
    # both engagements are the same shape, and the same sentence twice reads
    # like boilerplate.
    CONSULTING_SCOPE = (
        "Both consulting engagements cover architecture review, performance "
        "tuning, training, and a retainer for periodic consulting."
    )

    # Titles cited in the credentials, with the ISBN each one links by. Linked
    # through /book/<isbn> so no fixture primary key ever appears in markup --
    # a pk that moves keeps resolving, to the wrong book.
    #
    # Where a label names more than one edition, it links the NEWEST one it
    # names: that is the edition in print and the one a reader following the
    # link is best served by. test_cited_editions_land_on_the_edition_they_name
    # enforces that, and it is a semantic check -- the older
    # test_each_cited_book_link_reaches_a_page_naming_that_book strips the
    # bracket off the label before comparing, so it cannot see a label and a
    # destination that disagree about which edition they mean.
    LEARNING_SPARK = ("Learning Spark (1st edition)", "9781449358624")
    # Cites both editions and links the 2nd (pk 108, ISBN 9781098145859). It
    # linked the 1st until pk 101 was renamed to "High Performance Spark
    # (1st edition)", at which point the label said "1st and 2nd editions"
    # and the page it reached said it was only the first. The sentence is the
    # owner's approved copy and does not change; only the target moves.
    HIGH_PERFORMANCE_SPARK = (
        "High Performance Spark (1st and 2nd editions)", "9781098145859")
    FAST_DATA_PROCESSING = ("Fast Data Processing with Spark", "9781782167068")
    KUBEFLOW = ("Kubeflow for Machine Learning", "9781492050124")
    SCALING_PYTHON_RAY = ("Scaling Python with Ray", "9781098118808")

    @staticmethod
    def credential(*parts):
        """Build a credential line as linkable and plain segments.

        A string is literal copy; a (title, isbn) pair becomes a link to
        /book/<isbn>. Keeping the sentence as segments rather than as a blob
        of HTML means a test can reassemble the plain text and assert the
        owner's exact wording, which is the point -- these are publishing
        claims and the page must not drift away from what he approved.
        """
        segments = []
        for part in parts:
            if isinstance(part, str):
                segments.append({"text": part, "isbn": None})
            else:
                segments.append({"text": part[0], "isbn": part[1]})
        return segments

    def consulting_mailto(self, subject: str) -> str:
        return f"mailto:{self.CONSULTING_EMAIL}?subject={quote(subject)}"

    def services(self):
        return [
            {
                "name": "Liberated Bread",
                "kind": "Same company, its own site",
                "description": (
                    "The bread side of Pigs Can Fly Labs, with a site of its "
                    "own. There is nothing to order through this site — it "
                    "is still coming together."
                ),
                "url": LIBERATED_BREAD_URL,
                "cta_label": "Visit Liberated Bread",
                "cta_url": LIBERATED_BREAD_URL,
                "credentials": None,
                "note": None,
                "consulting": False,
                "coming_soon": True,
            },
            {
                "name": "Apache Spark Consulting",
                "kind": "Consulting",
                "description": (
                    "Help with Apache Spark: getting jobs to run, getting "
                    "them to run faster, and working out which of those two "
                    "problems you actually have."
                ),
                # The owner has confirmed he wrote both Fast Data Processing
                # with Spark (Packt, 2013) and Learning Spark 1e (O'Reilly,
                # 2015), and that the 2013 book is the first book written
                # about Apache Spark. So this asserts the claim rather than
                # hedging to "one of the first", which is what it said while
                # the question was open.
                #
                # Fast Data Processing comes last at the owner's request -- he
                # rates it the weakest of his books -- but the first-book fact
                # is the strongest line on the page, so it stays.
                "credentials": self.credential(
                    "From the co-author of ",
                    self.LEARNING_SPARK,
                    " and ",
                    self.HIGH_PERFORMANCE_SPARK,
                    ", and author of ",
                    self.FAST_DATA_PROCESSING,
                    " — the first book written about Apache Spark.",
                ),
                "url": None,
                "cta_label": (
                    f"Email {self.CONSULTING_EMAIL} with the project you'd "
                    "like help on."
                ),
                "cta_url": self.consulting_mailto("Apache Spark consulting"),
                "note": None,
                "consulting": True,
                "coming_soon": False,
            },
            {
                "name": "AI Consulting",
                "kind": "Consulting",
                "description": (
                    "Help with machine learning and AI systems — training, "
                    "serving, and the plumbing between them."
                ),
                # Two sentences, not one: eliding "co-author of" across the
                # clause ("and of the Spark books...") is ungrammatical, and
                # the owner flagged exactly that in an earlier draft.
                "credentials": self.credential(
                    "From the co-author of ",
                    self.KUBEFLOW,
                    " and ",
                    self.SCALING_PYTHON_RAY,
                    ". Much of today's ML tooling still runs on Spark, and "
                    "those books are ours too.",
                ),
                "url": None,
                "cta_label": (
                    f"Email {self.CONSULTING_EMAIL} with the project you'd "
                    "like help on."
                ),
                "cta_url": self.consulting_mailto("AI consulting"),
                "note": None,
                "consulting": True,
                "coming_soon": False,
            },
            {
                "name": "Fight Health Insurance",
                "kind": "Separate company",
                "description": (
                    "If your health insurance has denied a claim, Fight "
                    "Health Insurance helps you put an appeal together."
                ),
                "url": "https://www.fighthealthinsurance.com/",
                "cta_label": "Go to Fight Health Insurance",
                "cta_url": "https://www.fighthealthinsurance.com/",
                "credentials": None,
                # Stated outright rather than left to be inferred: this is
                # not a Pigs Can Fly Labs service and should not read as one.
                "note": (
                    "A separate company that Holden is involved in, not a "
                    "Pigs Can Fly Labs service."
                ),
                "consulting": False,
                "coming_soon": False,
            },
        ]

    def get(self, request):
        return render(request, 'services.html', context={
            'title': 'Services',
            'consulting_scope': self.CONSULTING_SCOPE,
            'services': self.services()})


class SubscribeView(View):
    def get(self, request):
        return render(request, 'subscribe_page.html', context={
            'title': 'Subscribe for updates',
            'areas': mailing.interest_choices(request),
            'forced_interest': mailing.ALL_SLUG,
            'signup_action': reverse('mailing-list-subscribe-all'),
        })


class BookByIsbnView(View):
    """/book/<isbn> -> 302 to the canonical /product/<pk>.

    A redirect rather than a second rendering of the product page: one
    canonical URL keeps the ISBN path from competing with /product/<pk> in
    search results, and makes the ISBN a stable alias that survives a pk
    changing -- which is the whole reason this exists. Templates link books by
    ISBN so no fixture primary key is ever hardcoded in markup.
    """

    # Matched in order. print_isbn first because a print ISBN is the one on
    # the back of the book and the one people paste; the legacy `isbn` column
    # is last because it is the one being migrated away from.
    ISBN_FIELDS = ("print_isbn", "ebook_isbn", "isbn")

    # An ISBN-13 is 13 digits and an ISBN-10 is 10; the column is 20. This is
    # generous room for separators on top of that, not a validity check --
    # anything that is not a real ISBN 404s on the lookup anyway. The point is
    # only that there is no reason to normalise and then run three queries
    # against a megabyte of URL.
    MAX_ISBN_LENGTH = 32

    @staticmethod
    def normalise(raw: str) -> str:
        """Strip the separators people paste ISBNs with.

        `978-1-960595-99-7` and `978 1 960595 99 7` are the same book as
        `9781960595997`. The stored values are bare digits, so anything
        keeping a hyphen would simply never match.
        """
        return re.sub(r"[\s\-‐-―]", "", raw).upper()

    def get(self, request, isbn):
        # Bounded before normalising, so an over-long URL is rejected without
        # running a regex substitution over it either.
        if len(isbn) > self.MAX_ISBN_LENGTH:
            raise Http404("ISBN too long")

        normalised = self.normalise(isbn)
        if not normalised:
            raise Http404("No ISBN given")

        for field in self.ISBN_FIELDS:
            product = Product.objects.filter(**{field: normalised}).first()
            if product is not None:
                # Permanent in meaning but issued as a 302: the mapping from
                # ISBN to pk is data, and a 301 would be cached by browsers
                # past our ability to correct it if a row were ever re-keyed.
                return redirect("product", pk=product.pk)

        # A deliberate 404 rather than a redirect to /products: an unknown
        # ISBN is a wrong URL, and bouncing it to the catalogue would tell
        # both the visitor and a crawler that the book exists here.
        raise Http404(f"No book with ISBN {normalised}")


class ProductView(View):
    def get(self, request, pk):
        product = get_object_or_404(Product, pk=pk)
        return render(request, 'single-product.html', context={
            'title': product.name,
            'product': product,
            'alt_links': product.get_alt_links(country=get_country_code(request)),
            # The round-down threshold reaches the page's JavaScript from the
            # one constant that defines it, rather than being written out a
            # second time where it could drift from the server's rule.
            'pwyw_round_down_below': PWYW_ROUND_DOWN_BELOW,
        })

class BaseCartView():
    """Common base cart view."""

    # What a PositiveBigIntegerField can physically hold. Python ints are
    # arbitrary precision, so every path which combines cart quantities must
    # enforce the database column's upper bound before doing the addition.
    # This is a storage capacity guard, not a purchase limit -- whether there
    # should be a product-level cap on quantity is a separate, still-open
    # decision.
    MAX_QUANTITY = 9223372036854775807

    @staticmethod
    def _merge_notice(names) -> str:
        listed = ", ".join(names)
        return (
            f"Your saved basket already held {listed}, so it has been "
            "combined with the basket you were using: the quantity is the "
            "total of both, and the amount is the one you chose most "
            "recently. Please check it before paying.")

    @classmethod
    def quantity_sum(cls, existing_quantity: int, added_quantity: int) -> int:
        """Add two valid quantities without overflowing the database field."""
        # Write this as subtraction rather than adding first: it remains safe
        # even if the values eventually come from a fixed-width integer type.
        if existing_quantity > cls.MAX_QUANTITY - added_quantity:
            raise CartQuantityOverflow(
                f"Combined quantity must be at most {cls.MAX_QUANTITY}.")
        return existing_quantity + added_quantity

    def get_cart(self, request) -> Cart:
        """Return the cart belonging to *this* requester.

        Every cart lookup in this app goes through here, so this is also the
        ownership boundary: an anonymous requester only ever gets the cart
        their session points at, and a logged-in requester only ever gets the
        cart attached to their user row.
        """
        user_cart = None
        session_cart = None
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            user_cart, _ = Cart.objects.get_or_create(user=user)
        # An anonymous cart can still be attached to the session even for a
        # logged-in user -- they filled it before logging in -- so always look,
        # and merge below if both exist.
        cart_id = request.session.get("cart_id")
        if cart_id is not None:
            # user__isnull keeps a stale/forged cart_id from ever resolving to
            # some user's persistent cart.
            session_cart = Cart.objects.filter(
                cart_id=cart_id, user__isnull=True).first()
            if session_cart is None:
                # Stale cookie pointing at a cart that no longer exists;
                # drop it instead of 500ing on every cart page from now on.
                del request.session["cart_id"]

        if user_cart is None:
            if session_cart is None:
                session_cart = Cart.objects.create()
                request.session["cart_id"] = session_cart.cart_id
            return session_cart
        if session_cart is None:
            return user_cart
        # Ok we have two carts time to merge them.
        self._merge_cart(request, session_cart, user_cart)
        return user_cart

    @staticmethod
    def _reconcile_pwyw_amount(existing, incoming) -> bool:
        """Decide which chosen amount survives a merge, and say if it moved.

        `existing` is the row already in the user's saved cart. `incoming` is
        the row from the anonymous session cart -- the one the buyer has been
        looking at, and the one they last named an amount on.

        The session row always wins, because its amount is the number the
        buyer was last shown. Keeping the saved row's amount instead means
        billing them something they never saw, which is the same surprise at
        the till that the round-down display exists to prevent.

        The five cases, all of which fall out of one comparison:

          only the session row has an amount   -> that amount, and say so
          only the saved row has an amount     -> cleared, so the suggestion
                                                  stands, and say so; the
                                                  session cart was showing
                                                  the suggestion, not the
                                                  saved amount
          both, and they differ                -> the session one, and say so
          both, and they agree                 -> unchanged, nothing to say
          neither                              -> unchanged, nothing to say

        Returns True when the surviving amount is not the one the saved row
        held, i.e. when the buyer needs telling before anything is billed.
        """
        if not existing.product.is_pwyw:
            return False
        if existing.chosen_amount == incoming.chosen_amount:
            # Covers both-None: nothing was named on either side, the
            # suggestion still stands, and there is nothing to report.
            return False
        existing.chosen_amount = incoming.chosen_amount
        # The hold, recorded on the row whose price just moved. The caller
        # saves this row either way, so it is persisted with the new amount
        # in the same statement.
        existing.pwyw_amount_merged = True
        # A Stripe Price is immutable and this row's was minted for the old
        # amount, so it has to go. refresh_pwyw_price() would re-mint at
        # checkout regardless; clearing it here keeps price_id and
        # effective_unit_amount() consistent in the meantime.
        existing.price_id = None
        return True

    def _merge_cart(self, request, session_cart: Cart, user_cart: Cart) -> None:
        """Fold an anonymous session cart into the logged-in user's cart.

        Rows for a product the user cart already holds have their quantity
        added on and are dropped; the rest are reparented. Either way we never
        end up with two rows for the same (cart, product).

        Quantities are still summed, including when the two rows carried
        different pay-what-you-want amounts. Two rows at different amounts are
        arguably not the same line at all, but this schema cannot hold two
        rows for one (cart, product) -- there is a unique constraint -- so
        some rule has to combine them, and dropping the saved row's quantity
        would throw away something the buyer put there on purpose. Summing is
        the rule this merge has always used and it is about count, not price.
        What changes is that the result is no longer billed unseen: any row
        whose amount moved is recorded below, and CheckoutView refuses to go
        to Stripe until the buyer has been shown the combined cart.

        The whole thing is one transaction: adding a quantity onto the
        surviving row and deleting the row it came from are two statements,
        and a crash between them would lose the quantity for good. Ordinary
        failures still roll the whole merge back so the next request can retry
        it; a deterministic quantity overflow is handled in-line by capping the
        surviving row at MAX_QUANTITY, dropping the duplicate session row and
        warning the shopper.
        """
        repriced = []
        with transaction.atomic():
            # Rows are linked to a cart both by FK and by the M2M; take the
            # union so a row that only made it into one still gets merged.
            session_products = CartProduct.objects.filter(
                Q(cart=session_cart) | Q(cart_products=session_cart)).distinct()
            for cart_product in session_products:
                session_cart.products.remove(cart_product)
                existing = CartProduct.objects.filter(
                    cart=user_cart, product=cart_product.product).first()
                if existing is not None:
                    if self._reconcile_pwyw_amount(existing, cart_product):
                        # For the log line only. The hold itself is on the
                        # row, set by _reconcile_pwyw_amount and saved
                        # below with the amount that caused it.
                        repriced.append(existing.product.name)
                    try:
                        existing.quantity = self.quantity_sum(
                            existing.quantity, cart_product.quantity)
                    except CartQuantityOverflow:
                        existing.quantity = self.MAX_QUANTITY
                        existing.save()
                        messages.warning(
                            request,
                            f"We capped {existing.product.name} at "
                            f"{self.MAX_QUANTITY} because the combined cart "
                            "quantity would not fit in storage.")
                        user_cart.products.add(existing)
                        cart_product.delete()
                        continue
                    existing.save()
                    user_cart.products.add(existing)
                    cart_product.delete()
                else:
                    cart_product.cart = user_cart
                    cart_product.save()
                    user_cart.products.add(cart_product)
            session_cart.delete()
        # The session lives outside the transaction, so drop the pointer only
        # once the merge has actually committed. If the block above rolled
        # back, the session still points at an intact cart.
        del request.session["cart_id"]
        if repriced:
            logger.info(
                "Merge repriced %s on cart %s; holding checkout until the "
                "cart has been shown.", ", ".join(repriced), user_cart.pk)

class SignupView(View):
    def get(self, request):
        in_use = request.GET.get('in_use', 'false')
        invalid = request.GET.get('invalid')
        return render(request, 'signup.html', context={
            'title': 'Sign Up', 'in_use': in_use, 'invalid': invalid})

    def post(self, request):
        # Both are required. Missing values used to reach generate_username()
        # as None and 500 on the AttributeError, and a missing password
        # reached set_password(None), which silently creates an account with
        # an unusable password that can never be logged into.
        email = normalize_email_identity(request.POST.get('email') or '')
        password = request.POST.get('password') or ''
        if not email or not password:
            return redirect(reverse('signup') + '?invalid=missing')
        try:
            validate_email(email)
        except ValidationError:
            return redirect(reverse('signup') + '?invalid=email')

        # email is not unique on auth.User, so this can legitimately match
        # more than one row; either way the address is taken.
        if User.objects.filter(email__iexact=email).exists():
            return redirect(reverse('signup') + '?in_use=true')

        # Reserve the address first. The functional unique constraint is
        # the arbiter when concurrent requests both pass the advisory
        # auth.User check above; the losing transaction creates no User.
        with transaction.atomic():
            try:
                identity = EmailIdentity.objects.create(
                    normalized_email=email)
            except IntegrityError as error:
                if _is_email_identity_conflict(error):
                    return redirect(reverse('signup') + '?in_use=true')
                raise
            user = _create_signup_user(email, password)
            identity.user = user
            identity.save(update_fields=["user"])

        # No cart is created here: get_cart() owns that, and creating one
        # unconditionally on a OneToOneField is an unprotected insert.

        login(request, user)
        return redirect('home')


class GoogleProductFeed(View):
    def get(self, request):
        everything_but_services = (Product.objects.exclude(
            cat=Product.Categories.SERVICES)
            .exclude(noorder=True))
        return render(request, "google_products.xml", context={'products': everything_but_services}, content_type="text/xml")



@method_decorator(never_cache, name="dispatch")
class CartView(View, BaseCartView):
    def get(self, request):
        cart = self.get_cart(request)
        # This view is the only thing that may clear the merge hold, because
        # it is the only thing that shows the buyer the merged basket. Read it
        # off the cart here; it is cleared at the bottom, once the page has
        # actually rendered into a body they will receive.
        repriced = cart.pwyw_merge_notice_names()
        if repriced:
            messages.warning(request, self._merge_notice(repriced))
        cart_products = cart.products.select_related("product")
        # A noorder line contributes nothing to the total. Public add-to-cart
        # refuses these and checkout re-checks, so one is only here because it
        # was added before the flag was set -- but it still cannot be bought,
        # and billing for it in the displayed total would be a number the
        # customer is never charged. pk 107 happens to be priced 0, so this
        # matters for the general case: an admin flagging an existing priced
        # product noorder would otherwise leave its price silently in the sum.
        total_price = sum(cp.total_price() for cp in cart_products
                          if not cp.product.noorder)
        total_display_price = "{0:.2f}".format(total_price / 100)
        has_physical = any(cp.product.is_physical_good() for cp in cart_products
                           if not cp.product.noorder)
        # The total is now the sum of the amounts that will actually be
        # billed, because a pay-what-you-want line contributes the amount the
        # buyer named rather than the owner's suggestion. The notice stays:
        # it is what tells a buyer the row is theirs to change.
        has_pwyw = any(cp.product.is_pwyw for cp in cart_products)
        has_unavailable = any(cp.product.noorder for cp in cart_products)
        response = render(request, 'cart.html', context={
            'title': 'Cart',
            'products': cart_products,
            'total_price': total_display_price,
            'has_physical': has_physical,
            'has_pwyw': has_pwyw,
            'has_unavailable': has_unavailable,
        })
        # Cleared here and nowhere else, and only for a request that is
        # actually being sent the basket:
        #
        #   HEAD  -- Django dispatches it to this same get(), and the body is
        #            stripped before it reaches the client. A bodiless
        #            response has by definition shown the buyer nothing, so it
        #            must not count as having seen anything.
        #   non-200 -- likewise nothing to look at.
        #   an exception above -- render() is eager, so it raises here and
        #            never reaches this line; the hold stands.
        #
        # That is also the answer to whether a /cart GET that redirects away
        # should clear it: no, and this shape means it cannot, rather than
        # relying on nobody adding such a branch later.
        if (repriced and request.method == "GET"
                and response.status_code == 200):
            cart.clear_pwyw_merge_notice()
        return response


class AddToCartView(View, BaseCartView):
    # POST only: a GET here is triggerable cross-site by an <img> tag or a
    # link prefetch, with no CSRF token involved.
    def post(self, request, product_id: int, quantity: int):
        # The quantity is a real form field, so the buy form works with no
        # JavaScript at all; the URL segment stays as the fallback for
        # existing links.
        posted_quantity = request.POST.get('quantity')
        if posted_quantity is not None:
            try:
                quantity = int(posted_quantity)
            except ValueError:
                return HttpResponseBadRequest("Quantity must be a number.")
        # Both paths converge here, so neither can disagree about the bounds.
        if quantity < 1:
            return HttpResponseBadRequest("Quantity must be at least 1.")
        if quantity > self.MAX_QUANTITY:
            return HttpResponseBadRequest(
                f"Quantity must be at most {self.MAX_QUANTITY}.")
        product = get_object_or_404(Product, pk=product_id)
        if not product.is_purchasable():
            return HttpResponseBadRequest("Product is not purchasable.")
        cart = self.get_cart(request)
        # The amount field only exists on a pay-what-you-want product's form,
        # and is only honoured for one: posting it at anything else changes
        # nothing, rather than repricing the catalogue.
        chosen_amount = None
        if product.is_pwyw:
            posted_amount = request.POST.get('chosen_amount')
            if posted_amount is not None:
                try:
                    chosen_amount = parse_pwyw_amount(posted_amount)
                except PwywAmountError as error:
                    # Same shape as the add-to-cart refusals this replaces:
                    # say why on the cart and add nothing. Base renders
                    # queued messages everywhere now, but the cart redirect is
                    # still deliberate: it returns the buyer to the checkout
                    # surface instead of posting feedback to a product GET.
                    messages.error(request, str(error))
                    return redirect('cart')

        cart_product, created = CartProduct.objects.get_or_create(
            cart=cart, product=product,
            defaults={'quantity': quantity, 'chosen_amount': chosen_amount})
        if not created:
            if chosen_amount is not None:
                # Adding the same pay-what-you-want product again with a new
                # amount means the new amount, not the first one: the buyer
                # has just said what they want to pay, on the only form that
                # asks. Dropping price_id makes the save below mint a Price
                # for it, because a Stripe Price cannot be repriced.
                cart_product.chosen_amount = chosen_amount
                cart_product.price_id = None
            # Adding a product that's already in the cart adds to what's
            # there rather than silently discarding the new quantity.
            try:
                cart_product.quantity = self.quantity_sum(
                    cart_product.quantity, quantity)
            except CartQuantityOverflow as error:
                return HttpResponseBadRequest(str(error))
            cart_product.save()

        cart.products.add(cart_product)
        return redirect('cart')


class RemoveFromCartView(View, BaseCartView):
    # POST only, for the same reason as AddToCartView.
    def post(self, request, cart_product_id: int):
        cart = self.get_cart(request)
        # Scoped to the requester's own cart: a row belonging to somebody
        # else's cart is a 404, never a delete.
        cart_product = get_object_or_404(
            CartProduct, pk=cart_product_id, cart=cart)
        cart.products.remove(cart_product)
        cart_product.delete()
        return redirect('cart')


class SetPwywAmountView(View, BaseCartView):
    """Change what a pay-what-you-want line in the cart is worth.

    POST only, for the same reason as AddToCartView: a GET would be
    triggerable cross-site with no CSRF token, and this one writes a price.
    """

    def post(self, request, cart_product_id: int):
        cart = self.get_cart(request)
        # Scoped to the requester's own cart, so a row id belonging to
        # somebody else is a 404 rather than a repricing of their basket.
        cart_product = get_object_or_404(
            CartProduct.objects.select_related("product"),
            pk=cart_product_id, cart=cart)
        if not cart_product.product.is_pwyw:
            return HttpResponseBadRequest(
                "That item does not have an amount to choose.")
        try:
            amount = parse_pwyw_amount(request.POST.get('chosen_amount'))
        except PwywAmountError as error:
            messages.error(request, str(error))
            return redirect('cart')
        cart_product.set_chosen_amount(amount)
        return redirect('cart')


class CheckoutView(View, BaseCartView):
    # POST only. A GET here creates a PENDING order and a Stripe session as a
    # side effect, which an <img> tag or a link prefetch could trigger
    # cross-site with no CSRF token involved. cart.html posts a real form.
    def post(self, request):
        coupon = request.POST.get("coupon") or None
        return self.start_checkout(request, coupon=coupon)

    def start_checkout(self, request, coupon=None):
        """Record the order, then hand the customer to Stripe.

        The PENDING order has to exist *before* Session.create, because its id
        is what gets passed as client_reference_id -- that is the only thread
        back from a webhook to this cart, which will be gone by then.
        """
        cart = self.get_cart(request)
        # get_cart() may have just merged an anonymous basket into the saved
        # one and changed a pay-what-you-want amount doing it. That reprice
        # must not be billed unseen: this is the same rule as showing the
        # rounded figure before charging it, and a merge that silently swaps
        # the amount the buyer was just looking at breaks it by another route.
        #
        # Read off the cart, and never cleared here. CartView owns the clear,
        # because CartView is what shows the buyer the basket, and the hold
        # itself is a column on the repriced row rather than a session key --
        # so logging out, logging back in, opening a second tab or issuing a
        # bodiless request cannot separate it from the cart it describes.
        #
        # Not strandable: this redirect goes to the page that clears it, so
        # the ordinary browser flow resolves in one hop.
        repriced = cart.pwyw_merge_notice_names()
        if repriced:
            logger.info(
                "Held checkout for cart %s: merge repriced %s.",
                cart.pk, ", ".join(repriced))
            # No message queued here on purpose: CartView queues it as it
            # renders, so repeated POSTs cannot stack up duplicate warnings.
            return redirect('cart')
        if not cart.products.exists():
            # Stripe rejects a session with no line items anyway; bailing here
            # keeps empty checkouts from leaving orphan PENDING orders behind.
            messages.error(
                request,
                "Your cart is empty. Add something to your cart before "
                "checking out.")
            return redirect('cart')
        # Stock is re-checked here, not just at add-to-cart: a cart can sit for
        # days, and stock is edited by hand in the admin, so what was
        # purchasable when it went in may not be now. Bounce back to the cart
        # rather than billing for something that cannot be shipped.
        unavailable = [
            cart_product.product.name
            for cart_product in cart.products.select_related("product")
            if not cart_product.product.is_purchasable()
        ]
        if unavailable:
            logger.info(
                "Blocked checkout for cart %s: %s no longer purchasable.",
                cart.pk, ", ".join(unavailable))
            messages.error(
                request,
                "Sorry, these are no longer available and need to be removed "
                "before checking out: " + ", ".join(unavailable) + ".")
            return redirect('cart')
        # Re-price every pay-what-you-want line from the amount its own cart
        # row holds, here, at the last moment before the order is snapshotted
        # and the session created.
        #
        # This is the security boundary for the whole feature. Nothing posted
        # to this endpoint is consulted -- `request.POST` is read for the
        # coupon and for nothing else -- so an amount injected at /checkout,
        # or a Price minted for an amount that has since been edited, cannot
        # decide what the buyer is charged. The database row does.
        for cart_product in cart.products.select_related("product"):
            cart_product.refresh_pwyw_price()
        user = request.user if request.user.is_authenticated else None
        order = Order.create_from_cart(cart, user=user)
        try:
            redirect_url, session_id = Payments.checkout(
                request, cart, coupon=coupon, order=order)
        except Exception:
            # No session was ever created, so nothing will ever arrive for
            # this order -- not even checkout.session.expired. Close it out
            # here or it stays PENDING in the admin forever.
            logger.exception(
                "Stripe checkout failed for order #%s; cancelling it.",
                order.pk)
            Order.objects.filter(
                pk=order.pk, status=Order.Status.PENDING).update(
                    status=Order.Status.CANCELLED)
            if order.amount_total == 0:
                # "If Stripe is being difficult with $0" -- the owner's clause,
                # and a requirement rather than a joke. A zero-total session is
                # the one shape of this feature with no payment behind it, so a
                # buyer who hits a Stripe problem here has nothing to retry and
                # no other way through. A 500 would leave a kid staring at a
                # stack trace with the book unbought; send them back to the
                # cart, where the owner's notice carries the mailto, and say to
                # use it. Every other failure still raises, because those are
                # payment problems the buyer can act on.
                messages.error(
                    request,
                    "Sorry -- Stripe would not set up this free order. "
                    "Please e-mail holden@pigscanfly.ca and we can send you a "
                    "copy directly.")
                return redirect('cart')
            raise
        order.stripe_session_id = session_id
        order.save(update_fields=['stripe_session_id', 'updated_at'])
        return redirect(redirect_url)


@method_decorator(never_cache, name="dispatch")
class CheckoutSuccessView(View, BaseCartView):
    """Where Stripe sends the customer after a completed Checkout session.

    Has to stay a GET -- Stripe redirects the browser here -- so it is
    reachable cross-site. The mere arrival of a redirect proves nothing:
    this URL can be hit cross-site with any session_id.

    The order shown is whatever the webhook already recorded. When the
    webhook has not yet run (or never will -- the customer closed the tab,
    the webhook is delayed, or the delivery was missed), a server-side
    reconciliation asks Stripe for the real payment status. That path is
    the fallback, not the primary source of truth: a customer who finishes
    on another device or pays by a delayed method never loads this page,
    and the webhook remains the only thing that can catch those cases.
    """

    def get(self, request):
        # Only a session id that resolves to a real order empties the cart.
        # Stripe substitutes it into success_url (see Payments.checkout), so
        # the genuine redirect always carries one; a bare cross-site GET of
        # this URL does not, and so can no longer clear a stranger's cart.
        order = None
        session_id = request.GET.get("session_id")
        if session_id:
            order = Order.objects.filter(
                stripe_session_id=session_id).prefetch_related('items').first()
        if order is not None:
            self.get_cart(request).clear()
            if order.status == Order.Status.PENDING:
                self._reconcile_with_stripe(order, session_id)
        context = {
            'title': 'Success! - Checkout',
            'order': order,
        }
        context.update(post_purchase_context(request, order))
        return render(request, 'checkout_success.html', context=context)

    def _reconcile_with_stripe(self, order: Order, session_id: str) -> None:
        """Ask Stripe for the real payment status, server-side.

        Only called when the webhook has not yet marked this order paid.
        Never raises and never redirects: this is a best-effort fallback
        inside a page the customer is already looking at. A timeout or error
        leaves the order as the webhook left it, and the page still renders.

        The Stripe lookup is gated on two cheap checks first -- the order
        must be PENDING and the session_id must have resolved to a local
        order -- so a scanner hitting the URL with random session_ids never
        reaches Stripe's API.
        """
        try:
            session = stripe.checkout.Session.retrieve(session_id)
        except Exception as e:
            logger.warning(
                "Checkout success page could not retrieve Stripe session "
                "%s for order #%s: %s", session_id, order.pk, e)
            return

        payment_status = session.get("payment_status")
        if payment_status not in StripeWebhookView.PAID_PAYMENT_STATUSES:
            logger.info(
                "Checkout success page: Stripe session %s for order #%s "
                "reports payment_status %r, which is not a paid status; "
                "leaving the order PENDING.",
                session_id, order.pk, payment_status)
            return

        webhook = StripeWebhookView()
        fields = webhook.paid_fields(session)
        with transaction.atomic():
            Order.objects.select_for_update().filter(pk=order.pk).first()
            updated = Order.objects.filter(
                pk=order.pk, status=Order.Status.PENDING).update(**fields)

        if not updated:
            order.refresh_from_db()
            if order.status == Order.Status.PAID:
                logger.info(
                    "Checkout success page: order #%s was already PAID "
                    "(likely raced with the webhook); running fulfilment.",
                    order.pk)
                webhook.fulfil_order(order)
            else:
                logger.info(
                    "Checkout success page: order #%s is past PENDING; "
                    "not overwriting.", order.pk)
            return

        order.refresh_from_db()
        webhook.fulfil_order(order)


def post_purchase_context(request, order: Optional[Order]) -> Dict[str, Any]:
    """Everything the "now what?" block below a completed order needs.

    Three asks, in the order they cost the buyer anything: join the list,
    follow us somewhere, and tell us what made you buy this. Shared by the
    success page and by the page the feedback form posts to, so somebody who
    answered one of them still gets offered the other two.

    Nothing here subscribes, records or infers anything about the buyer. The
    signup is the same double opt-in form as everywhere else -- having bought
    something is not consent to be mailed -- and the pre-filled address and
    pre-selected list are conveniences on a form they still have to submit.
    """
    products = []
    if order is not None:
        products = [item.product for item in order.items.all()]
    return {
        'areas': mailing.interest_choices(request),
        'selected_area': mailing.interest_for_products(products),
        # Stripe's record of where the receipt went, which is the address
        # somebody signing up here would type anyway. Only ever a value in a
        # form field: it is not submitted, and not subscribed, unless they
        # press the button.
        'signup_email': order.customer_email if order is not None else '',
        # Three rows, not one list of icons: Holden, the company, and
        # Liberated Bread are three different things to follow and a book
        # buyer may want any one of them without the others.
        'follow_targets': follow_targets(),
        # The feedback form needs an order to attach an answer to, and the
        # session id is how it names one (see PurchaseFeedbackView). No order
        # -- somebody who reached this page without one -- means no form.
        'feedback_session_id': (
            order.stripe_session_id or '' if order is not None else ''),
        'ask_for_feedback': (order is not None
                             and bool(order.stripe_session_id)
                             and PurchaseFeedback.has_room(order)),
    }


@method_decorator(never_cache, name="dispatch")
class PurchaseFeedbackView(View):
    """Where the checkout success page's "what made you buy this?" is posted.

    CSRF-protected, unlike the signup endpoint next door: this form is only
    ever rendered by us on a page of ours, so there is always a token to
    send, and nothing about it is meant to work pasted onto another site.

    The order is named by the Stripe session id, which is what the buyer
    holds -- Stripe substitutes it into the success URL. That is the only
    authority this endpoint recognises: an order primary key in the markup
    would be a number anybody could change to write a note onto somebody
    else's purchase.
    """

    def get(self, request):
        # Somebody following the action URL by hand. There is nothing to show
        # here -- the form lives on the success page -- so send them home.
        return redirect('home')

    ANSWER = ("Thank you — that is the sort of thing that decides what gets "
              "written next.")
    EMPTY = "There was nothing in the box, so there was nothing to send."

    def post(self, request):
        form = PurchaseFeedbackForm(request.POST)
        if not form.is_valid():
            # The only way here is an empty answer: every other field is
            # optional and the long ones truncate rather than reject.
            return self.render_result(
                request, order=self.order_for(request.POST.get("session_id")),
                ok=False, message=self.EMPTY, status=400)

        order = self.order_for(form.cleaned_data["session_id"])

        if form.is_bot():
            # Answered exactly like a real submission, minus the writing.
            logger.info("Dropped a honeypotted purchase feedback submission.")
            return self.render_result(request, order=order)

        if self.over_rate_limit(request):
            logger.warning(
                "Dropping purchase feedback: over the rate limit.")
            return self.render_result(request, order=order)

        if order is None:
            # A submission carrying a session id that matches no order: a
            # stale page, or somebody poking the endpoint. Answered like the
            # rest rather than saying which -- "no such order" on a URL
            # anybody can post to is an oracle for whether a session id is
            # one of ours.
            logger.info(
                "Purchase feedback arrived for a session id that matches no "
                "order; nothing stored.")
            return self.render_result(request, order=None)

        feedback = self.record(order, form)
        if feedback is not None:
            # Best effort, and deliberately outside the transaction that
            # wrote the row: the note is saved and visible in the admin
            # whether or not the mail goes out, and an SMTP timeout must not
            # be something a database lock is held across.
            feedback.notify_owner()
        return self.render_result(request, order=order)

    @staticmethod
    def record(order: Order, form: PurchaseFeedbackForm
               ) -> Optional[PurchaseFeedback]:
        """Write the note, or None when the order is already at its cap.

        The count and the write happen under one lock on the order row.
        Checking first and writing after is not enough on its own: two POSTs
        carrying the same session id can each see four notes and each write a
        fifth, and the per-source limiter does not stop that -- it allows ten
        an hour, and says nothing about two arriving at once.

        Same lock-then-act shape the success page's reconciliation uses. On
        SQLite select_for_update does nothing, which is accurate rather than
        broken: writes are serialised there anyway.
        """
        with transaction.atomic():
            Order.objects.select_for_update().filter(pk=order.pk).first()
            if not PurchaseFeedback.has_room(order):
                logger.warning(
                    "Order #%s already has %s feedback notes; dropping "
                    "another.", order.pk, PurchaseFeedback.MAX_PER_ORDER)
                return None
            return PurchaseFeedback.objects.create(
                order=order,
                reason=form.cleaned_data["reason"],
                may_quote=form.cleaned_data["may_quote"],
                quote_name=form.cleaned_data["quote_name"],
            )

    @staticmethod
    def order_for(session_id: Optional[str]) -> Optional[Order]:
        if not session_id:
            return None
        return Order.objects.filter(
            stripe_session_id=session_id).prefetch_related('items').first()

    def render_result(self, request, order: Optional[Order], ok: bool = True,
                      message: Optional[str] = None, status: int = 200):
        """The page that answers a submission.

        Carries the mailing list and the socials as well, because this is
        where somebody who answered the question ends up: the other two asks
        would otherwise be lost with the page they were on.
        """
        context = {
            'title': 'Thank you' if ok else 'Sorry',
            'ok': ok,
            'message': message or self.ANSWER,
        }
        context.update(post_purchase_context(request, order))
        if ok:
            # Answered; the form on this page would only invite a duplicate.
            context['ask_for_feedback'] = False
        return render(request, 'purchase_feedback_result.html',
                      context=context, status=status)

    def over_rate_limit(self, request) -> bool:
        """Whether this source has had its hour's worth.

        Keyed on the source only, unlike the mailing list's limit: there is no
        third party to protect here -- a submission cannot cause mail to
        anybody but the owner -- so the thing being bounded is writes to the
        table, and the per-order ceiling covers the rest.
        """
        limit = getattr(settings, "PURCHASE_FEEDBACK_RATE_LIMIT", 10)
        if not limit:
            return False
        source = get_storable_client_ip(request) or "unparseable-source"
        return over_cache_limit(f"purchase-feedback:{source}", limit)


@method_decorator(csrf_exempt, name='dispatch')
class StripeWebhookView(View):
    """Stripe's server-to-server callback. The only thing that marks a
    payment as real.

    POST only, CSRF exempt (Stripe has no token to send), and authenticated
    solely by the signature on the body. Every path out of here that is not a
    verified payload is a 400 before any state is touched.

    Everything runs synchronously, but each post-payment action has a durable
    completion marker on the order.  A redelivery of a paid event resumes any
    action whose marker is still empty; this matters when a worker disappears
    after the paid transition has committed but before fulfilment finishes.
    """

    # Both mean "the money arrived". async_payment_succeeded is the delayed
    # follow-up for payment methods that settle after checkout (ACH, some
    # bank debits), where the original completed event arrives unpaid.
    PAID_EVENTS = frozenset({
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
    })
    # Terminal failures. Only ever applied to a still-PENDING order, so they
    # can never undo a payment that already landed.
    CANCELLED_EVENTS = {
        "checkout.session.async_payment_failed": "the payment failed",
        "checkout.session.expired": "the checkout session expired",
    }
    # Payment statuses that mean the sale is done and fulfilment should run.
    # "no_payment_required" is what a zero-total session reports: Stripe
    # creates no PaymentIntent at all, so it can never become "paid". That is
    # a real case here rather than a curiosity -- the e-book is
    # pay-what-you-want with a floor of zero, and the owner's decision is that
    # a $0 order is still an order and the book still gets delivered.
    PAID_PAYMENT_STATUSES = frozenset({"paid", "no_payment_required"})

    # How long a fulfilment claim stays valid. Long enough that a worker
    # working through a slow Stripe lookup and two SMTP sends keeps its claim
    # -- the gunicorn timeout is 60s, so no live request can outlast this --
    # and short enough that a worker killed mid-fulfilment is retried well
    # inside the three days Stripe keeps redelivering.
    FULFILMENT_LEASE = timedelta(minutes=15)

    def post(self, request):
        secret = getattr(settings, "STRIPE_WEBHOOK_SECRET", "")
        if not secret:
            # Failing closed: with no secret nothing can be verified, so
            # nothing may be processed.
            logger.error(
                "STRIPE_WEBHOOK_SECRET is not set; rejecting a Stripe "
                "webhook delivery. Orders will stay PENDING until it is.")
            return HttpResponseBadRequest("Webhook secret is not configured.")

        signature = request.META.get("HTTP_STRIPE_SIGNATURE", "")
        try:
            event = stripe.Webhook.construct_event(
                request.body, signature, secret)
        except stripe.SignatureVerificationError:
            logger.warning("Rejected a Stripe webhook: bad signature.")
            return HttpResponseBadRequest("Invalid signature.")
        except ValueError:
            logger.warning("Rejected a Stripe webhook: malformed payload.")
            return HttpResponseBadRequest("Invalid payload.")

        event_type = event.get("type")
        session = (event.get("data") or {}).get("object") or {}

        if event_type in self.PAID_EVENTS:
            self.handle_paid(session)
        elif event_type in self.CANCELLED_EVENTS:
            self.handle_cancelled(session, self.CANCELLED_EVENTS[event_type])
        else:
            # 200 with no action, so Stripe stops redelivering event types
            # this endpoint has no opinion about.
            logger.debug("Ignoring Stripe event type %s.", event_type)
        return HttpResponse(status=200)

    def handle_paid(self, session) -> None:
        if session.get("payment_status") not in self.PAID_PAYMENT_STATUSES:
            # e.g. a completed session whose ACH debit has not settled. The
            # matching async_payment_succeeded will arrive later.
            logger.info(
                "Stripe session %s completed but is not paid (%s); leaving "
                "the order pending.",
                session.get("id"), session.get("payment_status"))
            return

        order = self.find_order(session)
        if order is None:
            # Nothing to attach it to and a retry would not change that, so
            # do not make Stripe keep trying.
            logger.warning(
                "No local order for Stripe session %s (ref %s).",
                session.get("id"), session.get("client_reference_id"))
            return

        fields = self.paid_fields(session)
        with transaction.atomic():
            # select_for_update serialises concurrent deliveries on Postgres;
            # the guarded UPDATE below is what actually makes this idempotent,
            # and it holds on any backend. Two simultaneous deliveries both
            # try to move PENDING -> PAID and exactly one row is affected, so
            # exactly one of them goes on to send the email.
            Order.objects.select_for_update().filter(pk=order.pk).first()
            updated = Order.objects.filter(
                pk=order.pk, status=Order.Status.PENDING).update(**fields)

        if not updated:
            order.refresh_from_db()
            if order.status == Order.Status.PAID:
                logger.info(
                    "Order #%s is already PAID; retrying incomplete "
                    "fulfilment for Stripe session %s.",
                    order.pk, session.get("id"))
                self.fulfil_order(order)
                return
            logger.info(
                "Order #%s is already past PENDING; ignoring a duplicate "
                "delivery of Stripe session %s.", order.pk, session.get("id"))
            return

        order.refresh_from_db()
        self.fulfil_order(order)

    def fulfil_order(self, order: Order) -> None:
        """Run only the paid order actions that have not completed yet.

        The PAID transition deliberately commits before this method.  Each
        successful action writes its own marker, so a crash at any point is
        repaired by Stripe's next delivery rather than turning PAID into a
        terminal, unfulfilled state.
        """
        if not self.claim_fulfilment(order):
            logger.info(
                "Order #%s fulfilment is already claimed by another worker; "
                "leaving it to them.", order.pk)
            return
        try:
            order.refresh_from_db()
            if order.reconciled_at is None:
                # Whether the owner has already been told about this order
                # *before* this attempt can change the answer. See below.
                notified_before = order.notified_at is not None
                order.reconcile_line_items()
                order.refresh_from_db()
                if notified_before and order.reconciled_at is not None:
                    # An earlier delivery could not reach Stripe, so the owner
                    # was emailed a pick list built from the cart snapshot and
                    # told it was unverified. This attempt got the real
                    # quantities, which may differ -- the customer can change
                    # them on Stripe's hosted page. Clearing the marker
                    # reissues the notification below, because otherwise the
                    # only instruction the owner ever received says to ship
                    # numbers the database now knows are wrong.
                    #
                    # Bounded to one extra email per order: reconciled_at only
                    # goes null -> set once, so this can never fire twice.
                    logger.info(
                        "Order #%s reconciled on a retry after the owner was "
                        "notified from an unverified snapshot; reissuing the "
                        "notification.", order.pk)
                    Order.objects.filter(pk=order.pk).update(notified_at=None)
                    order.notified_at = None

            order.refresh_from_db()
            if (order.digital_delivery_sent_at is None
                    and order.digital_items()):
                order.deliver_digital_goods()

            order.refresh_from_db()
            if order.receipt_sent_at is None and order.customer_email:
                # Best-effort: the receipt is a courtesy and must never
                # block delivery or the owner notification. Caught rather
                # than propagated, and the failure is recorded on the row.
                try:
                    order.send_receipt()
                except Exception:
                    logger.exception(
                        "Order #%s: receipt send raised unexpectedly.",
                        order.pk)

            # Kept last so the owner's message reports the final reconciliation
            # and digital-delivery outcome.
            order.refresh_from_db()
            if order.notified_at is None:
                order.notify_owner()
        finally:
            self.release_fulfilment(order)

    def claim_fulfilment(self, order: Order) -> bool:
        """Take the exclusive right to fulfil this order, or report defeat.

        One conditional UPDATE, which both Postgres and SQLite apply
        atomically, so of two workers racing on the same order exactly one
        sees a row count of 1. Nothing else here serialises them: the row lock
        in handle_paid is released with its transaction, and each completion
        marker is only written once its side effect has already happened, so
        without this both workers would read null markers and both would send.
        """
        now = timezone.now()
        return bool(
            Order.objects.filter(pk=order.pk).filter(
                Q(fulfilment_claimed_at__isnull=True)
                | Q(fulfilment_claimed_at__lt=now - self.FULFILMENT_LEASE)
            ).update(fulfilment_claimed_at=now))

    @staticmethod
    def release_fulfilment(order: Order) -> None:
        """Drop the claim so the next delivery can pick up anything left.

        Released rather than left to expire because a run that finished with
        an action still incomplete -- a bounced owner email, say -- should be
        retried by Stripe's next delivery immediately, not after the lease
        runs out.
        """
        Order.objects.filter(pk=order.pk).update(fulfilment_claimed_at=None)
        order.fulfilment_claimed_at = None

    def handle_cancelled(self, session, reason: str) -> None:
        order = self.find_order(session)
        if order is None:
            return
        updated = Order.objects.filter(
            pk=order.pk, status=Order.Status.PENDING).update(
                status=Order.Status.CANCELLED)
        if updated:
            logger.info("Order #%s cancelled: %s.", order.pk, reason)

    @staticmethod
    def find_order(session) -> Optional[Order]:
        """Map a Stripe session back to a local order.

        The stored stripe_session_id *is* the binding between an order and
        the payment for it, so the client_reference_id fallback may only ever
        reach an order that is not yet bound, or one bound to this very
        session. An order bound to a different session is never returned: the
        binding gets verified, not overwritten.
        """
        session_id = session.get("id")
        if session_id:
            order = Order.objects.filter(stripe_session_id=session_id).first()
            if order is not None:
                return order
        reference = (session.get("client_reference_id")
                     or (session.get("metadata") or {}).get("order_id"))
        if not reference:
            return None
        try:
            order = Order.objects.filter(pk=int(reference)).first()
        except (TypeError, ValueError):
            logger.warning(
                "Stripe session %s carried an unusable order reference %r.",
                session_id, reference)
            return None
        if order is None:
            return None
        if order.stripe_session_id and order.stripe_session_id != session_id:
            # Nothing legitimate produces this. Refuse rather than re-point a
            # paid-for order at a different session.
            logger.error(
                "Refusing Stripe session %s for order #%s: that order is "
                "already bound to session %s. Not marking it paid.",
                session_id, order.pk, order.stripe_session_id)
            return None
        return order

    @staticmethod
    def paid_fields(session) -> Dict[str, Any]:
        """The order columns a paid Stripe session dictates.

        Note this writes stripe_session_id, which is the second code path
        that touches that column. It is only safe because find_order() has
        already established that the order is either unbound or bound to this
        very session, so the write is a legitimate late binding or a no-op --
        never a re-pointing. Weaken that guard and this quietly becomes an
        overwrite again.
        """
        customer = session.get("customer_details") or {}
        billing = customer.get("address") or {}
        # Newer Stripe API versions moved shipping under collected_information;
        # older ones report it top level. Accept whichever this account sends.
        collected = session.get("collected_information") or {}
        shipping = (collected.get("shipping_details")
                    or session.get("shipping_details") or {})
        shipping_address = shipping.get("address") or {}
        totals = session.get("total_details") or {}

        fields: Dict[str, Any] = {
            "status": Order.Status.PAID,
            "paid_at": timezone.now(),
            "stripe_session_id": session.get("id"),
            "customer_email": customer.get("email") or "",
            "customer_name": customer.get("name") or "",
            "amount_total": session.get("amount_total") or 0,
            "amount_subtotal": session.get("amount_subtotal"),
            "amount_tax": totals.get("amount_tax"),
            "currency": (session.get("currency") or "usd").lower()[:3],
        }
        fields.update({
            "billing_name": customer.get("name") or "",
            "billing_line1": billing.get("line1") or "",
            "billing_line2": billing.get("line2") or "",
            "billing_city": billing.get("city") or "",
            "billing_state": billing.get("state") or "",
            "billing_postal_code": billing.get("postal_code") or "",
            "billing_country": billing.get("country") or "",
            "shipping_name": shipping.get("name") or "",
            "shipping_line1": shipping_address.get("line1") or "",
            "shipping_line2": shipping_address.get("line2") or "",
            "shipping_city": shipping_address.get("city") or "",
            "shipping_state": shipping_address.get("state") or "",
            "shipping_postal_code": shipping_address.get("postal_code") or "",
            "shipping_country": shipping_address.get("country") or "",
        })
        return fields


@method_decorator(never_cache, name="dispatch")
class DigitalDownloadView(View):
    """Serve a purchased book from a signed, expiring link.

    Unauthenticated by design: the buyer typically has no account, so the
    signature on the token *is* the authorisation. It carries the order and
    the product, and both are re-checked against the database here -- a token
    only works for an order that was actually paid, actually contains that
    product, and whose product is still one we are licensed to hand out.

    Nothing about the path comes from the URL. The token yields two integers;
    the filename is rebuilt from the product's stem through
    digital.resolve_asset_path, which is what keeps an admin-typed
    "../../etc/passwd" from becoming a file read.
    """

    EXPIRED_MESSAGE = (
        "This download link has expired.\n\n"
        "Links are good for {days} days. Write to {email} quoting your order "
        "number and we will send you a fresh one.\n")

    def get(self, request, token):
        try:
            order_pk, product_pk = parse_download_token(token)
        except SignatureExpired:
            # Distinguished from a bad signature: this is a real customer with
            # a real receipt, and telling them "404" would be a lie.
            return HttpResponse(
                self.EXPIRED_MESSAGE.format(
                    days=link_lifetime_days(),
                    email=settings.DEFAULT_FROM_EMAIL),
                status=410, content_type="text/plain; charset=utf-8")
        except BadSignature:
            logger.warning("Rejected a download token that did not verify.")
            raise Http404("No such download.")

        order = Order.objects.filter(pk=order_pk).first()
        if order is None or order.status not in (
                Order.Status.PAID, Order.Status.FULFILLED):
            # Includes an order that was never paid for, so a guessed-at
            # (unforgeable) pairing still gets nothing.
            raise Http404("No such download.")
        item = order.items.select_related('product').filter(
            product_id=product_pk).first()
        if item is None or item.product is None:
            raise Http404("No such download.")
        if not item.product.is_digitally_fulfilled():
            # The rights interlock, applied at serve time as well as at send
            # time: if sells_ebook has since been cleared, old links stop
            # working too.
            logger.warning(
                "Refusing to serve product %s for order #%s: it is not a "
                "product we are licensed to distribute.", product_pk, order_pk)
            raise Http404("No such download.")

        try:
            handle = open_asset(item.product.digital_asset_name)
        except DigitalAssetError as e:
            # A customer-visible 404, but the reason is only ever logged --
            # it names server paths.
            logger.error(
                "Order #%s: cannot serve the download for %r: %s",
                order_pk, item.product_name, e)
            raise Http404("No such download.")
        return FileResponse(
            handle, as_attachment=True,
            filename=f"{item.product.digital_asset_name}.zip",
            content_type="application/zip")


@method_decorator(never_cache, name="dispatch")
class CheckoutCancelView(View, BaseCartView):
    def get(self, request):
        return render(request, 'checkout_cancel.html', context={'title': 'Cancelled! - Checkout'})

class LoginView(View):
    def get(self, request):
        valid = request.GET.get('valid')
        return render(request, 'login.html', context={'title': 'Log In', 'valid': valid})

    def post(self, request):
        email = normalize_email_identity(request.POST.get('email') or '')
        password = request.POST.get('password') or ''
        if not email or not password:
            return redirect(reverse('login') + '?valid=false')

        # email is not unique on auth.User, so .get() here could raise
        # MultipleObjectsReturned and 500 the login page. Try each match
        # instead: only the one whose password checks out authenticates.
        for candidate in User.objects.filter(email__iexact=email):
            user = authenticate(request, email=email,
                                username=candidate.username, password=password)
            if user is not None:
                login(request, user)
                return redirect('home')
        return redirect(reverse('login') + '?valid=false')


@method_decorator(login_required, name='dispatch')
class LogoutView(View):
    def get(self, request):
        logout(request)
        return redirect('login')

@method_decorator(csrf_exempt, name='dispatch')
class MailingListSubscribeView(View):
    """The signup endpoint. CSRF exempt on purpose.

    The whole point is that a plain <form> pasted onto another site can post
    here, and such a form has no token to send. Nothing here reads the session
    or acts on behalf of a logged-in user, so there is no authority for a
    forged request to borrow: the worst it can do is create an unconfirmed
    subscription, which does nothing until that address clicks the link.

    Everything after this -- the confirmation email, the activation page, the
    unsubscribe page -- is django-newsletter's.
    """

    # None means "trust the submitted interest". Anything truthy here
    # overrides the form field server-side; keep the sentinel falsy value as
    # exactly None so a future empty string cannot silently fall back to user
    # input and reopen the tampering hole this subclass closes.
    forced_interest: Optional[str] = None

    def submitted(self, request) -> Dict[str, Any]:
        """The submitted fields, from a form post or a JSON body.

        A JSON body leaves request.POST empty, so without this a fetch()
        sending JSON -- the obvious thing to write against an endpoint that
        answers JSON -- would look to us like a submission with no email in
        it. Cached because a view is instantiated per request and the body
        can only be read once.
        """
        if not hasattr(self, "_submitted"):
            self._submitted = self._parse_body(request)
        return self._submitted

    @staticmethod
    def _parse_body(request) -> Dict[str, Any]:
        if request.content_type == "application/json":
            try:
                data = json.loads(request.body or b"{}")
            except ValueError:
                return {}
            return data if isinstance(data, dict) else {}
        return request.POST

    def wants_json(self, request) -> bool:
        return (request.content_type == "application/json"
                or self.submitted(request).get("format") == "json"
                or request.GET.get("format") == "json"
                or "application/json" in request.headers.get("Accept", ""))

    def json_response(self, payload: Dict[str, Any], status: int = 200):
        response = JsonResponse(payload, status=status)
        # Read by scripts on other origins. Safe as a wildcard precisely
        # because this endpoint has no session and no credentials: it does
        # nothing that depends on who is asking.
        response["Access-Control-Allow-Origin"] = "*"
        return response

    def get(self, request):
        # Somebody following the action URL by hand, or a form that lost its
        # method. Send them to the real page rather than 405ing.
        return redirect('subscribe')

    def options(self, request, *args, **kwargs):
        # Preflight for a cross-origin fetch() posting JSON. A plain form post
        # never gets here.
        response = HttpResponse(status=204)
        response["Access-Control-Allow-Origin"] = "*"
        response["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        response["Access-Control-Allow-Headers"] = "Content-Type, Accept"
        response["Access-Control-Max-Age"] = "86400"
        return response

    def post(self, request):
        form = MailingListSignupForm(self.submitted(request))
        if not form.is_valid():
            error = "That does not look like an email address."
            if self.wants_json(request):
                return self.json_response(
                    {"ok": False, "error": error,
                     "errors": form.errors}, status=400)
            return render(request, 'mailing_list_result.html', context={
                'title': 'Subscribe for updates',
                'ok': False,
                'message': error,
            }, status=400)

        self._form = form

        if form.is_bot():
            # Answered exactly like a success, minus the subscription: telling
            # a bot it was caught only teaches whoever wrote it.
            logger.info("Dropped a honeypotted mailing list signup.")
            return self.respond(request)

        email = form.cleaned_data["email"]
        if self.over_rate_limit(request, email):
            # Checked before anything is written, so a flood cannot fill the
            # table either. Answered like every other submission.
            logger.warning(
                "Dropping a mailing list signup for %s: over the rate limit.",
                email)
            return self.respond(request)

        interest = self.forced_interest or form.cleaned_data.get("interest", "")
        _subscription, to_confirm = mailing.subscribe(
            email=email,
            newsletter=mailing.newsletter_for(interest),
            name=form.cleaned_data.get("name", ""),
            ip=get_storable_client_ip(request),
            also_all=form.cleaned_data.get("all_updates", False))
        if to_confirm is not None:
            mailing.send_activation_email(to_confirm)
        return self.respond(request)

    # Deliberately the same words whatever happened: a new signup, an address
    # already subscribed, a suppressed one, a honeypotted one and a
    # rate-limited one are indistinguishable from outside. Saying which is a
    # membership oracle on an endpoint anybody can post to, and these lists
    # include things people would rather not have confirmed about them.
    ANSWER = ("Almost there — if that address is not already subscribed, "
              "check your email for a link to confirm.")

    def respond(self, request):
        target = self.next_url()
        if self.wants_json(request):
            payload = {"ok": True, "message": self.ANSWER}
            if target:
                payload["next"] = target
            return self.json_response(payload)
        if target:
            return redirect(target)
        return render(request, 'mailing_list_result.html', context={
            'title': 'Subscribe for updates',
            'ok': True,
            'message': self.ANSWER,
        })

    ENCODED_CONTROL = re.compile(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)",
                                 flags=re.IGNORECASE)

    @staticmethod
    def normalized_netloc(netloc: str) -> str:
        # Lower only ASCII A-Z. Python's Unicode lower() maps U+212A KELVIN
        # SIGN to ASCII "k", so non-ASCII lookalikes must pass through
        # unchanged instead of collapsing onto an allowlisted ASCII host.
        return ascii_lowercase(netloc)

    def next_url(self) -> str:
        form = getattr(self, "_form", None)
        if form is None:
            return ""
        return self.allowed_next(form.cleaned_data.get("next", ""))

    def allowed_next(self, target: str) -> str:
        target = (target or "").strip()
        if not target:
            return ""
        if any(ord(char) < 32 or ord(char) == 127 for char in target):
            return ""
        if self.ENCODED_CONTROL.search(target):
            return ""

        allowed = {self.normalized_netloc(entry)
                   for entry in getattr(settings,
                                        "MAILING_LIST_ALLOWED_NEXT_HOSTS", ())}
        if not allowed:
            return ""

        try:
            parsed = urlparse(target)
        except ValueError:
            return ""
        normalized_netloc = self.normalized_netloc(parsed.netloc)
        if normalized_netloc not in allowed:
            return ""
        normalized_target = parsed._replace(netloc=normalized_netloc).geturl()
        if not url_has_allowed_host_and_scheme(
                normalized_target, allowed, require_https=True):
            return ""
        return target

    # Confirmations one address can be sent per hour, whoever asks. Low: there
    # is no legitimate reason to need a fourth, and this is the ceiling on
    # using the endpoint to bury somebody in mail.
    PER_ADDRESS_LIMIT = 3

    def over_rate_limit(self, request, email: str) -> bool:
        """Whether this signup has had its hour's worth.

        Counted per *target address* as well as per source, because the source
        cannot be trusted: X-Forwarded-For is client-supplied and nginx appends
        to it, so a limit keyed only on the client's idea of its own address is
        bypassed by sending a different one -- or by sending nonsense, which
        used to disable this check altogether. The address being signed up is
        the one thing in the request that cannot be varied while still
        achieving anything, so it is the key that matters: it is what stops the
        endpoint being used to bury one person in confirmation mail.

        Deliberately crude: the cache is per worker process, so the real
        ceiling is this times the worker count. That bounds a flood without
        needing a shared cache the site does not otherwise run.
        """
        limit = getattr(settings, "MAILING_LIST_SIGNUP_RATE_LIMIT", 20)
        if not limit:
            return False
        # An unusable X-Forwarded-For shares one bucket rather than escaping
        # the count: junk in that header must not be a way out of the limit.
        source = get_storable_client_ip(request) or "unparseable-source"
        return (self._over(f"mailing-list-signups:{source}", limit)
                or self._over(f"mailing-list-address:{email}",
                              self.PER_ADDRESS_LIMIT))

    @staticmethod
    def _over(key: str, limit: int) -> bool:
        return over_cache_limit(key, limit)


class MailingListSubscribeAllView(MailingListSubscribeView):
    forced_interest = mailing.ALL_SLUG


@method_decorator(never_cache, name="dispatch")
@method_decorator(staff_member_required, name='dispatch')
class AdminHomeView(View):
    """One page listing where everything in the admin actually lives.

    The Django admin index only lists model changelists, so the things that
    are not a changelist -- the subscriber import, the send page, the
    embeddable form -- are unfindable unless something like this points at
    them.
    """

    def get(self, request):
        return render(request, 'admin/home.html', context={
            'title': 'Admin',
            'sections': self.sections(),
        })

    @staticmethod
    def link(url_name, label, description, args=None):
        try:
            return {"url": reverse(url_name, args=args or []),
                    "label": label, "description": description}
        except NoReverseMatch:
            # A URL that is not wired up (an app removed, a rename) should
            # cost one missing row, not the whole page.
            logger.warning("Admin home: no URL named %s.", url_name)
            return None

    def sections(self):
        sections = [
            ("Mailing list", [
                self.link('admin:newsletter_subscription_changelist',
                          "Subscribers",
                          "Every address, which list it is on, and whether it "
                          "has confirmed."),
                self.link('mailing-list-import',
                          "Import subscribers from CSV",
                          "Mailchimp or Google Forms exports. Checks the "
                          "suppression list, and can email everyone imported "
                          "to say the list changed."),
                self.link('admin:main_suppressedaddress_changelist',
                          "Suppressed addresses (never email)",
                          "Addresses no import may add. Bounces, complaints, "
                          "anyone who asked to be left alone."),
                self.link('admin:main_mailinglistmessage_changelist',
                          "Mailings — write and send",
                          "Write a mailing, then follow the “send…” link on "
                          "its row. Leave the lists empty to reach every "
                          "confirmed subscriber once."),
                self.link('admin:newsletter_newsletter_changelist',
                          "Interest areas",
                          "The lists people can subscribe to. Anyone who does "
                          "not pick one is on the general list."),
                {"url": staticfiles_storage.url(
                    'mailing-list/signup-form.html'),
                 "label": "Embeddable signup form",
                 "description": "Plain HTML to paste into another site so it "
                                "can sign people up here. Set its `interest` "
                                "to one of the slugs above."},
            ]),
            ("Store", [
                self.link('admin:main_order_changelist', "Orders",
                          "Paid orders to pick, pack and mark fulfilled."),
                self.link('admin:main_product_changelist', "Products",
                          "Prices, stock and the links on each product page."),
                self.link('admin:main_purchasefeedback_changelist',
                          "Why they bought",
                          "What buyers typed on the checkout success page. "
                          "Read-only; “may quote” is their permission, not a "
                          "setting."),
            ]),
            ("Site", [
                self.link('admin:index', "Django admin",
                          "Everything else, model by model."),
                self.link('admin:password_change', "Change your password", ""),
            ]),
        ]
        return [(name, [link for link in links if link])
                for name, links in sections]


@method_decorator(staff_member_required, name='dispatch')
class MailingListImportView(View):
    """Upload subscribers, or upload addresses to suppress.

    Staff only and behind the admin's login, because it is the one way into
    this app to add an address without that address agreeing to it. The
    suppression check is what keeps a stale export from putting somebody back
    on a list they left, and the notice is what gives them a way out if it
    happens anyway.
    """

    def get(self, request):
        return self.render_form(request, MailingListImportForm())

    def post(self, request):
        form = MailingListImportForm(request.POST, request.FILES)
        if not form.is_valid():
            return self.render_form(request, form)
        addresses = form.get_addresses()
        if form.cleaned_data["mode"] == form.MODE_SUPPRESS:
            suppressed, removed = mailing.suppress_addresses(
                addresses, reason=form.cleaned_data.get("reason", ""),
                user=request.user)
            messages.success(
                request,
                f"Suppressed {suppressed} new address(es) out of "
                f"{len(addresses)} in the file, and took {removed} live "
                "subscription(s) off their lists.")
            return redirect('mailing-list-import')

        result = mailing.import_addresses(
            addresses, newsletter=form.cleaned_data["newsletter"],
            notify=form.cleaned_data.get("notify", False), request=request)
        messages.success(request, result.summary())
        if result.suppressed:
            messages.warning(
                request,
                "Skipped as suppressed: " + ", ".join(result.suppressed[:20])
                + ("…" if len(result.suppressed) > 20 else ""))
        if result.notice_skipped:
            messages.warning(
                request,
                "That is too many addresses to email from this page. Write a "
                "mailing and send it instead, so it goes out in batches.")
        return redirect('mailing-list-import')

    def render_form(self, request, form):
        return render(request, 'admin/mailing_list_import.html', context={
            'title': 'Import mailing list subscribers',
            'form': form,
            'suppressed_count': SuppressedAddress.objects.count(),
        })


@method_decorator(staff_member_required, name='dispatch')
class MailingListSendView(View):
    """Send a mailing, to everyone or to the lists it names.

    django-newsletter can send a message to one newsletter; this exists for
    sending one mailing across several of them without anybody getting two
    copies.

    Sending is done a batch at a time because this runs inside a request with
    a worker timeout on it: a list long enough to outlive that timeout would
    otherwise be half-sent with no record of how far it got. Each batch claims
    a delivery per recipient, so clicking send again continues rather than
    starting over.
    """

    def get(self, request, pk):
        message = get_object_or_404(MailingListMessage, pk=pk)
        return self.render_page(request, message, MailingListSendForm())

    def post(self, request, pk):
        message = get_object_or_404(MailingListMessage, pk=pk)
        form = MailingListSendForm(request.POST)
        if not form.is_valid():
            return self.render_page(request, message, form)

        if "send_test" in request.POST:
            address = (form.cleaned_data.get("test_address")
                       or request.user.email)
            if not address:
                form.add_error(
                    "test_address",
                    "Your account has no email address, so say where the "
                    "test should go.")
                return self.render_page(request, message, form)
            try:
                message.send_test(address, request)
            except Exception as e:
                logger.exception("Test send of message %s failed.", message.pk)
                messages.error(request, f"Could not send the test: {e}")
            else:
                messages.success(request, f"Test sent to {address}.")
            return redirect('mailing-list-send', pk=message.pk)

        if "send_batch" in request.POST:
            if not message.pending_recipients().exists():
                messages.info(
                    request, "Everyone on that list already has this one.")
                return redirect('mailing-list-send', pk=message.pk)
            sent, failed = message.send_batch(request=request)
            remaining = message.pending_count()
            note = f"Sent {sent}."
            if failed:
                note += (f" {failed} could not be delivered; see the "
                         "deliveries below.")
            if remaining:
                note += f" {remaining} still to go — click send again."
            messages.success(request, note)
            return redirect('mailing-list-send', pk=message.pk)

        return self.render_page(request, message, form)

    def render_page(self, request, message, form):
        groups = list(message.interests.all())
        return render(request, 'admin/mailing_list_send.html', context={
            'title': f'Send: {message.subject}',
            'message': message,
            'form': form,
            # The page describes the audience with the model's own method, so
            # what it says cannot drift from what recipients() does.
            'groups': groups,
            'recipient_count': message.recipient_count(),
            'pending_count': message.pending_count(),
            'sent_count': message.sent_count(),
            'failed_count': message.failed_count(),
            'batch_size': send_batch_size(),
        })

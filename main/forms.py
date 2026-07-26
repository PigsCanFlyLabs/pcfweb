"""Forms for the mailing list."""

from typing import Optional

from django import forms

from newsletter.models import Newsletter

from main.mailing import parse_addresses
from main.utils import normalize_email


class MailingListSignupForm(forms.Form):
    """The public signup, posted from this site and from embedded forms on
    other sites.

    The list is a plain CharField rather than a ModelChoiceField: embedded
    forms carry a hard-coded slug in somebody else's markup, and a list that
    has since been renamed or hidden should quietly fall back to the general
    one rather than reject the signup and lose the address.
    """

    email = forms.EmailField(max_length=254)
    name = forms.CharField(max_length=200, required=False)
    # An explicit field on the form. Nothing here is inferred from which site
    # the request came from -- no Referer sniffing, no per-domain
    # configuration to keep in step with somebody else's markup.
    interest = forms.CharField(max_length=64, required=False)
    # The "send me everything" checkbox. Ticked adds the All list alongside
    # the topic; not ticked means only the topic, which is the promise the
    # subscribe page makes.
    all_updates = forms.BooleanField(required=False)
    # Hidden in the markup and invisible to a person; bots fill it in. A
    # submission with it set is answered exactly like a real one so whatever
    # filled it cannot tell the difference.
    website = forms.CharField(max_length=200, required=False)

    def clean_email(self) -> str:
        return normalize_email(self.cleaned_data["email"])

    def clean_name(self) -> str:
        """Collapse whitespace, so a name cannot be a paragraph.

        This value is rendered into the confirmation email django-newsletter
        sends ("Dear {{ name }},"). Autoescaping stops markup, but newlines
        survive it, and anyone can post any address here -- so without this the
        endpoint mails attacker-written prose from our own domain, above our
        own words.
        """
        return " ".join((self.cleaned_data.get("name") or "").split())[:100]

    # What a JSON caller might send for an unticked checkbox.
    # CharField.to_python stringifies, so 0 and false arrive as "0" and
    # "False" -- both truthy, and both would silently drop a real signup while
    # reporting success.
    NOT_FILLED_IN = {"", "0", "false", "none", "null", "undefined"}

    def is_bot(self) -> bool:
        filled = (self.cleaned_data.get("website") or "").strip().lower()
        return filled not in self.NOT_FILLED_IN


class MailingListImportForm(forms.Form):
    """Upload a file of addresses, either to subscribe them or to suppress
    them.

    One page for both because they are the same upload from the same export:
    Mailchimp gives you a file of subscribers and a file of people who left,
    and the second one belongs on the suppression list.

    django-newsletter has an import page of its own, and this deliberately is
    not it: that one cannot check a suppression list, cannot tell the people
    involved that anything happened, and reads CSV through unicodecsv -- an
    sdist-only package last released in 2017, which is not something to add to
    a production image to do what the standard library does.
    """

    newsletter = forms.ModelChoiceField(
        label="Mailing list", required=False,
        queryset=Newsletter.objects.all(),
        help_text="Which list these subscribers are being imported to.")
    address_file = forms.FileField(
        label="CSV file",
        help_text="A CSV with an email column, and a name column if you have "
                  "one -- a Mailchimp or Google Forms export as it comes. A "
                  "bare column of addresses works too.")

    MODE_SUBSCRIBE = "subscribe"
    MODE_SUPPRESS = "suppress"

    # Read before anything else on the page, so it is asked first.
    field_order = ["mode", "newsletter", "address_file", "notify", "reason"]

    mode = forms.ChoiceField(
        label="What is in this file",
        choices=[
            (MODE_SUBSCRIBE, "Subscribers — add them to the list below"),
            (MODE_SUPPRESS,
             "Addresses to suppress — never email these again, and take them "
             "off every list they are on"),
        ],
        initial=MODE_SUBSCRIBE, widget=forms.RadioSelect)
    notify = forms.BooleanField(
        required=False, initial=True,
        label="Email everyone imported to say the list changed",
        help_text="Tells them where they are and how to get off it. Only for "
                  "subscriber imports.")
    reason = forms.CharField(
        max_length=200, required=False,
        help_text="Only for suppression: why these addresses are here, for "
                  "whoever reads the list later.")

    def clean(self):
        upload = self.cleaned_data.get("address_file")
        if upload is not None:
            self.addresses = parse_addresses(upload)
            if not self.addresses:
                raise forms.ValidationError(
                    "No email addresses found in that file.")
        if (self.cleaned_data.get("mode") == self.MODE_SUBSCRIBE
                and not self.cleaned_data.get("newsletter")):
            # Importing into the wrong list is the mistake to be afraid of
            # here, so there is deliberately no default.
            raise forms.ValidationError(
                {"newsletter": "Pick which list these are being imported to."})
        return self.cleaned_data

    def get_addresses(self):
        return getattr(self, "addresses", {})


class MailingListSendForm(forms.Form):
    """The send controls on a message's admin page.

    Deliberately not a ModelForm: the message is edited on the normal admin
    change page, and this is only ever "send a test to this address" or "send
    the next batch", which are two different things and must not be one
    ambiguous submit.
    """

    test_address = forms.EmailField(required=False, label="Test address")

    def clean_test_address(self) -> Optional[str]:
        return normalize_email(
            self.cleaned_data.get("test_address") or "") or None

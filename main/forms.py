"""Forms for the mailing list: the public signup and the admin's CSV import."""

from typing import Optional

from django import forms

from main.models import InterestArea, MailingListSubscription


class MailingListSignupForm(forms.Form):
    """The public signup, posted from this site and from embedded forms on
    other sites.

    The interest area is a plain CharField rather than a ModelChoiceField:
    embedded forms carry a hard-coded slug in somebody else's markup, and an
    area that has since been renamed or deactivated should quietly fall back
    to the general group rather than reject the signup and lose the address.
    """

    email = forms.EmailField(max_length=254)
    name = forms.CharField(max_length=200, required=False)
    interest = forms.CharField(max_length=64, required=False)
    source = forms.CharField(max_length=200, required=False)
    # Hidden in the markup and invisible to a person; bots fill it in. A
    # submission with it set is answered exactly like a real one so whatever
    # filled it cannot tell the difference.
    website = forms.CharField(max_length=200, required=False)

    def clean_email(self) -> str:
        return MailingListSubscription.normalize_email(
            self.cleaned_data["email"])

    def is_bot(self) -> bool:
        return bool(self.cleaned_data.get("website"))

    def interest_area(self) -> InterestArea:
        slug = (self.cleaned_data.get("interest") or "").strip()
        if slug:
            area = InterestArea.objects.filter(slug=slug, active=True).first()
            if area is not None:
                return area
        return InterestArea.get_default()


class MailingListImportForm(forms.Form):
    """Upload a CSV of subscribers.

    Defaults to importing rows as already-subscribed: an import is the owner
    saying they have consent for these addresses. The double opt-in on the web
    signup covers the case where nobody can say that.
    """

    csv_file = forms.FileField(
        label="CSV file",
        help_text=(
            "A header row naming at least an email column; name, interest, "
            "source and status columns are used if present. A file that is "
            "just a column of addresses works too."))
    interest = forms.ModelChoiceField(
        queryset=InterestArea.objects.all(), required=False,
        help_text="The group rows land in unless the file names one. "
                  "Defaults to the general group.")
    status = forms.ChoiceField(
        choices=MailingListSubscription.Status.choices,
        initial=MailingListSubscription.Status.SUBSCRIBED,
        help_text="Applied to rows whose file does not say otherwise.")
    source = forms.CharField(
        max_length=200, required=False,
        help_text="Recorded against every imported row, e.g. where the list "
                  "came from.")
    dry_run = forms.BooleanField(
        required=False, initial=True, label="Dry run",
        help_text="Report what would happen without writing anything. Worth "
                  "doing once on any file you did not produce yourself.")

    def interest_area(self) -> InterestArea:
        return self.cleaned_data.get("interest") or InterestArea.get_default()


class MailingListSendForm(forms.Form):
    """The send controls on a message's admin page.

    Deliberately not a ModelForm: the message is edited on the normal admin
    change page, and this is only ever "send a test to this address" or "send
    the next batch", which are two different things and must not be one
    ambiguous submit.
    """

    test_address = forms.EmailField(required=False, label="Test address")

    def clean_test_address(self) -> Optional[str]:
        return MailingListSubscription.normalize_email(
            self.cleaned_data.get("test_address") or "") or None

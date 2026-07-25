import logging

import stripe
from django.core.management.base import BaseCommand

from main.models import Product

logger = logging.getLogger(__name__)


def _stripe_tax_code(stripe_product) -> str | None:
    if hasattr(stripe_product, "get"):
        return stripe_product.get("tax_code")
    return getattr(stripe_product, "tax_code", None)


class Command(BaseCommand):
    help = "Reconcile Stripe Product tax_code values from local Product rows."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write changes to live Stripe. Defaults to dry-run.",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        changed_label = "changed" if apply else "would-change"
        counts = {
            "examined": 0,
            changed_label: 0,
            "skipped-no-external-id": 0,
            "skipped-no-local-code": 0,
            "errored": 0,
        }

        mode = "APPLY" if apply else "DRY-RUN"
        self.stdout.write(f"{mode} stripe-product-tax-code-backfill start")
        if not apply:
            self.stdout.write(
                "DRY RUN: no changes were written to Stripe. Re-run with --apply to write."
            )

        for product in Product.objects.order_by("pk"):
            if not product.external_product_id:
                counts["skipped-no-external-id"] += 1
                self.stdout.write(
                    f"SKIP no-external-id product_pk={product.pk} name={product.name!r}"
                )
                continue

            local_tax_code = (product.tax_code or "").strip()
            if not local_tax_code:
                counts["skipped-no-local-code"] += 1
                self.stdout.write(
                    "SKIP no-local-tax-code "
                    f"product_pk={product.pk} stripe_product={product.external_product_id}"
                )
                continue

            attempted_change = False
            try:
                stripe_product = stripe.Product.retrieve(product.external_product_id)
                counts["examined"] += 1
                current_tax_code = _stripe_tax_code(stripe_product)

                if current_tax_code == local_tax_code:
                    self.stdout.write(
                        "OK tax-code-matches "
                        f"product_pk={product.pk} stripe_product={product.external_product_id} "
                        f"tax_code={local_tax_code}"
                    )
                    continue

                counts[changed_label] += 1
                if apply:
                    attempted_change = True
                    stripe.Product.modify(
                        product.external_product_id,
                        tax_code=local_tax_code,
                    )
                self.stdout.write(
                    f"{mode} {changed_label} "
                    f"product_pk={product.pk} stripe_product={product.external_product_id} "
                    f"stripe_tax_code={current_tax_code or '<default>'} "
                    f"local_tax_code={local_tax_code}"
                )
            except stripe.StripeError as error:
                counts["errored"] += 1
                if attempted_change:
                    self.stderr.write(
                        "APPLY failed-change "
                        f"product_pk={product.pk} stripe_product={product.external_product_id} "
                        f"local_tax_code={local_tax_code}"
                    )
                logger.error(
                    "Failed to reconcile Stripe Product tax_code for product_pk=%s stripe_product=%s error=%s",
                    product.pk,
                    product.external_product_id,
                    error,
                )
                self.stderr.write(
                    "ERROR stripe-product-tax-code "
                    f"product_pk={product.pk} stripe_product={product.external_product_id} "
                    f"error={error}"
                )

        self.stdout.write(
            "SUMMARY "
            f"examined={counts['examined']} "
            f"{changed_label}={counts[changed_label]} "
            f"skipped-no-external-id={counts['skipped-no-external-id']} "
            f"skipped-no-local-code={counts['skipped-no-local-code']} "
            f"errored={counts['errored']}"
        )

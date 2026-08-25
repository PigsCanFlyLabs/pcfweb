"""Attach every extra picture a product has on disk as a ProductImage row.

WHAT IT GRABS
-------------
The images on this site live in the sibling ``pcfweb-assets`` checkout and
arrive in the static tree as ``assets/images/...``; a product names its
primary one in ``image_name``. An extra picture of the same product is a file
that sits beside that one and starts with the same stem:

    book_covers/high_performance_spark_2ed.jpg          <- primary
    book_covers/high_performance_spark_2ed_back.jpg     <- grabbed
    book_covers/high_performance_spark_2ed_spread.jpg   <- grabbed

Matching on the stem rather than on a hand-written list is what makes this
worth running more than once: dropping a new ``..._back.jpg`` into the assets
repository is all it takes, with nothing here to edit. The primary pod runs
this on every startup (scripts/start-server.sh, right after seed_products),
so the next deploy attaches the new file everywhere -- including on a fresh
database, whose ProductImage table starts empty.

The stem rule alone is not enough, and the catalogue already contains the
counter-example that proves it::

    book_covers/high_performance_spark.jpg      <- 1st edition (pk 101)
    book_covers/high_performance_spark_2ed.jpg  <- 2nd edition (pk 108)

The second extends the first's stem but is a different book's cover, and
attaching it would put the 2nd edition's artwork on the 1st edition's listing
-- a misrepresented offer in the feed, not merely an untidy product page. So
any file that is some product's own ``image_name`` is never treated as an
extra picture of another product.

WHY IT REFUSES SOME FILES
-------------------------
Assets are stored in Git LFS, and a checkout without LFS materialised leaves
~130-byte *pointer files* with real image names. Those sail through any
existence check, and the site then ships with broken images -- the failure the
assets README calls out as completely silent. So a candidate is opened as an
image before it is attached, and one that will not open is reported and
skipped rather than written into the feed. A pointer file becoming a
``<g:additional_image_link>`` is a broken image URL sent to Google, which is
worse than not sending one.

IDEMPOTENT
----------
Re-running attaches nothing it has already attached: rows are keyed on
(product, image_name). It never touches the primary image, and it never
deletes -- an image attached by hand in the admin survives a run.
"""

from pathlib import Path
from typing import List, Optional, Set

from django.conf import settings
from django.core.management.base import BaseCommand

from main.models import Product, ProductImage


# Where image_name is rooted, matching Product.get_image_url().
IMAGE_PREFIX = "assets/images"

# Openable by Pillow and accepted by Google. WebP and TIFF are in Google's
# list too, but the originals/ tree keeps .webp masters that are not web
# assets, so the set here is the one the site actually serves.
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".bmp"}


def image_root() -> Optional[Path]:
    """The collected static tree's images directory, if there is one."""
    static_root = getattr(settings, "STATIC_ROOT", None)
    if not static_root:
        return None
    root = Path(static_root) / IMAGE_PREFIX
    return root if root.is_dir() else None


def is_a_real_image(path: Path) -> bool:
    """Whether *path* is an image, as opposed to an LFS pointer stub."""
    try:
        from PIL import Image

        with Image.open(path) as handle:
            handle.verify()
        return True
    except Exception:
        return False


def candidates_for(primary: str, root: Path,
                   claimed: Optional[Set[str]] = None) -> List[Path]:
    """Files that look like more pictures of the product owning *primary*.

    Siblings of the primary image whose name extends its stem. Two kinds of
    file are excluded:

    * the primary image itself, which never becomes an extra row; and
    * anything in *claimed* -- the ``image_name`` of any product in the
      catalogue. ``high_performance_spark_2ed.jpg`` extends
      ``high_performance_spark``'s stem while being the next edition's cover,
      so without this the 1st edition's listing would carry the 2nd
      edition's artwork.
    """
    primary_path = root / primary
    directory = primary_path.parent
    if not directory.is_dir():
        return []
    claimed = claimed or set()
    stem = primary_path.stem
    found = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        if path.stem == stem or not path.stem.startswith(f"{stem}_"):
            continue
        if str(path.relative_to(root)) in claimed:
            continue
        found.append(path)
    return found


class Command(BaseCommand):
    help = "Attach extra on-disk pictures to products as ProductImage rows."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be attached without writing anything.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        root = image_root()
        if root is None:
            # Not an error: CI and a fresh checkout both run without the
            # sibling assets tree, and a command that failed there would fail
            # the build over an optional enrichment.
            self.stdout.write(
                "No collected image tree at STATIC_ROOT/assets/images; "
                "run collectstatic with the pcfweb-assets checkout present. "
                "Nothing to do.")
            return

        attached = skipped = 0
        # Every cover the catalogue already claims as some product's primary
        # image. Collected before the loop so that a product processed early
        # cannot adopt a cover belonging to one processed later.
        claimed = set(
            Product.objects.exclude(image_name="")
            .values_list("image_name", flat=True))
        for product in Product.objects.exclude(image_name="").order_by("pk"):
            for path in candidates_for(product.image_name, root, claimed):
                name = str(path.relative_to(root))
                if not is_a_real_image(path):
                    skipped += 1
                    self.stderr.write(
                        f"  ! {name} is not a readable image (an "
                        "unmaterialised Git LFS pointer?); skipped")
                    continue
                if ProductImage.objects.filter(
                        product=product, image_name=name).exists():
                    continue
                attached += 1
                self.stdout.write(f"  + {product.name}: {name}")
                if not dry_run:
                    # The honest generic: the file could be a back cover or
                    # an interior page, and this command cannot tell. Rows
                    # are never updated on a re-run, so a better description
                    # typed into the admin survives.
                    #
                    # Bounded to the column, because Product.name's limit
                    # plus this prefix exceeds alt_text's limit: on
                    # PostgreSQL an over-long value is not truncated but
                    # rejected, and the exception would abort the command
                    # with this product's remaining images -- and every
                    # later product's -- left unattached.
                    alt_limit = ProductImage._meta.get_field(
                        "alt_text").max_length
                    ProductImage.objects.create(
                        product=product,
                        image_name=name,
                        position=ProductImage.objects.filter(
                            product=product).count(),
                        alt_text=(
                            f"Additional picture of "
                            f"{product.name}")[:alt_limit],
                    )

        verb = "would attach" if dry_run else "attached"
        self.stdout.write(f"{verb} {attached} image(s); skipped {skipped}.")

"""Materialise every static-sourced thumbnail into STATIC_ROOT at build time.

WHY THIS EXISTS
---------------
easy-thumbnails generates lazily: the first request that needs a thumbnail
builds it and writes it to ``thumbnail_default_storage``. In the cluster that
made each thumbnail a per-pod, per-boot artifact -- ``deploy.yaml`` mounts no
volume, so the directory it landed in is the container's own writable layer,
wiped on restart and invisible to the other two ``web`` replicas. The page
render and the browser's follow-up request for the image are independently
load-balanced, so pod A would write the file and emit the URL while pods B and
C answered that URL with 404. Cloudflare cached the 404 for four hours, which
turned a race into a stable, every-visitor outage for whichever covers lost it.
See the long note beside STORAGES in pigscanfly/settings.py.

Pointing the storage alias at STATIC_ROOT is only half the fix: it puts the
files in the tree the Dockerfile COPYs, but nothing puts them *there* until
some pod generates one. This command is the other half. Run after
collectstatic and before ``docker buildx build``, it leaves a real file on
disk for every thumbnail the site asks for, so the image ships with all of
them and no pod ever generates one at request time.

FIXTURE-DRIVEN, NOT DATABASE-DRIVEN
-----------------------------------
The catalogue comes from ``main/fixtures/initial_products.yaml``, not from the
database, because this runs on a developer's machine during build.sh where the
local sqlite file is routinely empty or stale. Reading the database there would
make the command generate nothing, report success, and ship an image with no
thumbnails in it -- the exact failure it exists to prevent, wearing a green
tick. For the same reason it refuses to run when the fixture yields no covers.

TEMPLATE-DECLARED THUMBNAILS
----------------------------
``{% static_thumbnail %}`` sources that are template literals rather than
product covers are listed in TEMPLATE_THUMBNAILS below. That list is a second
copy of what the templates say and could drift from them; the backstop is
main/tests/test_static_thumbnails.py, which renders the real pages and fails on
any <img> whose file is not on disk. Add a line here when you add a tag there.
"""

from __future__ import annotations

import os
from typing import Any, Iterable, List, Set, Tuple

import yaml

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from main.models import Product

# (static-relative source path, (width, height)) for every {% static_thumbnail %}
# whose source is a literal in a template. Product covers are not listed here --
# they come from the fixture below.
TEMPLATE_THUMBNAILS: List[Tuple[str, Tuple[int, int]]] = [
    # templates/base.html, the masthead logo on every page.
    ("assets/logo-cropped.png", (100, 53)),
]

# Git LFS pointer sentinel, same one scripts/check-image-assets.sh looks for. A
# pointer would fail generation anyway, but saying so beats "not an image".
LFS_SENTINEL = b"version https://git-lfs.github.com/spec/v1"


def _cover_names(fixture_path: str) -> List[str]:
    """Every distinct image_name in the product fixture, in a stable order."""
    with open(fixture_path, "rb") as fh:
        entries = yaml.safe_load(fh) or []
    if not isinstance(entries, list):
        raise CommandError(f"fixture root must be a list: {fixture_path}")

    names: Set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("model") != "main.product":
            continue
        fields = entry.get("fields") or {}
        if not isinstance(fields, dict):
            continue
        name = fields.get("image_name") or ""
        # Products with no cover fall back to Product.image; nothing static to
        # pre-generate for them.
        if name:
            names.add(str(name))
    return sorted(names)


class Command(BaseCommand):
    help = (
        "Generate every static-sourced thumbnail into STATIC_ROOT so the "
        "image ships with them and no pod generates one at request time.")

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--fixture",
            default=os.path.join(
                settings.BASE_DIR, "main", "fixtures",
                "initial_products.yaml"),
            help="Product fixture to read cover names from.")
        parser.add_argument(
            "--check",
            action="store_true",
            help=(
                "Verify every thumbnail is already on disk without generating "
                "anything, and exit non-zero if one is not. This is the seal "
                "on the artifact: run it after the generating pass, so it "
                "answers 'is the file the image will ship actually there?' "
                "rather than making it there. A check that generates on "
                "demand can never fail, which is the same as not having one."))
        parser.add_argument(
            "--static-root",
            default=None,
            help=(
                "Tree --check inspects. Defaults to STATIC_ROOT; tests point "
                "it at a temporary directory to prove the check can fail. "
                "Generation always writes through the configured storage and "
                "ignores this."))

    def handle(self, *args: Any, **options: Any) -> None:
        fixture_path = options["fixture"]
        check_only: bool = options["check"]
        # str(): django-stubs types STATIC_ROOT as str | None.
        static_root: str = options["static_root"] or str(settings.STATIC_ROOT)

        targets = list(iter_expected_thumbnails(fixture_path))
        # iter_expected_thumbnails always appends TEMPLATE_THUMBNAILS, so
        # count the covers specifically -- otherwise a fixture that lost every
        # product would still look like it had work to do.
        covers = [t for t in targets if t[0].startswith("assets/images/")]
        if not covers:
            # See the module docstring: a silent zero here is how an image
            # ships with no thumbnails and a passing build.
            raise CommandError(
                f"no product covers found in {fixture_path}. Refusing to "
                "report success: that would ship an image with no "
                "pre-generated thumbnails, which is the failure this command "
                "exists to prevent.")

        failures: List[str] = []
        done = 0
        for source, size in targets:
            try:
                if check_only:
                    self._check(static_root, source, size)
                else:
                    self.stdout.write(f"  {self._generate(source, size)}")
            except Exception as exc:
                failures.append(f"  {source} {size[0]}x{size[1]}: {exc}")
                continue
            done += 1

        if failures:
            verb = "verify" if check_only else "pre-generate"
            raise CommandError(
                "could not {} {} thumbnail(s):\n{}\n\nLooked under {}. If the "
                "sources are missing there, run collectstatic first "
                "(scripts/checks.sh does, then runs this command). If the "
                "sources are ~130 byte text stubs they are unmaterialised Git "
                "LFS pointers: run `git lfs install && git lfs pull` inside "
                "the pcfweb-assets checkout.".format(
                    verb, len(failures), "\n".join(failures), static_root))

        if check_only:
            self.stdout.write(
                f"All {done} thumbnail(s) present under {static_root}")
        else:
            self.stdout.write(
                f"Pre-generated {done} thumbnail(s) into "
                f"{settings.STATIC_ROOT}")

    def _thumbnail_names(
            self, source: str, size: Tuple[int, int]) -> List[str]:
        """The relative path(s) a request for this thumbnail may resolve to.

        Asked of easy-thumbnails rather than formatted here, so the check
        cannot pass while looking for a filename nothing requests. Naming is
        pure; this touches no disk.

        Two names, not one, and the pair is not decoration: the extension
        depends on whether the *generated* image came out transparent, which is
        not knowable from the source path. A transparent source lands as
        THUMBNAIL_TRANSPARENCY_EXTENSION (.png) while an opaque one lands as
        THUMBNAIL_EXTENSION (.jpg) -- the masthead logo is the former and every
        book cover the latter. Thumbnailer.get_existing_thumbnail() accepts
        either, so a check that demanded only the .jpg would fail the build on
        a logo that is present and correct.
        """
        thumbnailer = Product.static_thumbnailer(source)
        options = thumbnailer.get_options({"size": size})
        names = [str(thumbnailer.get_thumbnail_name(options))]
        transparent = str(
            thumbnailer.get_thumbnail_name(options, transparent=True))
        if transparent not in names:
            names.append(transparent)
        return names

    def _assert_real_image(self, path: str, description: str) -> None:
        if not os.path.isfile(path):
            raise CommandError(f"{description} is missing: {path}")
        if os.path.getsize(path) == 0:
            raise CommandError(f"{description} is empty: {path}")
        with open(path, "rb") as fh:
            if fh.read(len(LFS_SENTINEL)) == LFS_SENTINEL:
                raise CommandError(
                    f"{description} is an unmaterialised Git LFS pointer: "
                    f"{path}")

    def _check(
            self, static_root: str, source: str,
            size: Tuple[int, int]) -> None:
        """Assert the shipped tree already holds this thumbnail. No IO writes.

        This is the guard that looks at the artifact rather than at the code:
        the tree it reads is the one the Dockerfile COPYs into the image.
        """
        self._assert_real_image(
            os.path.join(static_root, source), "thumbnail source")

        candidates = self._thumbnail_names(source, size)
        present = [
            name for name in candidates
            if os.path.isfile(os.path.join(static_root, name))
        ]
        if not present:
            raise CommandError(
                "pre-generated thumbnail is missing; looked for "
                + " and ".join(
                    os.path.join(static_root, name) for name in candidates))
        for name in present:
            self._assert_real_image(
                os.path.join(static_root, name), "pre-generated thumbnail")

    def _generate(self, source: str, size: Tuple[int, int]) -> str:
        """Build one thumbnail and prove the file is really on disk."""
        # str(): django-stubs types STATIC_ROOT as str | None.
        static_root = str(settings.STATIC_ROOT)
        self._assert_real_image(
            os.path.join(static_root, source), "thumbnail source")

        thumb = Product.static_thumbnailer(source).get_thumbnail(
            {"size": size})

        # get_thumbnail() returning a URL is not proof the bytes landed: the
        # whole bug this command addresses was a URL pointing at a file that
        # was not where the next request would look for it. Check the file.
        self._assert_real_image(
            os.path.join(static_root, str(thumb.name)),
            f"thumbnail generated as {thumb.url}")
        return str(thumb.url)


def iter_expected_thumbnails(
        fixture_path: str) -> Iterable[Tuple[str, Tuple[int, int]]]:
    """Targets this command would generate, for tests to assert against."""
    for name in _cover_names(fixture_path):
        yield f"assets/images/{name}", Product.THUMB_SIZE
    yield from TEMPLATE_THUMBNAILS

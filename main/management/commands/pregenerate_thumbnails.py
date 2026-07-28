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

NO DATABASE
-----------
Generation deliberately goes through ``generate_thumbnail()`` plus an explicit
storage save, rather than the usual ``get_thumbnail()``. The only difference is
that ``get_thumbnail()`` also calls ``save_thumbnail()``, which writes rows to
the ``easy_thumbnails_source`` / ``easy_thumbnails_thumbnail`` cache tables --
and that is a dependency this command should not have:

  * Those rows land in whatever database the *build host* happens to have. In
    CI that is no database at all, so the command died with "no such table:
    easy_thumbnails_source" even for a source that was present and correct.
  * They are never consulted in production anyway. Both storages here are
    local, so ``Thumbnailer.thumbnail_exists()`` compares filesystem mtimes and
    returns the existing file without a query; the rows written on the build
    host never reach the cluster's Postgres in the first place.

So the cache write bought nothing and cost portability. Everything else --
which bytes are produced, and under which filename -- is identical, because
``get_thumbnail()`` generates through this same call.

MISSING ASSET TREE
------------------
The cover sources come from the sibling ``pcfweb-assets`` checkout, which CI
does not have (see ``--allow-absent-asset-tree`` below). That flag is scoped as
narrowly as it can be on purpose: it fires only when the cover tree is absent
*in its entirety*, never when the tree is there and something in it is wrong.
A guard that shrugs at any missing file is a guard that passes over a tree it
never inspected, which is the exact shape of the bug this command exists to
kill.

WHAT "PRESENT AND CORRECT" MEANS
--------------------------------
Existence is not the property that matters, so --check does not stop there. For
every source and every generated thumbnail it decodes the bytes through Pillow
(see _assert_real_image), because the failure that reaches a visitor is a file
the browser cannot render, and a file being at the path proves nothing about
that. Checking only os.path.isfile() would be the same bug as the missing-tree
one in miniature: a green tick over bytes nobody looked at.
"""

from __future__ import annotations

import os
from typing import Any, Iterable, List, Set, Tuple

import yaml
from PIL import Image

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

# Static-relative root of the tree that comes from the sibling pcfweb-assets
# checkout, i.e. the one thing a bare clone of *this* repo does not have.
# .gitignore excludes main/static/assets/images, and collectstatic lands it
# here. Everything outside this prefix -- the masthead logo, most obviously --
# is committed to this repository and is present in any checkout, so it is
# never covered by --allow-absent-asset-tree.
COVER_PREFIX = "assets/images/"

# Same shout as scripts/sync-local-assets.sh, for the same reason: a skipped
# check has to be impossible to mistake for a passing one when skimming a log.
BANNER = "!" * 70


def thumbnail_names(source: str, size: Tuple[int, int]) -> List[str]:
    """The relative path(s) a request for this thumbnail may resolve to.

    Asked of easy-thumbnails rather than formatted here, so the check cannot
    pass while looking for a filename nothing requests. Naming is pure; this
    touches no disk, and needs no database.

    Two names, not one, and the pair is not decoration: the extension depends
    on whether the *generated* image came out transparent, which is not
    knowable from the source path. A transparent source lands as
    THUMBNAIL_TRANSPARENCY_EXTENSION (.png) while an opaque one lands as
    THUMBNAIL_EXTENSION (.jpg) -- the masthead logo is the former and every
    book cover the latter. Thumbnailer.get_existing_thumbnail() accepts either,
    so a check that demanded only the .jpg would fail the build on a logo that
    is present and correct.

    Module-level rather than a Command method so the tests can ask the same
    question the command asks, instead of hardcoding '.290x380_q85.jpg' and
    quietly agreeing with themselves.
    """
    thumbnailer = Product.static_thumbnailer(source)
    options = thumbnailer.get_options({"size": size})
    names = [str(thumbnailer.get_thumbnail_name(options))]
    transparent = str(
        thumbnailer.get_thumbnail_name(options, transparent=True))
    if transparent not in names:
        names.append(transparent)
    return names


def _cover_tree_is_wholly_absent(static_root: str) -> bool:
    """True only when NOT ONE cover source exists under static_root.

    Deliberately all-or-nothing. "The pcfweb-assets checkout was never here"
    is a legitimate environment (CI, a fresh clone) and is distinguishable
    from "the assets are here and one of them is broken", which is a defect
    and must stay fatal. Anything short of total absence -- one cover missing,
    one an LFS pointer, one stale -- returns False and the normal enforcement
    runs over every target.
    """
    root = os.path.join(static_root, COVER_PREFIX.rstrip("/"))
    if not os.path.isdir(root):
        return True
    # A directory containing only empty subdirectories counts as absent too:
    # that is what a half-finished sync or a `rm` of the files leaves behind,
    # and there is nothing there to verify either way.
    for _dirpath, _dirnames, filenames in os.walk(root):
        if filenames:
            return False
    return True


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
        parser.add_argument(
            "--allow-absent-asset-tree",
            action="store_true",
            help=(
                "Skip the product covers -- loudly -- when the cover tree is "
                "absent in its entirety, instead of failing. For CI, which "
                "checks out this repository alone and so has no "
                "pcfweb-assets. Strictly all-or-nothing: if the tree exists "
                "at all, every cover in it is enforced exactly as without "
                "this flag, so a missing, stale, pointer-stub or unreadable "
                "cover still fails. Never pass it in build.sh -- that "
                "--check is the seal on the image that actually ships."))

    def handle(self, *args: Any, **options: Any) -> None:
        fixture_path = options["fixture"]
        check_only: bool = options["check"]
        # str(): django-stubs types STATIC_ROOT as str | None.
        static_root: str = options["static_root"] or str(settings.STATIC_ROOT)

        targets = list(iter_expected_thumbnails(fixture_path))
        # iter_expected_thumbnails always appends TEMPLATE_THUMBNAILS, so
        # count the covers specifically -- otherwise a fixture that lost every
        # product would still look like it had work to do.
        covers = [t for t in targets if t[0].startswith(COVER_PREFIX)]
        if not covers:
            # See the module docstring: a silent zero here is how an image
            # ships with no thumbnails and a passing build. Checked before the
            # skip below, so "the fixture lost its covers" stays fatal even in
            # the environment that is allowed to have no cover files.
            raise CommandError(
                f"no product covers found in {fixture_path}. Refusing to "
                "report success: that would ship an image with no "
                "pre-generated thumbnails, which is the failure this command "
                "exists to prevent.")

        if (options["allow_absent_asset_tree"]
                and _cover_tree_is_wholly_absent(static_root)):
            # Loud, on stderr, and it names what was not verified. A skip
            # nobody can see in the log is indistinguishable from a pass.
            self.stderr.write(BANNER)
            self.stderr.write(
                f"!! SKIPPED: {len(covers)} product cover thumbnail(s) were "
                "NOT verified.")
            self.stderr.write(BANNER)
            self.stderr.write(
                f"There is no cover tree at all under {os.path.join(static_root, COVER_PREFIX.rstrip('/'))} "
                "-- not one file. That is expected in CI, which checks out "
                "this repository alone and so has no sibling pcfweb-assets "
                "checkout; run scripts/sync-local-assets.sh to get one.\n"
                "\n"
                "Nothing about the covers has been checked here. build.sh "
                "runs `pregenerate_thumbnails --check` WITHOUT this flag "
                "before it builds the image, and that is what keeps a "
                "thumbnail-less artifact from reaching production.\n")
            targets = [t for t in targets if not t[0].startswith(COVER_PREFIX)]

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

    def _assert_real_image(self, path: str, description: str) -> None:
        """Fail unless `path` holds bytes that actually decode as an image.

        Existence is not the property that matters. The thing that breaks
        production is a file a browser cannot render, and "a file is at that
        path" does not imply that -- which is how a plain-text or truncated
        cover used to walk straight through this guard and get sealed into the
        image. A guard that never looks at the bytes is a guard that passes
        over a tree it never really inspected.

        The cheap structural checks come first so that their diagnosis wins.
        Missing, empty and unmaterialised-LFS-pointer each name their own
        specific cause and carry their own remedy; only bytes that are none of
        those reach the decoder and get reported as unreadable. Ordering is
        load-bearing: an LFS pointer is also undecodable, and "run git lfs
        pull" is a far more useful thing to say than "not an image".

        load() is the load-bearing call, not verify(), and the difference is
        not academic. verify() checks structure -- magic bytes, headers, PNG
        chunk CRCs -- but explicitly does NOT decode pixel data, so a JPEG
        whose header is intact and whose scan data was cut off PASSES it. That
        was measured, not assumed: a JPEG truncated to 50%, 75% and 90% passes
        verify() at every one of those lengths and fails load() at all three
        ("image file is truncated"). Truncation is a live risk here rather than
        a hypothetical, because these covers arrive from a sibling
        pcfweb-assets checkout over Git LFS: an interrupted `git lfs pull`, a
        half-written copy or a killed rsync all leave exactly that shape. A
        verify()-only guard would be the same class of bug as the one this
        module exists to kill, just further in.

        verify() is kept in front of it anyway, and it is worth being honest
        that it is not catching anything load() would miss -- in testing load()
        was a strict superset. It earns its two lines on diagnosis: for
        structural damage it names the specific defect ("broken PNG file (bad
        header checksum in b'IDAT')") where load() only offers "broken data
        stream when reading image file". On a guard whose whole job is telling
        an operator what is wrong with the artifact, that is worth paying for --
        and it is nearly free, because verify() reads headers rather than
        pixels: 0.4ms for the whole tree against load()'s 278ms.

        Two passes rather than one because verify() consumes the underlying
        file object and leaves the instance unusable, so load() needs a fresh
        open(). Whole decode pass over the real tree -- 12 files, 3.5MB, the
        covers up to 2100x2756 -- measures ~0.28s, which is why this is done
        unconditionally rather than behind a flag.
        """
        if not os.path.isfile(path):
            raise CommandError(f"{description} is missing: {path}")
        if os.path.getsize(path) == 0:
            raise CommandError(f"{description} is empty: {path}")
        with open(path, "rb") as fh:
            if fh.read(len(LFS_SENTINEL)) == LFS_SENTINEL:
                raise CommandError(
                    f"{description} is an unmaterialised Git LFS pointer: "
                    f"{path}")

        try:
            with Image.open(path) as probe:
                probe.verify()
        except Exception as exc:
            raise CommandError(
                f"{description} is not a readable image: {path} "
                f"({type(exc).__name__}: {exc}). The file is present and is "
                "not an LFS pointer, so something wrote non-image bytes "
                "there.") from exc

        # Fresh open: verify() above consumed the file object.
        try:
            with Image.open(path) as probe:
                probe.load()
        except Exception as exc:
            raise CommandError(
                f"{description} is a truncated or undecodable image: {path} "
                f"({type(exc).__name__}: {exc}). Its header parsed but the "
                "image data did not decode, which is what a half-finished "
                "transfer leaves behind; re-fetch it (`git lfs pull` in the "
                "pcfweb-assets checkout) and re-run "
                "`manage.py pregenerate_thumbnails`.") from exc

    def _check(
            self, static_root: str, source: str,
            size: Tuple[int, int]) -> None:
        """Assert the shipped tree already holds this thumbnail. No IO writes.

        This is the guard that looks at the artifact rather than at the code:
        the tree it reads is the one the Dockerfile COPYs into the image.
        """
        source_path = os.path.join(static_root, source)
        self._assert_real_image(source_path, "thumbnail source")

        candidates = thumbnail_names(source, size)
        # Existence -- and only existence -- decides which of the two candidate
        # names a request resolves to, because that is the question
        # Thumbnailer.get_existing_thumbnail() asks. So it stays the selection
        # predicate here. It is emphatically NOT the verification: every name it
        # selects then goes through _assert_real_image below, which decodes it.
        # Selecting on decodability instead would be worse than useless -- a
        # corrupt .jpg would be quietly passed over as "not present" and
        # reported as a missing thumbnail, or masked entirely by a healthy .png
        # sibling, while the corrupt file stayed in the image and kept being
        # served.
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
            thumb_path = os.path.join(static_root, name)
            # Decodes the thumbnail, not just its existence: a corrupt
            # thumbnail is a 'broken image' icon on the live site, and it is
            # reachable, so every present candidate has to be readable.
            self._assert_real_image(thumb_path, "pre-generated thumbnail")
            # Staleness is the failure that survives every other check: the
            # file is there, it is a real image, and it is a picture of the
            # cover as it used to be. Same comparison Thumbnailer
            # .thumbnail_exists() makes for local storages, so this is exactly
            # the condition under which a pod would decide to regenerate at
            # request time -- which is the thing that must never happen in the
            # cluster.
            if os.path.getmtime(source_path) > os.path.getmtime(thumb_path):
                raise CommandError(
                    f"pre-generated thumbnail is stale: {thumb_path} is older "
                    f"than its source {source_path}, so every pod would "
                    "regenerate it at request time. Re-run "
                    "`manage.py pregenerate_thumbnails`.")

    def _generate(self, source: str, size: Tuple[int, int]) -> str:
        """Build one thumbnail and prove the file is really on disk."""
        # str(): django-stubs types STATIC_ROOT as str | None.
        static_root = str(settings.STATIC_ROOT)
        self._assert_real_image(
            os.path.join(static_root, source), "thumbnail source")

        thumbnailer = Product.static_thumbnailer(source)

        # generate_thumbnail() + an explicit save, rather than get_thumbnail().
        # See "NO DATABASE" in the module docstring: get_thumbnail() would also
        # write easy_thumbnails cache rows, which CI has no tables for and
        # production never reads. The bytes and the filename are identical --
        # get_thumbnail() produces them through this same call.
        thumb = thumbnailer.generate_thumbnail({"size": size})
        storage = thumbnailer.thumbnail_storage

        # Delete first. Django's Storage.save() does not overwrite: it picks a
        # free name, so re-running would quietly scatter
        # <name>_<random>.jpg files that nothing ever requests while the real
        # one stayed stale. easy-thumbnails' own save_thumbnail() deletes for
        # this reason too.
        try:
            storage.delete(thumb.name)
        except Exception:
            pass
        saved_name = storage.save(thumb.name, thumb)
        if saved_name != thumb.name:
            raise CommandError(
                f"storage renamed the thumbnail from {thumb.name} to "
                f"{saved_name}; something already occupies that path and the "
                "file the templates ask for was not written")

        # A returned name is not proof the bytes landed: the whole bug this
        # command addresses was a URL pointing at a file that was not where
        # the next request would look for it. Check the file.
        written = os.path.join(static_root, str(saved_name))
        self._assert_real_image(written, f"thumbnail generated as {saved_name}")
        return str(storage.url(saved_name))


def iter_expected_thumbnails(
        fixture_path: str) -> Iterable[Tuple[str, Tuple[int, int]]]:
    """Targets this command would generate, for tests to assert against."""
    for name in _cover_names(fixture_path):
        yield f"assets/images/{name}", Product.THUMB_SIZE
    yield from TEMPLATE_THUMBNAILS

"""Thumbnails must be shipped artifacts, not per-pod runtime ones.

THE BUG THESE PIN
-----------------
easy-thumbnails resolves its output storage through the
THUMBNAIL_DEFAULT_STORAGE_ALIAS entry in STORAGES and, finding none, used to
fall back to `default_storage` -- MEDIA_ROOT. MEDIA_ROOT is /opt/app/media in
the image and deploy.yaml mounts no volume there, so it was the container's own
writable layer: wiped on restart, and private to each of the three `web`
replicas. Generation is lazy, so a thumbnail existed only on whichever pod had
rendered the page. The page render and the browser's follow-up request for the
image are load-balanced independently, so pod A emitted /media/<name> while
pods B and C answered that URL with 404 -- and Cloudflare cached the 404 for
four hours (Cache-Control: max-age=14400), turning a per-request race into a
stable outage for whichever covers lost it. That is why it looked like a fixed
set of broken books rather than something flaky, and why it was invisible under
run_local.sh: one process, one filesystem, no CDN, so the process that writes
the thumbnail is the process that serves it.

WHY THESE TESTS ARE NOT DECORATION
----------------------------------
The load-bearing assertion is not "the file exists" -- under a single test
process with a writable STATIC_ROOT, rendering the page *creates* the file, so
that check passes no matter what. It is:

  * no rendered <img> may point under MEDIA_URL (fails outright before the
    STORAGES fix, when every cover rendered as /media/...), and
  * rendering the real pages must not CREATE any thumbnail, because the build
    is supposed to have made all of them already. The tree is snapshotted after
    pre-generation and compared after rendering, so a template that asks for a
    size the pre-generator does not know about shows up as a new file and fails
    here -- which is the drift that would put lazy generation back in prod.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from typing import Set

from PIL import Image

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from main.management.commands.pregenerate_thumbnails import (
    COVER_PREFIX, _cover_tree_is_wholly_absent, iter_expected_thumbnails,
    thumbnail_names)
from main.models import Product
from main.tests.base import REPO_ROOT

IMG_SRC = re.compile(r"<img[^>]+src=\"([^\"]+)\"")

# Pages that render product covers. /products is the storefront listing and /
# carries both the featured book and the highlight carousels.
COVER_PAGES = ["/", "/products"]

FIXTURE = Path(settings.BASE_DIR) / "main" / "fixtures" / (
    "initial_products.yaml")

# The book covers live in the sibling pcfweb-assets checkout, which CI does not
# have -- it checks out this repository alone. A handful of assertions below
# genuinely need the real collected artifact and cannot be faked; they are
# skipped, visibly, when it is absent, rather than quietly weakened into
# something that passes everywhere and proves nothing. Everything that CAN be
# exercised hermetically is, in AbsentAssetTreeSkipTest below, so the command's
# own logic is still covered on a bare checkout.
REAL_COVERS_PRESENT = not _cover_tree_is_wholly_absent(
    str(settings.STATIC_ROOT))
NEEDS_REAL_COVERS = (
    "needs the pcfweb-assets covers under STATIC_ROOT; run "
    "scripts/sync-local-assets.sh and collectstatic")


def _snapshot(root: str) -> Set[str]:
    return {
        os.path.join(dirpath, name)
        for dirpath, _dirs, files in os.walk(root)
        for name in files
    }


def _write_image(path: Path, size=(400, 500)) -> None:
    """A real, decodable image at `path`. Parents created."""
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (120, 90, 60)).save(path, "JPEG")


def _build_static_tree(root: Path, *, covers=True, thumbs=True) -> None:
    """A synthetic STATIC_ROOT shaped like the real collected one.

    Lets the skip logic be tested on a bare checkout: what the command cares
    about is which files exist at which paths, not what they are pictures of.
    Thumbnail filenames come from thumbnail_names(), the same helper the
    command uses, so this cannot drift into agreeing with itself.
    """
    for source, size in iter_expected_thumbnails(str(FIXTURE)):
        if source.startswith(COVER_PREFIX) and not covers:
            continue
        _write_image(root / source)
        if thumbs:
            # Written after the source, so it is the newer file and the
            # staleness check is satisfied.
            _write_image(root / thumbnail_names(source, size)[0], (290, 380))


class RenderedThumbnailTest(TestCase):
    fixtures = ["initial_products"]

    def _srcs(self):
        srcs = []
        for page in COVER_PAGES:
            response = self.client.get(page)
            self.assertEqual(
                response.status_code, 200,
                f"{page} did not render; the rest of this test would be "
                "vacuous")
            srcs += IMG_SRC.findall(response.content.decode())
        return srcs

    def test_no_rendered_image_is_served_from_media(self):
        """The regression itself.

        Before the STORAGES fix every cover rendered as
        /media/assets/images/book_covers/<name>.290x380_q85.jpg -- a path that
        only existed on the pod that happened to render the page.
        """
        srcs = self._srcs()

        covers = [s for s in srcs if "book_covers/" in s]
        self.assertGreaterEqual(
            len(covers), 4,
            "expected the fixture's book covers on these pages; without them "
            f"this test asserts nothing. Got: {srcs}")

        offenders = [s for s in srcs if s.startswith(settings.MEDIA_URL)]
        self.assertEqual(
            offenders, [],
            "these images are served out of MEDIA_ROOT, which is pod-local "
            "ephemeral storage with no volume behind it -- the replica that "
            "answers the image request is not the one that rendered the page, "
            "so it 404s. They belong in STATIC_ROOT, which ships in the image.")

    def test_every_rendered_thumbnail_is_under_static_url(self):
        for src in self._srcs():
            if src.startswith(("http://", "https://", "data:")):
                continue
            self.assertTrue(
                src.startswith(settings.STATIC_URL),
                f"{src} is not served from the static tree that ships in the "
                "image")

    @unittest.skipUnless(REAL_COVERS_PRESENT, NEEDS_REAL_COVERS)
    def test_rendering_generates_nothing_the_build_did_not(self):
        """Pre-generation must cover everything the templates actually ask for.

        This is the drift guard. If a template starts requesting a size the
        pre-generator does not know about, the render below materialises it
        lazily -- exactly the per-pod runtime generation that broke
        production -- and it shows up here as a file that was not in the
        snapshot.

        Needs the real covers: the point is that rendering the REAL pages
        creates nothing, and the real pages read the real STATIC_ROOT. A
        synthetic tree elsewhere would not be the thing under test.
        """
        call_command(
            "pregenerate_thumbnails", stdout=StringIO(), stderr=StringIO())
        before = _snapshot(settings.STATIC_ROOT)

        self._srcs()

        created = sorted(_snapshot(settings.STATIC_ROOT) - before)
        self.assertEqual(
            created, [],
            "rendering created these files, so the build would not have "
            "shipped them and each pod would generate its own copy at request "
            "time. Teach pregenerate_thumbnails about them (TEMPLATE_THUMBNAILS "
            f"or Product.THUMB_SIZE): {created}")


class PregenerateCommandTest(SimpleTestCase):
    """The build-path guard: does it look at the shipped tree, and can it fail?"""

    @unittest.skipUnless(REAL_COVERS_PRESENT, NEEDS_REAL_COVERS)
    def test_check_passes_against_the_real_collected_tree(self):
        call_command(
            "pregenerate_thumbnails", stdout=StringIO(), stderr=StringIO())
        out = StringIO()
        call_command(
            "pregenerate_thumbnails", "--check", stdout=out, stderr=StringIO())
        self.assertIn("present under", out.getvalue())

    def test_check_fails_when_the_shipped_tree_lacks_a_thumbnail(self):
        """Proof the guard is not decoration.

        A tree holding the SOURCE images but none of the generated thumbnails
        is exactly the artifact the build used to produce, and the check must
        refuse it. Synthetic sources rather than copies of the real ones, so
        this runs on a bare checkout too -- --check never decodes an image, it
        asks whether a real file is at the path a request would ask for.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _build_static_tree(Path(tmp), thumbs=False)

            with self.assertRaises(CommandError) as caught:
                call_command(
                    "pregenerate_thumbnails", "--check", "--static-root", tmp,
                    stdout=StringIO(), stderr=StringIO())

        self.assertIn("pre-generated thumbnail is missing", str(caught.exception))

    def test_check_fails_on_an_lfs_pointer_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            for source, _size in iter_expected_thumbnails(str(FIXTURE)):
                dest = Path(tmp) / source
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(
                    "version https://git-lfs.github.com/spec/v1\n"
                    "oid sha256:abc123\nsize 123456\n")

            with self.assertRaises(CommandError) as caught:
                call_command(
                    "pregenerate_thumbnails", "--check", "--static-root", tmp,
                    stdout=StringIO(), stderr=StringIO())

        self.assertIn("Git LFS pointer", str(caught.exception))

    def test_empty_fixture_is_refused_rather_than_reported_as_success(self):
        """A build host with no catalogue must not ship a thumbnail-less image."""
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty.yaml"
            empty.write_text("[]\n")

            with self.assertRaises(CommandError) as caught:
                call_command(
                    "pregenerate_thumbnails", "--fixture", str(empty),
                    stdout=StringIO(), stderr=StringIO())

        self.assertIn("no product covers found", str(caught.exception))

    def test_expected_targets_cover_every_fixture_cover_and_the_logo(self):
        targets = dict(iter_expected_thumbnails(str(FIXTURE)))

        self.assertIn("assets/logo-cropped.png", targets)
        for name in ["learning_spark_1ed", "high_performance_spark",
                     "kubeflow_for_ml", "scaling_python_with_ray",
                     "distributed_computing_4_kids"]:
            source = f"assets/images/book_covers/{name}.jpg"
            self.assertIn(source, targets)
            self.assertEqual(targets[source], Product.THUMB_SIZE)


class AbsentAssetTreeSkipTest(SimpleTestCase):
    """--allow-absent-asset-tree must be narrow enough to be worth having.

    scripts/checks.sh passes this flag because CI checks out this repository
    alone and the covers live in the sibling pcfweb-assets checkout. That is a
    hole in a guard, so it is bounded on exactly one axis: the ENTIRE cover
    tree being absent. The tests here are the proof of that boundary -- a
    "skip when anything is missing" flag would recreate the very defect this
    module exists to pin, a guard reporting success over a tree it never
    looked at.

    Hermetic: synthetic trees under --static-root, so these run identically on
    a developer's machine and on a bare CI checkout.
    """

    def _check(self, root, *flags):
        call_command(
            "pregenerate_thumbnails", "--check", "--static-root", str(root),
            *flags, stdout=StringIO(), stderr=StringIO())

    def test_complete_tree_passes(self):
        """Positive control.

        Without this, the failures asserted below could come from a malformed
        synthetic tree rather than from the mutation each one makes, and the
        whole class would prove nothing.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _build_static_tree(Path(tmp))
            self._check(tmp, "--allow-absent-asset-tree")

    def test_skip_does_not_mask_a_missing_thumbnail(self):
        """THE assertion this flag has to earn.

        Sources present, one thumbnail deleted: the tree is not wholly absent,
        so the flag must not engage and the check must still fail. This is the
        difference between "CI has no assets" and "the build produced a broken
        artifact", and conflating them is how a thumbnail-less image ships.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _build_static_tree(Path(tmp))
            victim = Path(tmp) / thumbnail_names(
                f"{COVER_PREFIX}book_covers/learning_spark_1ed.jpg",
                Product.THUMB_SIZE)[0]
            self.assertTrue(victim.is_file(), "fixture drift: nothing deleted")
            victim.unlink()

            with self.assertRaises(CommandError) as caught:
                self._check(tmp, "--allow-absent-asset-tree")

        self.assertIn(
            "pre-generated thumbnail is missing", str(caught.exception))
        self.assertIn("learning_spark_1ed", str(caught.exception))

    def test_skip_does_not_mask_a_single_missing_cover_source(self):
        """One cover gone is a defect; only ALL of them gone is the CI case."""
        with tempfile.TemporaryDirectory() as tmp:
            _build_static_tree(Path(tmp))
            (Path(tmp) / COVER_PREFIX
             / "book_covers/kubeflow_for_ml.jpg").unlink()

            with self.assertRaises(CommandError) as caught:
                self._check(tmp, "--allow-absent-asset-tree")

        self.assertIn("thumbnail source is missing", str(caught.exception))

    def test_skip_does_not_mask_an_lfs_pointer(self):
        """A ~130 byte text stub is present-but-wrong, so it must still fail."""
        with tempfile.TemporaryDirectory() as tmp:
            _build_static_tree(Path(tmp))
            (Path(tmp) / COVER_PREFIX
             / "book_covers/high_performance_spark.jpg").write_text(
                "version https://git-lfs.github.com/spec/v1\n"
                "oid sha256:abc123\nsize 123456\n")

            with self.assertRaises(CommandError) as caught:
                self._check(tmp, "--allow-absent-asset-tree")

        self.assertIn("Git LFS pointer", str(caught.exception))

    def test_skip_does_not_mask_a_stale_thumbnail(self):
        """Present, a real image, and a picture of the old cover.

        The one failure that survives every existence check. A pod finding a
        thumbnail older than its source regenerates it at request time, which
        is precisely the behaviour the pre-generation exists to remove.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _build_static_tree(Path(tmp))
            source = (Path(tmp) / COVER_PREFIX
                      / "book_covers/scaling_python_with_ray.jpg")
            thumb = Path(tmp) / thumbnail_names(
                f"{COVER_PREFIX}book_covers/scaling_python_with_ray.jpg",
                Product.THUMB_SIZE)[0]
            # Age the thumbnail rather than touching the source: same relation,
            # and it cannot be confused with the file simply being rewritten.
            old = os.path.getmtime(source) - 60
            os.utime(thumb, (old, old))

            with self.assertRaises(CommandError) as caught:
                self._check(tmp, "--allow-absent-asset-tree")

        self.assertIn("is stale", str(caught.exception))

    def test_wholly_absent_tree_is_skipped_only_with_the_flag(self):
        """The CI case, and the proof the flag is doing the work.

        Same tree twice: it fails without the flag and passes with it, so the
        pass is attributable to the flag and not to the tree being acceptable.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _build_static_tree(Path(tmp), covers=False)

            with self.assertRaises(CommandError) as caught:
                self._check(tmp)
            self.assertIn("thumbnail source is missing", str(caught.exception))

            self._check(tmp, "--allow-absent-asset-tree")

    def test_the_skip_announces_itself(self):
        """A skip nobody sees is indistinguishable from a pass."""
        with tempfile.TemporaryDirectory() as tmp:
            _build_static_tree(Path(tmp), covers=False)
            err = StringIO()
            call_command(
                "pregenerate_thumbnails", "--check", "--static-root", tmp,
                "--allow-absent-asset-tree",
                stdout=StringIO(), stderr=err)

        message = err.getvalue()
        self.assertIn("SKIPPED", message)
        self.assertIn("NOT verified", message)
        # Says how many went unchecked, and points at what still enforces them.
        self.assertIn("5 product cover", message)
        self.assertIn("build.sh", message)

    def test_an_empty_directory_counts_as_absent(self):
        """What a half-finished sync or an `rm` of the files leaves behind."""
        with tempfile.TemporaryDirectory() as tmp:
            _build_static_tree(Path(tmp), covers=False)
            (Path(tmp) / COVER_PREFIX / "book_covers").mkdir(parents=True)

            self.assertTrue(_cover_tree_is_wholly_absent(tmp))
            self._check(tmp, "--allow-absent-asset-tree")

    def test_the_flag_never_excuses_a_fixture_with_no_covers(self):
        """The vacuity guard outranks the skip.

        "No covers anywhere" must stay fatal even in the environment that is
        allowed to have no cover files, or a fixture that lost every product
        would sail through CI.
        """
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty.yaml"
            empty.write_text("[]\n")

            with self.assertRaises(CommandError) as caught:
                call_command(
                    "pregenerate_thumbnails", "--allow-absent-asset-tree",
                    "--fixture", str(empty),
                    stdout=StringIO(), stderr=StringIO())

        self.assertIn("no product covers found", str(caught.exception))


class BuildSealTest(SimpleTestCase):
    """build.sh's --check must stay unconditional. It is the actual seal."""

    def test_build_sh_does_not_pass_the_skip_flag(self):
        build = (REPO_ROOT / "build.sh").read_text()
        self.assertIn("./manage.py pregenerate_thumbnails --check", build)
        self.assertNotIn(
            "pregenerate_thumbnails --check --allow-absent-asset-tree", build)
        self.assertNotIn(
            "pregenerate_thumbnails --allow-absent-asset-tree --check", build)

    def test_checks_sh_passes_the_skip_flag_and_not_a_bare_run(self):
        """CI's invocation is the tolerant one; that is the only tolerant one."""
        checks = (REPO_ROOT / "scripts" / "checks.sh").read_text()
        self.assertIn(
            "./manage.py pregenerate_thumbnails --allow-absent-asset-tree",
            checks)


class ThumbnailPackagingTest(SimpleTestCase):
    """The pre-generated files are only a fix if they reach the image.

    Everything above runs in one process against a writable STATIC_ROOT, which
    is precisely the path that never reproduced the bug. These four assertions
    are about the artifact instead: they read the Dockerfile, .dockerignore and
    build.sh off disk and pin the steps that carry static/ into the image and
    refuse to seal one without the thumbnails in it.
    """

    ROOT = REPO_ROOT

    def test_the_dockerfile_copies_the_static_tree_in(self):
        dockerfile = (self.ROOT / "Dockerfile").read_text()
        self.assertIn(
            "COPY --chown=www-data:www-data static /opt/app/static",
            dockerfile)

    def test_the_static_tree_is_not_dockerignored(self):
        # The one-line change that would ship an image with no thumbnails and
        # put lazy per-pod generation straight back.
        ignored = [line.strip() for line
                   in (self.ROOT / ".dockerignore").read_text().splitlines()]
        for entry in ["static", "static/", "static/*"]:
            self.assertNotIn(entry, ignored)

    def test_the_build_pregenerates_before_it_tests(self):
        checks = (self.ROOT / "scripts" / "checks.sh").read_text()
        self.assertIn("./manage.py pregenerate_thumbnails", checks)
        self.assertLess(
            checks.index("collectstatic"),
            checks.index("pregenerate_thumbnails"),
            "collectstatic must put the sources in STATIC_ROOT before "
            "anything tries to thumbnail them")

    def test_the_build_verifies_the_tree_before_pushing_the_image(self):
        build = (self.ROOT / "build.sh").read_text()
        self.assertIn("./manage.py pregenerate_thumbnails --check", build)
        self.assertLess(
            build.index("pregenerate_thumbnails --check"),
            build.index("docker buildx build"),
            "the seal has to run before the image is built and pushed, or it "
            "is reporting on an artifact that already shipped")


class ThumbnailStorageSettingsTest(SimpleTestCase):
    """The setting that actually moved the files, pinned directly.

    Without the STORAGES alias, THUMBNAIL_MEDIA_ROOT is inert:
    easy_thumbnails.storage.get_storage() never instantiates
    ThumbnailFileSystemStorage and falls back to default_storage instead. So
    asserting on THUMBNAIL_MEDIA_ROOT alone would pass while the files went
    right back to MEDIA_ROOT.
    """

    def test_thumbnail_storage_writes_into_static_root(self):
        from easy_thumbnails.storage import thumbnail_default_storage

        self.assertEqual(
            thumbnail_default_storage.location, settings.STATIC_ROOT)
        self.assertEqual(
            thumbnail_default_storage.base_url, settings.STATIC_URL)
        self.assertNotEqual(settings.STATIC_ROOT, settings.MEDIA_ROOT)

    def test_static_url_is_absolute(self):
        """THUMBNAIL_MEDIA_URL is derived from STATIC_URL in the class body.

        Django normalises STATIC_URL to a leading slash at settings-load time,
        but easy-thumbnails does not normalise its own URL setting -- and the
        class body reads the raw value. A bare 'static/' would render every
        thumbnail as a relative URL that resolves differently per page depth.
        """
        self.assertTrue(settings.STATIC_URL.startswith("/"))
        self.assertTrue(settings.THUMBNAIL_MEDIA_URL.startswith("/"))

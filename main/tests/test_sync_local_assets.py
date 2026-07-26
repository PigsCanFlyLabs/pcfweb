"""scripts/sync-local-assets.sh -- the shared image-asset sync.

The bug this script closes was STALENESS, not absence. main/static/assets/images
is a gitignored derived copy of ../pcfweb-assets/images that only build.sh ever
refreshed, so a checkout whose last build predated PR #23 -- which relocated
every book cover into images/book_covers/ -- kept a *full* image directory that
was nevertheless wrong: flat cover files, no book_covers/ subdirectory, and
orphaned files for retired products. Every book cover 404d while the banners
and the logo rendered fine.

Because the directory existed, the obvious-looking fix (copy it if it is
missing) would have changed nothing at all. So the headline test here is
test_a_stale_tree_is_replaced_wholesale: it starts from exactly that stale
shape and proves the sync both adds book_covers/ and removes the orphans.
"""

import os
import subprocess
import tempfile
from pathlib import Path
from textwrap import dedent

from django.test import SimpleTestCase

from main.tests.base import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "sync-local-assets.sh"
BUILD = REPO_ROOT / "build.sh"
RUN_LOCAL = REPO_ROOT / "run_local.sh"

LFS_POINTER = (
    "version https://git-lfs.github.com/spec/v1\n"
    "oid sha256:abc123\n"
    "size 123456\n"
)

# The covers as pcfweb-assets holds them today, under images/book_covers/.
COVERS = (
    "learning_spark_1ed.jpg",
    "high_performance_spark.jpg",
    "kubeflow_for_ml.jpg",
    "scaling_python_with_ray.jpg",
    "distributed_computing_4_kids.jpg",
)

# What a July-10 build.sh run left behind: the covers flat at the top level,
# and assets for products and services the site no longer has.
STALE_ORPHANS = (
    "spacebeaver-logo.png",
    "spacebeaver-draft.png",
    "transit.jpg",
    "transit-large.png",
)

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_USAGE = 2
EXIT_REPORTED = 3


class SyncLocalAssetsTestCase(SimpleTestCase):
    """A temporary pcfweb-assets checkout and a temporary destination.

    Every path the script writes to is redirected into the temporary tree, so
    running the suite never touches the developer's own main/static/assets.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

        # The sibling checkout.
        self.assets_dir = self.root / "pcfweb-assets"
        self.source_images = self.assets_dir / "images"
        self.covers_dir = self.source_images / "book_covers"
        self.covers_dir.mkdir(parents=True)
        for name in COVERS:
            (self.covers_dir / name).write_bytes(b"\xff\xd8\xff real jpeg")
        # A top-level asset, of the kind that never moved and never broke.
        (self.source_images / "banner.jpg").write_bytes(b"\xff\xd8\xff banner")

        # The destination, laid out like main/static/assets.
        self.static_assets = self.root / "static-assets"
        self.static_assets.mkdir()
        self.dest = self.static_assets / "images"

        self.fixture = self.root / "initial_products.yaml"
        self.write_fixture()

    def write_fixture(self, image_names=None):
        if image_names is None:
            image_names = [f"book_covers/{name}" for name in COVERS]
        entries = "".join(
            dedent(
                f"""\
                - model: main.product
                  pk: {100 + index}
                  fields:
                    image_name: "{image_name}"
                """
            )
            for index, image_name in enumerate(image_names)
        )
        self.fixture.write_text(entries, encoding="utf-8")

    def make_stale_dest(self):
        """Recreate the stale shape: flat covers, no book_covers/, orphans."""
        self.dest.mkdir()
        for name in COVERS[:4]:
            (self.dest / name).write_bytes(b"\xff\xd8\xff stale flat cover")
        for name in STALE_ORPHANS:
            (self.dest / name).write_bytes(b"\x89PNG retired product")
        (self.dest / "banner.jpg").write_bytes(b"\xff\xd8\xff banner")

    def run_sync(self, *args, assets_dir=None, cwd=None):
        env = dict(os.environ)
        env.update({
            "ASSETS_DIR": str(self.assets_dir if assets_dir is None
                              else assets_dir),
            "STATIC_ASSETS_DIR": str(self.static_assets),
            "PRODUCT_FIXTURE": str(self.fixture),
        })
        return subprocess.run(
            [str(SCRIPT), *args],
            cwd=str(REPO_ROOT if cwd is None else cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def dest_names(self):
        return {
            str(path.relative_to(self.dest))
            for path in self.dest.rglob("*") if path.is_file()
        }


class StaleTreeTest(SyncLocalAssetsTestCase):
    """The actual bug. A directory that exists and is wrong."""

    def test_a_stale_tree_is_replaced_wholesale(self):
        self.make_stale_dest()
        # Precondition: this is the shape that made the bug invisible. The
        # directory is full, so "is it missing?" answers no.
        self.assertTrue(self.dest.is_dir())
        self.assertFalse((self.dest / "book_covers").exists())

        result = self.run_sync()

        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        # The covers arrive at the path the fixture actually asks for.
        for name in COVERS:
            with self.subTest(cover=name):
                self.assertTrue((self.dest / "book_covers" / name).is_file())
        # The flat copies are gone, not left alongside.
        for name in COVERS[:4]:
            with self.subTest(flat=name):
                self.assertFalse((self.dest / name).exists())
        # And so are the retired products' assets. This is what a merge-style
        # copy would have left behind forever.
        for name in STALE_ORPHANS:
            with self.subTest(orphan=name):
                self.assertFalse((self.dest / name).exists())

    def test_the_result_is_exactly_the_source_tree(self):
        """Stronger than "the covers are there": nothing extra survives."""
        self.make_stale_dest()

        self.run_sync()

        expected = {
            str(path.relative_to(self.source_images))
            for path in self.source_images.rglob("*") if path.is_file()
        }
        self.assertEqual(self.dest_names(), expected)

    def test_the_sync_is_not_conditional_on_the_destination_being_absent(self):
        """Mutation guard for the fix that would have changed nothing.

        A `[ -d "$dest" ] || cp` shaped sync passes a from-scratch test and
        does nothing whatsoever for the stale tree that actually broke the
        site. Assert the copy is reached with a populated destination.
        """
        self.make_stale_dest()
        before = self.dest_names()
        self.assertIn("spacebeaver-logo.png", before)

        result = self.run_sync()

        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        self.assertNotEqual(before, self.dest_names())

    def test_syncing_a_second_time_is_idempotent(self):
        first = self.run_sync()
        after_first = self.dest_names()
        second = self.run_sync()

        self.assertEqual(first.returncode, EXIT_OK, first.stderr)
        self.assertEqual(second.returncode, EXIT_OK, second.stderr)
        self.assertEqual(after_first, self.dest_names())


class HealthyCheckoutTest(SyncLocalAssetsTestCase):
    def test_a_healthy_checkout_syncs_cleanly(self):
        result = self.run_sync()

        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("Synced 6 image asset(s)", result.stdout)

    def test_nested_directories_are_preserved(self):
        result = self.run_sync()

        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        self.assertTrue((self.dest / "book_covers").is_dir())
        self.assertEqual((self.dest / "book_covers" / COVERS[0]).read_bytes(),
                         (self.covers_dir / COVERS[0]).read_bytes())

    def test_it_works_from_any_working_directory(self):
        """build.sh runs from the repo root; nothing guarantees a caller does."""
        result = self.run_sync(cwd=self.root)

        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        self.assertTrue((self.dest / "book_covers" / COVERS[0]).is_file())


class AbsentSiblingTest(SyncLocalAssetsTestCase):
    """Failure mode 1: the pcfweb-assets checkout is not there."""

    def setUp(self):
        super().setUp()
        self.absent = self.root / "not-a-checkout"

    def test_it_is_fatal_by_default(self):
        result = self.run_sync(assets_dir=self.absent)

        self.assertEqual(result.returncode, EXIT_FATAL)
        self.assertIn("pcfweb-assets checkout is missing", result.stderr)

    def test_it_names_the_expected_path_and_the_clone_command(self):
        result = self.run_sync(assets_dir=self.absent)

        self.assertIn(str(self.absent / "images"), result.stderr)
        self.assertIn(
            "git clone https://github.com/PigsCanFlyLabs/pcfweb-assets.git",
            result.stderr,
        )

    def test_warn_mode_reports_and_continues(self):
        result = self.run_sync("--warn", assets_dir=self.absent)

        self.assertEqual(result.returncode, EXIT_REPORTED)
        self.assertIn("pcfweb-assets checkout is missing", result.stderr)
        self.assertIn("WARNING", result.stderr)

    def test_fatal_mode_refuses_before_deleting_anything(self):
        """The old inline `rm -rf` ran first, so a build without the sibling
        checkout failed *and* left you with no images to fall back on. The
        README used to document that as a recovery procedure. Checking the
        source before touching the destination removes the footgun."""
        self.make_stale_dest()
        before = self.dest_names()

        result = self.run_sync(assets_dir=self.absent)

        self.assertEqual(result.returncode, EXIT_FATAL)
        self.assertEqual(before, self.dest_names())

    def test_warn_mode_keeps_the_existing_tree_rather_than_emptying_it(self):
        """A stale tree beats no tree when there is nothing to replace it.

        Deleting first and discovering the source is absent second would take
        a developer from "the book covers are broken" to "every image on the
        site is broken", with no way back but a clone.
        """
        self.make_stale_dest()
        before = self.dest_names()

        result = self.run_sync("--warn", assets_dir=self.absent)

        self.assertEqual(result.returncode, EXIT_REPORTED)
        self.assertEqual(before, self.dest_names())
        self.assertIn("Leaving the existing", result.stderr)


class LfsPointerTest(SyncLocalAssetsTestCase):
    """Failure mode 2: the checkout is there but never had `git lfs pull`."""

    def setUp(self):
        super().setUp()
        (self.covers_dir / COVERS[0]).write_text(LFS_POINTER)

    def test_it_is_fatal_by_default(self):
        result = self.run_sync()

        self.assertEqual(result.returncode, EXIT_FATAL)
        self.assertIn("unmaterialised Git LFS pointers", result.stderr)

    def test_it_names_the_file_and_the_lfs_command(self):
        result = self.run_sync()

        # The file list comes from check-image-assets.sh, the single detector.
        self.assertIn(str(self.dest / "book_covers" / COVERS[0]),
                      result.stderr)
        self.assertIn("git lfs install && git lfs pull", result.stderr)
        # And the remedy points at the source checkout, not at the copy.
        self.assertIn(f"cd {self.assets_dir}", result.stderr)

    def test_warn_mode_reports_and_continues(self):
        result = self.run_sync("--warn")

        self.assertEqual(result.returncode, EXIT_REPORTED)
        self.assertIn("unmaterialised Git LFS pointers", result.stderr)

    def test_the_pointer_still_lands_so_the_report_is_about_real_state(self):
        """The copy happens first; the guard reads what was actually copied."""
        self.run_sync("--warn")

        self.assertEqual(
            (self.dest / "book_covers" / COVERS[0]).read_text(), LFS_POINTER)


class OversizeAssetTest(SyncLocalAssetsTestCase):
    def setUp(self):
        super().setUp()
        self.huge = self.source_images / "panorama.jpg"
        self.huge.write_bytes(b"\xff\xd8\xff")
        # Sparse: the ceiling is checked against the apparent size.
        os.truncate(self.huge, 6_000_001)

    def test_it_is_fatal_by_default(self):
        result = self.run_sync()

        self.assertEqual(result.returncode, EXIT_FATAL)
        self.assertIn("image assets over 5MB", result.stderr)
        self.assertIn("panorama.jpg", result.stderr)
        self.assertIn("pcfweb-assets/originals/", result.stderr)

    def test_warn_mode_reports_and_continues(self):
        result = self.run_sync("--warn")

        self.assertEqual(result.returncode, EXIT_REPORTED)
        self.assertIn("image assets over 5MB", result.stderr)

    def test_a_file_just_under_the_ceiling_passes(self):
        os.truncate(self.huge, 5_000_000)

        result = self.run_sync()

        self.assertEqual(result.returncode, EXIT_OK, result.stderr)


class FixtureMismatchTest(SyncLocalAssetsTestCase):
    """Failure mode 3: healthy checkout, but the fixture asks for a file
    that is not in it. A content bug, not a plumbing one."""

    def setUp(self):
        super().setUp()
        self.write_fixture([
            "book_covers/learning_spark_1ed.jpg",
            "book_covers/never_published.jpg",
        ])

    def test_it_is_fatal_by_default(self):
        result = self.run_sync()

        self.assertEqual(result.returncode, EXIT_FATAL)
        self.assertIn("fixture references images that do not exist",
                      result.stderr)

    def test_it_reports_the_pk_list(self):
        result = self.run_sync()

        self.assertIn("pk=101", result.stderr)
        self.assertIn("book_covers/never_published.jpg", result.stderr)

    def test_it_says_the_mismatch_is_genuine_when_nothing_else_is_wrong(self):
        result = self.run_sync()

        self.assertIn("genuine content mismatch", result.stderr)

    def test_it_does_not_claim_a_genuine_mismatch_when_lfs_is_broken(self):
        """Same symptom, different cause: do not send them chasing content."""
        (self.covers_dir / "learning_spark_1ed.jpg").write_text(LFS_POINTER)

        result = self.run_sync("--warn")

        self.assertEqual(result.returncode, EXIT_REPORTED)
        self.assertNotIn("genuine content mismatch", result.stderr)
        self.assertIn("fix that first", result.stderr)

    def test_warn_mode_reports_and_continues(self):
        result = self.run_sync("--warn")

        self.assertEqual(result.returncode, EXIT_REPORTED)
        self.assertIn("pk=101", result.stderr)


class FailureModesAreDistinguishableTest(SyncLocalAssetsTestCase):
    """The three modes look identical from a browser and have different fixes.

    A developer seeing a missing cover cannot tell them apart, so the script
    has to. Each remedy must appear only for the mode it actually fixes.
    """

    CLONE = "git clone https://github.com/PigsCanFlyLabs/pcfweb-assets.git"
    LFS = "git lfs install && git lfs pull"
    CONTENT = "genuine content mismatch"

    def test_absent_sibling_gives_only_the_clone_remedy(self):
        result = self.run_sync("--warn", assets_dir=self.root / "absent")

        self.assertIn(self.CLONE, result.stderr)
        self.assertNotIn(self.CONTENT, result.stderr)

    def test_lfs_pointers_give_only_the_lfs_remedy(self):
        (self.covers_dir / COVERS[0]).write_text(LFS_POINTER)

        result = self.run_sync("--warn")

        self.assertIn(self.LFS, result.stderr)
        self.assertNotIn(self.CLONE, result.stderr)
        self.assertNotIn(self.CONTENT, result.stderr)

    def test_a_content_mismatch_gives_only_the_content_explanation(self):
        self.write_fixture(["book_covers/never_published.jpg"])

        result = self.run_sync("--warn")

        self.assertIn(self.CONTENT, result.stderr)
        self.assertNotIn(self.CLONE, result.stderr)
        self.assertNotIn(self.LFS, result.stderr)


class WarnModeContractTest(SyncLocalAssetsTestCase):
    def test_warn_mode_reports_every_problem_not_just_the_first(self):
        """Fatal mode stops at the first; warn mode should not make the
        developer restart the server once per problem."""
        self.huge = self.source_images / "panorama.jpg"
        self.huge.write_bytes(b"\xff\xd8\xff")
        os.truncate(self.huge, 6_000_001)
        (self.covers_dir / COVERS[0]).write_text(LFS_POINTER)
        self.write_fixture(["book_covers/never_published.jpg"])

        result = self.run_sync("--warn")

        self.assertEqual(result.returncode, EXIT_REPORTED)
        self.assertIn("image assets over 5MB", result.stderr)
        self.assertIn("unmaterialised Git LFS pointers", result.stderr)
        self.assertIn("pk=100", result.stderr)
        self.assertIn("3 asset problem(s) reported", result.stderr)

    def test_warn_mode_never_exits_one(self):
        (self.covers_dir / COVERS[0]).write_text(LFS_POINTER)

        result = self.run_sync("--warn")

        self.assertNotEqual(result.returncode, EXIT_FATAL)

    def test_a_clean_warn_run_exits_zero(self):
        """Exit 3 has to mean something, so it must not fire unconditionally."""
        result = self.run_sync("--warn")

        self.assertEqual(result.returncode, EXIT_OK, result.stderr)

    def test_the_report_is_hard_to_miss(self):
        result = self.run_sync("--warn", assets_dir=self.root / "absent")

        self.assertIn("!!!!!!!!!!", result.stderr)
        self.assertIn("render broken", result.stderr)

    def test_an_unknown_flag_is_a_usage_error(self):
        result = self.run_sync("--quiet")

        self.assertEqual(result.returncode, EXIT_USAGE)
        self.assertIn("usage:", result.stderr)

    def test_extra_arguments_are_a_usage_error(self):
        result = self.run_sync("--warn", "extra")

        self.assertEqual(result.returncode, EXIT_USAGE)

    def test_a_usage_error_syncs_nothing(self):
        """Refuse before touching anything, as run_local.sh's own guard does."""
        self.make_stale_dest()
        before = self.dest_names()

        result = self.run_sync("--warn", "extra")

        self.assertEqual(result.returncode, EXIT_USAGE)
        self.assertEqual(before, self.dest_names())


class SymlinkedSourceTest(SyncLocalAssetsTestCase):
    """A symlinked images/ used to walk straight past both guards.

    `cp -a` preserves a symlinked final component instead of copying the tree
    behind it. `find -type f` then matches nothing and check-image-assets.sh
    does not read through a symlink, so the size ceiling and the LFS pointer
    detector both reported success over a directory neither had looked inside:
    exit 0, "Synced 0 image asset(s)", pointers and oversized masters waved
    through. A guard that silently does nothing is worse than no guard,
    because it reports success.

    Fixed by copying through the link (`"$source_images/."` plus -L) rather
    than by refusing it, so that `images -> /mnt/big-disk/assets` stays a
    supported layout, with an assertion afterwards that nothing symlinked
    survived into the tree the guards are about to read.
    """

    def link_source(self, relative=False):
        """Turn images/ into a symlink pointing at a sibling directory."""
        real = self.assets_dir / "real-images"
        self.source_images.rename(real)
        self.source_images.symlink_to(real.name if relative else real)
        self.assertTrue(self.source_images.is_symlink())
        return real

    def test_a_pointer_behind_a_symlinked_source_is_caught(self):
        """The reviewer's reproduction. Was exit 0 with no pointer reported."""
        real = self.link_source()
        (real / "book_covers" / COVERS[0]).write_text(LFS_POINTER)

        result = self.run_sync()

        self.assertEqual(result.returncode, EXIT_FATAL)
        self.assertIn("unmaterialised Git LFS pointers", result.stderr)
        self.assertIn(str(self.dest / "book_covers" / COVERS[0]),
                      result.stderr)

    def test_an_oversized_file_behind_a_symlinked_source_is_caught(self):
        real = self.link_source()
        huge = real / "panorama.jpg"
        huge.write_bytes(b"\xff\xd8\xff")
        os.truncate(huge, 6_000_001)

        result = self.run_sync()

        self.assertEqual(result.returncode, EXIT_FATAL)
        self.assertIn("image assets over 5MB", result.stderr)
        self.assertIn("panorama.jpg", result.stderr)

    def test_the_destination_is_a_real_directory_not_a_copied_symlink(self):
        self.link_source()

        result = self.run_sync()

        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        self.assertTrue(self.dest.is_dir())
        self.assertFalse(self.dest.is_symlink())

    def test_the_guards_have_real_files_to_inspect(self):
        """The invariant, stated directly: after a successful sync there are
        real files under the destination for the guards to read."""
        self.link_source()

        result = self.run_sync()

        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        real_files = [p for p in self.dest.rglob("*")
                      if p.is_file() and not p.is_symlink()]
        self.assertEqual(len(real_files), 6)
        self.assertNotIn("Synced 0 image asset(s)", result.stdout)

    def test_a_healthy_symlinked_source_still_syncs(self):
        """Followed, not refused: this is a legitimate layout."""
        self.link_source()

        result = self.run_sync()

        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        for name in COVERS:
            with self.subTest(cover=name):
                self.assertTrue((self.dest / "book_covers" / name).is_file())

    def test_a_relative_symlink_target_is_followed_too(self):
        """The relative case dangles once copied, so it failed differently --
        and was reported as a fixture content mismatch, which it is not."""
        self.link_source(relative=True)

        result = self.run_sync()

        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        self.assertTrue((self.dest / "book_covers" / COVERS[0]).is_file())
        self.assertNotIn("genuine content mismatch", result.stderr)

    def test_an_internal_symlink_is_dereferenced(self):
        """One level down, the hole is identical: a symlinked file inside
        images/ is skipped by find -type f and by the pointer detector."""
        outside = self.root / "outside.jpg"
        outside.write_text(LFS_POINTER)
        (self.source_images / "linked.jpg").symlink_to(outside)

        result = self.run_sync()

        # It landed as a real file, so the pointer detector could read it.
        self.assertEqual(result.returncode, EXIT_FATAL)
        self.assertIn("unmaterialised Git LFS pointers", result.stderr)
        self.assertIn(str(self.dest / "linked.jpg"), result.stderr)

    def test_a_dangling_internal_symlink_fails_the_copy(self):
        """Deliberate consequence of -L, and the safer outcome: it used to
        land as a broken link that every guard skipped and every page 404d."""
        (self.source_images / "broken.jpg").symlink_to("/nowhere/nothing.jpg")

        result = self.run_sync()

        self.assertEqual(result.returncode, EXIT_FATAL)
        self.assertIn("copying the image assets failed", result.stderr)

    def test_the_no_symlinks_assertion_is_present(self):
        """A tripwire, and the one thing here with no behavioural test.

        With the copy flags correct nothing symlinked can reach the staged
        tree, so the assertion is unreachable -- which is what makes it a
        tripwire rather than a guard. Its value is in what it does when the
        flags are wrong: dropping the -L turns a silent fail-open into a loud
        refusal *because of this block*, which
        test_an_internal_symlink_is_dereferenced then catches. Pinned here so
        it cannot be deleted as dead code.
        """
        source = (REPO_ROOT / "scripts" / "sync-local-assets.sh").read_text()

        self.assertIn('find "$staging" -type l', source)
        self.assertIn("cannot be trusted", source)
        # It has to gate the install, not merely print.
        self.assertLess(source.index('find "$staging" -type l'),
                        source.index('mv "$staging" "$dest"'))

    def test_a_dangling_source_symlink_says_it_does_not_resolve(self):
        """"Missing" reads wrong when ls shows them an images entry."""
        self.source_images.rename(self.assets_dir / "moved")
        self.source_images.symlink_to(self.assets_dir / "gone")

        result = self.run_sync()

        self.assertEqual(result.returncode, EXIT_FATAL)
        self.assertIn("does not resolve", result.stderr)
        self.assertIn("gone", result.stderr)


class SymlinkedDestinationTest(SyncLocalAssetsTestCase):
    """The mirror case: could `rm -rf "$dest"` reach outside the destination?

    It cannot, because rm never recurses through a symlink -- it unlinks the
    link. But `rm -rf "$dest"/`, one character different, deletes the target's
    contents instead. These tests pin the safe behaviour so that character
    cannot reappear unnoticed.
    """

    def setUp(self):
        super().setUp()
        self.outside = self.root / "precious"
        self.outside.mkdir()
        (self.outside / "important.txt").write_text("must survive\n")
        self.dest.symlink_to(self.outside)

    def test_the_symlink_target_is_not_touched(self):
        result = self.run_sync()

        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        self.assertTrue(self.outside.is_dir())
        self.assertEqual((self.outside / "important.txt").read_text(),
                         "must survive\n")

    def test_the_destination_is_replaced_by_a_real_directory(self):
        result = self.run_sync()

        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        self.assertFalse(self.dest.is_symlink())
        self.assertTrue((self.dest / "book_covers" / COVERS[0]).is_file())

    def test_nothing_was_written_into_the_symlink_target(self):
        self.run_sync()

        self.assertEqual(
            {p.name for p in self.outside.iterdir()}, {"important.txt"})


class AtomicStagingTest(SyncLocalAssetsTestCase):
    """The copy is staged and renamed, so a failure cannot leave a deleted or
    half-written tree. This PR removed a README instruction that read
    "build.sh ... leaves your local images deleted -- re-clone to recover";
    another route to that state would have undone it."""

    def break_the_copy(self):
        """An unreadable source file makes cp fail partway through."""
        victim = self.source_images / "book_covers" / COVERS[2]
        victim.chmod(0)
        self.addCleanup(victim.chmod, 0o644)

    def test_a_failed_copy_leaves_the_previous_tree_byte_for_byte(self):
        self.make_stale_dest()
        before = {name: (self.dest / name).read_bytes()
                  for name in self.dest_names()}
        self.break_the_copy()

        result = self.run_sync()

        self.assertNotEqual(result.returncode, EXIT_OK)
        after = {name: (self.dest / name).read_bytes()
                 for name in self.dest_names()}
        self.assertEqual(before, after)

    def test_a_failed_copy_says_nothing_was_installed(self):
        self.make_stale_dest()
        self.break_the_copy()

        result = self.run_sync()

        self.assertEqual(result.returncode, EXIT_FATAL)
        self.assertIn("copying the image assets failed", result.stderr)
        self.assertIn("exactly as", result.stderr)

    def test_a_failed_copy_with_no_previous_tree_creates_nothing(self):
        self.break_the_copy()

        result = self.run_sync()

        self.assertEqual(result.returncode, EXIT_FATAL)
        self.assertFalse(self.dest.exists())

    def test_no_staging_directory_survives_a_success(self):
        result = self.run_sync()

        self.assertEqual(result.returncode, EXIT_OK, result.stderr)
        self.assertEqual(list(self.static_assets.glob(".images.staging.*")),
                         [])

    def test_no_staging_directory_survives_a_failure(self):
        """The EXIT trap has to fire on problem()'s exit 1 as well."""
        self.break_the_copy()

        result = self.run_sync()

        self.assertNotEqual(result.returncode, EXIT_OK)
        self.assertEqual(list(self.static_assets.glob(".images.staging.*")),
                         [])

    def test_no_staging_directory_survives_a_guard_failure(self):
        (self.covers_dir / COVERS[0]).write_text(LFS_POINTER)

        result = self.run_sync()

        self.assertEqual(result.returncode, EXIT_FATAL)
        self.assertEqual(list(self.static_assets.glob(".images.staging.*")),
                         [])

    def test_the_staging_directory_is_beside_the_destination(self):
        """It has to share a filesystem with the destination or the rename is
        a cross-device copy, which is neither atomic nor cheap."""
        source = (REPO_ROOT / "scripts" / "sync-local-assets.sh").read_text()

        self.assertIn('staging="$STATIC_ASSETS_DIR/.images.staging.$$"',
                      source)
        self.assertIn('trap \'rm -rf "$staging"\' EXIT INT TERM', source)

    def test_the_tree_is_renamed_rather_than_copied_into_place(self):
        source = (REPO_ROOT / "scripts" / "sync-local-assets.sh").read_text()

        self.assertIn('mv "$staging" "$dest"', source)
        # The old shape: a copy straight onto the live path.
        self.assertNotIn('cp -af "$source_images" "$STATIC_ASSETS_DIR/"',
                         source)

    def test_the_staging_leftovers_are_gitignored(self):
        """A SIGKILL is the one case the trap cannot cover."""
        ignored = (REPO_ROOT / ".gitignore").read_text()

        self.assertIn("main/static/assets/.images.staging.*", ignored)


class CallerContractTest(SimpleTestCase):
    """Both callers use the one script, and only one of them relaxes it."""

    def test_build_calls_the_sync(self):
        self.assertIn("./scripts/sync-local-assets.sh", BUILD.read_text())

    def test_build_does_not_pass_warn(self):
        """The deploy path must keep every guard fatal.

        Fatal is the script's default, so this is the whole of the contract:
        build.sh has to not opt out.
        """
        for line in BUILD.read_text().splitlines():
            if "sync-local-assets.sh" in line and not line.strip().startswith("#"):
                with self.subTest(line=line):
                    self.assertNotIn("--warn", line)

    def test_build_no_longer_carries_its_own_copy_of_the_sync(self):
        """Two copies of the rm/cp is the drift this refactor exists to stop."""
        build = BUILD.read_text()

        self.assertNotIn("rm -rf main/static/assets/images", build)
        self.assertNotIn("cp -af ../pcfweb-assets/images", build)
        self.assertNotIn("ASSET_MAX_BYTES", build)

    def test_run_local_calls_the_sync_in_warn_mode(self):
        self.assertIn("./scripts/sync-local-assets.sh --warn",
                      RUN_LOCAL.read_text())

    def test_run_local_syncs_after_the_production_guard(self):
        """#27's refusal stays first: no file is copied in a prod shell."""
        source = RUN_LOCAL.read_text()

        self.assertLess(source.index("refuse_production"),
                        source.index("sync-local-assets.sh"))

    def test_run_local_syncs_before_starting_the_server(self):
        source = RUN_LOCAL.read_text()

        self.assertLess(source.index("sync-local-assets.sh"),
                        source.index("runserver_plus"))

    def test_run_local_treats_only_exit_three_as_survivable(self):
        """A crash in the sync script itself must still stop the script."""
        source = RUN_LOCAL.read_text()

        self.assertIn('[ "$asset_status" -ne 3 ]', source)
        self.assertIn('exit "$asset_status"', source)

    def test_run_local_checks_the_book_archives_without_requiring_them(self):
        source = RUN_LOCAL.read_text()

        self.assertIn("./scripts/check-book-assets.sh", source)
        # Guarded by `if !`, so a failure warns instead of aborting under -e.
        self.assertIn("if ! ./scripts/check-book-assets.sh", source)
        self.assertLess(source.index("check-book-assets.sh"),
                        source.index("runserver_plus"))

    def test_the_build_keeps_the_book_archives_fatal(self):
        """Locally a missing e-book is a 404; in production it is a customer
        being emailed a pointer stub. Same script, different severity."""
        build = BUILD.read_text()

        self.assertIn("./scripts/check-book-assets.sh", build)
        self.assertNotIn("if ! ./scripts/check-book-assets.sh", build)

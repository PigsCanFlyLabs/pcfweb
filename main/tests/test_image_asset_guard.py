import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-image-assets.sh"


class ImageAssetGuardTest(SimpleTestCase):
    def run_guard(self, path, label=None):
        command = [str(SCRIPT), str(path)]
        if label is not None:
            command.append(label)
        return subprocess.run(
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

    def test_real_images_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp)
            (assets / "pixel.png").write_bytes(
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            )
            (assets / "tiny.gif").write_bytes(b"GIF89a\x01\x00\x01\x00")

            result = self.run_guard(assets)

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_lfs_pointer_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp)
            (assets / "hero.jpg").write_text(
                "version https://git-lfs.github.com/spec/v1\n"
                "oid sha256:abc123\n"
                "size 123456\n"
            )

            result = self.run_guard(assets)

        self.assertNotEqual(result.returncode, 0)

    def test_lfs_pointer_output_names_the_offending_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp)
            pointer = assets / "nested" / "hero.jpg"
            pointer.parent.mkdir()
            pointer.write_text(
                "version https://git-lfs.github.com/spec/v1\n"
                "oid sha256:abc123\n"
                "size 123456\n"
            )

            result = self.run_guard(assets)

        self.assertIn(str(pointer), result.stderr)
        self.assertIn("git lfs install && git lfs pull", result.stderr)
        self.assertIn("pcfweb-assets/README.md", result.stderr)

    def test_lfs_sentinel_on_second_line_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp)
            (assets / "photo.jpg").write_text(
                "not a pointer\n"
                "version https://git-lfs.github.com/spec/v1\n"
            )

            result = self.run_guard(assets)

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_multiple_lfs_pointers_are_all_reported_in_sorted_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp)
            first = assets / "a.jpg"
            second = assets / "nested" / "b.jpg"
            second.parent.mkdir()
            for pointer in (second, first):
                pointer.write_text(
                    "version https://git-lfs.github.com/spec/v1\n"
                    "oid sha256:abc123\n"
                    "size 123456\n"
                )

            result = self.run_guard(assets)

        self.assertNotEqual(result.returncode, 0)
        first_index = result.stderr.index(f"  {first}")
        second_index = result.stderr.index(f"  {second}")
        self.assertLess(first_index, second_index)

    def test_unreadable_image_asset_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp)
            image = assets / "locked.jpg"
            image.write_bytes(b"\xff\xd8\xff")
            image.chmod(0)

            try:
                result = self.run_guard(assets)
            finally:
                image.chmod(0o600)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"ERROR: cannot read image asset: {image}",
                      result.stderr)

    def test_empty_and_absent_directories_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty"
            empty.mkdir()
            absent = Path(tmp) / "absent"

            empty_result = self.run_guard(empty)
            absent_result = self.run_guard(absent)

        self.assertEqual(empty_result.returncode, 0, empty_result.stderr)
        self.assertEqual(absent_result.returncode, 0, absent_result.stderr)

    def test_build_invokes_the_guard_after_the_oversized_check(self):
        build_script = (REPO_ROOT / "build.sh").read_text()

        oversized_block = "if [ -n \"$oversized\" ]; then"
        source_guard_call = (
            "./scripts/check-image-assets.sh main/static/assets/images "
            "\"source image assets\""
        )
        checks_call = "./scripts/checks.sh"
        static_guard_call = (
            "./scripts/check-image-assets.sh static/assets/images "
            "\"collected static image assets\""
        )

        self.assertIn(source_guard_call, build_script)
        self.assertIn(static_guard_call, build_script)
        self.assertLess(build_script.index(oversized_block),
                        build_script.index(source_guard_call))
        self.assertLess(build_script.index(source_guard_call),
                        build_script.index(checks_call))
        self.assertLess(build_script.index(checks_call),
                        build_script.index(static_guard_call))

    def test_static_root_pointer_output_names_collected_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            static_images = Path(tmp) / "static" / "assets" / "images"
            static_images.mkdir(parents=True)
            pointer = static_images / "hero.jpg"
            pointer.write_text(
                "version https://git-lfs.github.com/spec/v1\n"
                "oid sha256:abc123\n"
                "size 123456\n"
            )

            result = self.run_guard(static_images,
                                    "collected static image assets")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "ERROR: collected static image assets contain unmaterialised "
            "Git LFS pointers:",
            result.stderr,
        )
        self.assertIn(str(pointer), result.stderr)
        self.assertIn("collectstatic --clear", result.stderr)
        self.assertIn("rm", result.stderr)

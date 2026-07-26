import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-image-assets.sh"


class ImageAssetGuardTest(SimpleTestCase):
    def run_guard(self, path):
        return subprocess.run(
            [str(SCRIPT), str(path)],
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
        guard_call = "./scripts/check-image-assets.sh main/static/assets/images"
        checks_call = "./scripts/checks.sh"

        self.assertIn(guard_call, build_script)
        self.assertLess(build_script.index(oversized_block),
                        build_script.index(guard_call))
        self.assertLess(build_script.index(guard_call),
                        build_script.index(checks_call))

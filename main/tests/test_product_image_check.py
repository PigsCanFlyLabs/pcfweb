from pathlib import Path
import subprocess
import tempfile
from textwrap import dedent

from django.test import SimpleTestCase


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check-product-images.sh"


class ProductImageCheckScriptTest(SimpleTestCase):
    def run_check(self, fixture_text, files=(), make_images_dir=True):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = root / "initial_products.yaml"
            images_dir = root / "images"
            fixture.write_text(dedent(fixture_text), encoding="utf-8")
            if make_images_dir:
                images_dir.mkdir()
                for name in files:
                    path = images_dir / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"image")

            return subprocess.run(
                [CHECK_SCRIPT, fixture, images_dir],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_all_present_passes(self):
        result = self.run_check(
            """
            - model: main.product
              pk: 100
              fields:
                image_name: "book_covers/learning_spark_1ed.jpg"
            """,
            files=("book_covers/learning_spark_1ed.jpg",),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")

    def test_one_missing_fails_and_names_pk_and_path(self):
        result = self.run_check(
            """
            - model: main.product
              pk: 101
              fields:
                image_name: "book_covers/high_performance_spark.jpg"
            """
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pk=101", result.stderr)
        self.assertIn("book_covers/high_performance_spark.jpg", result.stderr)
        self.assertIn(
            "/images/book_covers/high_performance_spark.jpg",
            result.stderr,
        )

    def test_multiple_missing_are_all_reported(self):
        result = self.run_check(
            """
            - model: main.product
              pk: 103
              fields:
                image_name: "book_covers/scaling_python_with_ray.jpg"
            - model: main.product
              pk: 102
              fields:
                image_name: "book_covers/kubeflow_for_ml.jpg"
            """
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pk=102", result.stderr)
        self.assertIn("pk=103", result.stderr)
        self.assertIn("book_covers/kubeflow_for_ml.jpg", result.stderr)
        self.assertIn("book_covers/scaling_python_with_ray.jpg", result.stderr)
        self.assertLess(
            result.stderr.index("pk=102"),
            result.stderr.index("pk=103"),
        )

    def test_empty_and_absent_image_name_pass(self):
        result = self.run_check(
            """
            - model: main.product
              pk: 100
              fields:
                image_name: ""
            - model: main.product
              pk: 101
              fields:
                name: "No image"
            """
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_absent_directory_fails_closed(self):
        result = self.run_check(
            """
            - model: main.product
              pk: 100
              fields:
                image_name: "book_covers/learning_spark_1ed.jpg"
            """,
            make_images_dir=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("product image directory is absent", result.stderr)

    def test_unparseable_fixture_fails_closed(self):
        result = self.run_check(
            """
            - model: main.product
              pk: [not valid
            """
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not parse product fixture", result.stderr)

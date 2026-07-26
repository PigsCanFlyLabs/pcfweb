#!/bin/bash
set -euo pipefail

fixture_path="${1:-main/fixtures/initial_products.yaml}"
images_dir="${2:-main/static/assets/images}"

python - "$fixture_path" "$images_dir" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml


def load_fixture(path: Path) -> list[dict[str, Any]]:
    with path.open("rb") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        return []
    if not isinstance(data, list):
        raise TypeError("fixture root must be a list")
    return data


def main() -> int:
    fixture_path = Path(sys.argv[1])
    images_dir = Path(sys.argv[2])

    try:
        entries = load_fixture(fixture_path)
    except Exception as exc:
        print(
            f"ERROR: could not parse product fixture {fixture_path}: {exc}",
            file=sys.stderr,
        )
        return 1

    if not images_dir.is_dir():
        print(
            f"ERROR: product image directory is absent: {images_dir}",
            file=sys.stderr,
        )
        return 1

    missing: list[tuple[str, str, Path]] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("model") != "main.product":
            continue
        fields = entry.get("fields") or {}
        if not isinstance(fields, dict):
            continue
        image_name = fields.get("image_name") or ""
        if not image_name:
            continue
        path = images_dir / image_name
        if not path.is_file():
            missing.append((str(entry.get("pk")), str(image_name), path))

    if missing:
        print(
            "ERROR: product fixture references missing image files:",
            file=sys.stderr,
        )
        sorted_missing = sorted(missing, key=lambda item: item[2].as_posix())
        for pk, image_name, path in sorted_missing:
            print(
                f"  pk={pk} image_name={image_name} path={path}",
                file=sys.stderr,
            )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
PY

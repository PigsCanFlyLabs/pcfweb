#!/bin/bash
set -euo pipefail

asset_dir="${1:-main/static/assets/images}"
asset_label="${2:-source image assets}"
sentinel="version https://git-lfs.github.com/spec/v1"

if [ ! -d "$asset_dir" ]; then
  exit 0
fi

file_list=$(mktemp)
trap 'rm -f "$file_list"' EXIT
find "$asset_dir" -type f -print0 > "$file_list"
sort -z -o "$file_list" "$file_list"

pointers=()
while IFS= read -r -d '' file; do
  if [ ! -r "$file" ]; then
    echo "ERROR: cannot read image asset: $file" >&2
    exit 1
  fi

  set +e
  awk -v sentinel="$sentinel" '
    NR == 1 { exit($0 == sentinel ? 0 : 1) }
    END { if (NR == 0) exit 1 }
  ' < "$file"
  status=$?
  set -e

  case "$status" in
    0)
      pointers+=("$file")
      ;;
    1)
      ;;
    *)
      echo "ERROR: could not scan image asset: $file" >&2
      exit "$status"
      ;;
  esac
done < "$file_list"

if [ "${#pointers[@]}" -gt 0 ]; then
  echo >&2
  echo "ERROR: ${asset_label} contain unmaterialised Git LFS pointers:" >&2
  printf '  %s\n' "${pointers[@]}" >&2
  echo >&2
  if [ "$asset_label" = "collected static image assets" ]; then
    echo "Remove the stale collected artifact with \`collectstatic --clear\` or rm," >&2
    echo "then re-run build.sh. If the source asset is also a pointer, run" >&2
    echo "\`git lfs install && git lfs pull\` inside the pcfweb-assets checkout first." >&2
    echo "See pcfweb-assets/README.md." >&2
  else
    echo "Run \`git lfs install && git lfs pull\` inside the pcfweb-assets checkout," >&2
    echo "then re-run build.sh. See pcfweb-assets/README.md." >&2
  fi
  exit 1
fi

#!/bin/bash
# Validate the digital book archives and stage them for the Docker build.
#
# Usage: scripts/check-book-assets.sh <source-dir> <dest-dir>
#
# Called by build.sh before `docker buildx build`. Split out of it so the
# guard can be tested without running a build (see BookAssetGuardTest in
# main/tests.py) -- this is the one check standing between a customer and
# being emailed a 130-byte text file instead of a book.
#
# The failure it exists to stop: a build host that never ran `git lfs pull`,
# or whose GitHub LFS quota is exhausted (clones still succeed there; they
# just hand back pointers), has pointer stubs where the books should be.
# Nothing downstream would notice -- the stub copies into the image fine, the
# webhook serves it fine, and the customer gets a file starting
# "version https://git-lfs.github.com/spec/v1".
#
# See the README in the pcfweb-book-assets repository.
set -eu

BOOK_ASSETS_DIR="${1:?usage: check-book-assets.sh <source-dir> <dest-dir>}"
DEST_DIR="${2:?usage: check-book-assets.sh <source-dir> <dest-dir>}"

# A real illustrated book is tens of megabytes; an LFS pointer is ~130 bytes.
# Anything under a megabyte is not a book, whatever else it might be.
MIN_BOOK_ARCHIVE_BYTES=$((1024 * 1024))
LFS_POINTER_FIRST_LINE='version https://git-lfs.github.com/spec/v1'

shopt -s nullglob
archives=("$BOOK_ASSETS_DIR"/*.zip)
shopt -u nullglob

if [ ${#archives[@]} -eq 0 ]; then
  echo "ERROR: no book archives found in $BOOK_ASSETS_DIR" >&2
  echo "Clone the pcfweb-book-assets repository next to this checkout, then" >&2
  echo "run 'git lfs install && git lfs pull' inside it." >&2
  exit 1
fi

for archive in "${archives[@]}"; do
  if head -n 1 "$archive" | grep -qxF "$LFS_POINTER_FIRST_LINE"; then
    echo "ERROR: $archive is a Git LFS pointer file, not a real book archive" >&2
    echo "Run 'git lfs pull' in $BOOK_ASSETS_DIR, and check the GitHub LFS" >&2
    echo "storage quota and budget, before building." >&2
    exit 1
  fi

  bytes=$(wc -c < "$archive")
  if [ "$bytes" -lt "$MIN_BOOK_ARCHIVE_BYTES" ]; then
    echo "ERROR: $archive is only $bytes bytes; expected a real book archive" >&2
    exit 1
  fi

  magic=$(LC_ALL=C head -c 4 "$archive" | od -An -tx1 | tr -d ' \n')
  if [ "$magic" != "504b0304" ]; then
    echo "ERROR: $archive does not start with ZIP magic bytes PK\\003\\004" >&2
    exit 1
  fi
done

# Only stage once every archive has passed, so a partial copy can never be
# what gets built.
rm -rf "$DEST_DIR"
mkdir -p "$DEST_DIR"
for archive in "${archives[@]}"; do
  cp -a "$archive" "$DEST_DIR/"
done

echo "Staged ${#archives[@]} book archive(s) from $BOOK_ASSETS_DIR into $DEST_DIR"

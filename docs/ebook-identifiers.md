# E-book identifiers: where each one lives and how it was sourced

This is the provenance record for the book identifier fields on `Product`. It exists
because these values are easy to confuse, expensive to get wrong, and impossible to
re-derive from memory later. If you are about to fill in a blank field, read this
first — several blanks are deliberate.

Verified 2026-07-26.

## The four identifier namespaces are not interchangeable

A single title can carry four different numbers, and only one of them belongs in
each field:

| Namespace | Example | Field | What consumes it |
|---|---|---|---|
| Print ISBN-13 | `9781491943205` | `print_isbn` | `get_gtin()` -> Google Merchant `<g:gtin>` |
| Retail EPUB ISBN-13 | `9781491943151` | `ebook_isbn` | `get_gtin()` fallback; `/book/<isbn>` lookup |
| Amazon Kindle ASIN | `B0725YT69J` | `ebook_asin` | `get_kindle_link()` -> `/dp/<asin>` |
| O'Reilly platform ID | `9781491943199` | **none — unused** | nothing |

**An ISBN can never build an Amazon URL.** `_amazon_url()` interpolates an ASIN into
`/dp/<asin>`; nothing in this codebase feeds an ISBN into that path. Putting an ISBN in
`ebook_asin` produces a dead buy button.

**The O'Reilly platform IDs are deliberately unused.** O'Reilly's `library/view/` URLs
carry a third ISBN-shaped number per title (100 -> `9781449359034`, 101 -> `9781491943199`,
102 -> `9781492050117`, 103 -> `9781098118792`). Nobody has been able to confirm what
O'Reilly calls these, because `oreilly.com` returns 403 to automated fetches. They are
recorded here only so a future maintainer does not "correct" our retail EPUB ISBNs to
them. If you have an O'Reilly author account, one look at a catalogue page settles it.

## Where values may be stored

`ebook_isbn` is **not** in `SEED_PROTECTED_FIELDS`, so it ships in
`main/fixtures/initial_products.yaml` through a normal PR.

`ebook_asin`, `print_asin`, `default_asin`, `external_product_id`, and `stock` **are**
protected (`main/management/commands/seed_products.py`). A fixture row carrying one makes
`seed_products` raise `CommandError` and exit non-zero; because `scripts/start-server.sh`
runs under `set -euo pipefail`, that stops the primary pod booting. **Enter ASINs through
the Django admin, never the fixture.**

## Current state

**Not all of this is live on `main` today, and the table would be a lie if it did not
say so.** As of this writing `main` carries **no** `ebook_isbn` for pks 100-103 and
**no** `ebook_asin` for any row at all.

- The `ebook_isbn` column becomes shipped state when PR #40 (`feat/ebook-isbns`) merges.
  Until then `/book/<ebook-isbn>` returns **404** for pks 100-103, because the value is
  simply not in the fixture that `seed_products` reads on a clean database.
- The `ebook_asin` column is **verified data, not shipped state**. No `ebook_asin` has
  ever been set on any row; the values below are the outcome of sourcing work and take
  effect only once they are actually stored (see "Where values may be stored" above).
  Until then no "Buy on Amazon (ebook)" button renders for any product.

Treat the table as *what each identifier is*, not as *what the database currently
contains*.

| pk | Title | `ebook_isbn` | `ebook_asin` | Bookshop e-book |
|---|---|---|---|---|
| 100 | Learning Spark, 1st ed. | `9781449359058` | `B00SW0TY8O` | none |
| 101 | High Performance Spark, 1st ed. | `9781491943151` | **intentionally blank** | none (print only) |
| 102 | Kubeflow for Machine Learning | `9781492050070` | `B08L5Q9W59` | yes, DRM-free |
| 103 | Scaling Python with Ray | `9781098118761` | `B0BNM6PQ9Q` | yes, DRM-free |
| 104 | DC4K, print | **blank by design** | none | not listed |
| 105 | DC4K, Executive Edition | **blank by design** | none | not listed |
| 106 | DC4K, e-book | `9781960595980` | none | not listed |
| 107 | Fast Data Processing with Spark | **blank — not found** | none | not listed |

## The deliberate blanks — do not "fix" these

**pk 101 has no Kindle ASIN because Amazon delisted the edition.** `B0725YT69J` was
genuinely the 1st edition's Kindle ASIN — it still appears in review markup on the live
1st-edition print page — but the product page has been deleted and returns 404 on every
Amazon domain (`.com`, `us`, `.co.uk`, `.ca`, `.de`) via both `/dp/` and `/gp/product/`.
There is no replacement. The 2nd edition's Kindle ASIN `B0H3CMNN3Q` **must not be used
here**: it is a different book (Karau + **Polak** + Warren, covering Spark 4.x) and
linking it from a 1st-edition listing would misrepresent the product.

**pks 104 and 105 are print-only by owner decision**, not by omission. There is no
separate Executive Edition e-book; buyers who want more are expected to pay what they
want. Do not borrow pk 106's ISBN for them.

**pk 107 has no e-book ISBN that anyone can vouch for.** The only value in circulation,
`9781782167075`, appears solely on Goodreads and is the print ISBN `9781782167068` with
the publisher item number incremented by one — the exact shape of a fabricated
identifier. It was rejected rather than shipped. `9781784392574` also surfaces in
searches for this title but is the **2nd edition** (Sankar & Karau), a different book.

**pk 100's Bookshop print link was removed rather than repointed** (landed on `main`
in PR #42). It used to be a
keyword search that returned "No results found", i.e. a live buy button landing on an
empty page. Bookshop has no 1st-edition listing; its only Learning Spark entry is the
2nd edition, which is a different book by different authors.

## How these were verified, and the limits of that

Each ISBN was sourced from retailers that state the format explicitly (Booktopia,
VitalSource, Kobo, ebooks.com) and then re-verified against a page carrying the e-book
ISBN **alongside the exact print ISBN in our fixture** — edition linkage, not title
matching. That is what resolved a genuine conflict on pk 101, where RedShelf lists
`9781491943175` (most likely the PDF) against `9781491943151` from two other retailers.

Bookshop.org independently reports the same EPUB ISBNs for pks 102 and 103, which is
useful corroboration from an unrelated source.

ASINs were sourced and then independently re-derived by a second agent; each was
confirmed by fetching the Amazon page and reading the title, subtitle, and full author
list, which is what distinguishes editions. Note that Amazon serves these bodies
gzip-compressed — grepping the raw response finds nothing and looks like a bot-block.

**A caveat that matters more than it sounds.** Every one of the thirteen candidate ISBNs
evaluated for these books — *including every value that was rejected as wrong* — passes
its ISBN-13 check digit. The check-digit test proves only that nobody mistyped a digit.
It is not evidence that a value is the right book, the right edition, or the right
format. Treat a green test as a floor, not as validation.

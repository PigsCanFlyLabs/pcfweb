# E-book identifiers: where each one lives and how it was sourced

This is the provenance record for the book identifier fields on `Product`. It exists
because these values are easy to confuse, expensive to get wrong, and impossible to
re-derive from memory later. If you are about to fill in a blank field, read this
first — several blanks are deliberate.

Verified 2026-07-26. DC4K launch state (pks 104-106) re-verified 2026-08-01,
when the book went live on Amazon.

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

`print_asin`, `default_asin`, `external_product_id`, and `stock` **are** protected
(`main/management/commands/seed_products.py`). A fixture row carrying one makes
`seed_products` raise `CommandError` and exit non-zero; because `scripts/start-server.sh`
runs under `set -euo pipefail`, that stops the primary pod booting. `stock` in particular
must stay protected — a deployment resetting inventory is a production incident.

`ebook_asin` **was** in that protected set until PR #44 moved it out, so the verified
Kindle ASINs ship in the fixture rather than being typed into the admin. The ownership
rule is asymmetric, and the asymmetry is the whole point:

- On a row where the fixture **sets** `ebook_asin` (pks 100, 102, 103, 106, 108), the
  fixture wins: an ASIN edited in the Django admin is **overwritten on every primary
  deployment**. Change those values by PR, not by admin.
- On a row where the fixture **omits** `ebook_asin` (pks 101, 104, 105, 107), the admin
  wins: `seed_products` calls `.update(**fixture_fields)` with only the keys present in
  the YAML, so an omitted key never reaches SQL and an admin-entered value survives
  redeployment. This is why pk 101's blank is a deliberate choice rather than a value
  waiting to be wiped.

## Current state

Both columns are shipped fixture state now: PR #40 landed the retail EPUB ISBNs and
PR #44 landed the fixture-owned ASINs, so `seed_products` writes everything in the
table below onto a clean database, `/book/<ebook-isbn>` resolves for every row with an
`ebook_isbn`, and the "Buy on Amazon (ebook)" button renders wherever an `ebook_asin`
is set. The table is both *what each identifier is* and *what a freshly seeded
database contains*.

Rows added since the original sourcing pass: pk 108 (High Performance Spark 2nd ed.)
arrived with its Kindle ASIN, and pk 106 gained its ASIN when DC4K launched on Amazon
in late July 2026 (verified 2026-08-01 from the Kindle listing's own page, with the
paperback listing's format swatch confirming edition linkage — see the fixture
comments on pks 104-106).

| pk | Title | `ebook_isbn` | `ebook_asin` | Bookshop e-book |
|---|---|---|---|---|
| 100 | Learning Spark, 1st ed. | `9781449359058` | `B00SW0TY8O` | none |
| 101 | High Performance Spark, 1st ed. | `9781491943151` | **intentionally blank** | none (print only) |
| 102 | Kubeflow for Machine Learning | `9781492050070` | `B08L5Q9W59` | yes, DRM-free |
| 103 | Scaling Python with Ray | `9781098118761` | `B0BNM6PQ9Q` | yes, DRM-free |
| 104 | DC4K, print | **blank by design** | none | not listed |
| 105 | DC4K, Executive Edition | **blank by design** | none | not listed |
| 106 | DC4K, e-book | `9781960595980` | `B0HC5Y42R2` | not listed |
| 107 | Fast Data Processing with Spark | **blank — not found** | none | not listed |
| 108 | High Performance Spark, 2nd ed. | **blank — not found** | `B0H3CMNN3Q` | unverifiable (bot wall) |

## The deliberate blanks — do not "fix" these

**pk 101 has no Kindle ASIN because Amazon delisted the edition.** `B0725YT69J` was
genuinely the 1st edition's Kindle ASIN — it still appears in review markup on the live
1st-edition print page — but the product page has been deleted and returns 404 on every
Amazon domain (`.com`, `us`, `.co.uk`, `.ca`, `.de`) via both `/dp/` and `/gp/product/`.
There is no replacement.

This finding has been **challenged once and re-confirmed**, so do not re-litigate it from a
search result. A reviewer asserted the ASIN was live and cited two descriptive-slug URLs
(`amazon.com/-/zh_TW/Holden-Karau-ebook/dp/B0725YT69J` and
`amazon.ca/High-Performance-Spark-Practices-Optimizing-ebook/dp/B0725YT69J`). Both return
**HTTP 404** with a ~2 KB dogs-of-Amazon body. A descriptive slug carries the title and
author names in the path and therefore *looks* authoritative, but it has no power to
resurrect a delisted ASIN — bare `/dp/`, `/gp/product/`, and slugged forms all 404
identically across `.com`, `.ca`, `.co.uk`, and `.de`, with no redirect to any other ASIN.

Two methodological traps make this specific check easy to get wrong in both directions:
Amazon serves bodies **gzip-compressed**, so grepping a raw response finds nothing and
resembles a bot-block; and a 404 is indistinguishable from rate-limiting unless you run a
positive control. The control here is `B0H3CMNN3Q` (the 2nd edition), which returns
**HTTP 200 with a 1.6 MB product page** from the same client, same UA, same moment. The
404s are therefore real delisting, not blocking. The 2nd edition's Kindle ASIN `B0H3CMNN3Q` **must not be used
here**: it is a different book (Karau + **Polak** + Warren, covering Spark 4.x) and
linking it from a 1st-edition listing would misrepresent the product.

**pks 104 and 105 are print-only by owner decision**, not by omission. There is no
separate Executive Edition e-book; buyers who want more are expected to pay what they
want. Do not borrow pk 106's ISBN for them — and do not give either of them pk 106's
Kindle ASIN, which belongs to the e-book row alone.

Two DC4K launch facts sit adjacent to this doc's scope and are recorded here so
nobody "completes" them wrongly: pk 104's `amazon_link` points at Amazon's paperback
listing, whose print run carries **Amazon's own ISBN-13, 9781960595010** — that number
must never enter `print_isbn`, `ebook_isbn`, or any other identifier field here (it
would become the row's GTIN and misidentify the direct-sold edition). And pk 105 has
**no Amazon listing at all, checked on 2026-08-01**: the Executive Edition is the
direct-store exclusive, so its absence from Amazon is the product working, not a link
waiting to be sourced.

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

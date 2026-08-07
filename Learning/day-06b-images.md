# Day 6b — Pictures Become Searchable

Personal learning log. Not read by Claude automatically — this is for me, to
recall what I built and why once the project is done. Not needed as context for
future sessions or development; it's a record, not a spec.

Day 6 was split in two. **6a** made a document's text searchable. **6b** is the
image half: the figures inside a PDF become retrievable alongside its prose, and
a scanned PDF stops being a failed upload.

---

## The idea in one paragraph

An embedding is a list of 1536 numbers describing *meaning* — similar things land
near each other. Day 6a turned text into those numbers. Cohere `embed-v4` can put
a **picture** into that same space, so a photo of a factory sits near the words
"factory". That means a question typed in words can return a chart, with no
captioning, no OCR, and no second search index. One space, two kinds of thing.

Before today, a bar chart in a report was invisible: no text, so no chunk, so it
could never be an answer.

---

## Part 0 — What this day did *not* need

No migration. No new package.

Migration 003 back on Day 6a already added `chunks.chunk_type`, `image_path` and
`page_number`, specifically because `chunks` was empty then and adding columns to
an empty table is instant. Doing it today would have meant re-ingesting and
re-paying for every embedding.

No Pillow either — PyMuPDF finds the pictures, crops them, shrinks them and
JPEG-encodes them by itself. I nearly added an image library out of habit.

**Lesson:** check what the library you already have can do before adding one that
does the same thing.

---

## Part 1 — Finding a picture

### 1. A PDF stores two completely different things that both look like pictures

- **Raster / bitmap** — actual pixels. A photo, a screenshot, a scanned page.
  Listed by `page.get_image_info()`.
- **Vector drawing** — not a picture at all, but instructions: "draw a blue
  rectangle here, then a line there". Every chart out of Excel, matplotlib or
  LaTeX is this. It appears in **no image listing whatsoever**.

So asking a PDF "what images do you have?" silently misses every chart — usually
the most valuable figure in a report. `page.cluster_drawings()` groups loose
vector strokes into boxes, which is the only way those become visible.

### 2. The actual trick: render the *region*, not the object

Both sources give the same thing — a rectangle on a page. Rather than extracting
the stored image object, the code screenshots that rectangle:

```python
page.get_pixmap(clip=box, dpi=…, colorspace=pymupdf.csRGB, alpha=False)
    .tobytes("jpeg", jpg_quality=80)
```

This is the whole design, and it solves three problems at once:

| Problem | Why rendering the region fixes it |
|---|---|
| Vector charts have no stored image to extract | Drawing instructions become pixels |
| One figure is often stored as a dozen tiled strips | The screenshot captures what a *reader* sees, not how it's stored |
| An extracted object may have the wrong colour space or an alpha channel | The render is always plain RGB, no transparency |

An earlier version of my thinking framed this as "image objects **vs** rendered
regions". That was wrong — it confused *what we go looking for* with *how we turn
it into a picture*. We always render regions. The only real choice was which
regions to look for.

### 3. Merging: a chart and its legend are one figure

`cluster_drawings` returns the legend as its own cluster. Embedded separately, a
legend would be stored as if it were an answer. So overlapping or touching boxes
are fused before anything else happens.

One subtlety that cost a while: `Rect.intersects()` is **false** for two boxes
that merely share an edge — and a figure sitting directly against its caption is
exactly that case. The fix is to also treat a valid-but-empty intersection as a
touch:

```python
if merged[i].intersects(merged[j]) or (merged[i] & merged[j]).is_valid:
```

It's O(n²) with a restart after each merge. Marked `ponytail:` and left alone —
a page has a few dozen boxes, not a few thousand.

### 4. The junk filters, and why the numbers live on their own lines

Raw candidates include bullets, dividers, table borders, underlines and logos.
Junk in the index is worse than junk on the page: a logo appearing on 40 pages
would be embedded 40 times and compete with real figures in *every* search.

```python
MIN_AREA_FRACTION = 0.03        # smaller than 3% of the page is a bullet or icon
MAX_ASPECT_RATIO = 8            # long and thin is a divider, never a figure
REPEAT_PAGE_FRACTION = 0.5      # same spot on over half the pages is furniture
REPEAT_MIN_PAGES = 3
REPEAT_EXEMPT_AREA_FRACTION = 0.5
MAX_IMAGES_PER_DOCUMENT = 75
MAX_IMAGE_PIXELS = 2_000_000
RENDER_DPI = 150
JPEG_QUALITY = 80
POSITION_TOLERANCE_POINTS = 5
```

Every one of these is a **guess about visual output**. They are named constants on
one line each precisely so tuning is a one-character edit, not surgery.

Order matters: cheap rejections (area, aspect ratio) run **before** the expensive
render, so we never pay to draw something we're about to throw away. The repeat
check runs last, because it can't know what repeats until every page has been
seen.

### 5. Checking the filters cost nothing, on purpose

`scripts/inspect_images.py` imports the real `find_images`, dumps every surviving
crop into a folder as a JPEG, and prints one line per rejected box naming the rule
that killed it:

```
page  21   428x37   at (112,85)   dropped: too thin (11.5:1)
page  56   428x14   at (112,306)  dropped: too small (< 3% of the page)
```

Testing this through the running app would have cost an upload plus a paid
embedding **per candidate — including the junk I was trying to delete**. The
script is free and runs in a second, so the numbers could be tuned and re-checked
freely. It imports the shipping function rather than copying it, so what I looked
at is exactly what runs in production.

**Lesson:** when a decision needs human eyes, build the cheapest possible way to
put it in front of them. The alternative isn't "test it later" — it's "never
actually check".

---

## Part 2 — What the real document taught me

### 6. The cap was cutting the wrong end of the document

My 85-page project report produced **69** figures that passed every filter. The
cap was 50. It kept the first 50 in reading order and dropped the rest — pages
57 through 83.

Which is the results and evaluation section. The single most valuable half of the
document, thrown away by a spend brake I'd set arbitrarily.

Raised to **75**. The cost of being wrong here was asymmetric and I hadn't
noticed: too low silently loses content, too high costs a few cents.

**Lesson:** a cap that truncates in document order doesn't drop "some" of the
data, it drops **the end** — and in a report, the end is the conclusions.

### 7. The repeat rule nearly deleted every scanned document

A scanned page is one full-page image in the identical position on every page.
That is also, exactly, the signature of a logo. The furniture filter would have
deleted all of it and left a scanned PDF with zero images — reproducing the very
failure 6b existed to fix.

```python
REPEAT_EXEMPT_AREA_FRACTION = 0.5   # no logo is half a page
```

Big boxes are exempt from the repeat rule. Two rules that are each individually
correct combined into a bug, and only a real scanned file would have shown it.

### 8. I had no scanned PDF, so I made one

```python
o.new_page(...).insert_image(page.rect, stream=page.get_pixmap(dpi=100)
                                                  .tobytes("jpeg", jpg_quality=70))
```

Screenshot three pages of a normal PDF, put each screenshot in as a full-page
image, save. No text layer at all — which is precisely what a scanner produces.

First attempt came out at **18.7 MB** (uncompressed pixels at 150 DPI) and was
refused by the 10 MB upload guard — an accidental confirmation that the guard
still works. JPEG at 100 DPI brought the same file to **0.21 MB**.

Result: 0 text chunks, 3 images, `ready`. Before today this file failed outright
with *"No readable text found in this file."*

---

## Part 3 — Making an image a chunk

### 9. An image chunk is just a chunk that carries a JPEG

```python
@dataclass(frozen=True)
class Chunk:
    index: int
    content: str
    page_number: int
    token_count: int
    image: bytes | None = None
```

One type, one loop, one insert path. `content` holds a label — `"[Image from
page 41]"` — because `chunks.content` is `not null` in the schema. **The label is
never what gets embedded.** The picture is. The label exists so Day 8 has
something to print next to the image.

### 10. Images go last, and that is load-bearing

`items = text_chunks + image_chunks`, with image indices continuing straight on
from the text: 0–90 text, 91–159 images.

A step resumes from `max(chunk_index)` already in the database. Appending images
means a resumed step never re-renders a picture it already stored, and that
number keeps meaning what it meant on Day 6a.

### 11. Determinism matters more for images than it did for text

If `find_images` returned its results in a different order on a second call — a
set iteration, a dict ordering, a float comparison landing differently — then
"resume at chunk 120" would point at a **different picture**. Image A's vector
would be stored against image B's file. No exception. Nothing in any log. Search
returns the wrong picture, forever.

So boxes are sorted into reading order (page, then y, then x) before anything is
dropped, and the self-check builds a PDF in memory and asserts:

```python
assert first_images == second_images, \
    "find_images is not deterministic — resume would store a vector against the wrong picture"
```

Written **before** anything touched Supabase or cost money. That was the hinge of
the whole day.

### 12. Text and pictures can't travel in the same Cohere call

`embed(texts=…)` and `embed(images=…)` are separate requests, so a batch
straddling the text→image boundary would be impossible to send. `_batches()`
splits the work into runs of one kind:

- Text: 96 per request.
- Images: 8 per request — the limit is 20 MB *combined*, not a count, and a
  smaller batch also loses less paid work when a rate limit interrupts.

Verified rather than assumed: **embed-v4 has no limit on how many images one
request may carry.** The "~2 megapixel cap" I wrote down on Day 6a could **not**
be found anywhere in the installed SDK — I corrected the 6a log. 2 MP is a
self-imposed guideline; 20 MB combined is Cohere's actual rule.

### 13. Storage: file first, row second — and the path is not random

```python
image_path = f"{user_id}/{document_id}/img-{item.index}.jpg"
bucket.upload(path=image_path, file=item.image,
              file_options={"content-type": "image/jpeg", "upsert": "true"})
```

- **File before row**, same as `upload_document`. A row pointing at a file that
  was never written is a broken document that looks fine.
- **Deterministic path plus `upsert`** means a replayed step *overwrites its own
  file* instead of leaving a duplicate.
- `storage.foldername(...)[1]` is still the user id, so the storage RLS policy
  from `001_init.sql` passes with no change.

That second point stopped being theoretical. A step died mid-run (see §16) and
was replayed — and Storage still held **exactly 69 files**, not 70. The design
paid off under precisely the condition it was written for.

### 14. Who gets to decide a document has failed

`parse()` used to raise when a file yielded no text. Now it returns `[]`, and the
router fails the document only when there is **neither** text **nor** images.

The reason is about knowledge, not style: `parse` can only see half the picture.
A scanned PDF has no text and is now perfectly ingestible. Only the caller sees
both halves, so only the caller can judge.

**Lesson:** the decision belongs wherever all the information is. A function that
can only see part of the problem shouldn't be the one throwing.

### 15. Delete had been leaking files since Day 5

`chunks` rows vanish on their own — the database cascades them. Storage objects do
not. So every deleted document was quietly leaving its images in the bucket
forever: paid for, and appearing in no listing anywhere.

Fixed by selecting `image_path` from the chunks *before* deleting the row, then
passing all of those paths to the existing `remove()` call alongside the original
PDF. Verified: deleting the scanned document removed both the `.pdf` and the
entire `{document-id}/` folder.

Residual gap, marked `ponytail:` rather than papered over: this finds images that
made it into a row. A step that died between uploading a JPEG and inserting its
row leaves that one file behind. Listing the folder prefix would catch those too,
but its pagination behaviour is unverified, and no orphan has ever actually
appeared.

---

## Part 4 — The bug that was hiding since Day 6a

### 16. `"exp" claim timestamp check failed`

Mid-upload, Supabase refused a write:

```
{'statusCode': 403, 'error': Unauthorized, 'message': "exp" claim timestamp check failed}
```

The Clerk token had expired *during* the step.

**My first explanation was wrong.** I said the 45-second clock started after the
document was downloaded, parsed and all 69 images were rendered — so rendering
time came free on top of the budget. Plausible. So I measured it:

```
parse+chunk  0.2s -> 91 chunks
find_images  0.9s -> 69 images
```

1.1 seconds. Not the cause, not close. **Measuring took thirty seconds and stopped
me fixing the wrong thing.**

**The real cause:** `ingest_step` worked to a hard-coded 45 seconds while having
no idea how much life its token actually had. `get_current_user` decodes the
token — including its `exp` claim — and then throws everything except `sub` away.
And Clerk's `getToken()` hands the browser a **cached** token, minting a new one
only when the current one is nearly dead. So a step can begin holding 15 seconds
of validity and then work for 45.

**Why Day 6a never hit it:** the trial key's rate limit cut text steps short after
two or three batches, so they almost never ran the full budget. Images embed with
far fewer tokens, so 6b's steps finally ran to the ceiling and exposed a bug that
had been there all along.

### 17. The fix: stop guessing, and reclassify the error

Two changes, doing different jobs.

**Stop it happening** — use the token's own expiry as the deadline:

```python
budget = min(
    STEP_BUDGET_SECONDS,
    expires_at - time.time() - TOKEN_SAFETY_MARGIN_SECONDS,
)
```

A new `get_token_expiry` dependency returns the `exp` claim. It *depends on*
`get_current_user`, so FastAPI has already verified that exact token's signature
and expiry before it runs — reading the claim again without re-verifying is safe,
and decoding twice would just repeat work. `CurrentUser` was left untouched so the
other five handlers needed no changes.

The clock also now starts at the top of the request rather than after parsing, so
the budget covers the whole thing.

**Make it harmless if it happens anyway** — an expired token is *temporary*. The
next step arrives with a fresh one. It belongs in the same category as a rate
limit, not the same category as a corrupt file:

```python
except Exception as exc:
    if not _token_expired(exc):
        raise
    logger.warning("Token expired mid-step at chunk %s of %s", written, total)
    break        # keep the progress, stay `processing`
```

This is exactly §15 of the Day 6a log applied to a second kind of temporary
error. I wrote that lesson down and still had to learn it twice.

**Lesson:** when code needs to know something, check whether it already has it and
is discarding it. The expiry was decoded, on every request, and thrown away.

**Honest limit:** the fix is only partly proven. It ran successfully in a real
request, so it doesn't break anything — but that document finished in seconds, so
the clamp never had to bite and the `break` path has never executed. It gets its
real test on the next long document.

---

## Part 5 — Verification

| Check | What it proved |
|---|---|
| Self-check: `first_images == second_images` | The hinge. Resume cannot mismatch a vector to the wrong picture |
| `inspect_images.py`, crops opened by eye | The junk rules work on a real document — no logos, no dividers, charts present |
| `min(vector_dims) = max(vector_dims) = 1536` | Cohere accepted our JPEG data URIs and returned the width the column and HNSW index require |
| 91 text + 69 images = 160 chunks | Both kinds written; matches the local count exactly |
| `count(distinct chunk_index) = 160`, `min = 0`, `max = 159` | Text and images form **one unbroken sequence** across the boundary |
| 0 missing vectors, 0 image chunks without a file | No half-written image chunk |
| Exactly 69 files in Storage after a mid-run failure and replay | Deterministic paths + upsert really do prevent duplicates |
| Scanned PDF → `ready`, 3 images, 0 text | A previously-refused document is now useful |
| Delete → both the `.pdf` and the image folder gone | The Storage leak is closed |

---

## Gotchas worth remembering

- **A PDF's charts are not images.** They're vector drawing instructions and
  appear in no image listing. `cluster_drawings()` plus a clip render is the only
  way to see them.
- **`Rect.intersects()` is false for boxes that merely touch.** Also test whether
  the intersection is valid-but-empty, or a figure and its caption stay separate.
- **A cap applied in document order truncates the end**, and in a report the end
  is the conclusions.
- **Two individually-correct filters can combine into a bug.** "Same spot on every
  page = furniture" plus "a scanned page is a full-page image" = delete the whole
  document.
- **embed-v4 takes images as data URIs** (`data:image/jpeg;base64,…`), no count
  limit, 20 MB combined per request. `input_type="image"` is its own value, and
  text and images cannot be mixed in one call.
- **Clerk's `getToken()` returns a cached token.** Never assume a request arrives
  with a full token lifetime — read `exp` if the work is long.
- **Measure before fixing.** My confident explanation of the token bug was wrong,
  and one 30-second timing run caught it before I changed the wrong code.
- **You can build a scanned PDF in five lines of PyMuPDF** by re-inserting
  rendered pages as images. Compress them, or you'll blow your own upload limit.

---

## Still open

- **Day 7 must retrieve both kinds** and pass images to a vision model, and it
  must use `input_type="search_query"` — silent quality loss otherwise.
- **Day 8 needs signed URLs** to display images beside an answer. Store the
  *path* in `messages.sources`, never the signed URL: those expire, and saved
  conversations would show broken images forever.
- **The modality gap.** In a text-heavy corpus, images may be under-retrieved.
  Measure it in Day 11's eval. The escalation is embedding image + short caption
  interleaved into one vector, which embed-v4 supports natively. Do not build it
  up front.
- **Old documents stay text-only.** No backfill: it would need a hole in the
  "already ready" guard, and "has it got image chunks?" cannot distinguish "never
  scanned" from "scanned, found none". Delete and re-upload instead.
- **The token fix needs a long document** to actually exercise it.

**What this achieves overall:** a document is no longer just its words. A PDF now
becomes 91 pieces of prose *and* 69 figures, all sitting in one shared meaning
space, so a question asked in English can return the chart that answers it. A
scanned PDF — previously a hard failure — is now an ordinary document. Day 7 has
something visual to retrieve for the first time.

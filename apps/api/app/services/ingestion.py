"""Turning an uploaded file into embedded chunks.

Everything here is a plain function: no FastAPI, no routing, no database writes.
The endpoint in `routers/documents.py` orchestrates these; Day 7 re-uses `embed`
for the user's question and Day 11's eval imports `chunk` directly.

One property in this file is load-bearing. Ingestion runs as a sequence of
separate HTTP requests, each picking up from the last `chunk_index` already in
the database. That only works if `parse` and `chunk` are **deterministic** — the
same bytes must always produce the same list, in the same order. If they didn't,
"resume at chunk 40" would mean a different piece of text on every request. The
self-check at the bottom of this file exists to defend exactly that.
"""

from __future__ import annotations

import base64
import codecs
import io
import logging
import math
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache

import cohere
import pymupdf
import tiktoken
from docx import Document as DocxDocument
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.config import get_settings

logger = logging.getLogger(__name__)

EMBED_MODEL = "embed-v4.0"
EMBED_DIMENSIONS = 1536

# `-fast`, not `-pro`: this app is a self-funded, rate-limited demo (per-user
# daily caps, no BYOK — see CLAUDE.md's non-goals), so cost per question is a
# design constraint, not an afterthought. `-pro` scores a little higher; Day
# 11.5's before/after retrieval numbers are what decide if that trade is worth
# revisiting.
RERANK_MODEL = "rerank-v4.0-fast"

# Cohere rejects a batch larger than this. Kept as a constant so a rejected
# request points at one line rather than a number buried in a loop.
EMBED_BATCH_SIZE = 96

# embed-v4 sets no limit on how many images one request may carry; the ceiling
# is 20MB of them combined. Eight crops at roughly 100KB each leaves an
# enormous margin, and a smaller batch also loses less work to a rate limit.
IMAGE_EMBED_BATCH_SIZE = 8

# ...and the margin was assumed rather than enforced, which is the whole of
# finding 20. "Roughly 100KB each" describes a figure cropped out of a text
# page; `MAX_IMAGE_PIXELS` below permits a crop twenty times that, and a scanned
# document is one full-page image per page. Base64 then adds a third. Eight
# scanned pages is 11-16MB, and the request is refused for its size.
#
# What made that a permanent failure rather than a bad minute: a size rejection
# is not `TooManyRequestsError`, so `ingest_step`'s transient list does not
# catch it and the document is marked `failed` — and batching was a fixed
# stride, so every retry rebuilt the identical oversized batch and failed
# identically. A scanned PDF could never be ingested at all.
#
# 16MB rather than 20: the ceiling is on the whole request, and the JSON
# envelope, the field names and the data-URI prefixes are all counted by the
# server and none of them are image bytes.
MAX_IMAGE_REQUEST_BYTES = 16 * 1024 * 1024

# The prefix Cohere requires on each image. Counted because it is part of the
# string that gets sent, and 8 of them is not nothing next to a byte ceiling.
_DATA_URI_PREFIX = "data:image/jpeg;base64,"


def _image_batches(jpegs: list[bytes]) -> Iterator[list[bytes]]:
    """Group pictures into requests that fit, by size as well as by count.

    Yields at least one image per batch even when that image alone is over the
    ceiling. A picture cannot be split, so the alternative is an empty batch and
    an infinite loop; sending it alone at least earns a specific error about
    that one file instead of dragging seven innocent pages down with it.
    """
    batch: list[bytes] = []
    batch_bytes = 0

    for jpeg in jpegs:
        # Base64 is 4 characters per 3 bytes, rounded up. Computed rather than
        # encoded: encoding here just to measure would hold a second copy of
        # every picture in memory, which is the other way to fall over.
        encoded = 4 * ((len(jpeg) + 2) // 3) + len(_DATA_URI_PREFIX)

        if batch and (
            len(batch) >= IMAGE_EMBED_BATCH_SIZE
            or batch_bytes + encoded > MAX_IMAGE_REQUEST_BYTES
        ):
            yield batch
            batch, batch_bytes = [], 0

        batch.append(jpeg)
        batch_bytes += encoded

    if batch:
        yield batch

    # In plain English: walk the pictures one at a time, adding each to the
    # current pile. Before adding, check whether it would make the pile too many
    # or too heavy — if so, send the pile as it stands and start a fresh one.
    # The `batch and` at the front is what stops an over-sized picture producing
    # an empty pile forever: a pile that is still empty always accepts the next
    # picture, whatever it weighs.

# CLAUDE.md locks these in: 800 tokens with 100 of overlap. The overlap is why a
# sentence split across two chunks is still findable — both copies contain it.
CHUNK_SIZE_TOKENS = 800
CHUNK_OVERLAP_TOKENS = 100

# --- Picking pictures out of a page ---------------------------------------
#
# Every one of these is a guess tuned by eye with `scripts/inspect_images.py`,
# which is why they are named constants on one line each rather than numbers
# buried in the filter.
#
# A box smaller than this fraction of the page is a bullet, an icon or a rule.
MIN_AREA_FRACTION = 0.03
# Long and thin means a divider line or an underline, never a figure.
MAX_ASPECT_RATIO = 8
# A box in the same spot on this share of the pages (and at least
# REPEAT_MIN_PAGES of them) is furniture: a logo, a header, a footer.
REPEAT_PAGE_FRACTION = 0.5
REPEAT_MIN_PAGES = 3
# ...unless it is huge. A scanned page is one full-page image in the identical
# spot on every page, and the repeat rule would otherwise delete the entire
# document. No logo is half a page.
REPEAT_EXEMPT_AREA_FRACTION = 0.5
# ...or unless the picture actually changes. How many pages of a repeated
# position to render before believing it is furniture. A logo is byte-identical
# everywhere, so two samples already settle it; a slide deck's charts differ on
# the first comparison. Sampling instead of rendering the lot keeps a 200-page
# report with a logo at three renders rather than two hundred.
REPEAT_SAMPLE_PAGES = 3
# Spend brake: each image is a billed embedding and a Storage object. Raised
# from 50 after a real 85-page project report came in at 69 figures — the cap
# was cutting its results section, which is the half worth retrieving.
MAX_IMAGES_PER_DOCUMENT = 75
# Cohere's verified limit is 20MB combined per request, not a pixel count; this
# is a safety guideline that also keeps one JPEG small enough to batch freely.
MAX_IMAGE_PIXELS = 2_000_000
RENDER_DPI = 150
JPEG_QUALITY = 80
# Coordinates are floats. Rounded to this many points before being compared for
# "same spot on many pages", so a half-point of drift doesn't hide a logo.
POSITION_TOLERANCE_POINTS = 5


@dataclass(frozen=True)
class Chunk:
    """One slice of a document, ready to be embedded and stored.

    An image chunk is the same thing carrying a JPEG: `image` holds the picture
    and `content` starts out as a placeholder label like "[Image from page 4]",
    because `chunks.content` is `not null` in the schema. Day 12: the caller
    (`routers/documents.py`) replaces this placeholder with a real caption from
    `rag.caption_image` before the row is written, and embeds that caption
    instead of the picture — a typed question can match a caption's words in a
    way it never could a raw pixel vector. `Chunk.content` itself stays the
    placeholder; it exists only to satisfy the `not null` column until then.
    """

    index: int
    content: str
    page_number: int
    token_count: int
    image: bytes | None = None


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def download(supabase, file_path: str) -> bytes:
    """Fetch the original file back out of Supabase Storage.

    Takes the caller's Supabase client rather than building one, so this read is
    subject to the same storage RLS policy as everything else — the `{user_id}/`
    path prefix check from 001_init.sql.
    """
    return supabase.storage.from_("documents").download(file_path)


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------


def parse(data: bytes, mime_type: str | None) -> list[tuple[int, str]]:
    """Extract text as `(page_number, text)` pairs.

    Page numbers are 1-based and survive into `chunks.page_number`, so a citation
    on Day 8 can say which page an answer came from. DOCX and TXT have no pages
    in any meaningful sense, so they report page 1 rather than inventing a number
    that would be wrong.

    Returns `[]` when a file yields no text at all, rather than raising. A
    scanned PDF is a stack of photographs with no text layer, and since Day 6b
    those photographs are themselves embeddable — such a document ingests as
    images only. Only the caller can see both halves, so only the caller can
    decide that a file with neither text nor pictures is a failure.

    An unreadable *file type* still raises here: that is a fact about the file,
    knowable without looking at anything else.
    """
    if mime_type == "application/pdf":
        pages = _parse_pdf(data)
    elif mime_type == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ):
        pages = _parse_docx(data)
    elif mime_type == "text/plain":
        pages = _parse_txt(data)
    else:
        raise ValueError(f"Cannot read files of type {mime_type!r}.")

    # Drop pages that are blank or whitespace-only: they would otherwise become
    # empty chunks, and an empty chunk costs an embedding call to say nothing.
    return [(number, text) for number, text in pages if text.strip()]


def _parse_pdf(data: bytes) -> list[tuple[int, str]]:
    # `stream=` keeps the file in memory. Writing it to a temp file first would
    # mean cleaning that file up on every error path.
    with pymupdf.open(stream=data, filetype="pdf") as document:
        return [(number, page.get_text()) for number, page in enumerate(document, start=1)]


def _parse_docx(data: bytes) -> list[tuple[int, str]]:
    document = DocxDocument(io.BytesIO(data))
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    # ponytail: paragraphs only — text inside tables is skipped. Add table
    # extraction if a real document loses content to this.
    return [(1, text)]


# Above this share of unreadable characters, a "text" file is not text we can
# use. Deliberately loose: a long document with a handful of stray bytes stays
# under it, which is the case `errors="replace"` was chosen for in the first
# place.
UNUSABLE_TEXT_RATIO = 0.1


def _parse_txt(data: bytes) -> list[tuple[int, str]]:
    # Notepad's "Unicode" option writes UTF-16, and on this project's own OS that
    # is one menu click away. Decoded as UTF-8 it does not fail — every second
    # byte is a NUL, which is a perfectly legal character — so it would pass the
    # ratio check below and get embedded as text riddled with nulls. The byte
    # order mark is the one reliable tell, and testing for it costs a comparison.
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return [(1, data.decode("utf-16"))]

    try:
        return [(1, data.decode("utf-8-sig"))]
    except UnicodeDecodeError:
        pass

    # `errors="replace"` rather than a hard failure: one bad byte in an otherwise
    # fine file should not cost the user the whole upload.
    text = data.decode("utf-8", errors="replace")

    # But a file that is *mostly* replacement characters is not a file with one
    # bad byte — it is a file in an encoding we did not recognise, and embedding
    # it stores expensive nonsense that no search will ever match. Silence is the
    # danger here: nothing errors, nothing warns, and the damage only shows up
    # later as retrieval that inexplicably misses.
    unusable = text.count("�") + text.count("\x00")
    if unusable > len(text) * UNUSABLE_TEXT_RATIO:
        raise ValueError(
            "This file is not readable as UTF-8 text. Re-save it with UTF-8 "
            "encoding and upload it again."
        )

    return [(1, text)]

    # In plain English: try the encodings that can be identified for certain
    # first — UTF-16 announces itself with a byte order mark, and a strict UTF-8
    # decode either works or raises. Only when both have been ruled out do we
    # fall back to decoding loosely, replacing anything unreadable with "�".
    #
    # Then count the damage. A few "�" in a long file is the tolerable case the
    # loose decode exists for. A file that is one tenth "�" or more was never
    # UTF-8, and is refused with a message telling the user what to change.


# ---------------------------------------------------------------------------
# Chunk
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _encoding() -> tiktoken.Encoding:
    """Tokeniser used to measure chunk length.

    This is OpenAI's tokeniser, and Cohere's is not identical — so the count is
    an approximation, off by a few percent. That is fine here: it decides where
    to cut text, not what to bill. Cached because building it reads a file.
    """
    return tiktoken.get_encoding("cl100k_base")


def _count_tokens(text: str) -> int:
    return len(_encoding().encode(text))


@lru_cache(maxsize=1)
def _splitter() -> RecursiveCharacterTextSplitter:
    """Splits on paragraph breaks first, then sentences, then words.

    "Recursive" means it tries the biggest separator that still gets a chunk
    under the limit, so a chunk usually ends at a natural boundary instead of
    mid-sentence.
    """
    return RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE_TOKENS,
        chunk_overlap=CHUNK_OVERLAP_TOKENS,
        length_function=_count_tokens,
    )


def chunk(pages: list[tuple[int, str]]) -> list[Chunk]:
    """Slice parsed pages into overlapping chunks, numbered from 0.

    Deterministic by construction: pages are processed in order, the splitter has
    no randomness, and the index is a running counter. Same input, same output,
    every time — which is what lets a later request resume from an index.
    """
    chunks: list[Chunk] = []

    for page_number, text in pages:
        for piece in _splitter().split_text(text):
            # The splitter can emit whitespace-only pieces at page boundaries.
            if not piece.strip():
                continue
            chunks.append(
                Chunk(
                    index=len(chunks),
                    content=piece,
                    page_number=page_number,
                    token_count=_count_tokens(piece),
                )
            )

    return chunks


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImageRegion:
    """One picture cut out of a page, already encoded as a JPEG."""

    page_number: int
    jpeg: bytes


def _candidate_boxes(page) -> list[pymupdf.Rect]:
    """Every rectangle on this page that might be a picture.

    Two sources, because a PDF stores the two kinds of picture completely
    differently. A photo is *raster*: actual pixels, listed by
    `get_image_info()`. A chart out of Excel or LaTeX is a *vector drawing*:
    instructions like "draw a blue rectangle here", which appear in no image
    listing at all. `cluster_drawings()` groups those strokes into boxes.

    Asking only for the image list would silently miss every chart — usually the
    most valuable figure in a report.
    """
    page_rect = page.rect
    boxes: list[pymupdf.Rect] = []

    for info in page.get_image_info():
        boxes.append(pymupdf.Rect(info["bbox"]))

    boxes.extend(pymupdf.Rect(box) for box in page.cluster_drawings())

    # Clip to the page: a bbox can extend past the paper, and rendering outside
    # it produces bands of blank pixels.
    clipped = [box & page_rect for box in boxes]
    return [box for box in clipped if not box.is_empty and box.is_valid]


def _merge_overlapping(boxes: list[pymupdf.Rect]) -> list[pymupdf.Rect]:
    """Fuse boxes that touch or overlap into one.

    A chart and its legend are two clusters but one figure; a tiled photograph
    is a dozen strips of one picture. Without this we would embed the fragments
    separately and store a legend as if it were an answer.

    ponytail: O(n^2) restart-on-merge. Fine at the few-dozen boxes a page has;
    switch to an interval sweep if a pathological page ever crawls.
    """
    merged = list(boxes)
    changed = True

    while changed:
        changed = False
        for i in range(len(merged)):
            for j in range(i + 1, len(merged)):
                # `intersects` is False for boxes that merely share an edge, and
                # a figure abutting its caption is exactly that. The
                # intersection of two disjoint boxes is invalid; of two touching
                # ones it is empty but valid — which is the case this adds.
                if merged[i].intersects(merged[j]) or (merged[i] & merged[j]).is_valid:
                    merged[i] = merged[i] | merged[j]
                    del merged[j]
                    changed = True
                    break
            if changed:
                break

    return merged


def _looks_like_a_figure(box: pymupdf.Rect, page_area: float) -> str | None:
    """`None` if the box is worth keeping, otherwise the rule that rejected it.

    Returning the reason rather than a bool is what lets
    `scripts/inspect_images.py` tell you *why* your chart disappeared.
    """
    if box.width <= 0 or box.height <= 0:
        return "empty"
    if page_area <= 0 or (box.width * box.height) / page_area < MIN_AREA_FRACTION:
        return f"too small (< {MIN_AREA_FRACTION:.0%} of the page)"
    ratio = max(box.width, box.height) / min(box.width, box.height)
    if ratio > MAX_ASPECT_RATIO:
        return f"too thin ({ratio:.1f}:1)"
    return None


def _position_key(box: pymupdf.Rect) -> tuple[int, int, int, int]:
    tolerance = POSITION_TOLERANCE_POINTS
    return tuple(round(value / tolerance) for value in (box.x0, box.y0, box.x1, box.y1))


def _renders_identically(document, group: list[tuple[int, pymupdf.Rect]]) -> bool:
    """True when every sampled copy of this box is the same picture.

    The one thing that separates a logo from a slide deck's charts: both repeat
    in the same spot on many pages, but only the logo repeats the same *content*.
    Position alone used to decide this, which silently deleted every chart in a
    deck of slides — one chart per slide, all in the same placeholder.

    Stops at the first difference, so the expensive case (a real figure) is also
    the cheap one.
    """
    first = None
    for page_number, box in group[:REPEAT_SAMPLE_PAGES]:
        rendered = _render(document[page_number - 1], box)
        if first is None:
            first = rendered
        elif rendered != first:
            return False
    return True


# In plain English: draw the same rectangle from the first few pages it turns up
# on and see whether the pictures come out identical. A company logo does. A
# different chart on every slide does not — and that is the whole difference
# between something worth deleting and something worth keeping.


def _render(page, box: pymupdf.Rect) -> bytes:
    """Screenshot this rectangle of this page as a JPEG.

    Rendering the *region* is the whole trick: it is the only thing that turns a
    vector chart into a picture at all, and it captures a fragmented figure as
    the single image a reader sees rather than as its stored pieces.

    The DPI drops for a large region so no crop blows past MAX_IMAGE_PIXELS —
    a page is measured in points (1/72 inch), so pixels = points * dpi / 72.
    """
    area_points = max(box.width * box.height, 1.0)
    dpi = min(RENDER_DPI, 72 * math.sqrt(MAX_IMAGE_PIXELS / area_points))
    pixmap = page.get_pixmap(
        clip=box,
        dpi=max(int(dpi), 1),
        colorspace=pymupdf.csRGB,
        # JPEG has no transparency; flattening here rather than letting the
        # encoder guess a background.
        alpha=False,
    )
    return pixmap.tobytes("jpeg", jpg_quality=JPEG_QUALITY)


def find_images(
    data: bytes,
    mime_type: str | None,
    on_reject=None,
) -> list[ImageRegion]:
    """Every figure worth embedding, in a fixed order. PDFs only.

    Deterministic for the same reason `chunk` is, and it matters for the same
    reason: image chunks continue the text's `chunk_index` numbering, and a
    resumed step trusts that index to mean the same picture it meant last time.
    Boxes are therefore sorted by position — reading order, top to bottom — and
    never by anything that could vary between runs.

    `on_reject(page_number, box, reason)` is called for every discarded box. The
    app passes nothing; `scripts/inspect_images.py` passes a printer, so tuning
    the thresholds looks at the code that actually ships rather than a copy.

    DOCX and TXT return `[]`: DOCX images are a Day 7+ question and a TXT file
    has none.
    """
    if mime_type != "application/pdf":
        return []

    with pymupdf.open(stream=data, filetype="pdf") as document:
        # (page number, box, share of the page it covers)
        kept: list[tuple[int, pymupdf.Rect, float]] = []

        for page_number, page in enumerate(document, start=1):
            page_area = page.rect.width * page.rect.height
            for box in _merge_overlapping(_candidate_boxes(page)):
                reason = _looks_like_a_figure(box, page_area)
                if reason:
                    if on_reject:
                        on_reject(page_number, box, reason)
                    continue
                kept.append((page_number, box, (box.width * box.height) / page_area))

        # Reading order, top to bottom, before anything is dropped — so the cap
        # below cuts the tail of the document rather than an arbitrary set.
        kept.sort(key=lambda item: (item[0], round(item[1].y0, 2), round(item[1].x0, 2)))

        # Page furniture: the same box, holding the same picture, on many pages.
        # A logo on 40 pages would otherwise be embedded 40 times and compete
        # with real figures in every search — junk in the index is worse than
        # junk on the page.
        #
        # Two escape hatches, because "same spot on many pages" describes real
        # content just as well as it describes furniture:
        #
        # 1. Big boxes are exempt. A scanned document is one full-page image per
        #    page in the identical spot, and this rule would delete all of it.
        # 2. What survives that is dropped only if it renders to the identical
        #    picture every time. A slide deck puts a *different* chart in the
        #    same placeholder on every slide at roughly a third of the page —
        #    too small for the first hatch — so position alone discarded the
        #    entire deck and reported `images_total: 0` as a success.
        by_position: dict[tuple[int, int, int, int], list[tuple[int, pymupdf.Rect, float]]] = (
            defaultdict(list)
        )
        for item in kept:
            by_position[_position_key(item[1])].append(item)

        threshold = max(REPEAT_MIN_PAGES, len(document) * REPEAT_PAGE_FRACTION)
        furniture = {
            key
            for key, group in by_position.items()
            if len(group) >= threshold
            and all(fraction < REPEAT_EXEMPT_AREA_FRACTION for _, _, fraction in group)
            and _renders_identically(document, [(number, box) for number, box, _ in group])
        }

        survivors: list[tuple[int, pymupdf.Rect]] = []

        for page_number, box, _ in kept:
            key = _position_key(box)
            if key in furniture:
                if on_reject:
                    on_reject(
                        page_number,
                        box,
                        f"furniture — same picture on {len(by_position[key])} pages",
                    )
                continue
            survivors.append((page_number, box))

        for page_number, box in survivors[MAX_IMAGES_PER_DOCUMENT:]:
            if on_reject:
                on_reject(page_number, box, f"over the {MAX_IMAGES_PER_DOCUMENT}-image cap")

        return [
            ImageRegion(page_number=number, jpeg=_render(document[number - 1], box))
            for number, box in survivors[:MAX_IMAGES_PER_DOCUMENT]
        ]


# ---------------------------------------------------------------------------
# Embed
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _cohere() -> cohere.ClientV2:
    # Built on first use, not at import time, so the self-check below and any
    # test of `chunk` run without needing a real API key.
    return cohere.ClientV2(api_key=get_settings().cohere_api_key)


def embed(texts: list[str], input_type: str = "search_document") -> list[list[float]]:
    """Turn text into 1536-dimension vectors.

    `input_type` is not cosmetic. Cohere embeds a stored passage and a search
    query differently, and mixing them up degrades retrieval *silently* — no
    error, just worse answers. Ingestion uses the default `search_document`;
    Day 7 must pass `search_query` for the user's question.

    Uses the Cohere SDK rather than LiteLLM on purpose. LiteLLM's Cohere path
    infers text-vs-image by sniffing for base64, so a call is all-text or
    all-image and `input_type` is not a first-class parameter — which would block
    Day 6b's interleaved text+image vectors. LiteLLM remains our chat client.

    `output_dimension` is pinned rather than left to the model's default: these
    vectors go into a `vector(1536)` column with an HNSW index built for that
    width, and a different width is rejected by Postgres.
    """
    vectors: list[list[float]] = []

    for start in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[start : start + EMBED_BATCH_SIZE]
        response = _cohere().embed(
            model=EMBED_MODEL,
            texts=batch,
            input_type=input_type,
            output_dimension=EMBED_DIMENSIONS,
            embedding_types=["float"],
        )
        # `float_` with a trailing underscore: `float` is a Python builtin, so
        # the SDK cannot use the bare name.
        vectors.extend(response.embeddings.float_ or [])

    if len(vectors) != len(texts):
        # Silence here would mean chunk N getting chunk N+1's vector — every
        # search subtly wrong, with nothing in the logs.
        raise RuntimeError(
            f"Cohere returned {len(vectors)} vectors for {len(texts)} inputs."
        )

    return vectors


def rerank_documents(query: str, documents: list[str], top_n: int) -> list[int]:
    """Score `documents` against `query` and return their original indexes,
    best match first, truncated to `top_n`.

    A second, slower pass over a small candidate set — unlike `embed`, this
    can't be precomputed at ingestion time, because it scores each document
    against *this* question, not against documents in isolation. That's also
    why it only ever runs over the handful of candidates `retrieve()` already
    narrowed things down to, never the whole corpus.

    Returns indexes rather than reordering `documents` itself: the caller
    (`rag.rerank`) holds the full chunk dicts this function never sees, and
    Cohere's response already comes back as `{index, relevance_score}` pairs —
    passing indexes through is less code than rebuilding chunk dicts here.
    """
    if not documents:
        return []

    response = _cohere().rerank(
        model=RERANK_MODEL,
        query=query,
        documents=documents,
        top_n=min(top_n, len(documents)),
    )
    return [result.index for result in response.results]

    # In plain English: hand Cohere the question and the list of candidate
    # texts. It scores each one for how well it actually answers the question
    # (not just how similar it *sounds*), and hands back which ones scored
    # best, in order, as positions in the list we sent — not the texts
    # themselves. `min(top_n, len(documents))` is there so asking for 5 out of
    # a candidate list that only has 3 doesn't error.


def embed_images(jpegs: list[bytes]) -> list[list[float]]:
    """Turn pictures into vectors in the same space as `embed`'s text.

    That shared space is the point of the whole day: a question typed as words
    can land near a chart, so a picture becomes a possible answer instead of
    being invisible to search.

    Cohere takes an image as a *data URI* — the file base64-encoded into one
    long string — not as raw bytes. `input_type="image"` is its own value, not
    `search_document`: the model is told this is a stored picture, and text and
    images cannot be mixed in a single call.
    """
    vectors: list[list[float]] = []

    # Grouped by weight as well as by count — see `_image_batches` and the
    # note on MAX_IMAGE_REQUEST_BYTES. The caller already hands us no more than
    # IMAGE_EMBED_BATCH_SIZE at a time, so on ordinary figures this splits
    # nothing; it exists for the scanned page, where eight crops are megabytes
    # each and the request would be refused for its size.
    for batch in _image_batches(jpegs):
        response = _cohere().embed(
            model=EMBED_MODEL,
            images=[
                f"data:image/jpeg;base64,{base64.b64encode(jpeg).decode()}"
                for jpeg in batch
            ],
            input_type="image",
            output_dimension=EMBED_DIMENSIONS,
            embedding_types=["float"],
        )
        vectors.extend(response.embeddings.float_ or [])

    if len(vectors) != len(jpegs):
        # Same reasoning as `embed`: a miscount here would silently pair a
        # vector with the wrong picture.
        raise RuntimeError(
            f"Cohere returned {len(vectors)} vectors for {len(jpegs)} images."
        )

    return vectors


# ---------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Guards the one property resume depends on, and the one that would break
    # without raising anything. Needs no API key and costs nothing to run:
    #   apps\api> .venv\Scripts\python.exe -m app.services.ingestion
    sample = [
        (1, "The quick brown fox. " * 300),
        (2, "Second page about something else entirely. " * 200),
    ]

    first = chunk(sample)
    second = chunk(sample)

    assert first == second, "chunk() is not deterministic — resume would corrupt data"
    assert [c.index for c in first] == list(range(len(first))), "indexes are not sequential from 0"
    assert all(c.content.strip() for c in first), "an empty chunk got through"
    assert {c.page_number for c in first} == {1, 2}, "page numbers were lost"
    assert max(c.token_count for c in first) <= CHUNK_SIZE_TOKENS, "a chunk exceeds the token limit"

    print(f"OK — {len(first)} chunks, deterministic, indexes 0..{len(first) - 1}")

    # The same guarantee for images, on a PDF built here in memory rather than a
    # fixture file: three pages, each carrying a "logo" in the identical corner,
    # and page 1 also carrying a drawn rectangle (standing in for a vector
    # chart), a bitmap (a photo) and a hairline rule (a divider).
    document = pymupdf.open()
    logo = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 64, 64))
    logo.set_rect(logo.irect, (30, 60, 200))
    photo = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 128, 128))
    photo.set_rect(photo.irect, (200, 30, 30))

    for page_index in range(3):
        page = document.new_page()
        page.insert_image(pymupdf.Rect(40, 40, 190, 190), pixmap=logo)
        if page_index == 0:
            page.draw_rect(pymupdf.Rect(60, 300, 400, 560), color=(0, 0, 1), fill=(0.8, 0.85, 1))
            page.insert_image(pymupdf.Rect(60, 600, 400, 760), pixmap=photo)
            page.draw_line(pymupdf.Point(60, 240), pymupdf.Point(540, 240))

    sample_pdf = document.tobytes()
    document.close()

    first_images = find_images(sample_pdf, "application/pdf")
    second_images = find_images(sample_pdf, "application/pdf")

    assert first_images == second_images, (
        "find_images is not deterministic — resume would store a vector against "
        "the wrong picture"
    )
    assert first_images, "no images found in the sample PDF at all"
    assert all(region.jpeg.startswith(b"\xff\xd8") for region in first_images), "not a JPEG"
    assert {region.page_number for region in first_images} == {1}, (
        "the repeated logo was not filtered out — pages 2 and 3 contain nothing else"
    )
    assert find_images(sample_pdf, "text/plain") == [], "non-PDF should yield no images"

    # A slide deck: six pages, one chart each, every one in the identical
    # placeholder at about a third of the page. Same position on every page and
    # too small for the big-box exemption, so the old position-only rule
    # discarded all six and reported success with zero images. They differ in
    # content, which is the whole point — that is what now saves them.
    deck = pymupdf.open()
    for page_index in range(6):
        page = deck.new_page()
        shade = 0.15 * page_index
        page.draw_rect(
            pymupdf.Rect(90, 200, 510, 600),
            color=(0, 0, 0),
            fill=(shade, 1 - shade, 0.5),
        )

    deck_pdf = deck.tobytes()
    deck.close()

    deck_images = find_images(deck_pdf, "application/pdf")

    assert len(deck_images) == 6, (
        "a slide deck's charts were filtered out as page furniture — they sit in "
        f"the same spot on every slide but are different pictures (got {len(deck_images)})"
    )

    # And the reverse must still hold: identical content in the same spot is
    # still furniture, or the logo case above regresses the moment this passes.
    reasons: list[str] = []
    find_images(sample_pdf, "application/pdf", on_reject=lambda _p, _b, r: reasons.append(r))
    assert any("furniture" in reason for reason in reasons), (
        "the repeated logo was no longer reported as furniture"
    )

    # Finding 20: batches must fit by weight, not only by count. The failure
    # this prevents is not a slow request — it is a document that can never be
    # ingested at all, because a size rejection marks it `failed` and the next
    # attempt rebuilds the identical batch.
    def encoded_size(group: list[bytes]) -> int:
        return sum(4 * ((len(j) + 2) // 3) + len(_DATA_URI_PREFIX) for j in group)

    # Ordinary figures: nothing should be split, or every ingest pays extra
    # round trips for a problem it does not have.
    small = [b"x" * 100_000] * 8
    assert list(_image_batches(small)) == [small], (
        "eight ordinary crops were split — this batching should be invisible to them"
    )

    # Scanned pages: eight of these is the 11-16MB request that gets refused.
    scanned = [b"x" * 3_000_000] * 8
    groups = list(_image_batches(scanned))
    assert len(groups) > 1, "an oversized batch was not split — finding 20 is still open"
    assert sum(len(g) for g in groups) == len(scanned), "images were lost or duplicated"
    for group in groups:
        assert encoded_size(group) <= MAX_IMAGE_REQUEST_BYTES, (
            "a batch is still over the request ceiling"
        )

    # A single picture bigger than the whole ceiling must still go, alone. The
    # alternative is an empty batch, which would loop forever.
    huge = [b"x" * (MAX_IMAGE_REQUEST_BYTES * 2)]
    assert list(_image_batches(huge)) == [huge], "an over-ceiling image must still be sent alone"
    assert list(_image_batches([])) == [], "an empty list must yield no requests"

    sizes = ", ".join(f"{len(region.jpeg) // 1024}KB" for region in first_images)
    print(
        f"OK — {len(first_images)} images, deterministic, all on page 1 ({sizes}); "
        f"deck kept {len(deck_images)}/6 charts; logo still filtered; "
        f"8 scanned pages split into {len(groups)} requests, all under the ceiling"
    )

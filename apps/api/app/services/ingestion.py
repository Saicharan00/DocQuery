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
import io
import logging
import math
from collections import Counter
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

# Cohere rejects a batch larger than this. Kept as a constant so a rejected
# request points at one line rather than a number buried in a loop.
EMBED_BATCH_SIZE = 96

# embed-v4 sets no limit on how many images one request may carry; the ceiling
# is 20MB of them combined. Eight crops at roughly 100KB each leaves an
# enormous margin, and a smaller batch also loses less work to a rate limit.
IMAGE_EMBED_BATCH_SIZE = 8

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
    and `content` holds only a label like "[Image from page 4]", because
    `chunks.content` is `not null` in the schema. The label is never what gets
    embedded — the picture is.
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


def _parse_txt(data: bytes) -> list[tuple[int, str]]:
    # `errors="replace"` rather than a hard failure: one bad byte in an otherwise
    # fine file should not cost the user the whole upload.
    return [(1, data.decode("utf-8", errors="replace"))]


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

        # Page furniture: the same box in the same spot on many pages. A logo on
        # 40 pages would otherwise be embedded 40 times and compete with real
        # figures in every search — junk in the index is worse than junk on the
        # page. Big boxes are exempt because a scanned document is one full-page
        # image per page in the identical spot, and this rule would otherwise
        # delete the entire document.
        appearances = Counter(_position_key(box) for _, box, _ in kept)
        threshold = max(REPEAT_MIN_PAGES, len(document) * REPEAT_PAGE_FRACTION)
        survivors: list[tuple[int, pymupdf.Rect]] = []

        for page_number, box, area_fraction in kept:
            repeats = appearances[_position_key(box)]
            if repeats >= threshold and area_fraction < REPEAT_EXEMPT_AREA_FRACTION:
                if on_reject:
                    on_reject(page_number, box, f"repeats on {repeats} pages")
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

    for start in range(0, len(jpegs), IMAGE_EMBED_BATCH_SIZE):
        batch = jpegs[start : start + IMAGE_EMBED_BATCH_SIZE]
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

    sizes = ", ".join(f"{len(region.jpeg) // 1024}KB" for region in first_images)
    print(f"OK — {len(first_images)} images, deterministic, all on page 1 ({sizes})")

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

import io
import logging
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

# CLAUDE.md locks these in: 800 tokens with 100 of overlap. The overlap is why a
# sentence split across two chunks is still findable — both copies contain it.
CHUNK_SIZE_TOKENS = 800
CHUNK_OVERLAP_TOKENS = 100


@dataclass(frozen=True)
class Chunk:
    """One slice of a document, ready to be embedded and stored."""

    index: int
    content: str
    page_number: int
    token_count: int


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

    Raises ValueError when a file yields no text at all. That is a real and
    common case — a scanned PDF is a stack of photographs with no text layer —
    and the message reaches the user, so it says what happened.
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
    pages = [(number, text) for number, text in pages if text.strip()]

    if not pages:
        raise ValueError(
            "No readable text found in this file. If it is a scanned PDF, the "
            "pages are images and there is no text to extract."
        )

    return pages


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

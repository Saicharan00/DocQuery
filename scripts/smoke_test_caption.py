"""Day 12 smoke test — the hinge of the whole image-captioning plan.

Before wiring captioning into the ingestion pipeline for real, prove the idea
works: download the real Figure 1 image from the Attention paper (the eval
set's `fig-01` ground-truth chunk), caption it with the new
`rag.caption_image`, embed the caption, embed `fig-01`'s real eval question
as a search query, and print how close they land — compared against the
~0.18-0.25 pixel-only band Day 7/11 measured for image chunks.

    apps\\api> .venv\\Scripts\\python.exe ..\\..\\scripts\\smoke_test_caption.py --token "<jwt>"

Spends one vision call (caption) + two embed calls (caption + question).
Throwaway — delete once the pipeline itself is verified end to end.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "api"))

from supabase import Client, ClientOptions, create_client  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.services import ingestion, rag  # noqa: E402

# scripts/eval_cache/corpus_chunks.json names a specific chunk id for this
# question, but that cache is a point-in-time snapshot — the document may
# have been re-uploaded since, minting new chunk ids. Looked up fresh by
# document name + page instead of trusting the stale id.
FIG01_DOCUMENT_NAME = "Attention is all you need.pdf"
FIG01_PAGE_NUMBER = 3
FIG01_QUESTION = (
    "Looking at the architecture diagram (Figure 1), what two blocks are "
    "stacked at the very top of the decoder, right before the model outputs "
    "its probabilities?"
)


def _build_supabase_client(token: str) -> Client:
    """Copied from eval.py's function of the same name."""
    settings = get_settings()
    return create_client(
        settings.supabase_url,
        settings.supabase_anon_key,
        options=ClientOptions(
            headers={"Authorization": f"Bearer {token}"},
            auto_refresh_token=False,
            persist_session=False,
        ),
    )


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    return dot / (norm_a * norm_b)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token", required=True, help="A fresh Clerk session JWT.")
    args = parser.parse_args()

    supabase = _build_supabase_client(args.token)

    documents = (
        supabase.table("documents")
        .select("id, name")
        .ilike("name", f"%{FIG01_DOCUMENT_NAME}%")
        .execute()
        .data
        or []
    )
    if not documents:
        print(f'No document matching "{FIG01_DOCUMENT_NAME}" found under this account.')
        return 1

    document_ids = [doc["id"] for doc in documents]
    images = (
        supabase.table("chunks")
        .select("image_path, page_number, document_id")
        .in_("document_id", document_ids)
        .eq("chunk_type", "image")
        .order("page_number")
        .execute()
        .data
        or []
    )
    if not images:
        print(f'No image chunks found for "{FIG01_DOCUMENT_NAME}".')
        return 1

    # Prefer the exact page Figure 1 is on; fall back to the first image chunk
    # in the document if the page number ever shifts (re-upload, re-parse).
    row = next((r for r in images if r["page_number"] == FIG01_PAGE_NUMBER), images[0])
    print(f"Figure found on page {row['page_number']}. image_path = {row['image_path']}")

    jpeg = ingestion.download(supabase, row["image_path"])
    print(f"Downloaded {len(jpeg)} bytes. Asking the vision model to caption it...")

    caption = rag.caption_image(jpeg)
    print(f"\nCaption:\n  {caption}\n")

    caption_vector = ingestion.embed([caption])[0]
    query_vector = ingestion.embed([FIG01_QUESTION], input_type="search_query")[0]

    similarity = _cosine(caption_vector, query_vector)
    print(f"Caption-vs-question cosine similarity: {similarity:.4f}")
    print("Pixel-only image band measured on Day 7/11 (BUILD.md): ~0.18-0.25.")
    print(
        "Meaningfully above that band means the idea works; near or inside it "
        "means the caption isn't specific enough and the prompt needs work "
        "before the full pipeline is worth building on top of it."
    )
    return 0

    # In plain English: fetch the real diagram, describe it with the new AI
    # call, turn that description into a search-fingerprint, turn the real
    # question about that diagram into a search-fingerprint too, and print how
    # numerically close those two fingerprints are — that closeness is what
    # "will search find this" actually measures.


if __name__ == "__main__":
    raise SystemExit(main())

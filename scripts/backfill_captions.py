"""Day 12: retroactively caption every existing image chunk that still holds
the pre-caption placeholder ("[Image from page N]"), so documents uploaded
before the captioning fix (`app/services/rag.caption_image`) get the same
retrieval improvement as new ones — new uploads are already fixed by
`routers/documents.py`'s `ingest_step`; this is the one-off catch-up for
everything ingested before that change shipped.

Three phases, on disk between each, for the same reason `eval.py` is split
into `retrieve` / `generate` / `judge`: RLS is the only security boundary in
this codebase (no service-role key, ever), so every Supabase call needs a
real Clerk JWT, and that token lives ~60 seconds. The paid captioning calls
can take much longer than that across many images, so they must not be the
thing racing the token — only listing chunks and writing results back are.

    apps\\api> .venv\\Scripts\\python.exe ..\\..\\scripts\\backfill_captions.py fetch --token "<jwt>"
    apps\\api> .venv\\Scripts\\python.exe ..\\..\\scripts\\backfill_captions.py caption
    apps\\api> .venv\\Scripts\\python.exe ..\\..\\scripts\\backfill_captions.py apply --token "<jwt>"

`fetch` and `apply` need a fresh token each (mint two, or one and be quick
between them). `caption` needs neither — it only touches the file `fetch`
wrote and the paid caption/embed APIs, and prints an estimated cost with the
same "type yes to spend real money" confirmation `eval.py`'s `generate` and
`judge` use, since unlike a live upload this is a manual run, not something
capped by `MAX_IMAGES_PER_DOCUMENT` as it happens.

Pass `--limit N` to `fetch` to backfill only the first N pending chunks —
the dry run for this pipeline, cheaper than a full run while proving the
same code path end to end.

Throwaway, like `inspect_images.py`: delete it once every existing image
chunk has been backfilled.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path

# Same reason as eval.py: a caption can contain non-ASCII text, and Windows
# PowerShell's console default codepage can't encode it.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "api"))

import litellm  # noqa: E402
from jose import jwt  # noqa: E402
from supabase import Client, ClientOptions, create_client  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.services import ingestion, rag  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / "backfill_cache"
PENDING_PATH = CACHE_DIR / "pending.json"
RESULTS_PATH = CACHE_DIR / "results.json"

# What `routers/documents.py`'s `_items()` stamps on every fresh image chunk
# before captioning replaces it. A chunk still starting with this has never
# been captioned — new or old, that is the one thing that distinguishes them.
PLACEHOLDER_PREFIX = "[Image from page"

# Same value and same reason as eval.py: below this much life left on the
# token, don't start a burst of Supabase calls that might not finish.
MIN_SECONDS_REMAINING = 20

MAX_RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BACKOFF_SECONDS = 65


def _seconds_remaining(token: str) -> float:
    """Copied from eval.py's function of the same name — unverified on
    purpose, RLS is the security boundary, not this script."""
    claims = jwt.get_unverified_claims(token)
    return float(claims["exp"]) - time.time()


def _build_supabase_client(token: str) -> Client:
    """Copied from eval.py's function of the same name, which is itself
    copied from `app.deps.get_supabase_client`'s body (not callable
    standalone — it is wired to FastAPI's dependency injection)."""
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


def _is_rate_limit_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}"
    return "RateLimit" in text or "429" in text or "TooManyRequests" in text


def cmd_fetch(token: str, limit: int | None) -> int:
    remaining = _seconds_remaining(token)
    if remaining < MIN_SECONDS_REMAINING:
        print(f"Only {remaining:.0f}s left on this token (need {MIN_SECONDS_REMAINING}s minimum). Paste a fresh one.")
        return 1

    print(f"Token has {remaining:.0f}s left — starting the burst.")
    supabase = _build_supabase_client(token)

    rows = (
        supabase.table("chunks")
        .select("id, document_id, image_path, content")
        .eq("chunk_type", "image")
        .execute()
        .data
        or []
    )
    pending_rows = [r for r in rows if (r["content"] or "").startswith(PLACEHOLDER_PREFIX)]
    not_yet_captioned = len(pending_rows)

    if limit is not None:
        pending_rows = pending_rows[:limit]

    # The true count, before --limit trims what's actually fetched — printing
    # the post-slice length here would silently under-report how much work is
    # really left whenever --limit is used for a dry run.
    print(f"{len(rows)} image chunk(s) total, {not_yet_captioned} not yet captioned.")
    print(f"Downloading {len(pending_rows)} (--limit {limit})..." if limit is not None else "Downloading all of them...")

    pending = []
    for row in pending_rows:
        try:
            jpeg = ingestion.download(supabase, row["image_path"])
        except Exception as exc:  # noqa: BLE001 — logged and skipped, not fatal
            print(f"  {row['id']}: could not download {row['image_path']} ({exc}) — skipped")
            continue
        pending.append(
            {
                "id": row["id"],
                "document_id": row["document_id"],
                "image_path": row["image_path"],
                "jpeg_b64": base64.b64encode(jpeg).decode(),
            }
        )

    CACHE_DIR.mkdir(exist_ok=True)
    PENDING_PATH.write_text(json.dumps(pending), encoding="utf-8")
    print(f"Wrote {len(pending)} pending chunk(s) to {PENDING_PATH.name}. Run `caption` next.")
    return 0

    # In plain English: while the token is still alive, ask the database for
    # every image row that still has the placeholder text, download each
    # picture from Storage, and save all of it to one file on disk — so the
    # slow, paid part below can run later without needing the token at all.


def cmd_caption() -> int:
    if not PENDING_PATH.exists():
        print(f"{PENDING_PATH.name} not found. Run `fetch` first.")
        return 1

    pending = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
    results: dict = json.loads(RESULTS_PATH.read_text(encoding="utf-8")) if RESULTS_PATH.exists() else {}
    todo = [item for item in pending if item["id"] not in results]

    if not todo:
        print("Nothing to caption — every fetched chunk already has a result. Run `apply` next.")
        return 0

    rag.api_key_for(rag.DEFAULT_MODEL)  # fail fast on a missing key, before spending anything

    try:
        _, output_cost = litellm.cost_per_token(
            model=rag.DEFAULT_MODEL, prompt_tokens=0, completion_tokens=rag.CAPTION_TOKENS
        )
        estimate = f"~${output_cost * len(todo):.4f}"
    except Exception:  # noqa: BLE001 — pricing is a nice-to-have, not a blocker
        estimate = "unknown"

    print(f"{len(todo)} chunk(s) to caption, estimated cost {estimate} (captioning only — embedding is separate and small).")
    if input('Type "yes" to spend real money and proceed, anything else to cancel: ').strip().lower() != "yes":
        print("Cancelled — no calls made.")
        return 0

    for item in todo:
        jpeg = base64.b64decode(item["jpeg_b64"])
        caption = None
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            try:
                caption = rag.caption_image(jpeg)
                break
            except Exception as exc:  # noqa: BLE001 — logged and skipped, not fatal
                if _is_rate_limit_error(exc) and attempt < MAX_RATE_LIMIT_RETRIES:
                    print(f"  {item['id']}: rate-limited, waiting {RATE_LIMIT_BACKOFF_SECONDS}s...")
                    time.sleep(RATE_LIMIT_BACKOFF_SECONDS)
                    continue
                print(f"  {item['id']}: FAILED ({type(exc).__name__}: {exc})")
                break

        if caption is None:
            continue

        vector = ingestion.embed([caption])[0]
        results[item["id"]] = {"content": caption, "embedding": vector}
        # Written after every chunk, not once at the end, so a crash midway
        # loses at most the one caption in flight — same reason eval.py's
        # phases write their caches incrementally.
        RESULTS_PATH.write_text(json.dumps(results), encoding="utf-8")
        print(f"  {item['id']}: {caption[:80]}{'...' if len(caption) > 80 else ''}")

    # `results` accumulates across every `fetch` this cache has ever seen, so
    # comparing it against this run's `pending` count (a smaller, more recent
    # fetch) would print a nonsense fraction like "7/6". Report against `todo`
    # instead — what this run actually set out to do.
    done_this_run = sum(1 for item in todo if item["id"] in results)
    print(f"Captioned {done_this_run}/{len(todo)} this run ({len(results)} total ever cached). Run `apply` next.")
    return 0


def cmd_apply(token: str) -> int:
    if not RESULTS_PATH.exists():
        print(f"{RESULTS_PATH.name} not found. Run `fetch` then `caption` first.")
        return 1

    results = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    if not results:
        print("No results to apply.")
        return 0

    remaining = _seconds_remaining(token)
    if remaining < MIN_SECONDS_REMAINING:
        print(f"Only {remaining:.0f}s left on this token (need {MIN_SECONDS_REMAINING}s minimum). Paste a fresh one.")
        return 1

    print(f"Token has {remaining:.0f}s left. Applying {len(results)} result(s)...")
    supabase = _build_supabase_client(token)

    applied = 0
    for chunk_id, result in results.items():
        supabase.table("chunks").update(
            {"content": result["content"], "embedding": result["embedding"]}
        ).eq("id", chunk_id).execute()
        applied += 1

    print(f"Applied {applied}/{len(results)}.")
    return 0

    # In plain English: for every caption we already paid for and saved to
    # disk, overwrite that chunk's stored text and search-fingerprint with
    # the new ones. Nothing here calls a paid API — it's a database write.


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch_parser = subparsers.add_parser("fetch", help="List + download every uncaptioned image chunk.")
    fetch_parser.add_argument("--token", required=True, help="A fresh Clerk session JWT.")
    fetch_parser.add_argument("--limit", type=int, default=None, help="Only fetch the first N (for a dry run).")

    subparsers.add_parser("caption", help="Caption + embed everything fetch wrote to disk. Spends real money.")

    apply_parser = subparsers.add_parser("apply", help="Write captioned results back to the chunks table.")
    apply_parser.add_argument("--token", required=True, help="A fresh Clerk session JWT.")

    args = parser.parse_args()

    if args.command == "fetch":
        return cmd_fetch(args.token, args.limit)
    if args.command == "caption":
        return cmd_caption()
    if args.command == "apply":
        return cmd_apply(args.token)

    return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""LangSmith wiring for the chat pipeline. Day 10a.

Three jobs, all small, all here rather than scattered:

1. Copy the LangSmith settings into `os.environ`, which is the only place the SDK
   looks. See the block below — this is the whole reason the module exists.
2. Open and close the one root run that every span in a request hangs off.
3. Strip base64 images out of trace payloads before they are uploaded.

Nothing in here may ever break a request. Tracing is a convenience for us; the
answer is what the user came for. Every function swallows its own failures and
logs them, which is the one place in this codebase where swallowing is correct.
"""

import hashlib
import logging
import os
import re
from contextlib import nullcontext
from functools import lru_cache
from typing import Any

from langsmith import Client, RunTree, tracing_context

# Not exported from the top-level package, but it is the SDK's own answer to
# "is tracing on?" — it understands the LANGCHAIN_* aliases and the tracing
# context vars, which a hand-rolled env check would silently disagree with. The
# version is pinned in uv.lock, so this cannot move under us without a
# deliberate `uv lock`.
from langsmith.utils import get_env_var, tracing_is_enabled

from app.config import get_settings

logger = logging.getLogger(__name__)

_settings = get_settings()


# ---------------------------------------------------------------------------
# Settings -> os.environ
# ---------------------------------------------------------------------------
#
# pydantic-settings reads the root .env into the Settings object and never
# exports to os.environ (config.py:76-80). The LangSmith SDK has no equivalent
# escape hatch — it is configured through the environment or not at all — so
# something has to bridge the two.
#
# Plain assignment, NOT setdefault, and the reason is a real bug caught on
# 2026-08-13. Importing `litellm` calls `load_dotenv()` as a side effect, which
# copies the whole .env into os.environ *verbatim*. A file saying
# `LANGSMITH_TRACING=True` therefore lands as the string "True", and
# `langsmith.utils.tracing_is_enabled` compares `== "true"` — lowercase, exact.
# Result: tracing silently off, no error anywhere, and whether it happens at all
# depends on whether litellm was imported before this module. `setdefault` could
# not fix it, because the variable was already set.
#
# Assigning from `_settings` fixes both halves at once. pydantic has already
# parsed "True"/"true"/"1"/"yes" into a real bool, and it reads real environment
# variables in preference to the file — so `_settings` is the correctly merged,
# correctly normalised view on Railway and locally alike.
#
# This must run at import time regardless: `get_env_var` is @lru_cache'd and
# caches the *miss*, so one lookup before these are set poisons the cache for
# the life of the process.
if _settings.langsmith_tracing and _settings.langsmith_api_key:
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = _settings.langsmith_api_key
    os.environ["LANGSMITH_PROJECT"] = _settings.langsmith_project
    if _settings.langsmith_endpoint:
        os.environ["LANGSMITH_ENDPOINT"] = _settings.langsmith_endpoint
else:
    # Explicitly off, so a stray "True" left in the environment by litellm's
    # load_dotenv cannot switch tracing on behind our back either.
    os.environ["LANGSMITH_TRACING"] = "false"
    if _settings.langsmith_tracing and not _settings.langsmith_api_key:
        # The likeliest misconfiguration: tracing wanted, key missing (unset,
        # blank, or dropped from the deploy's env). This module exists because
        # a *silent* tracing-off bug already cost real debugging time once —
        # so this specific case gets a line in the log instead of vanishing
        # the same way.
        logger.warning(
            "LANGSMITH_TRACING is on but LANGSMITH_API_KEY is missing; "
            "tracing is disabled."
        )
    # In plain English: if we wanted tracing but have no key for it, say so in
    # the log now, rather than leaving it as a mystery for whoever later
    # notices no traces are showing up.

# Discard anything the SDK looked up and cached before the block above ran.
# Cheap insurance against the poisoned-cache hazard described there, and it makes
# this module's behaviour independent of who got imported first.
get_env_var.cache_clear()

# In plain English: if the settings say tracing is on and we have a key, write
# those four values into the process environment, which is the only place the
# LangSmith library looks. We overwrite rather than fill-in-if-missing, because
# another library may already have put a differently-capitalised version there
# and LangSmith is fussy about the exact spelling. The key's value is never
# logged or printed.


# ---------------------------------------------------------------------------
# The root run
# ---------------------------------------------------------------------------


def start_root(
    name: str,
    inputs: dict[str, Any],
    metadata: dict[str, Any],
    tags: list[str],
) -> RunTree | None:
    """Open the run that every span in this request will hang off, or None.

    Returns None when tracing is off, and the callers treat None as "do nothing".
    That guard has to live here: unlike `@traceable`, `RunTree.post()` does
    *not* check whether tracing is enabled (run_trees.py:780 calls create_run
    unconditionally), so without it a keyless local run would attempt a network
    call on every single question.
    """
    if not tracing_is_enabled():
        return None

    try:
        root = RunTree(
            name=name,
            run_type="chain",
            inputs=inputs,
            # NOT metadata=... — RunTree sets extra="ignore", so a `metadata`
            # kwarg is accepted and silently thrown away. It is a property over
            # `extra`, and this is the only way in through the constructor.
            extra={"metadata": metadata},
            tags=tags,
            project_name=_settings.langsmith_project,
        )
        root.post()
        return root
    except Exception:
        logger.exception("Could not start a LangSmith trace")
        return None


def finish_root(
    root: RunTree | None,
    outputs: dict[str, Any] | None = None,
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Close the root run. Safe to call with None, and safe to call on any path.

    Every exit from the chat endpoint has to reach this — the three pre-flight
    HTTPExceptions, a provider failure mid-stream, an empty answer, a browser
    that disconnects, and the happy path. A root that is never closed shows in
    the dashboard as a request still running, forever, which reads as a hang.
    """
    if root is None:
        return

    try:
        root.end(outputs=outputs, error=error, metadata=metadata)
        root.patch()
    except Exception:
        logger.exception("Could not finish the LangSmith trace %s", root.id)


def parent(root: RunTree | None):
    """Context manager that adopts `root` as the parent for spans inside it.

    This is the piece that makes a single tree possible. LangSmith normally
    links a span to its parent through a contextvar, and that cannot survive
    this endpoint: the handler runs in a FastAPI worker thread, and the answer
    is streamed from a generator that Starlette pulls one chunk at a time, each
    pull a fresh hop into the thread pool with a fresh copy of the context.
    Passing `root` as an ordinary closure variable and re-entering it here is
    what holds the tree together. Measured, not assumed — without it the spike
    produced three unrelated traces instead of one.
    """
    return tracing_context(parent=root) if root is not None else nullcontext()


# ---------------------------------------------------------------------------
# Keeping images out of the payload
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _client() -> Client:
    """One LangSmith client for the process.

    Built lazily rather than at import: constructing it opens a session, and a
    deployment with tracing switched off should never make one at all.
    """
    return Client()


_session_id_cache: str | None = None


def _session_id() -> str | None:
    """The LangSmith id of our project, looked up once.

    `create_feedback` warns that filing feedback without it is deprecated and
    "will stop working in a future release", so it is worth one lookup per
    process. Cached because the answer cannot change while we run, and `None` on
    failure so a naming or network problem costs the deprecation warning rather
    than the feedback.

    Not `@lru_cache`: that would memoize a failed lookup's `None` just as
    happily as a real id, and a transient network blip on the first call after
    a deploy would then cost every piece of feedback its `session_id` for the
    life of the container. A module-level variable lets a failure be retried
    on the next call instead, while a success is still remembered for good.
    """
    global _session_id_cache
    if _session_id_cache is not None:
        return _session_id_cache

    try:
        _session_id_cache = str(
            _client().read_project(project_name=_settings.langsmith_project).id
        )
        return _session_id_cache
    except Exception:
        logger.exception("Could not resolve the LangSmith project id")
        return None
    # In plain English: remember the id once we successfully find it, so we
    # never look it up twice — but if the lookup fails, don't remember the
    # failure. The next call tries again instead of being stuck with "unknown"
    # forever.


def record_feedback(run_id: str, score: int | None, comment: str | None) -> list[str]:
    """Attach a reader's verdict to the trace of the answer they read.

    Returns the names of the parts that were **not** recorded; an empty list
    means everything asked for landed. Failures are logged rather than raised:
    the caller decides what to tell the user, and this module's rule is that
    nothing in it may break a request.

    The thumb and the sentence go in under **different keys**. `key` is what
    groups feedback into a column in the LangSmith UI, so a single key holding
    both would mean the score column gained a second, score-less row every time
    somebody explained themselves. Two keys keeps "how are the answers doing"
    answerable by averaging one column, with the comments beside it.

    They also get a `try` each, and that is the point of returning a list rather
    than a bool. Sharing one `try` meant a comment failing *after* a score had
    already been filed reported total failure — the reader was told nothing was
    saved while their rating was already in, and doing as they were told and
    retrying counted that rating twice. Two keys exist precisely to keep one
    reader's opinion out of the average twice; one shared `try` handed it back.
    """
    if not tracing_is_enabled():
        # No trace to hang anything on, so nothing asked for was stored. The
        # guards mirror the two below exactly: a thumbs-down is `score == 0`,
        # which is falsy, so it has to be tested against `None` and not for
        # truth — otherwise the one rating people give when they are unhappy is
        # the one that silently reports itself as never having been asked for.
        requested = ["rating"] if score is not None else []
        if comment:
            requested.append("note")
        return requested

    session = _session_id()
    failed: list[str] = []

    if score is not None:
        try:
            _client().create_feedback(
                run_id, key="user_rating", score=score, session_id=session
            )
        except Exception:
            logger.exception("Could not record the rating for run %s", run_id)
            failed.append("rating")

    if comment:
        try:
            _client().create_feedback(
                run_id, key="user_comment", comment=comment, session_id=session
            )
        except Exception:
            logger.exception("Could not record the note for run %s", run_id)
            failed.append("note")

    return failed

    # In plain English: file the star rating and the written note as two separate
    # errands, and report back exactly which ones didn't get done. Before this
    # they were one errand, so if the second half went wrong the caller was told
    # the whole thing had — and the reader, believing it, sent their rating in a
    # second time.


def anon(user_id: str) -> str:
    """A stable short hash of a Clerk user id, for trace metadata.

    A trace can be shared publicly, and a raw Clerk `sub` is a stable identifier
    that also appears as the first segment of every storage path. Hashing keeps
    what the tag is *for* — telling one user's traces from another's — while
    giving a public link nothing to correlate on.

    Still lookup-able when debugging: hash the id you are hunting for and filter
    on the result. It is one-way to a reader, not to you.
    """
    return hashlib.sha256(user_id.encode()).hexdigest()[:12]


# The `{user_id}/` prefix that `001_init.sql`'s storage policy requires, and that
# therefore starts every image path. Matched here so it can be lifted out of trace
# payloads without losing the rest of the path, which is what tells you which
# figure was chosen.
_USER_PREFIX = re.compile(r"^user_[A-Za-z0-9]+/")


def redact(value: Any) -> Any:
    """Replace every base64 data URI with a short placeholder, at any depth.

    `load_images` returns `{path: "data:image/jpeg;base64,..."}` and
    `build_messages` inlines up to five of those into the prompt. Trace inputs
    and outputs are uploaded verbatim, so untouched this would ship several
    megabytes per image question — slow, wasteful of the free tier, and useless
    to read, since a wall of base64 tells you nothing. The image *paths* survive,
    so you can still see which figures were chosen.

    Total by design: it never raises, because it runs inside the tracing path
    and a redaction bug must not be able to take down an answer.
    """
    if isinstance(value, str):
        if value.startswith("data:"):
            # base64 is 4 characters per 3 bytes; close enough for a label.
            return f"<image: {len(value) * 3 // 4 // 1024} KB>"
        # Storage paths start with the owner's Clerk id. Swap that one segment
        # and keep the rest, so a shared trace still shows which figure was used
        # without saying whose it was.
        return _USER_PREFIX.sub("<user>/", value)
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value

    # In plain English: walk whatever it is given — a string, a dict, a list, or
    # any nesting of those — and swap any string that looks like an embedded
    # image for a short label saying roughly how big it was. Everything else is
    # handed back untouched.


# Arguments that are plumbing, not data. The Supabase client is a live connection
# object: there is nothing readable to show, and asking LangSmith to serialise it
# is pure cost at best.
_SKIP_ARGS = frozenset({"supabase", "self", "cls"})


def clean_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    """`process_inputs` hook: drop the plumbing, shrink the images."""
    return {
        name: redact(value)
        for name, value in inputs.items()
        if name not in _SKIP_ARGS
    }


# ---------------------------------------------------------------------------
# Self-check: `uv run python -m app.services.tracing`
# ---------------------------------------------------------------------------
#
# Same shape as the checks at the bottom of rag.py and ingestion.py. Needs no
# key, no network and no database — it only exercises the redaction logic, which
# is the one part of this module with branches worth getting wrong.

if __name__ == "__main__":
    fake_image = "data:image/jpeg;base64," + "A" * 40_000

    # A bare data URI becomes a label.
    assert redact(fake_image).startswith("<image:"), redact(fake_image)
    assert "base64" not in redact(fake_image)

    # Ordinary text is left completely alone.
    assert redact("the ionosphere delays the signal") == "the ionosphere delays the signal"

    # The owner's Clerk id comes out of a storage path; the rest of the path,
    # which is the part that tells you *which* figure was used, stays.
    assert redact("user_3ABCdef/doc-id/img-157.jpg") == "<user>/doc-id/img-157.jpg"
    assert "user_3ABCdef" not in str(redact({"image_path": "user_3ABCdef/d/img-1.jpg"}))
    # A path that never had the prefix is untouched.
    assert redact("docs/fig1.jpg") == "docs/fig1.jpg"

    # The metadata tag is stable (so it still groups a user's traces) and one-way
    # (so a public link has nothing to correlate on).
    assert anon("user_3ABCdef") == anon("user_3ABCdef")
    assert anon("user_3ABCdef") != anon("user_9ZYXwvu")
    assert "user_3ABCdef" not in anon("user_3ABCdef")

    # Nested inside the real shapes: load_images' dict, and build_messages' list
    # of content parts.
    nested = {
        "images": {"docs/fig1.jpg": fake_image},
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "what is this?"},
                {"type": "image_url", "image_url": {"url": fake_image}},
            ]},
        ],
    }
    cleaned = redact(nested)
    assert "base64" not in str(cleaned), "a data URI survived the walk"
    # The path is data, not payload, and has to survive so citations stay legible.
    assert "docs/fig1.jpg" in str(cleaned)
    assert "what is this?" in str(cleaned)

    # The live Supabase connection never reaches the payload.
    assert clean_inputs({"supabase": object(), "query_vector": [0.1]}) == {"query_vector": [0.1]}

    # Closing a trace that was never opened is a no-op, not a crash — every
    # caller relies on this when tracing is switched off.
    finish_root(None, outputs={"answer": "x"})

    # `_session_id` must retry a failed lookup on the next call rather than
    # caching the `None` — swap in a fake `_client` that always raises, and
    # confirm it gets called every time instead of just once.
    _calls = {"n": 0}

    def _always_fails():
        _calls["n"] += 1
        raise RuntimeError("simulated network failure")

    _client = _always_fails  # shadows the module-level function for this check
    assert _session_id() is None
    assert _session_id() is None
    assert _calls["n"] == 2, "a failed lookup must not be cached"

    # A rating that files and a note that does not must be reported as exactly
    # that. Reporting the pair as a total failure is what sent a reader back to
    # resubmit a rating already in the average — the double-count the two-key
    # split exists to prevent.
    _filed: list[str] = []

    class _HalfBrokenClient:
        def create_feedback(self, run_id, key, score=None, comment=None, session_id=None):
            if key == "user_comment":
                raise RuntimeError("simulated LangSmith failure")
            _filed.append(key)

    def _enabled():
        return True

    def _half_broken():
        return _HalfBrokenClient()

    def _fake_session():
        return "session-1"

    tracing_is_enabled = _enabled
    _client = _half_broken
    _session_id = _fake_session

    assert record_feedback("run-1", score=1, comment="too vague") == ["note"], (
        "a failed note must be reported on its own, not as total failure"
    )
    assert _filed == ["user_rating"], "the rating should still have been filed"

    # Rating alone, on the same half-broken client: nothing failed, so nothing
    # is reported — this is the path the real UI takes.
    _filed.clear()
    assert record_feedback("run-2", score=0, comment=None) == []
    assert _filed == ["user_rating"], "a thumbs-down is score 0 and must still file"

    # Tracing off: everything asked for is unrecorded, and a thumbs-down still
    # counts as having been asked for despite being falsy.
    def _disabled():
        return False

    tracing_is_enabled = _disabled
    assert record_feedback("run-3", score=0, comment="x") == ["rating", "note"]
    assert record_feedback("run-4", score=None, comment=None) == []

    print(f"OK - redaction, arg filtering, None-safety, feedback split. tracing on: {_enabled()}")

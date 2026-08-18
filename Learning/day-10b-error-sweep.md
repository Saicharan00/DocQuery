# Day 10b — Closing Day 10

Personal log, not read by Claude automatically. Short on purpose.

- **Auth ≠ authorization.** A token can be genuine (signed, unexpired, right
  issuer) and still not be *ours* — Clerk's `azp` says who asked for it. CORS
  is a browser convention about replies, not a server-side check; it stops
  nothing that speaks HTTP directly.
- **Check the budget before spending, not after.** Ingest was paying for a
  download + parse + embed call before asking if the token had time to save
  the result. Same trap as `/chat`'s token floor: the check must sit well
  under a *fresh* token's life, or it refuses good tokens too and wedges the
  feature permanently.
- **A byte cap needs to be a byte cap.** Batching images by *count* assumed a
  typical size; a scanned page is 20x that. Deterministic failures (wrong
  size, not a rate limit) don't self-heal on retry — they need the actual
  ceiling enforced, not a proxy for it.
- **List truth from the source, not from a row that might not exist yet.**
  Orphaned files came from deleting by querying a table a step hadn't
  finished writing to. Listing the storage folder itself found what the
  table couldn't.
- **Don't trust a plausible-sounding fix — read the library.** A
  `BackgroundTask` looked like the obvious way to close an abandoned stream.
  Starlette's own source showed it only runs on one of two disconnect paths.
  Would have shipped a fix that silently didn't fire half the time.
- **One generic error message is a lie by omission.** "Something went wrong"
  was equally true and equally useless for a rate limit, a dead key, and an
  oversized conversation — three of which needed opposite advice.
- **A shared trace is not just the answer.** It's the full retrieved text and
  the document's name. The privacy decision is *which document*, every time
  — caught one that shouldn't go public before it was committed anywhere.
- **Stacked PRs shrink as you merge them.** Branch B on top of branch A will
  show "nothing to merge" once A lands — that's correct, not a lost PR.

Day 10 closed: tracing, error handling, public trace link, all three
done-when boxes ticked.

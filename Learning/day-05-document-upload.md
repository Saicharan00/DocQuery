# Day 5 — Document Upload, Storage, and the RLS Payoff

Personal learning log. Not read by Claude automatically — this is for me, to
recall what I built and why once the project is done. Not needed as context
for future sessions or development; it's a record, not a spec.

---

## Part 0 — Making Supabase trust Clerk (config, not code)

### 1. The blocker Day 4 left behind, and why it was the whole day's hinge

Every RLS policy written on Day 1 reads the user id out of the token:

```sql
using (user_id = (current_setting('request.jwt.claims', true)::json ->> 'sub'))
```

Postgres only fills in `request.jwt.claims` from a token **Supabase itself
validated**. By default Supabase validates against its own secret; our tokens
are signed by Clerk. Until Supabase is told "Clerk is trustworthy," that
setting is empty, every policy compares `user_id` to `NULL`, and every query
returns nothing.

Nothing built before today exercised this. Day 3's `/me` only verifies the
token inside Python; Day 4 just displayed the result. Day 5 is the first time
Supabase is asked to *do* something on my behalf. BUILD.md's own reality check
named this as the single most likely thing in the project to break.

Two dashboard settings, no code:

1. **Clerk → "Connect with Supabase"** — adds a `"role": "authenticated"`
   claim to session tokens. Supabase maps that claim to the Postgres
   `authenticated` role, which is exactly the role `002_grants.sql` granted
   table access to.
2. **Supabase → Authentication → Third-Party Auth → add Clerk**, using the
   Clerk domain.

### 2. Why `GET /documents` was built first, alone

Sequencing was deliberate: build and deploy *only* the read endpoint, then
call it from the live dashboard before writing anything that depends on it.
An empty `[]` is the proof — it means the token was accepted, the role
resolved, RLS ran, and found no rows. A 401/403 would have meant the two
settings above, not the code. Finding that out *before* writing upload and
delete is worth an extra deploy cycle.

### 3. No `WHERE user_id = ...` anywhere — on purpose

Per CLAUDE.md, RLS is the security boundary. Adding a backend filter as well
would mean the app appears to work whether or not RLS functions, which hides
the only thing worth verifying. Every query in `documents.py` is unfiltered;
Postgres does the scoping. This is uncomfortable to look at and it's correct.

### 4. Two Day 4 bugs found while wiring the dashboard

- `api.ts` interpolated a null token before Clerk finished loading, producing
  the literal header `Bearer null` — which the backend can only report as a
  malformed token, indistinguishable from a real auth failure.
- `useApi()` returned a brand-new function on every render, and the dashboard
  did `useEffect(..., [api])`. Fetch → setState → re-render → new function
  reference → effect re-runs → fetch, forever. Day 4 *looked* fine because
  the data rendered; it was silently hammering `/me` in a loop. Fixed with
  `useCallback`.

---

## Part 1 — Backend: upload and delete

### 5. `python-multipart`, and why it isn't a library choice

Browsers send files as `multipart/form-data` — file bytes and form fields
packed into one body, separated by a random *boundary* string. FastAPI ships
no parser for that format and raises **at startup** the moment a route
declares a file parameter. Not a choice between libraries; it's the parser
FastAPI expects.

### 6. A document lives in two places, and the write order keeps them honest

Storage holds the bytes. The `documents` table holds the facts (id, name,
status, size). Neither can do the other's job: Storage can't answer "what docs
does this user have?", and a table can't hold a 10MB PDF. `file_path` is the
string tying them together.

**Upload writes Storage first, then the row.** The reverse would let a row
exist pointing at a file that was never written — a document that looks
perfectly fine in the UI and then fails Day 6's ingestion. If the insert
fails, the just-uploaded object is deleted, so a failure leaves nothing
behind in either direction.

**Delete writes in the opposite order: Storage object first, then the row.**
If storage deletion fails, the row survives and the delete can be retried.
Reverse it and a failure leaves a file with nothing pointing at it —
unfindable, unbillable to any UI, costing space forever.

The two orders look inconsistent until you see the shared rule: *never let the
database point at something that isn't there.*

### 7. The extension is the gate, and it also picks the MIME type

Only `.pdf`, `.docx`, `.txt` are accepted, and the recorded `mime_type` comes
from a lookup table keyed on that extension — **never** from the browser's
declared `Content-Type`, which is client-supplied and can be wrong or forged.
Filenames are also reduced to a basename (`PurePosixPath(...).name`) so a name
like `../../etc/notes.txt` can't steer the storage key. The key is always
`{user_id}/{uuid}{ext}`, which is what the storage policy in `001_init.sql`
checks via `storage.foldername(name)[1]`.

Magic-byte sniffing was deliberately skipped: a `.exe` renamed to `.pdf` will
fail Day 6's parser and land on `status='failed'`, which is designed
behaviour, not a gap.

### 8. Reading the upload in chunks

Rather than one `await file.read()` then a size check, the body is read a
megabyte at a time and rejected the moment it crosses 10MB. Same outcome for
the user; the difference is that an oversized upload stops costing memory
immediately instead of being fully buffered and only then refused.

### 9. `404`, not `403`, for someone else's document

`DELETE /documents/{id}` first selects the row. RLS scopes that select to the
caller, so another user's id returns nothing — indistinguishable from an id
that never existed. Returning 404 is intentional: a 403 would confirm that the
document exists and belongs to someone, which is information a stranger
shouldn't get.

### 10. Verifying the Storage client actually carries the Clerk token

Open question going in: does `supabase-py` forward our custom `Authorization`
header to the **Storage** API, or only to PostgREST? If only PostgREST, the
`{user_id}/` path prefix would be decoration rather than a boundary.

Checked the installed package instead of assuming. In `supabase 2.31.0`,
`Client.storage` is built from the same `options.headers` dict PostgREST uses,
and `_get_auth_headers()` reads an existing `Authorization` and only falls back
to the anon key when none was set. Confirmed empirically by constructing a
client and reading the Storage HTTP client's headers back — `Bearer <token>`,
with the anon key appearing only in `apiKey`. Storage RLS enforces the prefix
exactly like table RLS enforces rows.

### 11. No migration was needed — an earlier assumption corrected

I'd assumed a `003_` migration would be needed to add `WITH CHECK` clauses for
inserts. That was wrong: **PostgreSQL uses a policy's `USING` expression as its
`WITH CHECK` expression when the latter is omitted**, so the existing `FOR ALL`
policies already cover inserts. `ON DELETE CASCADE` for chunks was already in
`001_init.sql:31`. Day 5 touched no schema at all.

---

## Part 2 — Frontend: upload zone and document list

### 12. `FormData`, not JSON

A JSON body is text; file bytes aren't. Encoding a PDF into JSON would inflate
it and force the server to decode it back. `FormData` is the browser's native
container for exactly this.

Crucially, `api.ts` must **not** set a `Content-Type` header when the body is
`FormData` — the browser has to set it itself, because only it knows the
boundary string it generated. That was already handled from Step 0, so no
change was needed.

### 13. The same validation exists in two places, doing two different jobs

The upload zone re-checks extension and size even though the server does. The
browser copy is **feedback**: a bad file is refused instantly instead of after
a 10MB upload. The server copy is **security**: anyone can bypass a browser.
The duplication is deliberate, and when they disagree the server wins.

This distinction became concrete during verification — see §19.

### 14. Three drag-and-drop details that each break it completely

- **`preventDefault` on both `dragover` and `drop`.** A browser's default
  response to a dropped file is to navigate away and open it. Without
  cancelling, the drop zone never fires — the page just vanishes and shows
  the PDF. This is the most common reason a drop zone "does nothing."
- **The hidden `<input type="file">` must live *outside* the clickable zone.**
  Calling `input.click()` dispatches a click that bubbles up to the zone's own
  `onClick`, which calls `input.click()` again — infinite recursion.
- **`dragleave` fires when the pointer moves onto a child element.** Without
  checking `event.currentTarget.contains(event.relatedTarget)`, the highlight
  strobes as the cursor crosses the zone's own contents.

Also: resetting `event.target.value = ""` after a selection, or picking the
same file twice in a row silently does nothing — the browser suppresses
`change` when the value is unchanged.

### 15. Re-fetching instead of editing the list locally

After upload or delete, the dashboard calls `refreshDocuments()` rather than
adding or splicing the row client-side. Trade-off accepted: one extra request.
What it buys is that the list always shows what the server *actually* has, so
a half-failed operation shows the truth rather than an optimistic lie. It's
also the exact call Day 6 will poll to watch `pending → processing → ready`,
so tomorrow changes the timing, not the code.

### 16. Why every uploaded document says `pending` and stays there

`status='pending'` is written at insert and **nothing** changes it — the
`pending → processing → ready | failed` transitions are Day 6's ingestion
pipeline. The column and the badge exist now so tomorrow's work is a change in
*value*, not a change in schema or UI. Looked like a bug; wasn't.

---

## Part 3 — Two things that broke, and the fixes

### 17. `uvicorn: command not found` on Railway

The deploy died immediately after the first dependency change, restarting
three times (`restartPolicyMaxRetries: 3`).

`uv sync` doesn't install packages system-wide — it creates a project-local
`.venv`. A bare `uvicorn` in the start command means "find a program called
uvicorn on `PATH`," and that `.venv` isn't on `PATH`. The start command had
been bare `uvicorn` since Day 3 and worked only because Railway was serving a
**cached build layer**. Adding `python-multipart` changed `uv.lock`, which
invalidated the cache and forced a genuinely fresh build — exposing a
fragility that had been there all along. It would have surfaced on the first
dependency change, whenever that happened.

Fix in `apps/api/railway.json`:

```
uv run --frozen uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

- `uv run` resolves the project's environment explicitly instead of trusting `PATH`
- `--frozen` refuses to re-resolve the lockfile at boot, so a mismatch between
  `pyproject.toml` and `uv.lock` fails loudly at deploy rather than silently
  installing something the lock never pinned

A neat illustration turned up while testing locally: bare `uvicorn` resolved to
an unrelated **0.34.3** sitting on `PATH`, while `uv run --frozen` gave the
pinned **0.51.0**. Same class of bug, visible on my own machine.

Fallback if it ever recurs: `.venv/bin/uvicorn ...`, which needs no `uv` at
runtime.

### 18. A silent no-op delete that would have orphaned files

Found while investigating whether a deleted file was still in the bucket.
Supabase's delete-object endpoint answers **200 with the list of objects it
actually deleted**. If nothing matches, that list comes back **empty** —
success status, nothing deleted, no exception raised.

The original `DELETE /documents/{id}` never checked that list. A no-op would
have sailed through, deleted the database row, and returned `204` as though it
worked — producing precisely the orphan the storage-then-row ordering exists
to prevent. Now an empty result raises 502 *before* the row is touched, so the
document stays in the list and the delete can be retried.

The live path was working correctly; this is hardening against a case that
hadn't been hit. Worth noting that the bug was in the *absence* of a check,
not in anything the code did — the easiest kind to miss.

---

## Part 4 — Verification

### 19. What each check actually proved

- [x] **Upload a PDF** → row in `documents`, file under `{user_id}/` in Storage
- [x] **List renders and refreshes** without a manual page reload
- [x] **Delete removes both halves** — row *and* Storage object
- [x] **Second user sees an empty list** — the one that matters
- [x] **API is fail-closed** — `/health` 200 without a token; `/documents`,
      `/documents/upload`, `DELETE` all 401
- [x] **Signatures are really verified** — a hand-built, structurally valid JWT
      naming a `sub` was rejected with "Token signing key is not recognised"
- [x] **CORS locked to the Vercel origin** — other origins get 400 with no
      `access-control-allow-origin`
- [ ] Server-side (non-browser) rejection of `.exe` / >10MB against the live
      deployment — covered by a local stub harness, not yet by `curl` in
      production

**The two-user test, in detail**, because it's the point of the whole day:

1. Supabase SQL Editor (connects as `postgres` superuser, **bypasses RLS**)
   showed User A's document plainly sitting in the table.
2. An incognito window signed in as User B hit `/dashboard`, which calls
   `GET /documents` — a query with no owner filter anywhere in the Python.
3. B's screen showed **"No documents yet."**

The row was there. The query didn't exclude it. B got nothing anyway. The only
thing in between is Postgres applying the RLS policy. Nothing I wrote did that.

A subtlety worth recording: "No documents yet" is the *empty-state* string,
rendered only when the request **succeeded** and returned `[]`. A failure would
have shown a red error instead. That distinction matters — a 502 would also
produce an empty-looking screen and would have proved nothing.

### 20. Note on §13 — what the browser test does *not* prove

Dropping a `.exe` on the page gets rejected by JavaScript before the file ever
leaves the machine. That verifies the UI is friendly; it says nothing about
whether the server is safe, because an attacker simply wouldn't use the
browser. The honest version of that check is a `curl` call straight to the API,
which is why it's the one box left unticked above.

---

## Gotchas worth remembering

- **The Supabase Storage browser caches hard.** It showed a file as still
  present after it had been deleted, which nearly sent me hunting a bug that
  didn't exist. The truth is a SQL query — `storage.objects` is a real table:
  ```sql
  select name, created_at from storage.objects
  where bucket_id = 'documents' order by created_at desc;
  ```
- **In PowerShell, `curl` is an alias for `Invoke-WebRequest`,** not real curl.
  It rejects `-X` and `-F` with confusing parameter errors. Use `curl.exe`
  (the genuine one at `C:\Windows\System32\curl.exe`).
- **Getting a session token by hand** for API testing: browser console on the
  dashboard, `copy(await window.Clerk.session.getToken())` — `copy()` is a
  DevTools helper that puts the value straight on the clipboard. Expires in
  about 60 seconds, so set the command up first.
- **DevTools Network tab shows nothing** if a type filter other than `All` /
  `Fetch/XHR` is selected. `performance.getEntriesByType("resource")` in the
  console is a filter-proof alternative.
- **FastAPI introspects a dependency override's signature.** Overriding with
  `lambda log=log: ...` makes FastAPI treat `log` as a *request parameter* and
  substitute its own value — which silently broke a verification harness and
  made it report empty results. Use a zero-argument closure instead.

---

## Still open, flagged for Day 6

- Every document sits at `status='pending'` because nothing moves it. Day 6
  builds the ingestion pipeline that will: download → parse → chunk → embed →
  insert chunks → update status.
- BUILD.md's Day 6 notes an `error` column on `documents` for failure
  messages — that *will* need a migration, unlike Day 5.
- The frontend already polls-by-refetch on demand; Day 6 adds polling every 3s
  while anything is `pending` or `processing`.

**What this achieves overall:** the app now owns user data for the first time.
A file goes from a browser into Supabase Storage and comes back out again, with
a database row tracking it, and each user sees strictly their own — enforced by
Postgres rather than by anything in the application code. Every feature after
this (chunks, embeddings, conversations, messages) rides on the same
mechanism, verified end to end today with two real accounts.

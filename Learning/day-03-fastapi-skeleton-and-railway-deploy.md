# Day 3 — FastAPI Skeleton & Railway Deploy

Personal learning log. Not read by Claude automatically — this is for me, to
recall what I built and why once the project is done. Not needed as context
for future sessions or development; it's a record, not a spec.

---

## Part 1 — FastAPI skeleton (backend exists, rejects unauthenticated requests)

### 1. Why a backend at all, and why FastAPI

The frontend (Next.js, Day 4) will never talk to Supabase directly for
sensitive operations — it talks to this backend, which talks to Supabase.
FastAPI was the locked choice in `CLAUDE.md` because Python is what the RAG
pipeline (parsing, chunking, embeddings, LiteLLM) needs later, and FastAPI
gives async request handling + automatic request validation "for free" via
Pydantic.

### 2. The folder shape

```
apps/api/
  app/
    main.py           # creates the FastAPI app, wires CORS + routers
    config.py          # reads env vars into a typed Settings object
    deps.py             # "who is calling?" — JWT verification logic
    routers/
      health.py         # GET /health — no auth
      me.py              # GET /me — auth required
    models/, services/  # empty, filled in on later days
  pyproject.toml        # uv-managed dependency list
  railway.json           # tells Railway how to build/run this
```

**Why routers are split into files:** each route file only knows about its
own endpoints. `main.py` just imports and "mounts" them
(`app.include_router(...)`). This keeps `main.py` short and means Day 5's
`documents.py` router, Day 7's `chat.py` router, etc. can be added without
touching existing files.

### 3. `config.py` — typed settings instead of raw `os.environ`

Uses `pydantic-settings`. Every env var the app needs is declared once as a
typed field (`supabase_url: str`, `clerk_jwt_issuer: str`, etc.). If a
required var is missing, the app **fails to start** with a clear error,
instead of crashing later mid-request when some code finally tries to read
`os.environ["SUPABASE_URL"]` and gets `None`.

**Why this matters in practice:** it turns "missing env var" from a runtime
bug into a startup-time bug — you find out immediately on deploy, not on the
first real user request.

### 4. `deps.py` — the JWT verification logic (the important part)

This is where a request proves "I am Clerk user X" without the backend ever
talking to Clerk on every request. Broken into the smallest pieces:

**a. What a JWT actually is.** A JSON Web Token is three base64 chunks
separated by dots: `header.payload.signature`. The header says which
algorithm and which key signed it. The payload holds claims (`sub` = subject
= the user's Clerk ID, `iss` = issuer = which Clerk instance made this
token, `exp` = expiry). The signature proves the payload wasn't tampered
with — but only if you can verify it against the right public key.

**b. Where the "right public key" comes from — JWKS.** Clerk doesn't share
its private signing key with anyone. Instead it publishes its *public* keys
at a well-known URL: `<issuer>/.well-known/jwks.json` (JWKS = JSON Web Key
Set). Our backend fetches that once, caches it in memory (`JwksCache` in
`deps.py`), and re-fetches it only rarely (every hour, or immediately if a
token arrives signed by a key we don't recognize yet — `kid` = key ID, a
header field naming *which* key in the set signed this specific token).

**c. The actual verification (`get_current_user`):**
1. Read the `Authorization: Bearer <token>` header. If missing → `401
   Missing bearer token`.
2. Peek at the token's header (unverified) to read `kid`.
3. Look up that `kid` in the cached JWKS. If Clerk rotated keys and we don't
   have it yet, refetch. If it's *still* not found → `401 Token signing key
   is not recognised` (this bit me during testing — more below).
4. Verify the signature using the matched public key, algorithm `RS256`
   (Clerk's default), and check the `iss` claim matches our configured
   `CLERK_JWT_ISSUER` exactly, and that the token isn't expired.
5. Pull `sub` out of the verified payload — that's the Clerk user ID. Return
   it.

**Why this is a FastAPI "dependency" (`Depends(get_current_user)`) instead
of a decorator or middleware:** any route that needs auth just declares
`user_id: CurrentUser` as a parameter. FastAPI runs the check before the
route body executes, and the route function never has to think about JWTs
at all — it just receives a trusted string.

### 5. Why `get_current_user` is NOT the real security boundary

`CLAUDE.md` is explicit: **RLS is the security boundary**, not backend auth
checks. `get_current_user` exists so an unauthenticated request gets a
clean, fast `401` instead of an ugly database error. But the actual
guarantee that user A can never see user B's rows comes from the RLS
policies written on Day 1, enforced by Postgres itself.

This is why `get_supabase_client` (also in `deps.py`) forwards the caller's
*original* JWT to Supabase on every request (via the `Authorization`
header), using the `anon` key — not the `service_role` key, which would
bypass RLS entirely. Postgres re-derives `request.jwt.claims` from that
forwarded token and the Day 1 policies do the actual filtering. The FastAPI
layer authenticates; the database layer authorizes.

### 6. `/health` vs `/me` — why one has no auth

- `GET /health` → `{"status": "ok"}`, no auth. Purely "is the process up and
  responding." Railway's health-check pings this — if it required a Clerk
  JWT, Railway itself would never be able to confirm the service is alive.
- `GET /me` → `{"user_id": ...}`, requires a valid JWT. This is the first
  real proof that the whole chain (browser → JWT → FastAPI → Clerk JWKS →
  verified user id) works end to end.

### 7. CORS

`CORSMiddleware` in `main.py` allows requests from `cors_origins` (comma
separated, defaults to `http://localhost:3000` for local dev). Without this,
a browser calling this API from a different origin (like the eventual
Vercel URL) would have the response blocked by the browser itself, even
though the server responded fine — CORS is a browser-side protection, not a
server-side one. Day 4 will add the real Vercel URL here.

---

## Part 2 — Railway deploy (backend live on a real URL)

### 1. What Railway is doing

Railway takes a GitHub repo, builds it into a container, and runs it on a
public URL — no manually writing a Dockerfile required (though you can).

**Root Directory setting:** the repo is a monorepo (`apps/web`, `apps/api`,
etc.), but Railway needs to build just `apps/api`. Setting **Root
Directory** to `apps/api` in the service's Settings tab tells Railway "treat
this subfolder as the whole project" — so it finds `pyproject.toml` and
`railway.json` there instead of getting confused by the repo root.

**Nixpacks:** Railway's own auto-detecting builder. It looks at the project
(sees `pyproject.toml`, a Python project managed by `uv`) and generates a
build plan without needing a hand-written Dockerfile — installs `uv`, runs
`uv sync`, and moves on to the start command.

### 2. `railway.json` — telling Railway how to run it

```json
{
  "build": { "builder": "NIXPACKS" },
  "deploy": {
    "startCommand": "uvicorn app.main:app --host 0.0.0.0 --port $PORT",
    "healthcheckPath": "/health",
    "healthcheckTimeout": 60,
    "restartPolicyType": "ON_FAILURE",
    "restartPolicyMaxRetries": 3
  }
}
```

- `startCommand` — how to actually run the app. `--host 0.0.0.0` means
  "listen on all network interfaces" (required in a container — `127.0.0.1`
  would only accept connections from inside the container itself).
  `--port $PORT` — Railway assigns the port dynamically and injects it as an
  env var; hardcoding a port number would break on Railway's infrastructure.
- `healthcheckPath` — after deploying, Railway polls `/health` before
  routing real traffic to the new version. This is exactly why `/health`
  has zero auth — Railway itself has no Clerk JWT to send.
- `restartPolicyType: ON_FAILURE` — if the process crashes, Railway restarts
  it automatically (up to 3 times) instead of leaving the service down.

### 3. Env vars actually needed on Railway

Only three, because that's all `config.py` currently reads:
- `SUPABASE_URL`
- `SUPABASE_ANON_KEY`
- `CLERK_JWT_ISSUER`

`SUPABASE_SERVICE_ROLE_KEY`, `DATABASE_URL`, `CLERK_SECRET_KEY`, etc. are in
`.env.example` for documentation but aren't consumed by any code yet — no
need to set them on Railway until a future day's code actually reads them.

### 4. The debugging journey (this is where the real lessons were)

**a. `curl` on Windows PowerShell isn't `curl`.** PowerShell aliases `curl`
to `Invoke-WebRequest`, which takes completely different flags. `curl -i -s
... -H "..."` failed with a confusing `Invoke-WebRequest : Missing an
argument for parameter 'InFile'` error. Fix: call `curl.exe` explicitly
(the real curl binary, if installed) to bypass the alias, or use
`Invoke-WebRequest -Uri ... -Headers @{...}` in native PowerShell syntax.

**b. First real JWT test failed: `Token signing key is not recognised`.**
This error means the JWKS the backend fetched (from `CLERK_JWT_ISSUER`)
didn't contain the key that actually signed the token being tested — i.e.
the token came from a *different* Clerk instance than the one the backend
was configured to trust. Root cause: signed into **Clerk's own dashboard
login** (`clerk.clerk.com`) instead of DocQuery's own Clerk instance. Easy
mistake — both look like "signing into Clerk."

**c. Clerk has multiple relevant URLs, and they're not interchangeable:**
- **Clerk Dashboard** (`dashboard.clerk.com`) — where *you* manage the
  Clerk *application*. Signing in here has nothing to do with your app's
  end users.
- **Frontend API URL** (e.g. `https://xxx.clerk.accounts.dev`) — an API
  host used by Clerk's JS SDK under the hood. Visiting it directly in a
  browser shows a blank page — it's not meant for humans, it's an API
  endpoint. This is also the value `CLERK_JWT_ISSUER` must equal.
- **Account Portal** — Clerk's actual hosted sign-in/sign-up *page* for your
  app's end users. This is what needs to be visited to sign in as a test
  user and get a real session token, and it's found in the dashboard under
  Configure → Account Portal.

**d. `CLERK_JWT_ISSUER` needs the full `https://` scheme.** The dashboard
UI displays the Frontend API URL without the scheme prefix in some views,
which led to setting the Railway env var without `https://`. But
`config.py` builds the JWKS URL as `f"{issuer}/.well-known/jwks.json"` and
also does an exact string comparison against the token's `iss` claim (which
always includes `https://`, since JWT issuers are full URLs by spec) — so a
missing scheme breaks both the JWKS fetch and the issuer check. Fix: always
include `https://` in the env var.

**e. Development vs Production Clerk instances.** Confirmed via the
`CLERK_SECRET_KEY` prefix (`sk_test_...` = Development, `sk_live_...` =
Production). Production requires a verified custom domain, which this
project doesn't have yet (no Vercel deploy until Day 4) — so Development is
correct for now, and will need revisiting once a real domain exists.

### 5. Verification performed (Day 3 "done when")

- [x] `curl <railway-url>/health` → `200 {"status":"ok"}`
- [x] `curl <railway-url>/me` with no `Authorization` header → `401 {"detail":"Missing bearer token."}`
- [x] `curl <railway-url>/me` with a real Clerk JWT (grabbed from the
      Account Portal sign-in, on the correct Development instance) →
      `200` with the real `user_id`
- [x] `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `CLERK_JWT_ISSUER` set on Railway

**What this achieves overall:** the backend now exists on a real public
URL, correctly rejects unauthenticated traffic, and correctly verifies real
Clerk-issued identity tokens end to end — including the Clerk → Supabase
JWT handshake groundwork Day 5 will depend on for RLS to work through the
API instead of just the SQL editor.

---

## Small fix bundled in alongside this: `.env.example` naming

`CLERK_PUBLISHABLE_KEY` was renamed to `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`
in `.env.example`. Next.js only exposes env vars to browser-side code when
the name starts with `NEXT_PUBLIC_` — anything without that prefix stays
server-only. The publishable key is meant to be used client-side (Day 4's
`apps/web`), so it needs the prefix; `CLERK_SECRET_KEY` correctly has no
prefix since it must never reach the browser. Doesn't affect today's
FastAPI/Railway work — `config.py` doesn't read the publishable key at all.

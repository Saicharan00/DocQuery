# Day 4 — Next.js Skeleton & Vercel Deploy

Personal learning log. Not read by Claude automatically — this is for me, to
recall what I built and why once the project is done. Not needed as context
for future sessions or development; it's a record, not a spec.

---

## Part 1 — Scaffold + Clerk wiring (frontend exists, knows who's signed in)

### 1. Why `apps/web` is the last piece, and what "wiring" means

Days 1–3 built the whole backend spine with no UI: Supabase schema + RLS,
Clerk issuing tokens, FastAPI verifying them on Railway. Day 4 is the first
day a human can actually click something. The frontend's only two jobs today
are (a) let a user sign in via Clerk, and (b) prove it can call the live
Railway API with that user's identity attached.

### 2. Next.js 16, and the `middleware.ts` → `proxy.ts` rename

BUILD.md assumed Next.js 15, but 16 was current at scaffold time. The one
practical difference: Next 16 renamed the root middleware file from
`middleware.ts` to `src/proxy.ts`. Its job is unchanged — it's the one file
that runs on *every* matched request before any page or layout renders,
useful for attaching session context early. Next.js ships its own docs
inside `node_modules/next/dist/docs/` specifically so a coding agent (or a
human) working against unfamiliar major-version changes can check current
behavior instead of relying on stale training data — that's what caught this
rename.

### 3. Why Clerk's route protection moved out of middleware

Clerk's own `createRouteMatcher()` helper — the "normal" way to protect
routes in middleware — is deprecated in `@clerk/nextjs` v7 and logs a
warning if used. Instead, `proxy.ts` runs a bare `clerkMiddleware()` with no
route logic at all; it just makes Clerk's session available. The actual
gate — "is anyone allowed past this point" — was deferred to Part 2's
`dashboard/layout.tsx`, a plain server component. Same net effect (no
session → no access), zero deprecation warnings, and the guard logic lives
in one obvious place instead of a middleware matcher regex.

### 4. `<ClerkProvider>` goes inside `<body>`, not around `<html>`

Small but exact detail: wrapping `<html>` would mean Clerk's context
provider renders before the document even has a body, which isn't how
React's tree works here. `<ClerkProvider>` needs to be inside `<body>`,
wrapping `{children}`, so every page underneath it can call Clerk's hooks.

### 5. `auth()` is async — and the export trap

`@clerk/nextjs/server`'s `auth()` returns a `Promise`, always needs
`await`. The trap: the *top-level* `@clerk/nextjs` package also exports
something called `auth`, but typed as `never` — a decoy that exists for
historical/compat reasons. The real, usable one only comes from
`@clerk/nextjs/server`. Verified this against the installed package's own
`.d.ts` type definitions rather than trusting memory or docs, since a wrong
import here would silently break at compile time in a confusing way.

### 6. shadcn/ui init — the interactive prompt gotcha

`npx shadcn@latest init` normally asks interactive questions (which
component library, which style). In this terminal environment, arrow-key
input into that prompt didn't reliably register — it silently locked in the
default answer (Base UI) instead of the intended Radix UI once already.
Fix: skip the interactive prompt entirely and pass every choice as a flag:
`npx shadcn@latest init -b radix -p nova -c apps/web -y`. Lesson for any
future CLI wizard in this environment: prefer non-interactive flags over
arrow-key prompts.

### 7. Env vars added

`NEXT_PUBLIC_API_URL` — new key, holding the live Railway base URL. Added to
both `apps/web/.env.local` (real value, gitignored) and the root
`.env.example` (empty placeholder), per the repo rule that every new env key
gets documented in the same commit it's introduced.

---

## Part 2 — Dashboard guard, API client, pages, and the live Vercel↔Railway loop

### 1. The two-layer guard, concretely

`dashboard/layout.tsx` is a server component wrapping everything under
`/dashboard`:

```ts
const { userId } = await auth();
if (!userId) redirect("/sign-in");
```

Because every route under `dashboard/` renders *inside* this layout, an
unauthenticated request never reaches `dashboard/page.tsx` — Next.js's
nested-layout model does the enforcement, not a URL-matching regex in
middleware.

### 2. `useApi()` — why a hook, not a plain function

Getting the current user's JWT (`getToken()`) requires Clerk's React
context, which only exists inside client components. So `src/lib/api.ts` is
marked `"use client"` and exports a hook, `useApi()`, that closes over
`useAuth().getToken` and returns a `fetch` wrapper. Every call re-fetches
the token fresh (Clerk handles refreshing it under the hood — no caching
needed on this side) and attaches it as `Authorization: Bearer <token>`.
Non-2xx responses throw with the actual response body in the error message,
never just a bare status code — matches the repo rule against silent
failures.

### 3. The stray-slash build break

While testing, the dev server threw `Unterminated regexp literal` pointing
at a bare `/` on its own line at the end of `sign-in/[[...sign-in]]/page.tsx`
— a leftover keystroke from an earlier IDE edit, unrelated to anything built
today. Removing it brought the file back to exactly what was already
committed. Small reminder that build errors are sometimes just stray
characters, not logic bugs — worth reading the actual error location before
assuming something conceptually wrong.

### 4. The missing-scheme bug — the most instructive one today

The dashboard's `/me` call returned an "error" that, on inspection, was
Next.js's *own* 404 HTML page (`<title>404: This page could not be
found</title>`), not anything from the FastAPI backend. Root cause:
`NEXT_PUBLIC_API_URL` in `.env.local` was set to
`docquery-production-2187.up.railway.app` — missing the `https://` scheme.
`fetch()` treats a schemeless string as a *relative* path, so
`${API_URL}/me` resolved against the current origin
(`localhost:3000/docquery-production-2187.up.railway.app/me`) instead of as
an absolute URL pointing at Railway. Confirmed by grepping the dev server's
own request log, which showed exactly that malformed path being requested
locally. Lesson: a "same-origin" env var typo doesn't look like a network
error at all — it looks like a working request that happens to 404, which
is a much more confusing failure mode than an outright connection refusal.

### 5. Port conflicts and an orphaned dev server

Two separate stuck processes showed up back to back:
- A leftover `node.exe` was already bound to port 3000, so `next dev` fell
  back to 3001 automatically.
- After stopping the tracked background task for that server, the actual
  `next dev` *child* process kept running detached — Next.js's own
  dev-server lock file caught this and refused to start a second instance
  in the same project directory, reporting the orphaned PID directly in its
  own error message.

Both had to be killed by PID (`Stop-Process`) before a clean restart. The
practical cost of leaving port 3000 occupied: the app worked, just on the
wrong port, and cascaded into the next bug below.

### 6. Why the wrong port turned into a CORS error

Railway's `CORS_ORIGINS` default (`apps/api/app/config.py`) is
`http://localhost:3000` only. Once the dev server was accidentally running
on `:3001` (because of the port conflict above), the browser's `Origin`
header didn't match anything in the backend's allow-list, and the request
was blocked *by the browser itself* — the server never even gets to
respond in a way JavaScript can read. Symptom: a generic `Failed to fetch`
with no further detail, which is the classic signature of a CORS rejection
rather than an actual network or server error. Fixing the port conflict
(so the dev server ran on the expected `:3000`) resolved it without touching
Railway config at all.

### 7. `<UserButton />` — a small, approved scope addition

Testing the "sign out → redirected again" verification step surfaced a gap:
nothing on the dashboard offered a way to sign out. Added Clerk's
`<UserButton />` (an avatar with a built-in dropdown including "Sign out",
no custom logic required) — flagged as a small addition beyond the
original page spec and approved before adding, rather than silently
expanding scope.

### 8. Deploying to Vercel — why root directory matters

The repo is a monorepo (`apps/web` + `apps/api` + others). Vercel's default
assumption is that the repo root *is* the app. Setting **Root Directory** to
`apps/web` in the Vercel project's Configure screen tells it to treat that
subfolder as the whole project — otherwise it wouldn't find `package.json`
or correctly detect the Next.js framework preset. Same shape of setting as
Railway's Root Directory from Day 3, just Vercel's version of the same
monorepo problem.

### 9. Closing the CORS loop for real

Order mattered here: CORS couldn't be configured *before* Vercel handed
back a live URL, since the whole point is allow-listing that exact origin.
Once the Vercel deploy produced `https://doc-query-lac.vercel.app`, Railway
needed a **new** `CORS_ORIGINS` variable (it had never been explicitly set
before — the backend had been running on the code's `localhost:3000`
default the whole time), set to:

```
http://localhost:3000,https://doc-query-lac.vercel.app
```

`config.py`'s `cors_origin_list` property already splits on commas, so no
code change was needed — just the env var and a redeploy. Verified the fix
landed *before* trusting the browser, by sending a manual CORS preflight
request:

```
curl -i -X OPTIONS <railway-url>/me \
  -H "Origin: https://doc-query-lac.vercel.app" \
  -H "Access-Control-Request-Method: GET"
```

and confirming the response header `access-control-allow-origin` echoed
back the exact Vercel origin. Only after that did the live browser test
happen — sign in on the actual Vercel URL, dashboard shows email + user_id
pulled from the live Railway `/me` endpoint, no console errors.

### 10. Verification performed (Day 4 "done when")

- [x] `/dashboard` while signed out → redirects to `/sign-in` (local + live)
- [x] Sign in → dashboard shows `user_2…` id and email (local + live)
- [x] Sign out → `/dashboard` redirects again (via `<UserButton />`)
- [x] Live Vercel page successfully calls live Railway `/me`
- [x] Railway `CORS_ORIGINS` includes the Vercel origin, confirmed via a
      manual preflight check before the browser test

**What this achieves overall:** the two halves of the system — Next.js on
Vercel and FastAPI on Railway — now talk to each other for the first time,
across real origins, with a real signed-in user's identity flowing from
Clerk's browser SDK, through a JWT, to the backend's verification logic
built on Day 3. Everything from here on (uploads, chat, RAG) builds on this
same request shape: browser → Clerk token → `useApi()` → Railway → verified
`user_id`.

---

## Still open, flagged for Day 5

Supabase hasn't yet been configured to trust Clerk as a third-party auth
provider. `/me` only proves JWT verification works — it never touches the
database. Row Level Security depends on Supabase recognizing Clerk-issued
tokens directly; until that's wired up, forwarding a Clerk JWT to Supabase
would fail RLS checks even though the token itself is perfectly valid. This
is Day 5's first blocker, not something today's work was expected to solve.

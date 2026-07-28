# Day 2 — Clerk Auth Setup

Personal learning log. Not read by Claude automatically — this is for me, to
recall what I built and why once the project is done.

---

## 1. What Clerk is doing here

**Goal:** users can sign up / sign in, and the JWT Clerk issues carries the
user's ID in a place Supabase RLS already knows how to read.

**Steps taken:**
1. Created a Clerk application (single-user accounts — Organizations feature
   left off, matches the `CLAUDE.md` non-goal of no orgs/multi-tenant).
2. Enabled Email + Password and Google as sign-in methods.
3. Noted the three values Day 1's RLS design depends on:
   - `CLERK_PUBLISHABLE_KEY`
   - `CLERK_SECRET_KEY`
   - `CLERK_JWT_ISSUER` — the "Frontend API URL" shown on Clerk's API Keys
     page (e.g. `https://xxx-xx.clerk.accounts.dev`). JWKS lives at
     `<issuer>/.well-known/jwks.json` — that's what Day 3's FastAPI backend
     will fetch to verify JWT signatures.
4. Added all three (empty placeholders) to `.env.example`; real values went
   into the local, gitignored `.env` — never committed, never read back by
   Claude (enforced by the `protect-env.sh` hook from Day 1).

**Why no custom JWT template was needed:** Clerk's default session token
already puts the Clerk user ID in the standard `sub` claim. Day 1's RLS
policies already read `request.jwt.claims ->> 'sub'` — so Clerk's default
token shape lines up with the schema exactly as designed, with zero extra
Clerk-side configuration.

---

## 2. Verification performed (Day 2 "done when")

- [x] Signed up a test user via Clerk's hosted Account Portal.
- [x] Pulled the `__session` cookie (the JWT) from browser DevTools and
      decoded it at jwt.io.
- [x] Confirmed the decoded `sub` claim matches the test user's ID shown in
      the Clerk dashboard (`user_...`).
- [x] Clerk keys added to `.env.example`.

**What this achieves overall:** auth now produces exactly the identity token
Day 1's RLS was built to consume. Day 3 (FastAPI) just needs to verify the
JWT signature against Clerk's JWKS and pass the `sub` through untouched —
no mapping or translation layer required between Clerk and Postgres RLS.

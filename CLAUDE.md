# CLAUDE.md

Read this before doing anything. Full day-by-day plan in `BUILD.md`.

---

## Project

Multi-model RAG chat over user-uploaded documents. Next.js on Vercel + FastAPI on Railway + Supabase (Postgres + pgvector + Storage). Users bring their own LLM API key.

---

## Working Agreement

- I am the PM. I decide WHAT to build and WHICH approach to take.
- You are the engineer. You write the code and handle implementation details.
- ASK me before: choosing libraries, changing architecture, adding new endpoints, modifying the schema, or spending money (paid API calls).
- DON'T ask me about: variable names, file organization within agreed structure, error handling patterns, import choices, or other routine coding decisions.
- When I give a task, implement it fully. Don't stop halfway to ask if you should continue.
- Check existing code before creating new files. Don't duplicate utilities.
- Don't skip ahead. One day at a time — don't scaffold Day 7 code on Day 5.
- Don't invent scope. If BUILD.md doesn't mention it, ask.
- If something looks broken or wrong, say so and stop. Don't silently pick a different approach.
- If you don't know something, say so. Don't fabricate library APIs, Clerk claims, or Supabase behavior.
- before every day starts inform the user to use the plan mode to create plan and then do the coding or working part, by this user(me) can understand whats going on and leanr why we are doing it.

---

## Stack (locked — do not substitute without asking)

- **Frontend**: Next.js 15 App Router, Tailwind, shadcn/ui → Vercel
- **Backend**: FastAPI → Railway
- **Auth**: Clerk (single-user, no orgs)
- **DB + vectors + storage**: Supabase (Postgres + pgvector + Storage)
- **LLM routing**: LiteLLM
- **Embeddings**: OpenAI `text-embedding-3-small` (1536 dim)
- **Parsing**: PyMuPDF (PDF), python-docx (DOCX), builtin (TXT)
- **Chunking**: `RecursiveCharacterTextSplitter`, 800 tokens, 100 overlap
- **Observability**: LangSmith
- **Eval**: custom script + RAGAS

## Non-goals (v1)

No orgs. No Stripe. No local LLM / Ollama. No desktop app. No hosted API keys — user pastes their own.

---

## Repo layout

```
<project>/
├── CLAUDE.md
├── BUILD.md
├── README.md
├── .env.example
├── .gitignore
├── apps/
│   ├── web/       # Next.js (Vercel)
│   └── api/       # FastAPI (Railway)
├── infra/supabase/migrations/
└── scripts/       # eval.py etc.
```

Keep it flat. No `packages/`, `shared/`, `libs/` unless there's genuinely shared code.

---

## Rules

- **Python**: 3.11+, `uv` for deps (`uv add`, `uv sync` — not `pip install`).
- **Node**: npm.
- **Env vars**: every new key → also add to `.env.example` (empty value) in the same commit.
- **Secrets — hard rules**:
  - Never commit real keys.
  - Never read, print, log, or echo the contents of `.env`, `.env.local`, or any file holding real keys. Reference the variable name only (e.g. `OPENAI_API_KEY`), never its value.
  - Never include API key values in error messages, stack traces, or user-facing output.
  - Never log user-supplied API keys server-side. They live in memory for the duration of one request and then go away.
  - If I paste a real key into chat by accident, tell me immediately and don't repeat it back.
- **Migrations**: numbered SQL files. Never edit a run migration — write a new one.
- **RLS is the security boundary.** Don't rely on backend `WHERE user_id = X` filtering. Assume anything without RLS is a bug.
- **Errors surface to the user meaningfully.** No `except: pass`. No silent failures.
- **Streaming**: SSE, not WebSockets.
- **No tests unless I ask.**
- **Commit + push at end of each day**, even if incomplete. Commit history is part of the portfolio.

---

## My environment

Windows / PowerShell / VS Code / project on `E:\`. Translate bash commands to PowerShell before running.

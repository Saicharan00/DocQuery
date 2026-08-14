/** Mirrors `DocumentOut` in apps/api/app/models/document.py, and the chat
 *  contract in apps/api/app/routers/chat.py. */

export type DocumentStatus = "pending" | "processing" | "ready" | "failed";

export interface Document {
  id: string;
  name: string;
  file_path: string;
  status: DocumentStatus;
  file_size: number | null;
  mime_type: string | null;
  /** Why ingestion failed. Null unless `status` is "failed". */
  error: string | null;
  /** ISO 8601 timestamp. */
  created_at: string;
}

/**
 * One slice of ingestion, from `POST /documents/{id}/ingest/step`.
 *
 * The server works for ~45 seconds and returns this. `done` is false while there
 * is more to do, and the browser calls again — each call carrying a fresh Clerk
 * token, which is the whole reason ingestion is shaped this way.
 */
export interface IngestStep {
  done: boolean;
  chunks_done: number;
  chunks_total: number;
  status: DocumentStatus;
  /** Page the most recently stored chunk came from. 0 before anything is stored. */
  page: number;
  /** Pages in the document. 0 when the server had no reason to work it out. */
  pages: number;
  /** How many of `chunks_total` are pictures rather than text. */
  images_total: number;
}

// ---------------------------------------------------------------------------
// Chat
// ---------------------------------------------------------------------------

/** One retrieved chunk, as cited under an answer. Mirrors `rag.to_sources`. */
export interface Source {
  /** 1-based, and the same number the model writes as [1], [2] in its answer. */
  number: number;
  /**
   * The chunk's own id, which is what `GET /documents/{document_id}/images/
   * {chunk_id}` accepts. `image_path` below names the file but cannot be
   * fetched — it is a Storage path the browser holds no credential for.
   *
   * Optional because answers saved before this existed have no such field in
   * their stored `sources`. Those keep their citations and show no picture.
   */
  chunk_id?: string | null;
  document_id: string;
  document_name: string;
  /** Position within its own document — not the citation number. */
  chunk_index: number;
  page_number: number | null;
  chunk_type: "text" | "image";
  image_path: string | null;
  /** First 300 characters of the chunk (`rag.PREVIEW_CHARS`). */
  content_preview: string;
  similarity: number | null;
}

/**
 * The six things `/chat` can send down the stream.
 *
 * `conversation` arrives first, then `sources`, then many `token`s, then either
 * `done` or `error`. Nothing else is possible, which is what makes the switch
 * in the chat page exhaustive.
 *
 * `title` is the exception to that ordering, and it is conditional: it arrives
 * just before `done`, and only for a conversation that was created by this very
 * request. `_retitle` in chat.py names it once the first answer is finished, and
 * stays silent if the naming call failed.
 */
export type ChatEvent =
  | { event: "conversation"; data: { id: string; run_id: string | null } }
  | { event: "sources"; data: { sources: Source[] } }
  | { event: "token"; data: { text: string } }
  | { event: "title"; data: { title: string } }
  | { event: "error"; data: { detail: string } }
  | { event: "done"; data: Record<string, never> };

// In plain English: this is a "discriminated union" — a value that is exactly
// one of five shapes, told apart by its `event` field. Its payoff is in the
// chat page: inside `case "token":` TypeScript already knows `data` has a
// `text` and no `id`, so a typo like `data.tekst` fails the build instead of
// silently rendering "undefined".

/** One bubble on screen. `sources` only ever exists on an assistant message. */
export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  sources?: Source[];
  /**
   * The LangSmith trace this answer came from, which is what a rating is
   * attached to. Set both for an answer streamed just now and for one replayed
   * from history, since migration 006 stores it.
   *
   * Null or absent means no rating buttons: a user bubble, an answer from before
   * that migration, or one produced while the server had tracing switched off.
   */
  runId?: string | null;
}

/**
 * What `POST /feedback` accepts. Mirrors `FeedbackRequest`.
 *
 * Both `score` and `comment` are optional, and the server rejects a submission
 * carrying neither. They are separate because the thumb is sent on click and any
 * comment afterwards, as its own submission with no score — sending the score
 * again would count one reader twice in the average.
 */
export interface FeedbackBody {
  run_id: string;
  /** 1 for a thumbs up, 0 for a thumbs down. */
  score?: 0 | 1;
  comment?: string | null;
}

/**
 * What `POST /feedback/product` accepts. Mirrors `ProductFeedbackRequest`.
 *
 * No `run_id`, and that is the whole difference: this judges DocQuery itself,
 * so there is no answer and no trace to attach it to. It is stored in the
 * `product_feedback` table from migration 007 instead.
 *
 * Split the same way as the per-answer feedback — stars go the instant they are
 * clicked, any comment follows as its own submission — so both fields are
 * optional here and the server refuses one carrying neither.
 */
export interface ProductFeedbackBody {
  /** 1–5 stars. A product has degrees that a single answer does not. */
  rating?: 1 | 2 | 3 | 4 | 5;
  comment?: string | null;
}

// ---------------------------------------------------------------------------
// Conversation history
// ---------------------------------------------------------------------------

/** One row in the sidebar. Mirrors `ConversationOut`. */
export interface Conversation {
  id: string;
  /** Null is possible in the schema, so the UI falls back to "New conversation". */
  title: string | null;
  created_at: string;
  /** The sort key — last activity, not creation. ISO 8601. */
  updated_at: string;
}

/**
 * One stored message, as `GET /conversations/{id}/messages` returns it.
 * Mirrors `MessageOut`.
 *
 * Close to `ChatMessage` but not the same thing, and deliberately kept apart:
 * this is what the database holds, `ChatMessage` is what a bubble needs. A
 * streaming answer is a `ChatMessage` that has no row yet.
 */
export interface MessageRow {
  id: string;
  role: "user" | "assistant";
  content: string;
  model: string | null;
  sources: Source[] | null;
  /**
   * The LangSmith run that produced this answer, stored since migration 006.
   * Null on user rows, on answers written before that migration, and on any
   * answer produced while tracing was off.
   */
  run_id: string | null;
  created_at: string;
}

/**
 * Mirrors `rag.SUPPORTED_MODELS`.
 *
 * Duplicated here for the human-readable label, the same way `upload-zone.tsx`
 * duplicates the file limits: this copy is for the dropdown, and the `Literal`
 * in `apps/api/app/models/chat.py` is the one that actually decides. If they
 * disagree, the server refuses with a 422 and the user sees its message.
 */
export const MODELS = [
  { id: "gemini/gemini-3.5-flash-lite", label: "Gemini 3.5 Flash Lite" },
  { id: "gpt-5.4-nano", label: "GPT-5.4 Nano" },
] as const;

export type ModelName = (typeof MODELS)[number]["id"];

// `as const` freezes the array so TypeScript remembers the exact strings rather
// than widening them to plain `string`. That is what lets the line below derive
// `ModelName` as "gemini/gemini-3.5-flash-lite" | "gpt-5.4-nano" — one list to
// maintain instead of a list and a matching type.

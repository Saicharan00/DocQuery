/** Mirrors `DocumentOut` in apps/api/app/models/document.py. */

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
}

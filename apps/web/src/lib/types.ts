/** Mirrors `DocumentOut` in apps/api/app/models/document.py. */

export type DocumentStatus = "pending" | "processing" | "ready" | "failed";

export interface Document {
  id: string;
  name: string;
  file_path: string;
  status: DocumentStatus;
  file_size: number | null;
  mime_type: string | null;
  /** ISO 8601 timestamp. */
  created_at: string;
}

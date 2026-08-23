"use client";

import { useCallback, useState } from "react";
import { FileText, Loader2, RotateCw, Trash2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { AuthNotReadyError, useApi } from "@/lib/api";
import type { Document, DocumentStatus, IngestStep } from "@/lib/types";
import { cn } from "@/lib/utils";

type DocumentListProps = {
  documents: Document[];
  /** Live ingestion progress, keyed by document id. Absent until a step returns. */
  progress: Record<string, IngestStep>;
  /**
   * Documents this browser session stopped driving, whatever the server thinks.
   *
   * Needed because `status` alone is not a reliable answer to "is anything still
   * happening here". Recording a failure is itself a database write, and if that
   * write is what failed the row stays on `processing` for ever — a spinner with
   * no Retry beside it, because Retry used to appear only for `failed`.
   */
  abandonedIds: Set<string>;
  /**
   * Documents currently waiting for a free ingest slot server-side, not
   * failed and not abandoned — just queued behind other people's uploads.
   */
  queuedIds: Set<string>;
  /** Resume a failed document from wherever it stopped. */
  onRetry: (id: string) => void | Promise<void>;
  /**
   * Called after a successful delete so the parent can re-fetch the list.
   * Takes the deleted id so the parent can also tell an ingest loop still
   * driving that document that its next 404 is expected, not a failure.
   */
  onDeleted: (id: string) => void | Promise<void>;
};

/** The four states a document moves through as it is ingested. */
const STATUS_VARIANT: Record<
  DocumentStatus,
  "default" | "secondary" | "destructive" | "outline"
> = {
  pending: "secondary",
  processing: "secondary",
  ready: "default",
  failed: "destructive",
};

/** A document is "working" from upload until it is stored or has given up. */
function isWorking(document: Document): boolean {
  return document.status === "pending" || document.status === "processing";
}

/** Null until the first step returns — nothing is known about size before that. */
function percentOf(step: IngestStep | undefined): number | null {
  if (!step || step.chunks_total === 0) return null;
  return Math.round((step.chunks_done / step.chunks_total) * 100);
}

function describeProgress(step: IngestStep | undefined): string {
  if (!step) return "Reading the document…";

  const percent = percentOf(step) ?? 0;
  const chunks = `${step.chunks_done} of ${step.chunks_total} chunks embedded`;

  // Pages are missing for formats without them (DOCX, TXT), and most documents
  // have no figures at all, so the sentence is built rather than templated — a
  // stray "page 0 of 0" or "0 images" reads like a bug.
  const parts = [`${percent}%`];
  if (step.pages > 0) parts.push(`page ${step.page} of ${step.pages}`);
  parts.push(chunks);
  if (step.images_total > 0) {
    parts.push(`${step.images_total} ${step.images_total === 1 ? "image" : "images"}`);
  }

  return parts.join(" · ");
}

/**
 * Above this many chunks, one tile stands for several. 278 individual squares
 * would be visual noise, and the row would be taller than the list.
 */
const MAX_TILES = 120;

/** How many tiles to show while the document's size is still unknown. */
const PLACEHOLDER_TILES = 40;

/**
 * The document's chunks, lighting up as each is embedded.
 *
 * Deliberately not a progress bar. A bar shows a fraction; this shows the thing
 * itself — a document becoming N searchable pieces, which is exactly what the
 * server is doing and what makes retrieval possible later.
 */
function ChunkGrid({ step }: { step: IngestStep | undefined }) {
  const total = step?.chunks_total ?? 0;
  const known = total > 0;

  const tiles = known ? Math.min(total, MAX_TILES) : PLACEHOLDER_TILES;
  const filled = known ? Math.round((step!.chunks_done / total) * tiles) : 0;

  // The breathing region: the newer half of what has been embedded.
  const pulseStart = Math.floor(filled / 2);

  return (
    <div
      className={cn("mt-1.5 flex flex-wrap gap-0.5", !known && "animate-pulse")}
      role="progressbar"
      aria-valuenow={known ? step!.chunks_done : undefined}
      aria-valuemax={known ? total : undefined}
      aria-label="Chunks embedded"
    >
      {Array.from({ length: tiles }, (_, index) => (
        <span
          key={index}
          className={cn(
            "size-1.5 rounded-[1px] transition-colors duration-500",
            index < filled ? "bg-primary" : "bg-muted-foreground/20",
            // The newer half of what's been embedded breathes together, so the
            // growing edge is a moving region rather than a hard line — and
            // there is still motion during the pause after a rate limit.
            index >= pulseStart && index < filled && "animate-pulse",
          )}
        />
      ))}
    </div>
  );
}

function formatSize(bytes: number | null): string {
  if (bytes === null) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export function DocumentList({
  documents,
  progress,
  abandonedIds,
  queuedIds,
  onRetry,
  onDeleted,
}: DocumentListProps) {
  const api = useApi();

  // Which row is mid-delete, so only that button shows a spinner.
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const remove = useCallback(
    async (id: string) => {
      setError(null);
      setDeletingId(id);

      try {
        await api(`/documents/${id}`, { method: "DELETE" });
        await onDeleted(id);
      } catch (e) {
        if (e instanceof AuthNotReadyError) {
          setError("Still signing you in. Try again in a moment.");
        } else {
          setError((e as Error).message);
        }
      } finally {
        setDeletingId(null);
      }
    },
    [api, onDeleted],
  );

  if (documents.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No documents yet. Upload one to get started.
      </p>
    );
  }

  return (
    <div>
      {error && (
        <p role="alert" className="mb-2 text-sm text-destructive">
          {error}
        </p>
      )}

      <ul className="divide-y rounded-lg border">
        {documents.map((document) => (
          <li
            key={document.id}
            className="flex items-center gap-3 px-4 py-3 text-sm"
          >
            <FileText className="size-4 shrink-0 self-start text-muted-foreground" />

            <div className="min-w-0 flex-1">
              {/* truncate, because a long filename would otherwise push the
                  delete button off the row. */}
              <p className="truncate font-medium">{document.name}</p>
              <p className="text-xs text-muted-foreground">
                {formatDate(document.created_at)}
                {document.file_size !== null &&
                  ` · ${formatSize(document.file_size)}`}
              </p>

              {/* Everything below comes from the step responses, not from a
                  poll — the loop driving ingestion already knows where it is.
                  Suppressed once this session has given up, because a progress
                  bar that cannot move is a worse lie than no progress bar. */}
              {isWorking(document) &&
                !abandonedIds.has(document.id) &&
                !queuedIds.has(document.id) && (
                  <>
                    <ChunkGrid step={progress[document.id]} />
                    <p className="mt-1.5 text-xs text-muted-foreground">
                      {describeProgress(progress[document.id])}
                    </p>
                  </>
                )}

              {/* Shown instead of the progress bar, not alongside it: a queued
                  document has no step response yet, so the bar would just say
                  "Reading the document…" forever, which looks identical to
                  something broken. This is the line that tells a visitor "the
                  demo is fine, there's just a line" instead of them giving up
                  on it. */}
              {isWorking(document) &&
                !abandonedIds.has(document.id) &&
                queuedIds.has(document.id) && (
                  <p className="mt-1.5 text-xs text-muted-foreground">
                    Server is busy with other documents right now — yours is
                    queued and will start automatically.
                  </p>
                )}

              {/* The row says `processing` but nothing is driving it any more —
                  the server never managed to write the failure down. Without
                  this line the badge alone would suggest work still in flight. */}
              {isWorking(document) && abandonedIds.has(document.id) && (
                <p className="mt-1 text-xs text-destructive">
                  Stopped before this document finished.
                </p>
              )}

              {document.status === "failed" && document.error && (
                <p className="mt-1 text-xs text-destructive">
                  {document.error}
                </p>
              )}
            </div>

            <Badge variant={STATUS_VARIANT[document.status]}>
              {document.status}
            </Badge>

            {(document.status === "failed" ||
              abandonedIds.has(document.id)) && (
              // Resumes from the chunks already stored, so nothing that was
              // paid for gets embedded twice.
              //
              // Gated on "the server called it failed OR this session gave up"
              // rather than on the status alone. The second half is what covers
              // a document whose failure could not be recorded: one condition
              // for every cause of a stalled row, instead of a new branch each
              // time a new cause turns up.
              <Button
                variant="outline"
                size="sm"
                onClick={() => void onRetry(document.id)}
              >
                <RotateCw />
                Retry
              </Button>
            )}

            <Button
              variant="ghost"
              size="icon-sm"
              aria-label={`Delete ${document.name}`}
              disabled={deletingId === document.id}
              onClick={() => void remove(document.id)}
            >
              {deletingId === document.id ? (
                <Loader2 className="animate-spin" />
              ) : (
                <Trash2 />
              )}
            </Button>
          </li>
        ))}
      </ul>
    </div>
  );
}

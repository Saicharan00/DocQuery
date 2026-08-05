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
  /** Resume a failed document from wherever it stopped. */
  onRetry: (id: string) => void | Promise<void>;
  /** Called after a successful delete so the parent can re-fetch the list. */
  onDeleted: () => void | Promise<void>;
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

  // Pages are missing for formats without them (DOCX, TXT), so the sentence is
  // built rather than templated — a stray "page 0 of 0" reads like a bug.
  return step.pages > 0
    ? `${percent}% · page ${step.page} of ${step.pages} · ${chunks}`
    : `${percent}% · ${chunks}`;
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
        await onDeleted();
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
                  poll — the loop driving ingestion already knows where it is. */}
              {isWorking(document) && (
                <>
                  <ChunkGrid step={progress[document.id]} />
                  <p className="mt-1.5 text-xs text-muted-foreground">
                    {describeProgress(progress[document.id])}
                  </p>
                </>
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

            {document.status === "failed" && (
              // Resumes from the chunks already stored, so nothing that was
              // paid for gets embedded twice.
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

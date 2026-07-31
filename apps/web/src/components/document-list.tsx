"use client";

import { useCallback, useState } from "react";
import { FileText, Loader2, Trash2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { AuthNotReadyError, useApi } from "@/lib/api";
import type { Document, DocumentStatus } from "@/lib/types";

type DocumentListProps = {
  documents: Document[];
  /** Called after a successful delete so the parent can re-fetch the list. */
  onDeleted: () => void | Promise<void>;
};

/**
 * `pending` and `processing` are the statuses Day 6's ingestion will move a
 * document through. Today every upload lands on `pending` and stays there —
 * the badge exists now so tomorrow changes a value, not the UI.
 */
const STATUS_VARIANT: Record<
  DocumentStatus,
  "default" | "secondary" | "destructive" | "outline"
> = {
  pending: "secondary",
  processing: "secondary",
  ready: "default",
  failed: "destructive",
};

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

export function DocumentList({ documents, onDeleted }: DocumentListProps) {
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
            <FileText className="size-4 shrink-0 text-muted-foreground" />

            <div className="min-w-0 flex-1">
              {/* truncate, because a long filename would otherwise push the
                  delete button off the row. */}
              <p className="truncate font-medium">{document.name}</p>
              <p className="text-xs text-muted-foreground">
                {formatDate(document.created_at)}
                {document.file_size !== null &&
                  ` · ${formatSize(document.file_size)}`}
              </p>
            </div>

            <Badge variant={STATUS_VARIANT[document.status]}>
              {document.status}
            </Badge>

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

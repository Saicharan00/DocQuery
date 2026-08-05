"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useUser, UserButton } from "@clerk/nextjs";
import { DocumentList } from "@/components/document-list";
import { UploadZone } from "@/components/upload-zone";
import { AuthNotReadyError, useApi } from "@/lib/api";
import type { Document, IngestStep } from "@/lib/types";

/** How long to wait after a step that made no progress. Cohere's limit is per minute. */
const RATE_LIMIT_WAIT_MS = 20_000;

export default function DashboardPage() {
  const { user } = useUser();
  const api = useApi();

  const [data, setData] = useState<{ user_id: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [documents, setDocuments] = useState<Document[] | null>(null);
  const [documentsError, setDocumentsError] = useState<string | null>(null);

  useEffect(() => {
    // Guards against a response landing after the component has gone away.
    let cancelled = false;

    api<{ user_id: string }>("/me")
      .then((result) => {
        if (cancelled) return;
        setData(result);
        setError(null); // Clear anything left over from an earlier attempt.
      })
      .catch((e: Error) => {
        if (cancelled) return;
        // Clerk wasn't ready yet. The effect re-runs once it is — nothing to
        // show the user in the meantime.
        if (e instanceof AuthNotReadyError) return;
        setError(e.message);
      });

    return () => {
      cancelled = true;
    };
  }, [api]);

  // Lives outside the effect because upload and delete will both need to call
  // it to refresh the list.
  // Promise chain rather than async/await so the setState calls sit inside
  // callbacks. React's lint rejects state updates made synchronously in an
  // effect body, and an awaited call reads as synchronous to it.
  const refreshDocuments = useCallback(() => {
    return api<Document[]>("/documents")
      .then((result) => {
        setDocuments(result);
        setDocumentsError(null);
      })
      .catch((e: Error) => {
        if (e instanceof AuthNotReadyError) return;
        setDocumentsError(e.message);
      });
  }, [api]);

  useEffect(() => {
    void refreshDocuments();
  }, [refreshDocuments]);

  // ---------------------------------------------------------------------
  // Driving ingestion
  //
  // The server cannot finish a document on its own: the work outlives the
  // Clerk token it needs, and that token is what RLS checks. So the browser
  // calls the step endpoint repeatedly, and `useApi` fetches a fresh token on
  // every call — which is what keeps each request inside a token's lifetime.
  // ---------------------------------------------------------------------

  const [progress, setProgress] = useState<Record<string, IngestStep>>({});

  // Refs, not state: changing these must not trigger a re-render, or updating
  // one inside the loop would restart the effect that started the loop.
  const running = useRef<Set<string>>(new Set());
  const abandoned = useRef<Set<string>>(new Set());

  const ingest = useCallback(
    async (id: string) => {
      // A document already being driven, or one that has already failed on us
      // this session, is left alone. Without the second guard a document stuck
      // at `processing` would be retried on every refresh — a loop of billable
      // calls that nobody asked for.
      if (running.current.has(id) || abandoned.current.has(id)) return;
      running.current.add(id);

      try {
        let done = false;
        let previous = -1;

        while (!done) {
          const step = await api<IngestStep>(`/documents/${id}/ingest/step`, {
            method: "POST",
          });
          setProgress((current) => ({ ...current, [id]: step }));
          done = step.done;

          // A step that wrote nothing means the embedding provider is rate
          // limiting us. Calling straight back would just be refused again, so
          // wait it out — the limit is measured per minute.
          if (!done && step.chunks_done === previous) {
            await new Promise((resolve) => setTimeout(resolve, RATE_LIMIT_WAIT_MS));
          }
          previous = step.chunks_done;
        }
      } catch (e) {
        abandoned.current.add(id);
        if (!(e instanceof AuthNotReadyError)) {
          setDocumentsError((e as Error).message);
        }
      } finally {
        running.current.delete(id);
        // Re-fetch either way: the row now says `ready`, or `failed` with the
        // reason on it. Both are things the list should show.
        await refreshDocuments();
      }
    },
    [api, refreshDocuments],
  );

  const retry = useCallback(
    async (id: string) => {
      // Clear the "don't touch this again" mark, since the user has explicitly
      // asked. Ingestion resumes from the chunks already written rather than
      // starting over, so a retry re-embeds nothing it has already paid for.
      abandoned.current.delete(id);
      setDocumentsError(null);
      await ingest(id);
    },
    [ingest],
  );

  useEffect(() => {
    // Covers both cases with one rule: a document just uploaded arrives here as
    // `pending`, and a document left half-done by a closed tab arrives as
    // `processing`. `failed` is excluded on purpose — a retry is the user's
    // decision, because it costs money.
    documents
      ?.filter((d) => d.status === "pending" || d.status === "processing")
      .forEach((d) => void ingest(d.id));
  }, [documents, ingest]);

  return (
    <main className="p-8">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-2xl font-semibold">Dashboard</h1>
        <UserButton />
      </div>

      <p>Email: {user?.primaryEmailAddress?.emailAddress}</p>
      {error && <p className="text-red-600">Error: {error}</p>}
      {!data && !error && <p>Loading…</p>}
      {data && <p>User ID (from API): {data.user_id}</p>}

      <section className="mt-8 border-t pt-6">
        <h2 className="font-medium mb-3">Documents</h2>

        {/* Both children get `refreshDocuments`: a finished upload or delete
            re-fetches rather than editing the list locally, so what you see is
            what the server actually has. It is also the call Day 6 will poll
            to watch pending -> processing -> ready. */}
        <UploadZone onUploaded={refreshDocuments} />

        <div className="mt-6">
          {documentsError && (
            <p role="alert" className="text-sm text-destructive">
              Error: {documentsError}
            </p>
          )}
          {!documents && !documentsError && (
            <p className="text-sm text-muted-foreground">Loading documents…</p>
          )}
          {documents && (
            <DocumentList
              documents={documents}
              progress={progress}
              onRetry={retry}
              onDeleted={refreshDocuments}
            />
          )}
        </div>
      </section>
    </main>
  );
}

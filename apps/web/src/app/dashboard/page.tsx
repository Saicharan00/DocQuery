"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useUser, UserButton } from "@clerk/nextjs";
import { MessageSquare } from "lucide-react";

import { Button } from "@/components/ui/button";
import { DocumentList } from "@/components/document-list";
import { UploadZone } from "@/components/upload-zone";
import { AuthNotReadyError, ConflictError, useApi } from "@/lib/api";
import type { Document, IngestStep } from "@/lib/types";

/** How long to wait after a step that made no progress. Cohere's limit is per minute. */
const RATE_LIMIT_WAIT_MS = 20_000;

/**
 * How long to wait when the server says somebody else holds this document.
 *
 * Short, because the holder is a single in-flight step, not a whole document —
 * it releases as soon as that request returns.
 */
const CONFLICT_WAIT_MS = 2_000;

/**
 * Documents this tab has given up on. Deliberately outside the component.
 *
 * As a ref it died with the component, and that was a bug worth money: leaving
 * `/dashboard` and coming back mounted a fresh page with an empty set, so a
 * document you had explicitly abandoned lost its Retry button and was driven
 * again automatically — billable embedding calls nobody asked for. "This
 * session" always meant the tab, not the mount, so this is where it belongs.
 *
 * ponytail: still cleared by a reload, exactly as before. Surviving that would
 * mean a column in the database, and a document abandoned last week deserves
 * another chance anyway.
 */
const abandonedDocumentIds = new Set<string>();

export default function DashboardPage() {
  const { user } = useUser();
  const api = useApi();

  const [documents, setDocuments] = useState<Document[] | null>(null);
  const [documentsError, setDocumentsError] = useState<string | null>(null);

  // The `/me` call that used to live here is gone, along with the line that
  // printed its answer. It was Day 4 scaffolding: it existed to prove a browser
  // holding a Clerk token could reach FastAPI and be recognised. Every page
  // since is that same proof, so all it did now was print the Clerk id on
  // screen — an identifier worth keeping off a page that gets screenshotted and
  // screen-shared. It was not doing an auth check; `dashboard/layout.tsx`
  // redirects on the server before this component ever renders.

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

  // One controller per document being driven, so unmounting can stop them.
  // Without this the `while (!done)` loop below outlived the page: leaving
  // `/dashboard` did not stop it, and coming back started a *second* loop on
  // the same document against a fresh, empty `running` set.
  const controllers = useRef<Map<string, AbortController>>(new Map());

  // Documents deleted from under a still-running ingest loop. That loop has
  // no way to notice a delete on its own — it only checks `running`/
  // `abandoned` at the top of each call, not the document list — so its next
  // step 404s. This is what tells the catch block below that the 404 it just
  // got is expected, not a real failure worth an error banner nothing would
  // ever clear.
  const deletedIds = useRef<Set<string>>(new Set());

  // `abandonedDocumentIds` again, as state, for one reason: the list has to
  // *draw* it. Reading a plain module-level Set from the JSX would render once
  // and then never update, because nothing about mutating a Set tells React to
  // paint again.
  //
  // So the module set keeps its job (a synchronous guard the ingest loop can
  // consult and update without provoking a render, and which survives leaving
  // the page) and this keeps the other one. It is deliberately *not* in
  // `ingest`'s dependency list: putting it there would change that function's
  // identity on every update and restart the effect below, which is the hazard
  // the non-reactive set exists to avoid in the first place.
  //
  // Seeded from the module-level set rather than empty, so returning to this
  // page redraws the Retry buttons for whatever you had already given up on.
  const [abandonedIds, setAbandonedIds] = useState<Set<string>>(
    () => new Set(abandonedDocumentIds),
  );

  // Stop every running loop when the page goes away. `controllers.current` is
  // copied into a local first because by the time the cleanup runs the ref may
  // already point somewhere else — the standard React caveat about reading a
  // ref from a cleanup function.
  useEffect(() => {
    const inFlight = controllers.current;
    return () => {
      inFlight.forEach((controller) => controller.abort());
      inFlight.clear();
    };
  }, []);

  const ingest = useCallback(
    async (id: string) => {
      // A document already being driven, or one that has already failed on us
      // this session, is left alone. Without the second guard a document stuck
      // at `processing` would be retried on every refresh — a loop of billable
      // calls that nobody asked for.
      if (running.current.has(id) || abandonedDocumentIds.has(id)) return;
      running.current.add(id);

      const controller = new AbortController();
      controllers.current.set(id, controller);

      try {
        let done = false;
        let previous = -1;

        while (!done) {
          let step: IngestStep;

          try {
            step = await api<IngestStep>(`/documents/${id}/ingest/step`, {
              method: "POST",
              signal: controller.signal,
            });
          } catch (e) {
            if (!(e instanceof ConflictError)) throw e;
            // Somebody else holds this document's step — a second tab, or this
            // page's own previous mount still finishing its request. Neither is
            // a failure and neither is a reason to abandon the document: the
            // holder releases the moment its request returns, so wait and ask
            // again. Deliberately `continue` rather than counting this as a
            // step, so it cannot be mistaken for progress below.
            await new Promise((resolve) => setTimeout(resolve, CONFLICT_WAIT_MS));
            if (controller.signal.aborted) return;
            continue;
          }

          setProgress((current) => ({ ...current, [id]: step }));
          done = step.done;

          // A step that wrote nothing means the embedding provider is rate
          // limiting us. Calling straight back would just be refused again, so
          // wait it out — the limit is measured per minute.
          if (!done && step.chunks_done === previous) {
            await new Promise((resolve) => setTimeout(resolve, RATE_LIMIT_WAIT_MS));
            // The page may have gone during that 20-second wait.
            if (controller.signal.aborted) return;
          }
          previous = step.chunks_done;
        }
      } catch (e) {
        // We stopped it ourselves by leaving the page. Not a failure, and
        // nothing to draw — the component is on its way out.
        if (e instanceof DOMException && e.name === "AbortError") return;

        // Both lines are inside the guard on purpose. `AuthNotReadyError` means
        // Clerk had not minted a token yet — it is not a failure, and it fixes
        // itself: `api` changes identity the moment the token exists, which
        // re-runs the effect below and calls us again.
        //
        // Marking the document abandoned out here was the bug. That second call
        // hit the `abandoned` guard at the top of this function and returned
        // immediately, while the branch below deliberately shows nothing for
        // this error — so ingestion stopped for the whole session with an empty
        // screen and no way to tell it had. A document is written off for real
        // errors only.
        if (!(e instanceof AuthNotReadyError)) {
          if (deletedIds.current.has(id)) {
            // The document was deleted while this loop's request was still
            // in flight. The 404 that follows is simply the row being gone —
            // not something to mark abandoned (there is nothing left to
            // retry) and not something to show the user, who has no reason
            // to know this loop was even running.
            return;
          }
          abandonedDocumentIds.add(id);
          // A fresh Set, not the same one mutated: React compares by reference,
          // and handing back the identical object would change nothing on screen.
          setAbandonedIds(new Set(abandonedDocumentIds));
          setDocumentsError((e as Error).message);
        }
      } finally {
        running.current.delete(id);
        controllers.current.delete(id);
        // Re-fetch either way: the row now says `ready`, or `failed` with the
        // reason on it. Both are things the list should show.
        //
        // Except when we were aborted, which means the page is unmounting:
        // there is no list left to update, and firing a request on the way out
        // would be work with nowhere to land.
        if (!controller.signal.aborted) {
          await refreshDocuments();
        }
      }
    },
    [api, refreshDocuments],
  );

  // In plain English, the loop above: keep asking the server to do the next
  // slice of work on this file until it says there is none left, drawing the
  // progress bar from each reply.
  //
  // Three things can interrupt that. The server may say "I am already doing
  // this one for somebody else" — another tab, or this very page before you
  // navigated away — in which case we wait a couple of seconds and ask again,
  // because that is a queue, not a failure. The embedding service may say
  // "slow down", in which case we wait twenty seconds. And you may leave the
  // page, in which case we cancel outright: before this, the loop carried on
  // in the background, and returning to the dashboard started a *second* one
  // on the same file, so the two of them paid twice for the same pages and
  // then tripped over each other's writes.
  const retry = useCallback(
    async (id: string) => {
      // Clear the "don't touch this again" mark, since the user has explicitly
      // asked. Ingestion resumes from the chunks already written rather than
      // starting over, so a retry re-embeds nothing it has already paid for.
      abandonedDocumentIds.delete(id);
      setAbandonedIds(new Set(abandonedDocumentIds));
      setDocumentsError(null);
      await ingest(id);
    },
    [ingest],
  );

  const handleDeleted = useCallback(
    (id: string) => {
      // Marked before the refetch below, so it's in place by the time it
      // could possibly matter — the ingest loop's own next request, sent
      // independently, is the only other thing racing to read this set.
      deletedIds.current.add(id);
      return refreshDocuments();
    },
    [refreshDocuments],
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

        <div className="flex items-center gap-3">
          {/* The only way to reach the chat page until Day 9 builds the
              sidebar that would normally hold this. */}
          <Button asChild size="sm">
            <Link href="/dashboard/chat">
              <MessageSquare />
              Chat
            </Link>
          </Button>
          <UserButton />
        </div>
      </div>

      <p className="text-sm text-muted-foreground">
        Signed in as {user?.primaryEmailAddress?.emailAddress}
      </p>

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
              abandonedIds={abandonedIds}
              onRetry={retry}
              onDeleted={handleDeleted}
            />
          )}
        </div>
      </section>
    </main>
  );
}

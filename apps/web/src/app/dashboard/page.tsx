"use client";

import { useCallback, useEffect, useState } from "react";
import { useUser, UserButton } from "@clerk/nextjs";
import { AuthNotReadyError, useApi } from "@/lib/api";
import type { Document } from "@/lib/types";

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

      {/* Raw output on purpose: this is the instrument that proves Supabase
          accepts the Clerk token and RLS runs. Replaced by the real document
          list UI once upload and delete exist. */}
      <section className="mt-8 border-t pt-4">
        <h2 className="font-medium mb-2">Documents</h2>
        {documentsError && (
          <p className="text-red-600">Error: {documentsError}</p>
        )}
        {!documents && !documentsError && <p>Loading documents…</p>}
        {documents && (
          <>
            <p>{documents.length} document(s)</p>
            <pre className="mt-2 text-xs bg-neutral-100 dark:bg-neutral-900 p-2 rounded overflow-x-auto">
              {JSON.stringify(documents, null, 2)}
            </pre>
          </>
        )}
      </section>
    </main>
  );
}

"use client";

import { useEffect, useState } from "react";
import { useUser, UserButton } from "@clerk/nextjs";
import { AuthNotReadyError, useApi } from "@/lib/api";

export default function DashboardPage() {
  const { user } = useUser();
  const api = useApi();
  const [data, setData] = useState<{ user_id: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

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
    </main>
  );
}

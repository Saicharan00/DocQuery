"use client";

import { useEffect, useState } from "react";
import { useUser, UserButton } from "@clerk/nextjs";
import { useApi } from "@/lib/api";

export default function DashboardPage() {
  const { user } = useUser();
  const api = useApi();
  const [data, setData] = useState<{ user_id: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api("/me")
      .then(setData)
      .catch((e: Error) => setError(e.message));
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

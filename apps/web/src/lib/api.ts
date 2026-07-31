"use client";

import { useCallback } from "react";
import { useAuth } from "@clerk/nextjs";

const API_URL = process.env.NEXT_PUBLIC_API_URL;

/**
 * Raised when Clerk hasn't produced a session token yet.
 *
 * This is "not ready", not "failed" — it resolves on its own once Clerk
 * finishes loading. Callers should ignore it rather than showing it to the
 * user, otherwise every page flashes an auth error on first paint.
 */
export class AuthNotReadyError extends Error {
  constructor(message = "Authentication is still loading.") {
    super(message);
    this.name = "AuthNotReadyError";
  }
}

export function useApi() {
  const { getToken, isLoaded } = useAuth();

  // The returned function must keep a stable identity across renders. Anything
  // putting it in a useEffect dependency array would otherwise re-run on every
  // render — fetch, setState, re-render, new function, fetch again, forever.
  // Clerk memoises `getToken`, so in practice this only changes identity once,
  // when `isLoaded` flips false -> true.
  return useCallback(
    async function apiFetch<T = unknown>(
      path: string,
      init?: RequestInit,
    ): Promise<T> {
      if (!isLoaded) {
        throw new AuthNotReadyError();
      }

      const token = await getToken();
      if (!token) {
        // Never interpolate a null token into the header. `Bearer ${null}`
        // becomes the literal string "Bearer null", which the backend can only
        // report as a malformed token — indistinguishable from a real failure.
        throw new AuthNotReadyError("No active session.");
      }

      const res = await fetch(`${API_URL}${path}`, {
        ...init,
        headers: {
          // Deliberately no Content-Type default: when the body is FormData the
          // browser has to set it itself, including the multipart boundary.
          ...init?.headers,
          Authorization: `Bearer ${token}`,
        },
      });

      if (!res.ok) {
        throw new Error(await readErrorMessage(res));
      }

      // 204 No Content (our DELETE) has no body — res.json() would throw.
      if (res.status === 204 || res.headers.get("content-length") === "0") {
        return undefined as T;
      }

      return (await res.json()) as T;
    },
    [getToken, isLoaded],
  );
}

/** Pull FastAPI's `detail` out of an error response so the UI can show it. */
async function readErrorMessage(res: Response): Promise<string> {
  try {
    const body = await res.json();

    if (typeof body?.detail === "string") {
      return body.detail;
    }

    // Request-validation failures arrive as a list of objects, not a string.
    if (Array.isArray(body?.detail)) {
      const messages = body.detail
        .map((item: { msg?: string }) => item?.msg)
        .filter(Boolean);
      if (messages.length > 0) {
        return messages.join("; ");
      }
    }
  } catch {
    // Not JSON (a proxy error page, an empty body). Fall through.
  }

  return `Request failed (${res.status}).`;
}

"use client";

import { useCallback } from "react";
import { useAuth } from "@clerk/nextjs";

import type { ChatEvent, ModelName } from "@/lib/types";

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

// ---------------------------------------------------------------------------
// Streaming
//
// `useApi` above cannot serve /chat. Its last line is `await res.json()`, which
// means "wait until the whole response has arrived, then parse it" — and the
// whole point of /chat is that the answer is readable before it is finished.
// So this shares the token handling and the error parsing, and differs only in
// what it does with the body.
// ---------------------------------------------------------------------------

/** What `/chat` accepts. Mirrors `ChatRequest` in apps/api/app/models/chat.py. */
type ChatBody = {
  message: string;
  model: ModelName;
  conversation_id: string | null;
};

export function useChatStream() {
  const { getToken, isLoaded } = useAuth();

  return useCallback(
    async function* streamChat(
      body: ChatBody,
      signal: AbortSignal,
    ): AsyncGenerator<ChatEvent> {
      if (!isLoaded) {
        throw new AuthNotReadyError();
      }

      const token = await getToken();
      if (!token) {
        throw new AuthNotReadyError("No active session.");
      }

      const res = await fetch(`${API_URL}/chat`, {
        method: "POST",
        // Lets the caller cancel: closing the page, or hitting "New chat"
        // mid-answer, aborts the request instead of leaving it running.
        signal,
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify(body),
      });

      // Everything the endpoint can refuse cleanly, it refuses *before* the
      // stream opens — 429 daily cap, 400 no documents, 404 unknown
      // conversation, 503 model unconfigured. Those are ordinary responses with
      // a readable `detail`, so the parser written for `useApi` handles them.
      // Once the stream is open, a failure can only arrive as an `error` event.
      if (!res.ok) {
        throw new Error(await readErrorMessage(res));
      }
      if (!res.body) {
        throw new Error("The server sent no response body.");
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      // The server always signs off with `done` or `error` — every path through
      // `generate()` in chat.py does. So its absence is not a detail: it means
      // the connection died mid-answer.
      let signedOff = false;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        // `{ stream: true }` is not optional here. A multi-byte character — é,
        // an em dash, an emoji — can be split across two network chunks, and
        // without this the decoder would turn each half into a "�".
        buffer += decoder.decode(value, { stream: true });

        let split: number;
        while ((split = buffer.indexOf("\n\n")) !== -1) {
          const block = buffer.slice(0, split);
          buffer = buffer.slice(split + 2);

          const event = parseEvent(block);
          if (!event) continue;

          if (event.event === "done" || event.event === "error") {
            signedOff = true;
          }
          yield event;
        }
      }

      // `reader.read()` reports `done: true` both for a body that ended
      // properly and for a socket that dropped — they are indistinguishable at
      // this level. Without this check a half-answer would sit on screen
      // looking finished, which is worse than an error: you would trust it.
      //
      // `signal.aborted` excludes the case where *we* stopped it — New chat, or
      // leaving the page. That is not a failure and must not raise one.
      if (!signedOff && !signal.aborted) {
        throw new Error(
          "The connection dropped before the answer finished. Please try again.",
        );
      }

      // In plain English, the two loops above. The outer one takes whatever
      // bytes have arrived and adds them to `buffer`. The network decides where
      // to cut those chunks and has no idea where our events end, so one read
      // might bring half an event, or three and a half.
      //
      // That is what the inner loop is for. SSE ends every event with a blank
      // line, so as long as `buffer` still contains a "\n\n", everything before
      // it is one complete event: cut it off, hand it to the caller with
      // `yield`, and look again. Whatever is left over stays in `buffer` and
      // waits for the next read to complete it.
    },
    [getToken, isLoaded],
  );
}

/**
 * One SSE block — `event: token\ndata: {"text":"hi"}` — as a typed object.
 *
 * Returns null for anything that isn't a complete event: keep-alive comments,
 * stray blank lines. Malformed JSON is *not* ignored — that would mean dropping
 * part of an answer and letting the rest look complete, so it throws and the
 * page reports a broken stream.
 */
function parseEvent(block: string): ChatEvent | null {
  let name = "";
  let data = "";

  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) name = line.slice(6).trim();
    else if (line.startsWith("data:")) data = line.slice(5).trim();
  }

  if (!name || !data) return null;

  return { event: name, data: JSON.parse(data) } as ChatEvent;
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

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

/**
 * Raised when the session is genuinely over — not merely stale.
 *
 * The opposite end of `AuthNotReadyError`: that one resolves itself and must be
 * hidden, this one never resolves itself and must be shown. Thrown only after a
 * forced token refresh has already been tried and refused, so by the time a
 * caller sees it there is nothing left to do automatically.
 */
export class SessionExpiredError extends Error {
  constructor(
    message = "Your session has expired. Refresh the page to sign in again.",
  ) {
    super(message);
    this.name = "SessionExpiredError";
  }
}

/**
 * `fetch` with the bearer token attached, and one forced retry on a 401.
 *
 * Clerk hands out a cached token and only refreshes it near expiry, so a 401 is
 * usually a token that went stale in this tab rather than a session that ended
 * — a laptop reopened after lunch reaches this line every time. Asking Clerk
 * again with `skipCache` costs one round trip and fixes that case with nothing
 * on screen. A second 401 means the session really is gone.
 *
 * Shared because `useAuthedFetch` and `useChatStream` differ only in what they
 * do with the body. Written once in each, the 401 branch was missing from both
 * — and putting it here means the retry covers JSON, images and the stream
 * rather than whichever one someone remembered.
 */
type TokenGetter = (options?: { skipCache?: boolean }) => Promise<string | null>;

async function fetchWithToken(
  getToken: TokenGetter,
  url: string,
  init: RequestInit,
): Promise<Response> {
  const send = (token: string) =>
    fetch(url, {
      ...init,
      headers: { ...init.headers, Authorization: `Bearer ${token}` },
    });

  const token = await getToken();
  if (!token) {
    // Never interpolate a null token into the header. `Bearer ${null}` becomes
    // the literal string "Bearer null", which the backend can only report as a
    // malformed token — indistinguishable from a real failure.
    throw new AuthNotReadyError("No active session.");
  }

  const res = await send(token);
  if (res.status !== 401) {
    return res;
  }

  const fresh = await getToken({ skipCache: true });
  if (!fresh) {
    throw new SessionExpiredError();
  }

  const retried = await send(fresh);
  if (retried.status === 401) {
    throw new SessionExpiredError();
  }

  return retried;
}

// In plain English: attach the token and send the request. Anything other than
// a 401 comes straight back, untouched — this function has no opinion about
// ordinary errors. A 401 is the one status it acts on: ask Clerk for a brand
// new token, skipping the cached one that just failed, and send the identical
// request a second time. If that is refused too, stop retrying and raise the
// error the user can actually act on.

/**
 * One authenticated request, handed back as the raw `Response`.
 *
 * Split out of `useApi` because not everything this app fetches is JSON. A
 * cited figure comes back as `image/jpeg`, and `useApi` ends in `res.json()`,
 * which would choke on it. Everything up to that last line is identical, so it
 * lives here and `useApi` is now a thin JSON wrapper over it.
 *
 * Errors are still raised here rather than left to the caller: a failed request
 * must not reach two different call sites and get two different treatments.
 */
export function useAuthedFetch() {
  const { getToken, isLoaded } = useAuth();

  // The returned function must keep a stable identity across renders. Anything
  // putting it in a useEffect dependency array would otherwise re-run on every
  // render — fetch, setState, re-render, new function, fetch again, forever.
  // Clerk memoises `getToken`, so in practice this only changes identity once,
  // when `isLoaded` flips false -> true.
  return useCallback(
    async function authedFetch(
      path: string,
      init?: RequestInit,
    ): Promise<Response> {
      if (!isLoaded) {
        throw new AuthNotReadyError();
      }

      // Deliberately no Content-Type default: when the body is FormData the
      // browser has to set it itself, including the multipart boundary.
      const res = await fetchWithToken(getToken, `${API_URL}${path}`, init ?? {});

      if (!res.ok) {
        throw new Error(await readErrorMessage(res));
      }

      return res;
    },
    [getToken, isLoaded],
  );
}

export function useApi() {
  const authedFetch = useAuthedFetch();

  return useCallback(
    async function apiFetch<T = unknown>(
      path: string,
      init?: RequestInit,
    ): Promise<T> {
      const res = await authedFetch(path, init);

      // 204 No Content (our DELETE, and both feedback endpoints) has no body —
      // res.json() would throw.
      if (res.status === 204 || res.headers.get("content-length") === "0") {
        return undefined as T;
      }

      return (await res.json()) as T;
    },
    [authedFetch],
  );
}

// In plain English: the first function does everything every request needs —
// wait for the login to be ready, attach the token, send it, and raise a
// readable error if the server refused. The second one adds the single step
// that only makes sense for JSON: read the body and parse it. Anything wanting
// something other than JSON — a picture — calls the first one directly and
// reads the body its own way.

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

      const res = await fetchWithToken(getToken, `${API_URL}/chat`, {
        method: "POST",
        // Lets the caller cancel: closing the page, or hitting "New chat"
        // mid-answer, aborts the request instead of leaving it running.
        signal,
        headers: { "Content-Type": "application/json" },
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

      // Losing the network mid-answer does not close the socket. Nothing is left
      // to send the close notification, so `reader.read()` never settles — not
      // resolved, not rejected, just pending — and the page would sit on
      // "Thinking…" for as long as the OS takes to give up on the connection.
      // Silence is the only symptom available, so silence is what we watch: a
      // live stream is never quiet this long, because the server sends tokens
      // continuously. Deliberately generous — a slow model must not trip it.
      const IDLE_MS = 20_000;

      /** One read, but never an unbounded wait. */
      const readOrStall = async () => {
        let idle: ReturnType<typeof setTimeout> | undefined;
        try {
          return await Promise.race([
            reader.read(),
            new Promise<never>((_, reject) => {
              idle = setTimeout(
                () =>
                  reject(
                    new Error(
                      "The connection stalled before the answer finished. Please try again.",
                    ),
                  ),
                IDLE_MS,
              );
            }),
          ]);
        } finally {
          // Whichever side won, the timer must go. Left running it would fire
          // later and reject a promise nobody is waiting on any more.
          clearTimeout(idle);
        }
      };

      // In plain English: start a read and a 20-second alarm at the same time,
      // and take whichever finishes first. Bytes arriving cancel the alarm and
      // the next read sets a fresh one, so the countdown restarts on every
      // chunk. Only an actual 20-second gap lets the alarm win, and when it does
      // it throws — which is what turns an invisible dead connection into a
      // visible error message.

      while (true) {
        const { done, value } = await readOrStall();
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

  try {
    return { event: name, data: JSON.parse(data) } as ChatEvent;
  } catch {
    // Still a throw — skipping the block would drop part of an answer and let
    // the rest look complete, which is the one outcome worse than an error.
    // Only the wording changes. `JSON.parse` raises "Unexpected token < in JSON
    // at position 0", and that `<` is the first character of `<!DOCTYPE html>`:
    // a proxy or platform error page arriving where an event should be. The
    // user can act on "try again"; they can do nothing with a parser's opinion
    // about angle brackets.
    throw new Error("The answer stream was corrupted. Please try again.");
  }
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

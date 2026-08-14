"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { ArrowLeft, Send, ThumbsDown, ThumbsUp } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  AUTH_WAIT_MS,
  AuthNotReadyError,
  useApi,
  useAuthedFetch,
  useChatStream,
} from "@/lib/api";
import {
  MODELS,
  type ChatMessage,
  type FeedbackBody,
  type MessageRow,
  type ModelName,
  type Source,
} from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * One cited figure, shown rather than named.
 *
 * The picture is not on a URL the browser can simply point at. It lives in a
 * private Storage bucket, and the only thing allowed to read it is a request
 * carrying this reader's Clerk token — which an `<img src>` cannot send, since
 * the browser issues that request itself with no headers of our choosing.
 *
 * So the bytes are fetched the ordinary way, with the token attached, and then
 * handed to `<img>` as an object URL: a short local name standing for a blob
 * already in memory. Nothing is re-requested when it renders, and the name is
 * meaningless outside this tab.
 */
function ChunkImage({ source }: { source: Source }) {
  const authedFetch = useAuthedFetch();

  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  const { chunk_id: chunkId, document_id: documentId } = source;

  useEffect(() => {
    if (!chunkId) return;

    let objectUrl: string | null = null;
    let cancelled = false;

    authedFetch(`/documents/${documentId}/images/${chunkId}`)
      .then((res) => res.blob())
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch((e: Error) => {
        if (cancelled) return;
        // Clerk hasn't minted a token yet. `authedFetch` changes identity the
        // moment it has, which re-runs this effect, so the right move is to
        // keep showing the placeholder and say nothing.
        if (e instanceof AuthNotReadyError) return;
        setFailed(true);
      });

    return () => {
      cancelled = true;
      // An object URL pins its blob in memory until it is revoked. Page images
      // run to hundreds of kilobytes each and a long conversation cites many,
      // so leaving them would grow the tab's memory for as long as it is open.
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [authedFetch, chunkId, documentId]);

  // In plain English: when this figure appears on screen, ask the server for
  // its picture with the login token attached, and turn what comes back into a
  // temporary local address the <img> tag can use. `cancelled` covers scrolling
  // away or switching conversation before it arrives — the cleanup runs first,
  // so the reply finds `cancelled` already true and quietly stops. Whatever was
  // created gets thrown away on the way out, so the memory is handed back.

  // An answer saved before this feature has no chunk id in its stored sources,
  // so there is nothing to fetch. It keeps its citation and shows no picture.
  if (!chunkId) return null;

  return (
    <figure className="mt-2">
      {failed ? (
        <p className="rounded-md border border-dashed px-3 py-4 text-center text-xs text-muted-foreground">
          This figure couldn&apos;t be loaded.
        </p>
      ) : url ? (
        // A plain <img>, not next/image: that component fetches the URL from
        // its own optimiser, which cannot carry an Authorization header — and
        // there is nothing for it to optimise, because this is already a blob
        // sitting in memory.
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={url}
          alt={source.content_preview || `Figure from ${source.document_name}`}
          className="max-h-96 w-full rounded-md border bg-background object-contain"
        />
      ) : (
        <div className="h-32 animate-pulse rounded-md border bg-muted-foreground/10" />
      )}

      <figcaption className="mt-1 text-xs text-muted-foreground">
        [{source.number}] {source.document_name}
        {source.page_number !== null && ` · page ${source.page_number}`}
      </figcaption>
    </figure>
  );
}

/**
 * The citations behind one answer.
 *
 * A native <details> rather than a useState toggle: the browser already knows
 * how to open, close, and keyboard-focus this, and it stays usable if the
 * JavaScript on the page ever fails to load.
 */
function Sources({ sources }: { sources: Source[] }) {
  return (
    <details className="mt-2 text-xs">
      <summary className="cursor-pointer text-muted-foreground hover:text-foreground">
        Sources ({sources.length})
      </summary>

      <ul className="mt-2 space-y-2 border-l pl-3">
        {sources.map((source) => (
          <li key={source.number}>
            <p className="font-medium">
              [{source.number}] {source.document_name}
              <span className="font-normal text-muted-foreground">
                {source.page_number !== null && ` · page ${source.page_number}`}
                {` · chunk ${source.chunk_index}`}
              </span>
            </p>

            {/* An image chunk has no readable text to preview — what the model
                was shown is a picture, and it is rendered above the citation
                list by <ChunkImage>. This entry just marks which of the
                numbered sources that picture was. */}
            {source.chunk_type === "image" ? (
              <Badge variant="secondary" className="mt-1">
                image
              </Badge>
            ) : (
              <p className="mt-0.5 text-muted-foreground">
                {source.content_preview}…
              </p>
            )}
          </li>
        ))}
      </ul>
    </details>
  );
}

/**
 * Was that answer any good?
 *
 * The thumb is sent the moment it is clicked, before any comment box appears.
 * That ordering is deliberate: almost nobody writes a sentence, and asking for
 * one first would trade the signal most people will give for the one most people
 * won't.
 *
 * The comment is then a second, separate submission carrying no score, because
 * repeating the score would count one reader's opinion twice in the average.
 * Both land on the LangSmith trace of this exact answer, so a complaint arrives
 * attached to the retrieval and the prompt that caused it.
 */
function AnswerFeedback({ runId }: { runId: string }) {
  const api = useApi();

  const [score, setScore] = useState<0 | 1 | null>(null);
  const [comment, setComment] = useState("");
  const [commentSent, setCommentSent] = useState(false);
  const [failed, setFailed] = useState(false);

  const post = useCallback(
    async (body: FeedbackBody) => {
      try {
        await api("/feedback", {
          method: "POST",
          // `useApi` sets no Content-Type by default, because an upload needs
          // the browser to set its own multipart boundary. JSON has to say so.
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        setFailed(false);
        return true;
      } catch {
        // No message from the server is shown: the reader clicked a thumb, and
        // the useful thing to tell them is that it didn't stick.
        setFailed(true);
        return false;
      }
    },
    [api],
  );

  const rate = useCallback(
    (next: 0 | 1) => {
      // One vote per answer. The buttons disappear after the first, so this only
      // catches a double-click landing before the re-render.
      if (score !== null) return;
      setScore(next);
      void post({ run_id: runId, score: next }).then((ok) => {
        // Put the buttons back rather than leaving a rating on screen that was
        // never recorded.
        if (!ok) setScore(null);
      });
    },
    [post, runId, score],
  );

  const sendComment = useCallback(() => {
    const text = comment.trim();
    if (!text || commentSent) return;
    void post({ run_id: runId, comment: text }).then((ok) => {
      if (ok) setCommentSent(true);
    });
  }, [comment, commentSent, post, runId]);

  return (
    <div className="mt-2 border-t pt-2 text-xs">
      {score === null ? (
        <div className="flex items-center gap-1 text-muted-foreground">
          <span>Was this helpful?</span>
          <Button
            variant="ghost"
            size="icon"
            className="size-6"
            aria-label="This answer was helpful"
            onClick={() => rate(1)}
          >
            <ThumbsUp className="size-3.5" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="size-6"
            aria-label="This answer was not helpful"
            onClick={() => rate(0)}
          >
            <ThumbsDown className="size-3.5" />
          </Button>
        </div>
      ) : (
        <div className="space-y-1.5">
          <p className="text-muted-foreground">
            {score === 1 ? "Glad that helped." : "Sorry that missed."}{" "}
            {commentSent
              ? "Thanks — the detail is more useful than the thumb."
              : "Anything you'd add? (optional)"}
          </p>

          {!commentSent && (
            <div className="flex items-end gap-2">
              <textarea
                value={comment}
                onChange={(e) => setComment(e.target.value)}
                onKeyDown={(e) => {
                  if (
                    e.key === "Enter" &&
                    !e.shiftKey &&
                    !e.nativeEvent.isComposing
                  ) {
                    e.preventDefault();
                    sendComment();
                  }
                }}
                rows={2}
                // Matches `FeedbackRequest.comment`'s ceiling, so the server
                // never has to reject something the box let you type.
                maxLength={1000}
                placeholder="What was wrong, or what you expected instead"
                className="max-h-32 flex-1 resize-none rounded-md border bg-background px-2 py-1 text-xs field-sizing-content focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none"
              />
              <Button
                size="sm"
                variant="secondary"
                disabled={comment.trim() === ""}
                onClick={sendComment}
              >
                Send
              </Button>
            </div>
          )}
        </div>
      )}

      {failed && (
        <p role="alert" className="mt-1 text-destructive">
          Couldn&apos;t record that. Please try again.
        </p>
      )}
    </div>
  );
}

// In plain English: this shows a thumbs-up and a thumbs-down under an answer.
// Clicking one sends it straight away and swaps the buttons for a short thank-you
// and an optional box for saying more. Sending that box is a second, separate
// message. If either send fails, the thumbs come back so nothing is claimed that
// wasn't actually saved.

/**
 * The chat, for one conversation or for a conversation that doesn't exist yet.
 *
 * Both routes under `/dashboard/chat` render this: `page.tsx` with null, and
 * `[id]/page.tsx` with the id from the URL. It lives here rather than in either
 * page file because the alternative is maintaining this screen twice, and the
 * two entry points differ by exactly one value.
 *
 * `conversationId` is the *starting* id, not the current one. A brand-new chat
 * begins with null and learns its id from the opening SSE event, which is why
 * the prop seeds state instead of being used directly.
 */
export function ChatView({
  conversationId: initialConversationId,
}: {
  conversationId: string | null;
}) {
  const streamChat = useChatStream();
  const api = useApi();
  const pathname = usePathname();

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [model, setModel] = useState<ModelName>(MODELS[0].id);
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [conversationId, setConversationId] = useState(initialConversationId);

  // Starts true only when there is something to load. A new chat has no history
  // and must not flash "Loading…" before its empty state.
  const [isLoadingHistory, setIsLoadingHistory] = useState(
    initialConversationId !== null,
  );

  // A ref, not state: cancelling is a side effect, and nothing on screen
  // depends on which request is in flight.
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [messages]);

  // Leaving the page mid-answer aborts the request rather than leaving it to
  // stream into a component that no longer exists.
  useEffect(() => () => abortRef.current?.abort(), []);

  // Everything said in this conversation before the page was opened. This is
  // the point of Day 9a: without it a reload shows an empty screen, because the
  // messages only ever existed in the React state of a page that has gone.
  useEffect(() => {
    if (!initialConversationId) return;

    let cancelled = false;

    // The ceiling on the quiet wait in the catch below. Cleared by whichever
    // outcome arrives first, so it only ever fires when nothing arrives at all.
    const giveUp = setTimeout(() => {
      if (cancelled) return;
      setIsLoadingHistory(false);
      setError("Could not load this conversation. Refresh the page to retry.");
    }, AUTH_WAIT_MS);

    api<MessageRow[]>(`/conversations/${initialConversationId}/messages`)
      .then((rows) => {
        if (cancelled) return;
        clearTimeout(giveUp);
        setMessages(
          rows.map((row) => ({
            role: row.role,
            content: row.content,
            // `sources` is null on user rows and on any answer saved before
            // citations existed. `undefined` is what `ChatMessage` uses for
            // "none", and it is what stops <Sources> rendering an empty box.
            sources: row.sources ?? undefined,
            // Stored since migration 006, which is what lets an answer you have
            // come back to still be rated. Null on user rows and on anything
            // answered before that, and null means no buttons.
            runId: row.run_id,
          })),
        );
        setError(null);
        setIsLoadingHistory(false);
      })
      .catch((e: Error) => {
        if (cancelled) return;
        // Clerk hasn't produced a token yet. `api` changes identity the moment
        // it has, which re-runs this effect — so the right move is to keep
        // showing "Loading…" and say nothing.
        //
        // The timer is deliberately *not* cleared on this path: this is the one
        // outcome that resolves itself, and it is exactly the case the timer is
        // there to bound. If the token never arrives, no further call reaches
        // this component and the timer is the only thing left to speak up.
        if (e instanceof AuthNotReadyError) return;
        clearTimeout(giveUp);
        setError(e.message);
        setIsLoadingHistory(false);
      });

    return () => {
      cancelled = true;
      // Covers unmount and a re-run of this effect. Without it a load that
      // succeeded in a second would still be overwritten by an error nine
      // seconds later.
      clearTimeout(giveUp);
    };
  }, [api, initialConversationId]);

  // In plain English: when the page opens on an existing conversation, ask the
  // API for its messages and turn each stored row into a bubble. `cancelled`
  // guards the case where you click away before the answer arrives — without
  // it, the response would try to fill a component that no longer exists.

  /** Rewrite the newest message — always the assistant bubble being filled. */
  const updateLast = useCallback(
    (change: (message: ChatMessage) => ChatMessage) => {
      setMessages((current) =>
        current.map((message, index) =>
          index === current.length - 1 ? change(message) : message,
        ),
      );
    },
    [],
  );

  const send = useCallback(async () => {
    const question = input.trim();
    // An empty question would still cost a Cohere embedding call, and a second
    // send while one is running would interleave two answers into one bubble.
    if (!question || isStreaming) return;

    const controller = new AbortController();
    abortRef.current = controller;

    // Captured now, because by the time the stream finishes `conversationId`
    // will have been set and the closure would no longer be able to tell
    // whether this send is what created the conversation.
    const isFirstMessage = conversationId === null;
    let createdId: string | null = null;

    setInput("");
    setError(null);
    setIsStreaming(true);
    // The empty assistant bubble is created up front: it is where "Thinking…"
    // shows, and where every token appends. Creating it now means the token
    // handler never has to decide whether to add a bubble or extend one.
    setMessages((current) => [
      ...current,
      { role: "user", content: question },
      { role: "assistant", content: "" },
    ]);

    try {
      for await (const event of streamChat(
        { message: question, model, conversation_id: conversationId },
        controller.signal,
      )) {
        switch (event.event) {
          case "conversation":
            createdId = event.data.id;
            setConversationId(event.data.id);
            // Arrives before the first token, and belongs to the bubble being
            // filled — which is the last one, because `send` pushed it above.
            // Null when the server has tracing off; `AnswerFeedback` is then
            // never rendered, so no button can promise something unrecordable.
            updateLast((message) => ({ ...message, runId: event.data.run_id }));
            break;
          case "sources":
            updateLast((message) => ({ ...message, sources: event.data.sources }));
            break;
          case "token":
            updateLast((message) => ({
              ...message,
              content: message.content + event.data.text,
            }));
            break;
          case "title":
            // Nothing to do here, and that is not an oversight. The title is
            // already in the database by the time this arrives, and the sidebar
            // reads it from there when the URL below changes. The event is kept
            // because it is the only way the page could show a title without a
            // second request, should it ever want to.
            break;
          case "error":
            // The answer broke after streaming began. The response has been a
            // "200 OK" for several seconds by now, so this is the only way the
            // server can report it.
            setError(event.data.detail);
            break;
          case "done":
            if (isFirstMessage && createdId) {
              // The address bar starts saying `/dashboard/chat/<id>`, so a
              // reload or a bookmark now reaches this exact conversation.
              //
              // `window.history.replaceState`, not `router.replace`. Both change
              // the URL and both are seen by `usePathname` — Next.js documents
              // the native History API as integrating with its router — but
              // `router.replace` is a real navigation: it would unmount this
              // component, mount `[id]/page.tsx` in its place, and refetch from
              // the database the answer you are already reading. This changes
              // the URL and leaves the page alone.
              window.history.replaceState(null, "", `/dashboard/chat/${createdId}`);
            }
            break;
        }
      }
    } catch (e) {
      // Three different situations, and only one of them is alarming.
      if (e instanceof DOMException && e.name === "AbortError") {
        // We cancelled it — New chat, or the page closing. Not an error.
      } else if (e instanceof AuthNotReadyError) {
        setError("Still signing you in. Try again in a moment.");
      } else if (e instanceof TypeError) {
        // The one error whose text is the browser's, not ours: `fetch` rejects
        // with a TypeError when the network fails, and "Failed to fetch" tells
        // a user nothing they can act on.
        setError("Can't reach the server. Check your connection and try again.");
      } else {
        // Everything else carries a message written for a human — either the
        // server's `detail`, or the dropped-connection message from streamChat.
        setError((e as Error).message);
      }
    } finally {
      setIsStreaming(false);
      abortRef.current = null;
      // A send that failed before its first token would otherwise leave a blank
      // bubble on screen for good.
      setMessages((current) => {
        const last = current[current.length - 1];
        return last?.role === "assistant" && last.content === ""
          ? current.slice(0, -1)
          : current;
      });
    }
  }, [conversationId, input, isStreaming, model, streamChat, updateLast]);

  // "New chat" lives in the sidebar and is an ordinary link to /dashboard/chat.
  // Usually that link unmounts this component and mounts a fresh one, and there
  // is nothing to do here. One case escapes it: after the first answer the URL
  // was rewritten to /dashboard/chat/<id> *without* a navigation, so this
  // component is still the one `page.tsx` mounted. Following the link then
  // returns Next.js to a page it believes is already rendered, and the messages
  // would stay on screen under a URL that says "new chat".
  const previousPath = useRef(pathname);

  useEffect(() => {
    const changed = previousPath.current !== pathname;
    previousPath.current = pathname;

    if (changed && pathname === "/dashboard/chat") {
      // Without the abort, a stream still running would keep appending into the
      // list we just cleared.
      abortRef.current?.abort();
      setMessages([]);
      setConversationId(null);
      setError(null);
      setInput("");
      setIsLoadingHistory(false);
    }
  }, [pathname]);

  // The `changed` check is the load-bearing part, not decoration. Reacting to
  // the path *being* /dashboard/chat would wipe the conversation mid-answer:
  // the first reply arrives while the address bar still says /dashboard/chat,
  // and this effect would fire and clear the answer being streamed. Only an
  // actual transition into that path means "start a new chat".

  return (
    // No `flex-1` here any more. With one, the chat and the feedback box beside
    // it would each grab half the free width and the chat would sit far left of
    // centre. Without it the chat takes its natural width — 48rem, or the whole
    // space on a narrower screen — and the box gets exactly what is left over.
    <main className="mx-auto flex w-full max-w-3xl flex-col p-4">
      <header className="flex items-center gap-3 border-b pb-3">
        <Button variant="ghost" size="sm" asChild>
          <Link href="/dashboard">
            <ArrowLeft />
            Documents
          </Link>
        </Button>

        <div className="ml-auto flex items-center gap-2">
          {/* A native select. Two options don't justify a scripted dropdown,
              and this one is keyboard- and screen-reader-correct for free.
              Disabled mid-answer: the model is chosen per request, so changing
              it while one is streaming would be a promise we can't keep. */}
          <select
            value={model}
            onChange={(e) => setModel(e.target.value as ModelName)}
            disabled={isStreaming}
            aria-label="Model"
            className="rounded-md border bg-background px-2 py-1 text-sm disabled:opacity-50"
          >
            {MODELS.map((option) => (
              <option key={option.id} value={option.id}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
      </header>

      <div className="flex-1 space-y-4 overflow-y-auto py-6">
        {isLoadingHistory && (
          <p className="mt-12 text-center text-sm text-muted-foreground">
            Loading conversation…
          </p>
        )}

        {!isLoadingHistory && messages.length === 0 && (
          <p className="mt-12 text-center text-sm text-muted-foreground">
            Ask a question about your documents.
          </p>
        )}

        {messages.map((message, index) => (
          <div
            key={index}
            className={cn(
              "flex",
              message.role === "user" ? "justify-end" : "justify-start",
            )}
          >
            <div
              className={cn(
                "max-w-[85%] rounded-lg px-3 py-2 text-sm",
                message.role === "user"
                  ? "bg-primary text-primary-foreground"
                  : "bg-muted",
              )}
            >
              {/* whitespace-pre-wrap keeps the model's paragraph breaks. It
                  arrives as "\n" inside the text, which HTML would otherwise
                  collapse into a single space. */}
              <p className="whitespace-pre-wrap">
                {message.content || (
                  <span className="animate-pulse text-muted-foreground">
                    Thinking…
                  </span>
                )}
              </p>

              {/* Every cited figure, shown under the answer that used it.
                  Filtered on `chunk_id` as well as the type so an answer from
                  before that field existed renders nothing rather than a row
                  of broken placeholders. */}
              {message.sources
                ?.filter(
                  (source) => source.chunk_type === "image" && source.chunk_id,
                )
                .map((source) => (
                  <ChunkImage key={source.chunk_id} source={source} />
                ))}

              {message.sources && message.sources.length > 0 && (
                <Sources sources={message.sources} />
              )}

              {/* `runId` is absent on user bubbles, and null on answers written
                  before migration 006 or produced while the server had tracing
                  off — all of which correctly show no buttons. Everything else
                  is ratable, including answers replayed from history. The index
                  check keeps the buttons off the answer still being written,
                  without hiding them on the ones above it. */}
              {message.role === "assistant" &&
                message.runId &&
                (index !== messages.length - 1 || !isStreaming) && (
                  <AnswerFeedback runId={message.runId} />
                )}
            </div>
          </div>
        ))}

        {/* An empty bubble means the answer hasn't started, so "Thinking…"
            shows in its place — no separate loading flag to keep in sync. */}

        <div ref={bottomRef} />
      </div>

      {error && (
        <p role="alert" className="pb-2 text-sm text-destructive">
          {error}
        </p>
      )}

      <div className="flex items-end gap-2 border-t pt-3">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            // isComposing is true while an input method (for Japanese, Chinese,
            // Korean and others) is mid-character — there Enter commits the
            // character, and sending on it would fire off half a question.
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void send();
            }
          }}
          rows={2}
          maxLength={2000}
          placeholder="Ask a question…  (Enter to send, Shift+Enter for a new line)"
          className="max-h-40 flex-1 resize-none rounded-md border bg-background px-3 py-2 text-sm field-sizing-content focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none"
        />

        <Button
          size="icon"
          aria-label="Send"
          disabled={isStreaming || input.trim() === ""}
          onClick={() => void send()}
        >
          <Send />
        </Button>
      </div>
    </main>
  );
}

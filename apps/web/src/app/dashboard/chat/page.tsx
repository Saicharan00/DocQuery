"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { ArrowLeft, Send, SquarePen } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { AuthNotReadyError, useChatStream } from "@/lib/api";
import { MODELS, type ChatMessage, type ModelName, type Source } from "@/lib/types";
import { cn } from "@/lib/utils";

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
                was shown is a picture. Displaying it here would need a signed
                Storage URL the browser doesn't have, so it is named, not shown. */}
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

export default function ChatPage() {
  const streamChat = useChatStream();

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [model, setModel] = useState<ModelName>(MODELS[0].id);
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Null until the first answer comes back. The server mints the id and reports
  // it in the opening SSE event; sending it back on the next question is what
  // keeps the exchange in one conversation row.
  const [conversationId, setConversationId] = useState<string | null>(null);

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
            setConversationId(event.data.id);
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
          case "error":
            // The answer broke after streaming began. The response has been a
            // "200 OK" for several seconds by now, so this is the only way the
            // server can report it.
            setError(event.data.detail);
            break;
          case "done":
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

  const newChat = useCallback(() => {
    // Order matters only in that the abort must happen: without it a stream
    // from the old conversation would keep appending into the cleared list.
    // The cleanup in `finally` above is safe against this — it looks at the
    // list as it is by then, which is empty.
    abortRef.current?.abort();
    setMessages([]);
    setConversationId(null);
    setError(null);
    setInput("");
  }, []);

  return (
    <main className="mx-auto flex w-full max-w-3xl flex-1 flex-col p-4">
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

          <Button variant="outline" size="sm" onClick={newChat}>
            <SquarePen />
            New chat
          </Button>
        </div>
      </header>

      <div className="flex-1 space-y-4 overflow-y-auto py-6">
        {messages.length === 0 && (
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

              {message.sources && message.sources.length > 0 && (
                <Sources sources={message.sources} />
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

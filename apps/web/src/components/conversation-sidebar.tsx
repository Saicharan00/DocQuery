"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Pencil, Plus, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { AuthNotReadyError, useApi } from "@/lib/api";
import type { Conversation } from "@/lib/types";
import { cn } from "@/lib/utils";

const CHAT_PATH = "/dashboard/chat";

// `numeric: "auto"` is what turns "1 day ago" into "yesterday". Built into the
// browser — a date library would be a dependency for one label.
const relative = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });

// Largest first, because the loop below takes the first unit that fits.
const UNITS = [
  ["year", 365 * 24 * 60 * 60],
  ["month", 30 * 24 * 60 * 60],
  ["day", 24 * 60 * 60],
  ["hour", 60 * 60],
  ["minute", 60],
] as const;

function timeAgo(iso: string): string {
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;

  for (const [unit, size] of UNITS) {
    if (seconds >= size) {
      return relative.format(-Math.floor(seconds / size), unit);
    }
  }

  return relative.format(0, "second");
}

// In plain English: work out how many seconds ago the timestamp was, then walk
// the units from largest to smallest and use the first one it reaches a whole
// number of — 7000 seconds skips "year" and "month", lands on "hour", and
// becomes "1 hour ago". The count is negated because the formatter reads a
// negative number as the past. Anything under a minute falls out of the loop
// and formats as "now".

/**
 * Every conversation you have had, most recent first.
 *
 * Lives in the chat layout rather than a page, so it stays mounted while you
 * click between conversations — a sidebar that remounted on every click would
 * refetch and flicker each time.
 *
 * It refetches when the URL path changes, and that is the whole mechanism by
 * which a brand-new conversation appears here. `ChatView` rewrites the address
 * to `/dashboard/chat/<id>` once the first answer is finished; the path change
 * re-runs the effect below, and by then the server has already written the
 * generated title, because `_retitle` runs before the `done` event.
 */
export function ConversationSidebar() {
  const api = useApi();
  const router = useRouter();
  const pathname = usePathname();

  // Null means "not loaded yet", which is different from the empty array —
  // otherwise the first paint claims you have no conversations.
  const [conversations, setConversations] = useState<Conversation[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const activeId = pathname.startsWith(`${CHAT_PATH}/`)
    ? pathname.slice(CHAT_PATH.length + 1)
    : null;

  const refresh = useCallback(() => {
    return api<Conversation[]>("/conversations")
      .then((rows) => {
        setConversations(rows);
        setError(null);
      })
      .catch((e: Error) => {
        // Clerk not ready yet. `api` changes identity when it is, which re-runs
        // the effect below — showing an auth error on first paint would be a
        // lie that fixes itself a moment later.
        if (e instanceof AuthNotReadyError) return;
        setError(e.message);
      });
  }, [api]);

  useEffect(() => {
    void refresh();
  }, [refresh, pathname]);

  // `pathname` is in the dependency list without being used inside — that is
  // deliberate, and it is the refetch trigger described above.

  // Which row is currently a text box instead of a link. One at a time, so a
  // single id is enough state to describe it.
  //
  // This was `window.prompt` until the browser refused the call outright —
  // "prompt() is not supported", thrown before any request could be made.
  // `confirm()` below is unaffected and still works. Editing in place needs no
  // dialog at all, so nothing is left for a browser to block.
  const [editingId, setEditingId] = useState<string | null>(null);

  const rename = useCallback(
    async (conversation: Conversation, draft: string) => {
      const title = draft.trim();
      setEditingId(null);

      // An emptied box is not a rename, and the API would refuse it anyway —
      // `ConversationUpdate` requires at least one character.
      if (!title || title === conversation.title) return;

      try {
        await api(`/conversations/${conversation.id}`, {
          method: "PATCH",
          // `useApi` sets no Content-Type of its own, because an upload needs
          // the browser to set it. A JSON body has to say so itself.
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title }),
        });
        await refresh();
      } catch (e) {
        setError((e as Error).message);
      }
    },
    [api, refresh],
  );

  const remove = useCallback(
    async (conversation: Conversation) => {
      const name = conversation.title ?? "this conversation";
      if (!window.confirm(`Delete "${name}"? This cannot be undone.`)) return;

      try {
        await api(`/conversations/${conversation.id}`, { method: "DELETE" });

        if (conversation.id === activeId) {
          // You just deleted what you were reading. Staying put would leave the
          // page pointed at a conversation whose messages now 404. Navigating
          // changes the path, which refetches this list on the way.
          router.push(CHAT_PATH);
        } else {
          await refresh();
        }
      } catch (e) {
        setError((e as Error).message);
      }
    },
    [activeId, api, refresh, router],
  );

  return (
    <aside className="flex w-64 shrink-0 flex-col border-r">
      <div className="p-3">
        <Button asChild size="sm" className="w-full">
          <Link href={CHAT_PATH}>
            <Plus />
            New chat
          </Link>
        </Button>
      </div>

      <nav className="flex-1 overflow-y-auto px-2 pb-3">
        {error && (
          <p role="alert" className="px-2 py-1 text-xs text-destructive">
            {error}
          </p>
        )}

        {conversations === null && !error && (
          <p className="px-2 py-1 text-xs text-muted-foreground">Loading…</p>
        )}

        {conversations?.length === 0 && (
          <p className="px-2 py-1 text-xs text-muted-foreground">
            No conversations yet.
          </p>
        )}

        <ul className="space-y-0.5">
          {conversations?.map((conversation) => {
            const name = conversation.title ?? "New conversation";

            if (conversation.id === editingId) {
              return (
                <li key={conversation.id} className="px-2 py-2">
                  <form
                    onSubmit={(event) => {
                      event.preventDefault();
                      const input = event.currentTarget.elements.namedItem(
                        "title",
                      ) as HTMLInputElement;
                      void rename(conversation, input.value);
                    }}
                  >
                    <input
                      name="title"
                      defaultValue={conversation.title ?? ""}
                      aria-label="Conversation title"
                      autoFocus
                      maxLength={200}
                      onKeyDown={(event) => {
                        if (event.key === "Escape") setEditingId(null);
                      }}
                      onBlur={() => setEditingId(null)}
                      className="w-full rounded-md border bg-background px-2 py-1 text-sm focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none"
                    />
                  </form>
                </li>
              );
            }

            // A <form> so Enter submits — that behaviour is the browser's, not
            // ours. Escape abandons the edit, and so does clicking away, which
            // is the safer reading of a click that lands somewhere else.
            // `maxLength` mirrors the 200 that `ConversationUpdate` enforces
            // server-side; this copy only saves a doomed round trip.

            return (
              <li key={conversation.id} className="group relative">
                {/* Padding on the right keeps the title from running under the
                    two buttons, which sit on top rather than inside the link —
                    a button nested in a link is invalid HTML and clicking one
                    would navigate as well as act. */}
                <Link
                  href={`${CHAT_PATH}/${conversation.id}`}
                  className={cn(
                    "block rounded-md px-2 py-2 pr-16 hover:bg-muted",
                    conversation.id === activeId && "bg-muted",
                  )}
                >
                  <span className="block truncate text-sm">{name}</span>
                  <span className="text-xs text-muted-foreground">
                    {timeAgo(conversation.updated_at)}
                  </span>
                </Link>

                {/* Hidden until the row is hovered or something inside it takes
                    keyboard focus. `opacity-0` rather than `hidden`, so they
                    stay in the tab order and a keyboard user reaches them. */}
                <div className="absolute right-1 top-1.5 flex gap-0.5 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
                  <Button
                    variant="ghost"
                    size="icon"
                    className="size-7"
                    aria-label={`Rename ${name}`}
                    onClick={() => setEditingId(conversation.id)}
                  >
                    <Pencil />
                  </Button>

                  <Button
                    variant="ghost"
                    size="icon"
                    className="size-7"
                    aria-label={`Delete ${name}`}
                    onClick={() => void remove(conversation)}
                  >
                    <Trash2 />
                  </Button>
                </div>
              </li>
            );
          })}
        </ul>
      </nav>
    </aside>
  );
}

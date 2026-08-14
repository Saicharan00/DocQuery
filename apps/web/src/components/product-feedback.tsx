"use client";

import { useCallback, useState } from "react";
import { Star } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useApi } from "@/lib/api";
import type { ProductFeedbackBody } from "@/lib/types";
import { cn } from "@/lib/utils";

const RATINGS = [1, 2, 3, 4, 5] as const;

type Rating = (typeof RATINGS)[number];

/**
 * What do you make of DocQuery itself?
 *
 * The sibling of `AnswerFeedback` in chat-view.tsx, and deliberately a separate
 * thing rather than a mode of it. That one asks "was this answer right?" and
 * can only be answered next to an answer; this one asks "is this worth using?",
 * which someone can have an opinion about before they read a single reply — so
 * it sits beside the chat and is available the whole time, rather than under a
 * bubble that may not exist yet.
 *
 * The submission order copies `AnswerFeedback`, because the reasoning carries
 * over unchanged: the stars go the instant they are clicked, and the comment
 * box only appears afterwards. Asking for a sentence first would trade the
 * signal most people will give for the one most people won't.
 */
export function ProductFeedback() {
  const api = useApi();

  const [rating, setRating] = useState<Rating | null>(null);
  const [hovered, setHovered] = useState<Rating | null>(null);
  const [comment, setComment] = useState("");
  const [commentSent, setCommentSent] = useState(false);
  const [failed, setFailed] = useState(false);

  const post = useCallback(
    async (body: ProductFeedbackBody) => {
      try {
        await api("/feedback/product", {
          method: "POST",
          // `useApi` sets no Content-Type by default, because an upload needs
          // the browser to set its own multipart boundary. JSON has to say so.
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        setFailed(false);
        return true;
      } catch {
        setFailed(true);
        return false;
      }
    },
    [api],
  );

  const rate = useCallback(
    (next: Rating) => {
      // The stars stop being buttons after the first vote, so this only catches
      // a second click landing before the re-render.
      if (rating !== null) return;
      setRating(next);
      void post({ rating: next }).then((ok) => {
        // Put the stars back rather than leaving a score on screen that was
        // never recorded.
        if (!ok) setRating(null);
      });
    },
    [post, rating],
  );

  const sendComment = useCallback(() => {
    const text = comment.trim();
    if (!text || commentSent) return;
    // No rating in this body, on purpose. It was already sent as its own row,
    // and repeating it here would count one person's stars twice in the average.
    void post({ comment: text }).then((ok) => {
      if (ok) setCommentSent(true);
    });
  }, [comment, commentSent, post]);

  // `hidden … xl:flex` and not a narrower breakpoint: the sidebar takes 16rem
  // and the chat is capped at 48rem, so below about 1280px there is no room
  // left to put this in without squeezing the conversation. It is an opinion
  // box, and the chat outranks it.
  return (
    <aside className="hidden min-w-0 flex-1 flex-col gap-3 overflow-y-auto border-l p-4 xl:flex">
      <div>
        <h2 className="text-sm font-medium">How is DocQuery working out?</h2>
        <p className="mt-0.5 text-xs text-muted-foreground">
          About the whole thing — not one answer. The thumbs under each reply
          cover those.
        </p>
      </div>

      <div
        className="flex items-center gap-0.5"
        // One `onMouseLeave` on the row, rather than an `onMouseOut` per star:
        // moving between two stars would otherwise clear the highlight and set
        // it again on every crossing.
        onMouseLeave={() => setHovered(null)}
      >
        {RATINGS.map((value) => {
          // Fill every star up to and including the one being pointed at, or
          // up to the recorded score once there is one. That is what makes five
          // separate buttons read as a single 1-to-5 scale.
          const lit = value <= (hovered ?? rating ?? 0);

          return (
            <button
              key={value}
              type="button"
              disabled={rating !== null}
              aria-label={`${value} out of 5`}
              onMouseEnter={() => rating === null && setHovered(value)}
              onClick={() => rate(value)}
              className="rounded p-0.5 focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none disabled:cursor-default"
            >
              <Star
                className={cn(
                  "size-5",
                  lit
                    ? "fill-amber-400 text-amber-400"
                    : "text-muted-foreground/50",
                )}
              />
            </button>
          );
        })}
      </div>

      {/* In plain English, the block above: draw five star buttons. A star is
          filled if its number is less than or equal to whichever of these is
          set first — the star the mouse is over, or the score already given,
          or zero. Clicking one records it and switches all five off. */}

      {rating !== null && (
        <div className="space-y-1.5">
          <p className="text-xs text-muted-foreground">
            {commentSent
              ? "Thanks — the detail is more useful than the stars."
              : "Thanks. Anything you'd add? (optional)"}
          </p>

          {!commentSent && (
            <>
              <textarea
                value={comment}
                onChange={(e) => setComment(e.target.value)}
                onKeyDown={(e) => {
                  // Same rule as the chat box: Enter sends, Shift+Enter breaks
                  // the line, and `isComposing` keeps an input method for
                  // Japanese, Chinese or Korean from sending half a word.
                  if (
                    e.key === "Enter" &&
                    !e.shiftKey &&
                    !e.nativeEvent.isComposing
                  ) {
                    e.preventDefault();
                    sendComment();
                  }
                }}
                rows={4}
                // Matches `ProductFeedbackRequest.comment`'s ceiling, so the
                // server never has to reject something the box let you type.
                maxLength={2000}
                placeholder="What worked, what didn't, what's missing"
                className="max-h-64 w-full resize-none rounded-md border bg-background px-2 py-1.5 text-xs field-sizing-content focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none"
              />

              <Button
                size="sm"
                variant="secondary"
                className="w-full"
                disabled={comment.trim() === ""}
                onClick={sendComment}
              >
                Send
              </Button>
            </>
          )}
        </div>
      )}

      {failed && (
        <p role="alert" className="text-xs text-destructive">
          Couldn&apos;t record that. Please try again.
        </p>
      )}
    </aside>
  );
}

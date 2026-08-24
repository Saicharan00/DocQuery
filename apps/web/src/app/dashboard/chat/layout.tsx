import { ConversationSidebar } from "@/components/conversation-sidebar";
import { ProductFeedback } from "@/components/product-feedback";
import { AmbientWaves } from "@/components/ambient-waves";

/**
 * The sidebar, the chat, and the product feedback box, side by side.
 *
 * A layout rather than something each page renders: React keeps a layout
 * mounted while the pages inside it change, so clicking between conversations
 * swaps only the middle. The sidebar keeps its scroll position and its loaded
 * list instead of refetching on every click, and the feedback box keeps
 * whatever you had half-typed in it.
 *
 * Scoped to `chat` rather than the whole dashboard — a deliberate deviation
 * from BUILD.md, so the documents page stays a full-width upload screen.
 */
export default function ChatLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="chat-text flex h-dvh overflow-hidden">
      <AmbientWaves theme="sky" />
      <ConversationSidebar />
      {children}
      <ProductFeedback />
    </div>
  );
}

// `h-dvh`, and it replaced a `flex-1` that looked equivalent and was not.
//
// This div is a flex item of `<body>`, which is `min-h-full` — a floor, not a
// height. `flex-1` divides up a parent's spare space, and a parent whose height
// is decided by its contents has none to divide, so the div simply grew to fit
// the conversation. Everything below inherited that: the message list's
// `overflow-y-auto` never had a boundary to scroll inside, so the window
// scrolled instead and carried the composer off the bottom of the screen with
// it.
//
// `h-dvh` is one viewport tall, full stop, which gives the `flex-1` further in
// something real to measure against. `dvh` rather than `vh` because on a phone
// `vh` counts the space *behind* the browser's own toolbar, which would push
// the composer just out of reach. `overflow-hidden` then guarantees the page
// itself can never scroll — only the message list inside it does.
//
// Deliberately not fixed on `<body>`: the documents page is a plain
// `<main className="p-8">` that relies on the window scrolling, and a hard
// height there would cut off a long list of uploads.

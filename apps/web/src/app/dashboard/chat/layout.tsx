import { ConversationSidebar } from "@/components/conversation-sidebar";

/**
 * The sidebar, plus whichever chat is open beside it.
 *
 * A layout rather than something each page renders: React keeps a layout
 * mounted while the pages inside it change, so clicking between conversations
 * swaps only the right-hand side. The sidebar keeps its scroll position and its
 * loaded list instead of refetching on every click.
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
    <div className="flex flex-1 overflow-hidden">
      <ConversationSidebar />
      {children}
    </div>
  );
}

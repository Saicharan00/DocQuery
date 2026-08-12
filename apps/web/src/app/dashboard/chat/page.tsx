import { ChatView } from "@/components/chat-view";

/**
 * A chat that has no conversation yet.
 *
 * Null rather than an id because there is nothing to name yet: the server mints
 * the conversation when the first question arrives and reports the id in the
 * opening SSE event. `ChatView` then rewrites the URL to `/dashboard/chat/<id>`,
 * which is the route next door.
 */
export default function NewChatPage() {
  return <ChatView conversationId={null} />;
}

import { ChatView } from "@/components/chat-view";

/**
 * One saved conversation, opened from its own URL.
 *
 * The folder name `[id]` is a dynamic segment: it matches any single path
 * segment and hands the value over as `params`. One file therefore serves every
 * conversation that will ever exist.
 *
 * `params` is a Promise in this version of Next.js and has to be awaited —
 * confirmed in `node_modules/next/dist/docs/.../dynamic-routes.md`, not from
 * memory, because `apps/web/AGENTS.md` warns this is not the Next.js that older
 * examples describe.
 *
 * Nothing is validated here. An id that is not a real conversation — a typo, or
 * somebody else's — is a 404 from `GET /conversations/{id}/messages`, and RLS is
 * what decides that. Checking in the browser as well would duplicate the rule
 * and still not be the thing enforcing it.
 */
export default async function ConversationPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;

  // `key` is doing real work. Clicking from one saved conversation to another
  // renders this same component with a different id, and React would reuse the
  // instance — keeping the previous conversation's messages and, worse, the
  // previous conversation's id in state, so the next question would be filed
  // under the chat you just left. A changed key forces a fresh mount instead.
  return <ChatView key={id} conversationId={id} />;
}

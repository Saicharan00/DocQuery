import Link from "next/link";
import { Button } from "@/components/ui/button";

export default function Home() {
  return (
    <main className="flex flex-1 flex-col items-center justify-center gap-6 text-center">
      <h1 className="text-3xl font-semibold">DocQuery</h1>
      <p className="max-w-md text-zinc-600 dark:text-zinc-400">
        Chat with your own documents, using your own LLM API key.
      </p>
      <div className="flex gap-4">
        <Button asChild>
          <Link href="/sign-in">Sign in</Link>
        </Button>
        <Button asChild variant="outline">
          <Link href="/sign-up">Sign up</Link>
        </Button>
      </div>
    </main>
  );
}

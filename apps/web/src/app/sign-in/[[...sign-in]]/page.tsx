import { SignIn } from "@clerk/nextjs";
import { AuthShell } from "@/components/auth-shell";

export default function SignInPage() {
  return (
    <AuthShell title="Welcome back" subtitle="Sign in to continue to DocQuery">
      <SignIn
        forceRedirectUrl="/dashboard"
        appearance={{
          elements: {
            rootBox: "w-full",
            card: "w-full shadow-xl border border-border rounded-2xl",
          },
        }}
      />
    </AuthShell>
  );
}

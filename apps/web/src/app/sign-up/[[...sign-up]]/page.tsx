import { SignUp } from "@clerk/nextjs";
import { AuthShell } from "@/components/auth-shell";

export default function SignUpPage() {
  return (
    <AuthShell title="Create your account" subtitle="Get started with DocQuery">
      <SignUp
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

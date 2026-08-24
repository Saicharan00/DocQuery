import type { ReactNode } from "react";

export function AuthShell({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle: string;
  children: ReactNode;
}) {
  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-background px-4">
      {/* two decorative layers behind the card: a slow-drifting color glow, then a faint grid on top of it */}
      <div className="auth-aurora pointer-events-none absolute inset-0" aria-hidden />
      <div
        className="pointer-events-none absolute inset-0 bg-[linear-gradient(to_right,var(--border)_1px,transparent_1px),linear-gradient(to_bottom,var(--border)_1px,transparent_1px)] bg-[size:48px_48px] opacity-[0.25]"
        aria-hidden
      />
      <div className="animate-in fade-in zoom-in-95 slide-in-from-bottom-4 relative z-10 flex w-full max-w-md flex-col items-center gap-6 duration-700">
        <div className="flex flex-col items-center gap-1 text-center">
          <span className="text-xs font-semibold tracking-[0.2em] text-muted-foreground uppercase">
            DocQuery
          </span>
          <h1 className="text-2xl font-semibold text-foreground">{title}</h1>
          <p className="text-sm text-muted-foreground">{subtitle}</p>
        </div>
        <div className="relative w-full">
          <div className="card-glow pointer-events-none absolute -inset-8 -z-10" aria-hidden />
          {children}
        </div>
      </div>
    </div>
  );
}
// Wraps a Clerk auth widget with a centered card that fades/zooms in on load,
// sitting in front of a page-wide moving color glow and a faint grid, plus a
// tighter lavender "wave" glow hugging the card itself, on a plain background
// otherwise so it stays readable and fast (no images, no JS animation).

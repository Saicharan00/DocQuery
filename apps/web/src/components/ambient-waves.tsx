const THEME_CLASSES = {
  lavender: { waves: "lavender-waves", ball: "lavender-ball" },
  sky: { waves: "sky-waves", ball: "sky-ball" },
} as const;

/**
 * Decorative, viewport-fixed background: soft moving color blobs plus one
 * glowing orb that bounces off the edges. `-z-10` and `pointer-events-none`
 * keep it behind real content and out of the way of clicks.
 */
export function AmbientWaves({ theme }: { theme: keyof typeof THEME_CLASSES }) {
  const { waves, ball } = THEME_CLASSES[theme];
  return (
    <>
      <div className={`${waves} pointer-events-none fixed inset-0 -z-10`} aria-hidden />
      <div className={`bounce-ball ${ball} pointer-events-none fixed -z-10`} aria-hidden />
    </>
  );
}

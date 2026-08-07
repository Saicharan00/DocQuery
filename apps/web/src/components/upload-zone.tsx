"use client";

import { useCallback, useRef, useState } from "react";
import { Loader2, Upload } from "lucide-react";

import { Button } from "@/components/ui/button";
import { AuthNotReadyError, useApi } from "@/lib/api";
import type { Document } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * These duplicate the limits in apps/api/app/routers/documents.py on purpose.
 *
 * The copy here is feedback: you learn a file is unacceptable instantly, rather
 * than after waiting for a 10MB upload to finish. The copy on the server is
 * security — anyone can bypass the browser, so that is the one that decides.
 * If the two ever disagree, the server wins and the user sees its message.
 */
const MAX_FILE_BYTES = 10 * 1024 * 1024;
const ACCEPTED_EXTENSIONS = [".pdf", ".docx", ".txt"];

type UploadZoneProps = {
  /** Called after a successful upload so the parent can re-fetch the list. */
  onUploaded: () => void | Promise<void>;
};

function formatSize(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function describeRejection(file: File): string | null {
  const name = file.name.toLowerCase();

  if (!ACCEPTED_EXTENSIONS.some((extension) => name.endsWith(extension))) {
    const extension = name.includes(".")
      ? name.slice(name.lastIndexOf("."))
      : "no extension";
    return `“${file.name}” is a ${extension} file, which can't be read.`;
  }
  // Named with the actual size, because "too large" alone leaves you guessing
  // whether you missed by a little or a lot.
  if (file.size > MAX_FILE_BYTES) {
    return `“${file.name}” is ${formatSize(file.size)}, over the 10 MB limit.`;
  }
  if (file.size === 0) {
    return `“${file.name}” is empty — there is nothing in it to read.`;
  }
  return null;
}

export function UploadZone({ onUploaded }: UploadZoneProps) {
  const api = useApi();
  const inputRef = useRef<HTMLInputElement>(null);

  const [isDragging, setIsDragging] = useState(false);
  const [isUploading, setIsUploading] = useState(false);

  // Rendered from state rather than via <dialog>.showModal(): a value here is
  // on screen, with no browser API able to swallow it silently.
  const [error, setError] = useState<string | null>(null);

  const reject = useCallback((message: string) => {
    setError(message);
  }, []);

  const upload = useCallback(
    async (files: FileList | null) => {
      if (!files || files.length === 0) return;

      if (files.length > 1) {
        // The endpoint takes one file per request. Refusing is better than
        // quietly uploading one of several and leaving you to notice.
        reject(`You picked ${files.length} files. Upload one at a time.`);
        return;
      }

      const file = files[0];
      const rejection = describeRejection(file);
      if (rejection) {
        reject(rejection);
        return;
      }

      setError(null);
      setIsUploading(true);

      try {
        // FormData, not JSON: file bytes aren't text. The browser sets the
        // Content-Type itself because only it knows the multipart boundary
        // string it generated — which is why api.ts never sets that header.
        const body = new FormData();
        body.append("file", file);

        await api<Document>("/documents/upload", { method: "POST", body });
        await onUploaded();
      } catch (e) {
        // Server-side refusals land here too — the daily cap (429) and the
        // service ceiling (503) among them. They deserve the same visibility as
        // a file that was wrong before it left the browser.
        reject(
          e instanceof AuthNotReadyError
            ? "Still signing you in. Try again in a moment."
            : (e as Error).message,
        );
      } finally {
        setIsUploading(false);
      }
    },
    [api, onUploaded, reject],
  );

  return (
    <div>
      <div
        // A browser's default response to a dropped file is to navigate away
        // and open it. Cancelling that default on dragOver *and* drop is what
        // makes a drop zone work at all.
        onDragOver={(event) => {
          event.preventDefault();
          if (!isUploading) setIsDragging(true);
        }}
        onDragLeave={(event) => {
          // Moving onto a child element fires dragleave on the parent. Ignore
          // it unless the pointer has actually left the zone, or the highlight
          // flickers as you move across the contents.
          if (event.currentTarget.contains(event.relatedTarget as Node | null)) {
            return;
          }
          setIsDragging(false);
        }}
        onDrop={(event) => {
          event.preventDefault();
          setIsDragging(false);
          if (!isUploading) void upload(event.dataTransfer.files);
        }}
        onClick={() => {
          if (!isUploading) inputRef.current?.click();
        }}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            if (!isUploading) inputRef.current?.click();
          }
        }}
        role="button"
        tabIndex={0}
        aria-disabled={isUploading}
        aria-label="Upload a document"
        className={cn(
          "flex flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed p-10 text-center transition-colors",
          "focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none",
          isDragging
            ? "border-primary bg-primary/5"
            : "border-border hover:border-primary/50",
          isUploading ? "cursor-wait opacity-70" : "cursor-pointer",
        )}
      >
        {isUploading ? (
          <>
            <Loader2 className="size-6 animate-spin text-muted-foreground" />
            <p className="text-sm font-medium">Uploading…</p>
          </>
        ) : (
          <>
            <Upload className="size-6 text-muted-foreground" />
            <p className="text-sm font-medium">
              Drop a file here, or click to browse
            </p>
            <p className="text-xs text-muted-foreground">
              PDF, DOCX, or TXT — up to 10MB
            </p>
          </>
        )}
      </div>

      {/* Outside the zone, not inside it: a programmatic .click() on the input
          would bubble up to the zone's own onClick and call .click() again. */}
      {/* No `accept` filter on purpose. It makes the OS grey out everything
          else, so picking a wrong file is impossible and you never learn what
          the rules are. Better to let anything be chosen and say why it was
          refused — the server validates regardless. */}
      <input
        ref={inputRef}
        type="file"
        className="hidden"
        onChange={(event) => {
          void upload(event.target.files);
          // Clear it so picking the same file twice in a row still fires
          // change — the browser suppresses it when the value is unchanged.
          event.target.value = "";
        }}
      />

      {error && (
        <div
          role="alertdialog"
          aria-modal="true"
          aria-label="Upload refused"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
          // Clicking the backdrop dismisses. The check keeps a click inside the
          // panel from closing it as the event bubbles out.
          onClick={(event) => {
            if (event.target === event.currentTarget) setError(null);
          }}
        >
          <div className="w-full max-w-md rounded-lg border bg-background p-6 text-foreground shadow-lg">
            <h2 className="text-base font-semibold">
              {error.includes("limit reached")
                ? "Daily upload limit reached"
                : "Can't upload that file"}
            </h2>

            <p className="mt-2 text-sm text-muted-foreground">{error}</p>

            <div className="mt-4 rounded-md bg-muted/50 p-3 text-xs text-muted-foreground">
              <p className="mb-1.5 font-medium text-foreground">
                What is accepted
              </p>
              <ul className="list-disc space-y-1 pl-4">
                <li>PDF, DOCX or TXT</li>
                <li>Up to 10 MB</li>
                <li>One file at a time</li>
                <li>Not empty — a scanned PDF is fine, its pages are read as pictures</li>
                <li>15 documents per day</li>
              </ul>
            </div>

            <div className="mt-5 flex justify-end">
              <Button size="sm" onClick={() => setError(null)}>
                Got it
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

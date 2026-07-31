"use client";

import { useCallback, useRef, useState } from "react";
import { Loader2, Upload } from "lucide-react";

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

function describeRejection(file: File): string | null {
  const name = file.name.toLowerCase();

  if (!ACCEPTED_EXTENSIONS.some((extension) => name.endsWith(extension))) {
    return "Unsupported file type. Upload a PDF, DOCX, or TXT file.";
  }
  if (file.size > MAX_FILE_BYTES) {
    return "File is too large. The limit is 10MB.";
  }
  if (file.size === 0) {
    return "That file is empty.";
  }
  return null;
}

export function UploadZone({ onUploaded }: UploadZoneProps) {
  const api = useApi();
  const inputRef = useRef<HTMLInputElement>(null);

  const [isDragging, setIsDragging] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const upload = useCallback(
    async (files: FileList | null) => {
      if (!files || files.length === 0) return;

      if (files.length > 1) {
        // The endpoint takes one file per request. Refusing is better than
        // quietly uploading one of several and leaving you to notice.
        setError("One file at a time, please.");
        return;
      }

      const file = files[0];
      const rejection = describeRejection(file);
      if (rejection) {
        setError(rejection);
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
        if (e instanceof AuthNotReadyError) {
          setError("Still signing you in. Try again in a moment.");
        } else {
          setError((e as Error).message);
        }
      } finally {
        setIsUploading(false);
      }
    },
    [api, onUploaded],
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
      <input
        ref={inputRef}
        type="file"
        accept={ACCEPTED_EXTENSIONS.join(",")}
        className="hidden"
        onChange={(event) => {
          void upload(event.target.files);
          // Clear it so picking the same file twice in a row still fires
          // change — the browser suppresses it when the value is unchanged.
          event.target.value = "";
        }}
      />

      {error && (
        <p role="alert" className="mt-2 text-sm text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}

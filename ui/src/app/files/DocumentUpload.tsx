"use client";

import { FileText, Upload, X } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import {
  documentProcessingCapabilitiesApiV1KnowledgeBaseProcessingCapabilitiesGet,
  getDocumentApiV1KnowledgeBaseDocumentsDocumentUuidGet,
  getUploadUrlApiV1KnowledgeBaseUploadUrlPost,
  processDocumentApiV1KnowledgeBaseProcessDocumentPost,
  retryDocumentApiV1KnowledgeBaseDocumentsDocumentUuidRetryPost,
} from "@/client/sdk.gen";
import type { DocumentUploadResponseSchema } from "@/client/types.gen";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { detailFromError } from "@/lib/apiError";
import { useAuth } from "@/lib/auth";

import ExternalProcessingNotice from "./ExternalProcessingNotice";

const MAX_FILE_SIZE = 5 * 1024 * 1024;
const FORMATS = [".pdf", ".docx", ".txt", ".json", ".md", ".csv", ".xlsx"];
type Pending = {
  id: number;
  file: File;
  status: string;
  error?: string;
  upload?: DocumentUploadResponseSchema;
};
let nextId = 0;

export default function DocumentUpload({
  onUploadSuccess,
  onBusyChange,
}: {
  onUploadSuccess: () => void;
  onBusyChange?: (busy: boolean) => void;
}) {
  const { user, loading } = useAuth();
  const [files, setFiles] = useState<Pending[]>([]);
  const [mode, setMode] = useState<"full_document" | "chunked">(
    "full_document",
  );
  const [busy, setBusy] = useState(false);
  const [capability, setCapability] = useState<{
    chunked: boolean;
    issue?: unknown;
    embedding?: { model: string };
  }>();
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (loading || !user) return;
    let disposed = false;
    documentProcessingCapabilitiesApiV1KnowledgeBaseProcessingCapabilitiesGet()
      .then((response) => {
        if (disposed) return;
        if (response.error)
          setCapability({
            chunked: false,
            issue: detailFromError(response.error),
          });
        else setCapability(response.data as typeof capability);
      })
      .catch(() => {
        if (!disposed)
          setCapability({
            chunked: false,
            issue: "Unable to check model configuration. Refresh to retry.",
          });
      });
    return () => {
      disposed = true;
    };
  }, [loading, user]);
  const update = (id: number, values: Partial<Pending>) =>
    setFiles((old) => old.map((f) => (f.id === id ? { ...f, ...values } : f)));
  const add = (incoming: File[]) => {
    const valid = incoming.filter((file) => {
      if (
        !FORMATS.includes("." + file.name.split(".").pop()?.toLowerCase()) ||
        !file.size ||
        file.size > MAX_FILE_SIZE
      ) {
        toast.error(
          `${file.name}: use a supported, nonempty document up to 5 MiB.`,
        );
        return false;
      }
      return true;
    });
    if (files.length + valid.length > 20)
      toast.error(
        "Only the first 20 files are included. Upload remaining files in another batch.",
      );
    setFiles((old) =>
      [
        ...old,
        ...valid.map((file) => ({ id: ++nextId, file, status: "Ready" })),
      ].slice(0, 20),
    );
    if (input.current) input.current.value = "";
  };
  const run = async () => {
    setBusy(true);
    onBusyChange?.(true);
    for (const entry of files.filter((f) => f.status !== "Queued")) {
      try {
        update(entry.id, { status: "Uploading", error: undefined });
        let upload = entry.upload;
        if (!upload) {
          const result = await getUploadUrlApiV1KnowledgeBaseUploadUrlPost({
            body: {
              filename: entry.file.name,
              mime_type: entry.file.type || "application/octet-stream",
            },
          });
          if (result.error || !result.data)
            throw new Error(
              detailFromError(result.error, "Unable to prepare upload"),
            );
          upload = result.data;
          const sent = await fetch(upload.upload_url, {
            method: "PUT",
            body: entry.file,
            headers: {
              "Content-Type": entry.file.type || "application/octet-stream",
            },
          });
          if (!sent.ok)
            throw new Error("Storage upload failed. Retry this file.");
          update(entry.id, { upload });
        }
        const result =
          await processDocumentApiV1KnowledgeBaseProcessDocumentPost({
            body: {
              document_uuid: upload.document_uuid,
              s3_key: upload.s3_key,
              retrieval_mode: mode,
            },
          });
        if (result.error) {
          if (result.response?.status === 409) {
            const existing =
              await getDocumentApiV1KnowledgeBaseDocumentsDocumentUuidGet({
                path: { document_uuid: upload.document_uuid },
              });
            if (existing.error || !existing.data)
              throw new Error(detailFromError(existing.error));
            if (existing.data.processing_status === "failed") {
              const retried =
                await retryDocumentApiV1KnowledgeBaseDocumentsDocumentUuidRetryPost(
                  {
                    path: { document_uuid: upload.document_uuid },
                  },
                );
              if (retried.error)
                throw new Error(detailFromError(retried.error));
            }
          } else throw new Error(detailFromError(result.error));
        }
        update(entry.id, { status: "Queued" });
        onUploadSuccess();
      } catch (error) {
        update(entry.id, {
          status: "Failed",
          error: error instanceof Error ? error.message : "Upload failed",
        });
        onUploadSuccess();
      }
    }
    setBusy(false);
    onBusyChange?.(false);
  };
  return (
    <div className="space-y-4">
      <ExternalProcessingNotice />
      <input
        ref={input}
        type="file"
        className="hidden"
        multiple
        accept={FORMATS.join(",")}
        disabled={busy}
        onChange={(e) => add(Array.from(e.target.files || []))}
      />
      <div
        className="rounded-lg border border-dashed p-4 text-center"
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => {
          event.preventDefault();
          if (!busy && !loading && user)
            add(Array.from(event.dataTransfer.files));
        }}
      >
        <Button
          variant="outline"
          disabled={busy || loading || !user}
          onClick={() => input.current?.click()}
        >
          <Upload className="mr-2 h-4 w-4" />
          Choose Files
        </Button>
        <p className="mt-2 text-xs text-muted-foreground">
          Or drop documents here
        </p>
      </div>
      <p className="text-xs text-muted-foreground">
        Up to 20 files per batch, 5 MiB each. {FORMATS.join(", ")}. Scanned PDFs
        require a text export.
      </p>
      <div className="space-y-2">
        {files.map((entry) => (
          <div key={entry.id} className="rounded-lg border p-3">
            <div className="flex items-center gap-2">
              <FileText className="h-4 w-4 shrink-0" />
              <span className="truncate flex-1">{entry.file.name}</span>
              <span className="text-xs">{entry.status}</span>
              <Button
                variant="ghost"
                size="icon"
                aria-label={`Remove ${entry.file.name}`}
                disabled={busy}
                onClick={() =>
                  setFiles((old) => old.filter((f) => f.id !== entry.id))
                }
              >
                <X className="h-4 w-4" />
              </Button>
            </div>
            {entry.error && (
              <p role="alert" className="text-xs text-destructive">
                {entry.error}
              </p>
            )}
          </div>
        ))}
      </div>
      <Label>How should the agent use these documents?</Label>
      <RadioGroup
        value={mode}
        onValueChange={(value) => setMode(value as typeof mode)}
        disabled={busy || files.some((file) => file.upload)}
      >
        <label className="flex gap-3 rounded-lg border p-3">
          <RadioGroupItem value="full_document" />
          <span>
            <span className="font-medium">Full Document</span>
            <span className="block text-xs text-muted-foreground">
              Return all extracted text. No embedding model required.
            </span>
          </span>
        </label>
        <label className="flex gap-3 rounded-lg border p-3">
          <RadioGroupItem value="chunked" disabled={!capability?.chunked} />
          <span>
            <span className="font-medium">Chunked Search</span>
            <span className="block text-xs text-muted-foreground">
              Retrieve relevant sections using{" "}
              {capability?.embedding?.model ||
                "the organization Embedding model"}
              .
            </span>
          </span>
        </label>
      </RadioGroup>
      {capability?.issue ? (
        <p className="text-sm text-muted-foreground">
          {typeof capability.issue === "string"
            ? capability.issue
            : detailFromError({ detail: capability.issue })}{" "}
          <Link className="underline" href="/model-configurations">
            Models
          </Link>
        </p>
      ) : null}
      <Button
        className="w-full"
        disabled={
          busy ||
          !files.some((f) => f.status !== "Queued") ||
          (mode === "chunked" && !capability?.chunked)
        }
        onClick={run}
      >
        {busy ? "Uploading batch…" : "Upload & Process"}
      </Button>
      {files.some((f) => f.status === "Queued") && (
        <p className="text-xs text-muted-foreground">
          Queued files appear in the document list. Each file processes
          independently; failed files can be retried there.
        </p>
      )}
    </div>
  );
}

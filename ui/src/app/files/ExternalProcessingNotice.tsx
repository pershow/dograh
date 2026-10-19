"use client";

import { Info } from "lucide-react";

export default function ExternalProcessingNotice() {
  return (
    <div className="flex gap-3 rounded-lg border bg-muted/30 p-3">
      <Info className="h-4 w-4 shrink-0 mt-0.5" />
      <div className="text-xs text-muted-foreground">
        <p className="font-medium text-foreground">
          Documents are parsed on your Dograh server
        </p>
        <p className="mt-1">
          Full Document mode extracts text locally. Chunked Search sends
          extracted text to the Embedding provider configured in Models.
          Original files are not sent to MPS for parsing.
        </p>
      </div>
    </div>
  );
}

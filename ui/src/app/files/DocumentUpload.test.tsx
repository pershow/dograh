import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

import DocumentUpload from "./DocumentUpload";

const mocks = vi.hoisted(() => ({
  auth: { user: { id: "u" }, loading: false },
  capabilities: vi.fn(),
  upload: vi.fn(),
  process: vi.fn(),
  retry: vi.fn(),
  getDocument: vi.fn(),
}));
vi.mock("@/lib/auth", () => ({ useAuth: () => mocks.auth }));
vi.mock("@/client/sdk.gen", () => ({
  documentProcessingCapabilitiesApiV1KnowledgeBaseProcessingCapabilitiesGet:
    mocks.capabilities,
  getUploadUrlApiV1KnowledgeBaseUploadUrlPost: mocks.upload,
  getDocumentApiV1KnowledgeBaseDocumentsDocumentUuidGet: mocks.getDocument,
  processDocumentApiV1KnowledgeBaseProcessDocumentPost: mocks.process,
  retryDocumentApiV1KnowledgeBaseDocumentsDocumentUuidRetryPost: mocks.retry,
}));

beforeEach(() => {
  vi.clearAllMocks();
  mocks.auth.loading = false;
  mocks.capabilities.mockResolvedValue({
    data: { chunked: true, embedding: { model: "text-embedding-v4" } },
  });
  mocks.upload.mockImplementation(({ body }) =>
    Promise.resolve({
      data: {
        document_uuid: body.filename,
        s3_key: body.filename,
        upload_url: "https://storage.test/" + body.filename,
      },
    }),
  );
  mocks.process.mockResolvedValue({ data: {} });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true }));
});

it("continues a partial batch and retries only the failed item without uploading bytes again", async () => {
  mocks.process
    .mockResolvedValueOnce({ error: { detail: "Queue unavailable" } })
    .mockResolvedValueOnce({ data: {} });
  const { container } = render(<DocumentUpload onUploadSuccess={vi.fn()} />);
  await screen.findByText(/text-embedding-v4/);
  fireEvent.change(container.querySelector("input[type=file]")!, {
    target: {
      files: [
        new File(["first"], "first.txt"),
        new File(["second"], "second.md"),
      ],
    },
  });
  fireEvent.click(screen.getByRole("button", { name: "Upload & Process" }));
  await screen.findByText("Queue unavailable");
  await screen.findByText("Queued", { exact: true });
  expect(mocks.process).toHaveBeenCalledTimes(2);
  expect(fetch).toHaveBeenCalledTimes(2);
  await waitFor(() =>
    expect(
      (
        screen.getByRole("button", {
          name: "Upload & Process",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(false),
  );
  fireEvent.click(screen.getByRole("button", { name: "Upload & Process" }));
  await waitFor(() =>
    expect(screen.getAllByText("Queued", { exact: true })).toHaveLength(2),
  );
  expect(mocks.upload).toHaveBeenCalledTimes(2);
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(mocks.process).toHaveBeenCalledTimes(3);
});

it("waits for auth and keeps full-document ingestion available without embeddings", async () => {
  mocks.auth.loading = true;
  mocks.capabilities.mockResolvedValue({
    data: { chunked: false, issue: "Configure Embedding" },
  });
  const { rerender } = render(<DocumentUpload onUploadSuccess={vi.fn()} />);
  expect(mocks.capabilities).not.toHaveBeenCalled();
  mocks.auth.loading = false;
  rerender(<DocumentUpload onUploadSuccess={vi.fn()} />);
  await screen.findByText(/Configure Embedding/);
  const radios = screen.getAllByRole("radio");
  expect(radios[0].getAttribute("aria-checked")).toBe("true");
  expect((radios[1] as HTMLButtonElement).disabled).toBe(true);
});

it("recovers a lost queue response without starting the same document again", async () => {
  mocks.process
    .mockRejectedValueOnce(new Error("Connection lost"))
    .mockResolvedValueOnce({
      error: { detail: "Already exists" },
      response: { status: 409 },
    });
  mocks.getDocument.mockResolvedValue({
    data: { processing_status: "completed" },
  });
  const { container } = render(<DocumentUpload onUploadSuccess={vi.fn()} />);
  await screen.findByText(/text-embedding-v4/);
  fireEvent.change(container.querySelector("input[type=file]")!, {
    target: { files: [new File(["rules"], "rules.txt")] },
  });
  fireEvent.click(screen.getByRole("button", { name: "Upload & Process" }));
  await screen.findByText("Connection lost");
  fireEvent.click(screen.getByRole("button", { name: "Upload & Process" }));
  await screen.findByText("Queued", { exact: true });
  expect(fetch).toHaveBeenCalledOnce();
  expect(mocks.retry).not.toHaveBeenCalled();
});

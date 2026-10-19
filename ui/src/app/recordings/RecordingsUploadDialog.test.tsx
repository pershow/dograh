import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

import { RecordingsUploadDialog } from "./RecordingsUploadDialog";

const mocks = vi.hoisted(() => ({
  auth: { user: { id: "u" }, loading: false },
  capabilities: vi.fn(),
  transcribe: vi.fn(),
  urls: vi.fn(),
  save: vi.fn(),
}));
vi.mock("@/lib/auth", () => ({ useAuth: () => mocks.auth }));
vi.mock("@/client", () => ({
  transcriptionCapabilitiesApiV1WorkflowRecordingsTranscriptionCapabilitiesGet:
    mocks.capabilities,
  transcribeAudioApiV1WorkflowRecordingsTranscribePost: mocks.transcribe,
  getUploadUrlsApiV1WorkflowRecordingsUploadUrlPost: mocks.urls,
  createRecordingsApiV1WorkflowRecordingsPost: mocks.save,
}));
beforeEach(() => {
  vi.clearAllMocks();
  mocks.capabilities.mockResolvedValue({
    data: { available: true, provider: "elevenlabs", model: "scribe_v2" },
  });
  mocks.urls.mockResolvedValue({
    data: {
      items: [
        {
          recording_id: "rec",
          storage_key: "recordings/1/rec/a.wav",
          upload_url: "https://storage.test/audio",
        },
      ],
    },
  });
  mocks.save.mockResolvedValue({ data: {} });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true }));
});

it("shows generated-client errors, permits manual correction and saves it", async () => {
  mocks.transcribe.mockResolvedValue({
    error: { detail: { message: "Quota unavailable" } },
  });
  const done = vi.fn();
  render(
    <RecordingsUploadDialog
      open
      onOpenChange={vi.fn()}
      onUploadComplete={done}
    />,
  );
  await screen.findByText(/elevenlabs \/ scribe_v2/);
  fireEvent.change(document.querySelector("input[type=file]")!, {
    target: { files: [new File(["audio"], "a.wav", { type: "audio/wav" })] },
  });
  await screen.findByText("Quota unavailable");
  expect(
    screen.getByRole("button", { name: "Retry transcription" }),
  ).toBeTruthy();
  fireEvent.change(screen.getByLabelText("Transcript for a.wav"), {
    target: { value: "Manual correction" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Upload 1 Recording" }));
  await waitFor(() => expect(done).toHaveBeenCalledOnce());
  expect(mocks.save.mock.calls[0][0].body.recordings[0].transcript).toBe(
    "Manual correction",
  );
});

it("does not call a provider for unsupported STT and allows a manual transcript", async () => {
  mocks.capabilities.mockResolvedValue({
    data: { available: false, issue: "Use manual transcription" },
  });
  render(<RecordingsUploadDialog open onOpenChange={vi.fn()} />);
  await screen.findByText("Use manual transcription");
  fireEvent.change(document.querySelector("input[type=file]")!, {
    target: { files: [new File(["audio"], "a.wav")] },
  });
  expect(
    (
      (await screen.findByLabelText(
        "Transcript for a.wav",
      )) as HTMLTextAreaElement
    ).disabled,
  ).toBe(false);
  expect(mocks.transcribe).not.toHaveBeenCalled();
});

it("can retry provider transcription and saves its configuration provenance", async () => {
  mocks.transcribe
    .mockResolvedValueOnce({ error: { detail: "Try again" } })
    .mockResolvedValueOnce({
      data: {
        transcript: "Recognized speech",
        transcription_configuration: {
          provider: "elevenlabs",
          model: "scribe_v2",
          connection_revision: 2,
        },
      },
    });
  render(<RecordingsUploadDialog open onOpenChange={vi.fn()} />);
  await screen.findByText(/elevenlabs \/ scribe_v2/);
  fireEvent.change(document.querySelector("input[type=file]")!, {
    target: { files: [new File(["audio"], "a.wav")] },
  });
  fireEvent.click(
    await screen.findByRole("button", { name: "Retry transcription" }),
  );
  await waitFor(() =>
    expect(
      (screen.getByLabelText("Transcript for a.wav") as HTMLTextAreaElement)
        .value,
    ).toBe("Recognized speech"),
  );
  fireEvent.click(screen.getByRole("button", { name: "Upload 1 Recording" }));
  await waitFor(() => expect(mocks.save).toHaveBeenCalledOnce());
  expect(
    mocks.save.mock.calls[0][0].body.recordings[0].metadata
      .transcription_configuration.connection_revision,
  ).toBe(2);
});

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useWorkbenchVoice } from "../useWorkbenchVoice";

vi.mock("@/lib/apiClient", () => ({
  resolveBrowserBackendUrl: () => "http://localhost:8016",
}));
class Socket {
  static OPEN = 1;
  static instances: Socket[] = [];
  readyState = 1;
  send = vi.fn();
  close = vi.fn();
  onmessage: ((event: { data: string }) => Promise<void>) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  constructor(public url: string) {
    Socket.instances.push(this);
  }
}
const getToken = async () => "test-token";
const onUpdate = vi.fn();
describe("workbench microphone lifecycle", () => {
  let stop: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    Socket.instances = [];
    stop = vi.fn();
    vi.stubGlobal("WebSocket", Socket);
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        getUserMedia: vi
          .fn()
          .mockResolvedValue({ getTracks: () => [{ stop }] }),
      },
    });
  });
  afterEach(() => vi.unstubAllGlobals());
  it("shows permission errors and never opens the websocket", async () => {
    vi.mocked(navigator.mediaDevices.getUserMedia).mockRejectedValue(
      new Error("denied"),
    );
    const { result } = renderHook(() =>
      useWorkbenchVoice("s", getToken, [], "c", "suite", onUpdate),
    );
    await act(() => result.current.start());
    expect(result.current.status).toBe("off");
    expect(result.current.error).toBe("denied");
    expect(Socket.instances).toHaveLength(0);
  });
  it("sends current candidate, suite and attachment selection and cleans up on unmount", async () => {
    const { result, rerender, unmount } = renderHook(
      ({ assets, candidate }) =>
        useWorkbenchVoice("s", getToken, assets, candidate, "suite", onUpdate),
      { initialProps: { assets: ["a"], candidate: "c" } },
    );
    await act(() => result.current.start());
    rerender({ assets: ["b"], candidate: "selected-candidate" });
    expect(JSON.parse(Socket.instances[0].send.mock.calls.at(-1)![0])).toEqual({
      type: "context",
      asset_ids: ["b"],
      candidate_id: "selected-candidate",
      suite_id: "suite",
    });
    unmount();
    expect(stop).toHaveBeenCalledOnce();
    expect(Socket.instances[0].close).toHaveBeenCalledOnce();
  });
  it("stops a late microphone grant after the component unmounts", async () => {
    let grant!: (value: unknown) => void;
    vi.mocked(navigator.mediaDevices.getUserMedia).mockImplementation(
      () =>
        new Promise((resolve) => {
          grant = resolve as typeof grant;
        }),
    );
    const { result, unmount } = renderHook(() =>
      useWorkbenchVoice("s", getToken, [], "c", "suite", onUpdate),
    );
    let starting!: Promise<void>;
    act(() => {
      starting = result.current.start();
    });
    unmount();
    await act(async () => {
      grant({ getTracks: () => [{ stop }] });
      await starting;
    });
    expect(stop).toHaveBeenCalledOnce();
    expect(Socket.instances).toHaveLength(0);
  });
});

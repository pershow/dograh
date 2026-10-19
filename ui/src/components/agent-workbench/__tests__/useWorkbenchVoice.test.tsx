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
  it("only confirms the exact selection acknowledged by the server", async () => {
    const { result, rerender } = renderHook(
      ({ assets }) =>
        useWorkbenchVoice("s", getToken, assets, "c", "suite", onUpdate),
      { initialProps: { assets: ["a"] } },
    );
    await act(() => result.current.start());
    const message = async (data: object) => {
      await act(async () =>
        Socket.instances[0].onmessage?.({ data: JSON.stringify(data) }),
      );
    };
    expect(result.current.contextSynced).toBe(false);
    await message({
      type: "context.applied",
      asset_ids: ["a"],
      candidate_id: "c",
      suite_id: "suite",
    });
    expect(result.current.contextSynced).toBe(true);
    rerender({ assets: ["b"] });
    expect(result.current.contextSynced).toBe(false);
    await message({
      type: "context.applied",
      asset_ids: ["a"],
      candidate_id: "c",
      suite_id: "suite",
    });
    expect(result.current.contextSynced).toBe(false);
    await message({ type: "context.error", message: "附件尚未解析完成" });
    expect(result.current.error).toContain("解析");
    expect(Socket.instances[0].close).not.toHaveBeenCalled();
    await message({
      type: "context.applied",
      asset_ids: ["b"],
      candidate_id: "c",
      suite_id: "suite",
    });
    expect(result.current.contextSynced).toBe(true);
    expect(result.current.error).toBe("");
  });
  it("shows rejected voice commands without disconnecting the conversation", async () => {
    const { result } = renderHook(() =>
      useWorkbenchVoice("s", getToken, [], "c", "suite", onUpdate),
    );
    await act(() => result.current.start());
    await act(async () =>
      Socket.instances[0].onmessage?.({
        data: JSON.stringify({
          type: "tool.result",
          name: "run_tests",
          result: { error: "Task already running" },
        }),
      }),
    );
    expect(result.current.error).toBe("Task already running");
    expect(Socket.instances[0].close).not.toHaveBeenCalled();
  });
  it("keeps transient provider warnings recoverable", async () => {
    const { result } = renderHook(() =>
      useWorkbenchVoice("s", getToken, [], "c", "suite", onUpdate),
    );
    await act(() => result.current.start());
    await act(async () =>
      Socket.instances[0].onmessage?.({
        data: JSON.stringify({
          type: "warning",
          message: "Reconnecting voice",
        }),
      }),
    );
    expect(result.current.error).toBe("Reconnecting voice");
    expect(Socket.instances[0].close).not.toHaveBeenCalled();
    await act(async () =>
      Socket.instances[0].onmessage?.({
        data: JSON.stringify({
          type: "transcript",
          role: "assistant",
          text: "Recovered",
        }),
      }),
    );
    expect(result.current.error).toBe("");
  });
});

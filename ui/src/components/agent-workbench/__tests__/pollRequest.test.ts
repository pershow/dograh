import { describe, expect, it, vi } from "vitest";

import { pollRequest } from "../pollRequest";

describe("workbench polling recovery", () => {
  it("releases an unresolved auth or transport call and can retry", async () => {
    vi.useFakeTimers();
    let signal: AbortSignal | undefined;
    const pending = pollRequest(s => {signal=s;return new Promise(() => {});});
    const rejected = expect(pending).rejects.toThrow("后台任务继续执行");
    await vi.advanceTimersByTimeAsync(15000);
    await rejected;
    expect(signal?.aborted).toBe(true);
    await expect(pollRequest(async () => "new progress")).resolves.toBe("new progress");
    expect(vi.getTimerCount()).toBe(0);
    vi.useRealTimers();
  });
});

/** Release the polling loop even when authentication or transport hangs. */
export async function pollRequest<T>(request: (signal: AbortSignal) => Promise<T>, timeoutMs = 15000): Promise<T> {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      request(controller.signal),
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => {
          controller.abort();
          reject(new Error("进度同步暂时超时，正在重新连接；后台任务继续执行。"));
        }, timeoutMs);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

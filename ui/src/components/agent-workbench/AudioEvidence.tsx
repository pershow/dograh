"use client";

import { useEffect, useState } from "react";

import { downloadWorkbenchAudio } from "@/client";
import { Button } from "@/components/ui/button";
import { detailFromError } from "@/lib/apiError";
import { useAuth } from "@/lib/auth";

export type AudioReplayEvidence = {
  measurement_boundary: string; channel: string; sample_rate: number;
  input_coverage: number; max_input_lag_ms: number; output_frames: number;
  output_asset_id?: string; errors: string[];
};

export function AudioEvidence({evidence, session}: {evidence: AudioReplayEvidence; session?: string}) {
  const {user, loading} = useAuth();
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => () => { if (url) URL.revokeObjectURL(url); }, [url]);
  async function load() {
    if (!session || !evidence.output_asset_id || !user || loading) return;
    setBusy(true); setError("");
    try {
      const response = await downloadWorkbenchAudio({path: {session_id: session, asset_id: evidence.output_asset_id}, parseAs: "blob"});
      if (response.error) throw new Error(detailFromError(response.error, "录音加载失败"));
      if (!(response.data instanceof Blob)) throw new Error("录音响应格式不正确");
      setUrl(URL.createObjectURL(response.data));
    } catch (e) { setError(e instanceof Error ? e.message : "录音加载失败"); }
    finally { setBusy(false); }
  }
  return <div aria-label="语音回放证据">
    <strong>服务端音频回放 · {evidence.channel === "phone" ? "电话采样率" : "浏览器采样率"} · {evidence.sample_rate} Hz</strong>
    <p>输入覆盖 {(evidence.input_coverage * 100).toFixed(1)}%；最大投送偏差 {evidence.max_input_lag_ms.toFixed(1)} ms；输出 {evidence.output_frames} 个音频帧。</p>
    <p>覆盖识别、轮次判断、模型、工具和合成管线；不包含运营商线路、浏览器设备及真实网络。录音为 Agent 输出片段，时序以事件记录为准。</p>
    {session && evidence.output_asset_id && !url && <Button variant="outline" disabled={busy || loading || !user} onClick={() => void load()}>{busy ? "正在加载…" : "试听 Agent 输出"}</Button>}
    {url && <audio controls src={url} aria-label="Agent 回放输出"/>}
    {error && <p role="alert">{error}</p>}
    {evidence.errors.map((message, i) => <p role="alert" key={i}>{message}</p>)}
  </div>;
}

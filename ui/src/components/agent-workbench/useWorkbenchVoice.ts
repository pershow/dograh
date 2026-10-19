"use client";
import { useCallback, useEffect, useRef, useState } from "react";

import { resolveBrowserBackendUrl } from "@/lib/apiClient";

export function useWorkbenchVoice(
  sessionId: string | undefined,
  getToken: () => Promise<string | null>,
  assets: string[],
  candidateId: string,
  suiteId: string,
  onUpdate: () => void,
) {
  const [status, setStatus] = useState<"off" | "connecting" | "connected">(
    "off",
  );
  const [error, setError] = useState("");
  const [warning, setWarning] = useState("");
  const [syncedContext, setSyncedContext] = useState("");
  const ws = useRef<WebSocket | null>(null);
  const pc = useRef<RTCPeerConnection | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const audio = useRef<HTMLAudioElement | null>(null);
  const generation = useRef(0);
  const selected = useRef({
    asset_ids: assets,
    candidate_id: candidateId,
    suite_id: suiteId,
  });
  selected.current = {
    asset_ids: assets,
    candidate_id: candidateId,
    suite_id: suiteId,
  };
  const stop = useCallback(() => {
    generation.current++;
    if (ws.current?.readyState === WebSocket.OPEN)
      ws.current.send(JSON.stringify({ type: "disconnect" }));
    ws.current?.close();
    ws.current = null;
    pc.current?.close();
    pc.current = null;
    stream.current?.getTracks().forEach((track) => track.stop());
    stream.current = null;
    if (audio.current) {
      audio.current.pause();
      audio.current.srcObject = null;
      audio.current = null;
    }
    setStatus("off");
    setSyncedContext("");
  }, []);
  useEffect(() => () => stop(), [stop, sessionId]);
  useEffect(() => {
    if (ws.current?.readyState === WebSocket.OPEN)
      ws.current.send(JSON.stringify({ type: "context", ...selected.current }));
  }, [assets, candidateId, suiteId]);
  const start = useCallback(async () => {
    if (!sessionId || ws.current) return;
    setError("");
    setWarning("");
    setStatus("connecting");
    const attempt = ++generation.current;
    try {
      const media = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (attempt !== generation.current) {
        media.getTracks().forEach((track) => track.stop());
        return;
      }
      stream.current = media;
      const token = await getToken();
      if (attempt !== generation.current) return;
      const url = new URL(
        `/api/v1/agent-workbench/sessions/${sessionId}/voice`,
        resolveBrowserBackendUrl(),
      );
      url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
      url.searchParams.set("token", token || "");
      const socket = new WebSocket(url);
      ws.current = socket;
      socket.onmessage = async (event) => {
        if (attempt !== generation.current) return;
        try {
          const message = JSON.parse(event.data);
          if (message.type === "ready") {
            socket.send(
              JSON.stringify({ type: "context", ...selected.current }),
            );
            const connection = new RTCPeerConnection({
              iceServers: message.ice_servers,
            });
            pc.current = connection;
            const output = new Audio();
            output.autoplay = true;
            audio.current = output;
            connection.ontrack = (e) => {
              output.srcObject = e.streams[0];
              void output
                .play()
                .catch(() => setError("请允许浏览器播放声音。"));
            };
            media
              .getTracks()
              .forEach((track) => connection.addTrack(track, media));
            connection.createDataChannel("pipecat");
            connection.onconnectionstatechange = () => {
              if (connection.connectionState === "connected")
                setStatus("connected");
              if (["failed", "closed"].includes(connection.connectionState))
                stop();
            };
            await connection.setLocalDescription(
              await connection.createOffer(),
            );
            if (connection.iceGatheringState !== "complete")
              await new Promise<void>((resolve, reject) => {
                const timer = setTimeout(
                  () => reject(new Error("语音网络连接超时")),
                  15000,
                );
                connection.onicegatheringstatechange = () => {
                  if (connection.iceGatheringState === "complete") {
                    clearTimeout(timer);
                    resolve();
                  }
                };
              });
            if (attempt !== generation.current) return;
            socket.send(
              JSON.stringify({
                type: "offer",
                sdp: connection.localDescription?.sdp,
              }),
            );
          } else if (message.type === "answer") {
            await pc.current?.setRemoteDescription({
              type: "answer",
              sdp: message.sdp,
            });
          } else if (message.type === "error") {
            throw new Error(message.message);
          } else if (message.type === "context.applied") {
            setSyncedContext(
              JSON.stringify({
                asset_ids: message.asset_ids,
                candidate_id: message.candidate_id || "",
                suite_id: message.suite_id || "",
              }),
            );
            setError("");
          } else if (message.type === "context.error") {
            setSyncedContext("");
            setError(message.message);
          } else if (message.type === "warning") {
            setWarning(message.message);
          } else if (
            message.type === "tool.result" ||
            message.type === "transcript"
          ) {
            if (message.type === "transcript" && message.role === "assistant")
              setWarning("");
            if (message.type === "tool.result" && message.result?.error)
              setError(message.result.error);
            onUpdate();
          }
        } catch (error) {
          setError(error instanceof Error ? error.message : "语音连接失败");
          stop();
        }
      };
      socket.onerror = () => {
        setError("无法连接语音服务");
        stop();
      };
      socket.onclose = () => {
        if (attempt === generation.current) stop();
      };
    } catch (error) {
      setError(error instanceof Error ? error.message : "无法使用麦克风");
      stop();
    }
  }, [sessionId, getToken, onUpdate, stop]);
  return {
    status,
    error: error || warning,
    start,
    stop,
    contextSynced: syncedContext === JSON.stringify(selected.current),
  };
}

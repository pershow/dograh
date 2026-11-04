import { useEffect, useRef, useState } from "react";

import { getWorkbenchEvaluation } from "@/client/sdk.gen";
import { detailFromError } from "@/lib/apiError";

import type { Evaluation } from "./types";

/** Refresh evidence in the background without replacing it with an empty index. */
export function useEvaluationDetail(
  session: string | undefined, index: Evaluation | undefined,
  sessionRevision: string | undefined, enabled: boolean,
) {
  const [cached, setCached] = useState<{session: string; version: string; data: Evaluation}>();
  const [request, setRequest] = useState<{key: string; loading: boolean; error: string}>();
  const [attempt, setAttempt] = useState(0);
  const current = cached && cached.session === session && cached.data.id === index?.id ? cached : undefined;
  // A pinned historical batch can leave the compact index while still running.
  // Once its detail says it finished, an old running index must not regress it.
  const settled = index?.status === "running" && current && ["completed", "failed", "cancelled"].includes(current.data.status);
  // Older servers lack detail_revision. Keep their running batches fresh while
  // still retaining the displayed evidence during a request.
  const version = index?.detail_revision ?? (settled ? current.version : JSON.stringify([
    index?.status, index?.summary, index?.results,
    index?.status === "running" ? sessionRevision : undefined,
  ]));
  const key = JSON.stringify([session, index?.id, version, attempt]);
  const latest = useRef(index);
  latest.current = index;
  const id = index?.id;
  const summaryOnly = index?.summary_only;
  useEffect(() => {
    if (!enabled || !session || !id) return;
    if (!summaryOnly) {
      setCached({session, version, data: latest.current!});
      return;
    }
    const controller = new AbortController();
    setRequest({key, loading: true, error: ""});
    void getWorkbenchEvaluation({signal: controller.signal, path: {session_id: session, evaluation_id: id}})
      .then(response => {
        if (response.error || !response.data?.data)
          throw new Error(detailFromError(response.error, "结果详情读取失败"));
        if (!controller.signal.aborted) {
          setCached({session, version, data: response.data.data as unknown as Evaluation});
          setRequest({key, loading: false, error: ""});
        }
      }).catch(error => {
        if (!controller.signal.aborted)
          setRequest({key, loading: false, error: error instanceof Error ? error.message : "结果详情读取失败"});
      });
    return () => controller.abort();
  }, [enabled, session, id, summaryOnly, key, version]);
  const detail = current?.data;
  return {
    evaluation: index?.summary_only && detail
      ? {...index, ...(current.version === version ? detail : {results: detail.results}), summary_only: false}
      : index,
    loading: request?.key === key && request.loading,
    error: request?.key === key ? request.error : "",
    retry: () => setAttempt(value => value + 1),
  };
}

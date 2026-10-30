export type RuntimeMetricGroup = {channel: string; pipeline: string; fingerprint: string; runs: number; missing_evidence: number; incomplete_evidence: number; silent_turns: number; errors: number; latency_samples: number; p50_ms: number | null; p95_ms: number | null; first_generated_audio_p95_ms?: number | null; successful_tasks?: number; cost_per_success?: number | null; cost_missing?: number};

export function RuntimeMetrics({groups}: {groups: RuntimeMetricGroup[]}) {
  if (!groups.length) return null;
  return <details><summary>运行表现与证据覆盖</summary>
    <p>按通道、链路和配置版本分别统计最近运行。延迟终点为服务端开始播放，尚不能证明用户听到有效回答的时间。</p>
    {groups.map(group => <div key={`${group.channel}:${group.fingerprint}`}>
      <strong>{group.channel} · {group.pipeline} · {group.fingerprint.slice(0, 8)}</strong>
      <p>{group.runs} 次运行；缺少证据 {group.missing_evidence} 次，证据不完整 {group.incomplete_evidence} 次。</p>
      <p>服务端开始播放：P50 {group.p50_ms == null ? "暂无数据" : `${Math.round(group.p50_ms)} ms`}；P95 {group.p95_ms == null ? "暂无数据" : `${Math.round(group.p95_ms)} ms`}（{group.latency_samples} 个有效样本）</p>
      <p>轮次释放到首段合成音频 P95：{group.first_generated_audio_p95_ms == null ? "暂无数据" : `${Math.round(group.first_generated_audio_p95_ms)} ms`}。</p>
      <p>结构化任务完成 {group.successful_tasks ?? 0} 次；每次成功成本 {group.cost_per_success == null ? "证据不足" : `$${group.cost_per_success.toFixed(4)}`}（USD，仅使用已有完整成本记录）。</p>
      <p>未回应轮次 {group.silent_turns}；运行错误 {group.errors}。</p>
    </div>)}
  </details>;
}

import styles from "./workbench.module.css";

export type RuntimeProfileData = {
  version: number; stage: string; channel: string; pipeline: string; ready: boolean;
  fingerprint: string; configuration_fingerprint: string; model_fingerprint: string;
  unsupported_requirements: string[];
  models: Record<string, {provider?: string; model?: string; voice?: string; voice_id?: string; language?: string}>;
  settings: {key: string; requested: unknown; resolved: unknown; status: string; reason: string}[];
};

const labels: Record<string, string> = {
  context_compaction_enabled: "上下文压缩", tts_cache_enabled: "语音缓存",
  turn_start_strategy: "轮次开始策略", "node_routing.mode": "节点路由",
};

function valueText(value: unknown) {
  return typeof value === "boolean" ? value ? "开启" : "关闭" : String(value ?? "未设置");
}

export function RuntimeProfile({profile}: {profile: RuntimeProfileData}) {
  return <details className={styles.candidateResult}>
    <summary>{profile.stage === "runtime" ? "实际运行配置" : "运行配置预检"} · {({text: "文本", browser: "浏览器语音", phone: "电话", voice: "语音"} as Record<string,string>)[profile.channel] || profile.channel} · {profile.pipeline}</summary>
    {profile.stage !== "runtime" && <p>预检展示当前配置的解析结果；测试或通话记录展示实际运行结果。</p>}
    {!profile.ready && <p role="alert">当前链路不支持要求的能力：{profile.unsupported_requirements.join("、")}</p>}
    {Object.entries(profile.models).map(([role, model]) => <p key={role}>{role.toUpperCase()}：{model.provider} / {model.model}{(model.voice || model.voice_id) ? ` · ${model.voice || model.voice_id}` : ""}</p>)}
    <table><thead><tr><th>配置</th><th>请求值</th><th>生效值</th></tr></thead><tbody>
      {profile.settings.map(setting => <tr key={setting.key}><td>{labels[setting.key] || setting.key}{setting.status !== "applied" && <p>{setting.reason}</p>}</td><td>{valueText(setting.requested)}</td><td>{valueText(setting.resolved)}</td></tr>)}
    </tbody></table>
    <p className={styles.subtle}>配置记录：{profile.fingerprint.slice(0, 12)}</p>
  </details>;
}

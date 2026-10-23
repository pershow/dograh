"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { getModelConnectionCatalog, getWorkbenchBuildModel, listProviderConnections, updateWorkbenchBuildModel } from "@/client/sdk.gen";
import { compatibleConnections, resetDependentFields, selectionForConnection } from "@/components/model-connections/configuration";
import { ConfigurationSelect } from "@/components/model-connections/ConfigurationSelect";
import { SchemaFields } from "@/components/model-connections/SchemaFields";
import type { ModelConnectionCatalog, ProviderConnection, ServiceSelection } from "@/components/model-connections/types";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { useOrgConfig } from "@/context/OrgConfigContext";
import { detailFromError } from "@/lib/apiError";
import { useAuth } from "@/lib/auth";

const INHERIT = "__inherit__";

function dataOrThrow<T>(response: { data?: T; error?: unknown }): T {
  if (response.error) throw new Error(detailFromError(response.error, "无法读取构建模型配置"));
  if (!response.data) throw new Error("服务未返回构建模型配置");
  return response.data;
}

export function BuildModelSettings({ disabled, onSaved }: { disabled: boolean; onSaved: () => void }) {
  const { user, loading: authLoading } = useAuth();
  const { orgContext, loading: orgLoading } = useOrgConfig();
  const organizationId = orgContext?.organization_id;
  const userId = user?.id;
  const [catalog, setCatalog] = useState<ModelConnectionCatalog>();
  const [connections, setConnections] = useState<ProviderConnection[]>([]);
  const [providers, setProviders] = useState<string[]>([]);
  const [selection, setSelection] = useState<ServiceSelection | null>(null);
  const [saved, setSaved] = useState<ServiceSelection | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const request = useRef(0);

  const reload = useCallback(async () => {
    if (authLoading || orgLoading || !userId || organizationId == null) return;
    const current = ++request.current;
    setLoading(true);
    setError("");
    setNotice("");
    try {
      const responses = await Promise.all([getWorkbenchBuildModel(), getModelConnectionCatalog(), listProviderConnections()]);
      if (current !== request.current) return;
      const config = dataOrThrow(responses[0]);
      const nextCatalog = dataOrThrow(responses[1]) as unknown as ModelConnectionCatalog;
      const nextConnections = dataOrThrow(responses[2]);
      const nextSelection = config.llm ? { ...config.llm, settings: config.llm.settings || {} } : null;
      setSelection(nextSelection);
      setSaved(nextSelection);
      setProviders(config.supported_providers);
      setCatalog(nextCatalog);
      setConnections(nextConnections);
    } catch (cause) {
      if (current === request.current) setError(cause instanceof Error ? cause.message : "读取构建模型配置失败");
    } finally {
      if (current === request.current) setLoading(false);
    }
  }, [authLoading, orgLoading, userId, organizationId]);

  useEffect(() => {
    setCatalog(undefined);
    setSelection(null);
    setSaved(null);
    setLoading(true);
    setSaving(false);
    void reload();
    return () => { request.current += 1; };
  }, [reload]);

  const options = catalog ? compatibleConnections(catalog, connections, "llm").filter(connection => providers.includes(connection.provider)) : [];
  const connection = options.find(item => item.uuid === selection?.provider_connection_uuid);
  const unavailable = Boolean(selection && !connection);
  const fullSchema = connection ? catalog?.services.llm?.[connection.provider]?.settings_schema : undefined;
  const modelField = fullSchema?.properties?.model;
  const modelName = String(selection?.settings.model ?? modelField?.default ?? "").trim();
  const dirty = JSON.stringify(selection) !== JSON.stringify(saved);
  const edit = (next: ServiceSelection | null) => { setSelection(next); setNotice(""); setError(""); };

  const save = async () => {
    if (disabled || saving || !dirty || unavailable || (selection && !modelName)) return;
    const current = request.current;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const config = dataOrThrow(await updateWorkbenchBuildModel({ body: { llm: selection ? { ...selection, settings: { ...selection.settings, model: modelName } } : null } }));
      if (current !== request.current) return;
      const next = config.llm ? { ...config.llm, settings: config.llm.settings || {} } : null;
      setSelection(next);
      setSaved(next);
      setNotice("已保存，下次构建任务生效");
      onSaved();
    } catch (cause) {
      if (current === request.current) setError(cause instanceof Error ? cause.message : "保存构建模型失败");
    } finally {
      if (current === request.current) setSaving(false);
    }
  };

  return <section aria-label="构建助手模型" className="space-y-3 border-b pb-4 mb-4">
    <p className="text-xs text-muted-foreground">当前组织的构建助手共用此配置，仅影响构建助手，不影响 Agent 的通话模型。</p>
    {loading ? <p role="status">正在加载构建模型…</p> : catalog ? <>
      <fieldset disabled={disabled || saving} className="space-y-3 min-w-0">
        <div className="space-y-1.5">
          <Label htmlFor="workbench-build-provider">构建 Provider</Label>
          <ConfigurationSelect id="workbench-build-provider" value={selection?.provider_connection_uuid || INHERIT} disabled={disabled || saving}
            onValueChange={value => {
              if (value === INHERIT) edit(null);
              else {
                const next = options.find(item => item.uuid === value);
                if (next) edit(selectionForConnection(catalog, "llm", next));
              }
            }} options={[
              { value: INHERIT, label: "跟随 Agent 模型配置" },
              ...options.map(item => ({ value: item.uuid, label: `${item.name} · ${catalog.services.llm?.[item.provider]?.title || item.provider}` })),
              ...(unavailable && selection ? [{ value: selection.provider_connection_uuid, label: "已保存的连接不可用，请重新选择" }] : []),
            ]} />
        </div>
        {connection && modelField && selection && <SchemaFields key={connection.uuid} className="space-y-2" role="llm" provider={connection.provider}
          schema={{ ...fullSchema, required: ["model"], properties: { model: { ...modelField, title: "构建模型" } } }} values={selection.settings}
          onChange={(name, value) => edit({ ...selection, settings: resetDependentFields({ ...selection.settings, [name]: value }, fullSchema, name) })} />}
        {!selection && <p className="text-xs text-muted-foreground">构建助手沿用当前 Agent 的 LLM；Agent 未单独配置时使用组织默认。</p>}
        {unavailable && <p role="alert" className="text-xs text-destructive">连接已归档、配置不完整或不支持构建助手，请选择其他连接。</p>}
        <Button type="button" size="sm" disabled={disabled || saving || !dirty || unavailable || Boolean(selection && !modelName)} onClick={() => void save()}>
          {saving ? "保存中…" : "保存构建模型"}
        </Button>
      </fieldset>
      {options.length === 0 && <p className="text-xs text-muted-foreground">暂无可用连接，请先添加兼容 OpenAI 协议的 Provider。</p>}
    </> : <Button type="button" size="sm" variant="outline" onClick={() => void reload()}>重新加载构建模型</Button>}
    {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
    {notice && <p role="status" className="text-xs text-muted-foreground">{notice}</p>}
    <Link href="/provider-connections" target="_blank" rel="noopener noreferrer" className="text-xs underline">管理 Provider 连接</Link>
  </section>;
}

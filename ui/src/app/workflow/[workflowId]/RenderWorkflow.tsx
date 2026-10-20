import '@xyflow/react/dist/style.css';

import {
    applyEdgeChanges,
    applyNodeChanges,
    Background,
    BackgroundVariant,
    Panel,
    ReactFlow,
} from "@xyflow/react";
import { BrushCleaning, Maximize2, Minus, Plus } from 'lucide-react';
import { useRouter, useSearchParams } from 'next/navigation';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { toast } from 'sonner';

import { createWorkflowDraftApiV1WorkflowWorkflowIdCreateDraftPost, getWorkflowVersionsApiV1WorkflowWorkflowIdVersionsGet, listDocumentsApiV1KnowledgeBaseDocumentsGet, listRecordingsApiV1WorkflowRecordingsGet, listToolsApiV1ToolsGet } from '@/client';
import type { DocumentResponseSchema, RecordingResponseSchema, ToolResponse, WorkflowVersionResponse } from '@/client/types.gen';
import { AgentWorkbench } from "@/components/agent-workbench/AgentWorkbench";
import type { Candidate, Snapshot, WorkbenchSession } from "@/components/agent-workbench/types";
import { useNodeSpecs } from "@/components/flow/renderer";
import { FlowEdge, FlowNode, NodeType } from "@/components/flow/types";
import { HireExpertNudge } from "@/components/lead-forms/HireExpertNudge";
import { Button } from '@/components/ui/button';
import { Sheet, SheetContent } from '@/components/ui/sheet';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { useOnboarding } from '@/context/OnboardingContext';
import { UnsavedChangesProvider, useUnsavedChangesContext } from '@/context/UnsavedChangesContext';
import { detailFromError } from '@/lib/apiError';
import { WorkflowConfigurations } from '@/types/workflow-configurations';

import AddNodePanel from "../../../components/flow/AddNodePanel";
import CustomEdge from "../../../components/flow/edges/CustomEdge";
import { GenericNode } from "../../../components/flow/nodes/GenericNode";
import { EditorSetup, type EditorView, isEditorView } from './components/EditorSetup';
import { PhoneCallDialog } from './components/PhoneCallDialog';
import { VersionHistoryPanel } from './components/VersionHistoryPanel';
import type { WorkflowRuntimeNodeTransition } from './components/workflow-tester/types';
import { WorkflowEditorHeader } from "./components/WorkflowEditorHeader";
import { WorkflowTesterPanel } from './components/WorkflowTesterPanel';
import { WorkflowVersionDiffDialog } from './components/WorkflowVersionDiffDialog';
import { WorkflowProvider } from "./contexts/WorkflowContext";
import { type SavedVersion, useWorkflowState } from "./hooks/useWorkflowState";
import { layoutNodes } from './utils/layoutNodes';

const edgeTypes = {
    custom: CustomEdge,
};

const VERSIONS_PAGE_SIZE = 10;

// Compare saved data independent of JSON object key order. Never compare with
// React Flow's measured/selected node state or overwrite unsaved local edits.
function savedSnapshotKey(value: unknown): string {
    return JSON.stringify(value, (_key, item) => (
        item && typeof item === 'object' && !Array.isArray(item)
            ? Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]]))
            : item
    ));
}

interface RenderWorkflowProps {
    onDraftAdopted?: () => void;
    initialWorkflowName: string;
    workflowId: number;
    workflowUuid?: string;
    initialTotalRuns?: number | null;
    openTesterOnLoad?: boolean;
    initialFlow?: {
        nodes: FlowNode[];
        edges: FlowEdge[];
        viewport: {
            x: number;
            y: number;
            zoom: number;
        };
    };
    initialTemplateContextVariables?: Record<string, string>;
    initialWorkflowConfigurations?: WorkflowConfigurations;
    initialVersionNumber?: number | null;
    initialVersionStatus?: string | null;
    initialSelectedVersion?: WorkflowVersionResponse;
    user: { id: string; email?: string };
}

// The settings views register unsaved edits with this provider, so leaving
// them, whether by switching view, picking a version from History, or
// navigating away, asks first.
function RenderWorkflow(props: RenderWorkflowProps) {
    return (
        <UnsavedChangesProvider>
            <WorkflowEditor {...props} />
        </UnsavedChangesProvider>
    );
}

function WorkflowEditor({
    onDraftAdopted,
    initialWorkflowName,
    workflowId,
    workflowUuid,
    initialTotalRuns,
    openTesterOnLoad = false,
    initialFlow,
    initialTemplateContextVariables,
    initialWorkflowConfigurations,
    initialVersionNumber,
    initialVersionStatus,
    initialSelectedVersion,
    user,
}: RenderWorkflowProps) {
    const router = useRouter();
    const searchParams = useSearchParams();
    const { confirmNavigate, hasDirtyChanges } = useUnsavedChangesContext();
    const workbenchOpen = searchParams.get('workbench') === '1';
    const [candidatePreview, setCandidatePreview] = useState<Candidate>();
    const [previewNodes, setPreviewNodes] = useState<FlowNode[]>([]);
    const [previewEdges, setPreviewEdges] = useState<FlowEdge[]>([]);
    const isCandidatePreview = workbenchOpen && !!candidatePreview;
    const handlePreviewChange = useCallback((candidate: Candidate | undefined) => {
        setCandidatePreview(candidate);
        setPreviewNodes(structuredClone(candidate?.snapshot?.workflow_json.nodes ?? []) as FlowNode[]);
        setPreviewEdges(structuredClone(candidate?.snapshot?.workflow_json.edges ?? []) as FlowEdge[]);
    }, []);
    const setWorkbenchOpen = useCallback((open: boolean) => {
        const query = new URLSearchParams(searchParams.toString());
        if (open) query.set('workbench', '1');
        else query.delete('workbench');
        router.replace(`/workflow/${workflowId}?${query.toString()}`, { scroll: false });
        if (!open) handlePreviewChange(undefined);
    }, [router, searchParams, workflowId, handlePreviewChange]);
    const handleSessionReady = useCallback((session: WorkbenchSession) => {
        if (searchParams.get('session') === session.id) return;
        const query = new URLSearchParams(searchParams.toString());
        query.set('session', session.id);
        router.replace(`/workflow/${workflowId}?${query.toString()}`, { scroll: false });
    }, [router, searchParams, workflowId]);
    const { specs } = useNodeSpecs();
    const { hasCompletedAction } = useOnboarding();
    const [isPhoneCallDialogOpen, setIsPhoneCallDialogOpen] = useState(false);
    const [isVersionPanelOpen, setIsVersionPanelOpen] = useState(false);
    const [isTesterRailOpen, setIsTesterRailOpen] = useState(true);
    const [isTesterSheetOpen, setIsTesterSheetOpen] = useState(false);
    const [isDesktopViewport, setIsDesktopViewport] = useState(false);
    const [versions, setVersions] = useState<WorkflowVersionResponse[]>([]);
    const [versionsLoading, setVersionsLoading] = useState(false);
    const [versionsLoadingMore, setVersionsLoadingMore] = useState(false);
    const [versionsHasMore, setVersionsHasMore] = useState(false);
    const [activeVersionId, setActiveVersionId] = useState<number | null>(initialSelectedVersion?.id ?? null);
    const [comparingVersionId, setComparingVersionId] = useState<number | null>(null);
    const [versionDiffPair, setVersionDiffPair] = useState<{
        previousVersion: WorkflowVersionResponse;
        selectedVersion: WorkflowVersionResponse;
    } | null>(null);
    const hasAutoOpenedTester = useRef(false);
    // Version info that updates immediately from the GET/save/publish responses.
    const [currentVersionNumber, setCurrentVersionNumber] = useState<number | null>(initialVersionNumber ?? null);
    const [currentVersionStatus, setCurrentVersionStatus] = useState<string | null>(initialVersionStatus ?? null);
    // What the editor body shows. It lives in the URL (`?view=`) so a refresh
    // or a shared link lands on the same view; the canvas is the bare URL.
    const viewParam = searchParams.get('view');
    const editorView: EditorView = isEditorView(viewParam) ? viewParam : 'canvas';
    const setEditorView = useCallback((view: EditorView) => {
        const query = new URLSearchParams(searchParams.toString());
        if (view === 'canvas') query.delete('view');
        else query.set('view', view);
        const suffix = query.toString();
        router.replace(`/workflow/${workflowId}${suffix ? `?${suffix}` : ''}`, { scroll: false });
    }, [router, searchParams, workflowId]);
    const versionsFetched = useRef(false);
    // The version bookkeeping for settings saves lives below, after the
    // version fetcher it needs; the state hook reaches it through this ref.
    const applySavedVersionRef = useRef<(version: SavedVersion) => void>(() => undefined);
    const onVersionSaved = useCallback((version: SavedVersion) => applySavedVersionRef.current(version), []);
    // A model edit waiting to save or still saving: publishing or testing now
    // would use the previous settings.
    const [settingsSaving, setSettingsSaving] = useState(false);
    const [documents, setDocuments] = useState<DocumentResponseSchema[] | undefined>(undefined);
    const [tools, setTools] = useState<ToolResponse[] | undefined>(undefined);
    const [recordings, setRecordings] = useState<RecordingResponseSchema[]>([]);
    const [activeRuntimeNodeId, setActiveRuntimeNodeId] = useState<string | null>(null);
    const [externalDraftChanged, setExternalDraftChanged] = useState(false);
    const [workbenchCanReload, setWorkbenchCanReload] = useState(true);
    const reloadingDraft = useRef(false);
    const savedDraftKey = useMemo(() => savedSnapshotKey({
        name: initialWorkflowName,
        workflow_json: { nodes: initialFlow?.nodes ?? [], edges: initialFlow?.edges ?? [] },
        workflow_configurations: initialWorkflowConfigurations ?? {},
        template_context_variables: initialTemplateContextVariables ?? {},
    }), [initialWorkflowName, initialFlow, initialWorkflowConfigurations, initialTemplateContextVariables]);

    const {
        rfInstance,
        nodes,
        edges,
        isAddNodePanelOpen,
        workflowName,
        isDirty,
        workflowValidationErrors,
        templateContextVariables,
        setNodes,
        setEdges,
        setIsDirty,
        setIsAddNodePanelOpen,
        handleNodeSelect,
        saveWorkflow,
        workflowConfigurations,
        saveWorkflowConfigurations,
        saveTemplateContextVariables,
        defaultCallDispositions,
        defaultAnswerClassifierPrompt,
        textChatInactivityTimeoutConstraints,
        widgetTextDefaults,
        dictionary,
        saveDictionary,
        onConnect,
        onEdgesChange,
        onNodesChange,
        onDelete,
    } = useWorkflowState({
        readOnly: isCandidatePreview,
        initialWorkflowName,
        workflowId,
        initialFlow,
        initialTemplateContextVariables,
        initialWorkflowConfigurations,
        user,
        onVersionSaved,
    });

    // Single generic component for every node type. Seed with core node types
    // so the initial render is stable before specs load, then merge in any
    // spec-defined or already-present node types so plugin integrations like
    // Tuner render without extra React registrations.
    const nodeTypes = useMemo(() => {
        const typeNames = new Set<string>([
            ...Object.values(NodeType),
            ...specs.map((spec) => spec.name),
            ...nodes.map((node) => node.type),
            ...previewNodes.map((node) => node.type),
            ...(initialFlow?.nodes ?? []).map((node) => node.type),
        ]);
        return Object.fromEntries(
            Array.from(typeNames).map((typeName) => [typeName, GenericNode]),
        );
    }, [initialFlow?.nodes, nodes, previewNodes, specs]);

    // Derive hasDraft from the current version status
    const hasDraft = currentVersionStatus === "draft";

    // Fetch the first page of workflow versions, optionally forcing a refresh.
    // Pagination keeps the panel snappy when a workflow has accumulated a long
    // history — `workflow_json` is shipped per row, so loading hundreds at once
    // is expensive on the wire.
    const fetchVersions = useCallback(async (
        force = false,
        preserveActiveVersion = false,
    ) => {
        if (versionsFetched.current && !force) return;
        setVersionsLoading(true);
        try {
            const response = await getWorkflowVersionsApiV1WorkflowWorkflowIdVersionsGet({
                path: { workflow_id: workflowId },
                // Fetch one extra row so `hasMore` is exact. This also lets the
                // history panel reliably hide Compare on v1.
                query: { limit: VERSIONS_PAGE_SIZE + 1, offset: 0 },
            });
            if (response.error) {
                toast.error(detailFromError(response.error, "Failed to load version history"));
                return;
            }
            const data = response.data;
            if (data) {
                const page = data.slice(0, VERSIONS_PAGE_SIZE);
                setVersions(page);
                setVersionsHasMore(data.length > VERSIONS_PAGE_SIZE);
                // Set active version to draft if exists, else published.
                // Both live on the newest page so the first fetch always sees them.
                const current = page.find((v) => v.status === "draft") ?? page.find((v) => v.status === "published");
                if (current) {
                    setActiveVersionId((existingActiveVersionId) => (
                        preserveActiveVersion && existingActiveVersionId !== null
                            ? existingActiveVersionId
                            : current.id
                    ));
                    setCurrentVersionNumber(current.version_number);
                    setCurrentVersionStatus(current.status);
                }
                versionsFetched.current = true;
            }
        } finally {
            setVersionsLoading(false);
        }
    }, [workflowId]);

    const handleLoadMoreVersions = useCallback(async () => {
        if (versionsLoadingMore || !versionsHasMore) return;
        setVersionsLoadingMore(true);
        try {
            const response = await getWorkflowVersionsApiV1WorkflowWorkflowIdVersionsGet({
                path: { workflow_id: workflowId },
                query: { limit: VERSIONS_PAGE_SIZE + 1, offset: versions.length },
            });
            if (response.error) {
                toast.error(detailFromError(response.error, "Failed to load more versions"));
                return;
            }
            const data = response.data;
            if (data) {
                setVersions((prev) => [...prev, ...data.slice(0, VERSIONS_PAGE_SIZE)]);
                setVersionsHasMore(data.length > VERSIONS_PAGE_SIZE);
            }
        } finally {
            setVersionsLoadingMore(false);
        }
    }, [workflowId, versions.length, versionsLoadingMore, versionsHasMore]);

    const handleOpenVersionPanel = useCallback(() => {
        setIsVersionPanelOpen(true);
        void fetchVersions(false, true);
    }, [fetchVersions]);

    const handleCompareVersion = useCallback(async (version: WorkflowVersionResponse) => {
        if (comparingVersionId !== null) return;

        const selectedIndex = versions.findIndex((candidate) => candidate.id === version.id);
        if (selectedIndex < 0) {
            toast.error("That workflow version is no longer in the history list");
            return;
        }

        setComparingVersionId(version.id);
        try {
            // Fetch the selected row and the row immediately below it afresh.
            // The offset also handles comparisons that cross a pagination boundary.
            const response = await getWorkflowVersionsApiV1WorkflowWorkflowIdVersionsGet({
                path: { workflow_id: workflowId },
                query: { limit: 2, offset: selectedIndex },
            });
            if (response.error) {
                toast.error(detailFromError(response.error, "Failed to compare workflow versions"));
                return;
            }

            const pair = response.data;
            const selectedVersion = pair?.[0];
            const previousVersion = pair?.[1];

            // A new draft may have been inserted since the panel opened, shifting
            // every offset. Refresh instead of showing a mismatched comparison.
            if (!selectedVersion || selectedVersion.id !== version.id) {
                await fetchVersions(true, true);
                toast.error("Version history changed. Please try the comparison again.");
                return;
            }
            if (
                !previousVersion ||
                previousVersion.version_number >= selectedVersion.version_number
            ) {
                toast.error(`v${selectedVersion.version_number} has no previous version`);
                return;
            }

            // Keep the panel's cached copies in sync with the fresh pair so they
            // remain current when the dialog closes and history reopens.
            setVersions((currentVersions) => currentVersions.map((currentVersion) => (
                pair?.find((freshVersion) => freshVersion.id === currentVersion.id)
                ?? currentVersion
            )));
            setVersionDiffPair({ previousVersion, selectedVersion });
            setIsVersionPanelOpen(false);
        } catch {
            toast.error("Failed to compare workflow versions");
        } finally {
            setComparingVersionId(null);
        }
    }, [comparingVersionId, fetchVersions, versions, workflowId]);

    const handleVersionDiffOpenChange = useCallback((open: boolean) => {
        if (open) return;
        setVersionDiffPair(null);
        setIsVersionPanelOpen(true);
    }, []);

    const navigateToVersion = useCallback((version: number | null) => {
        const query = new URLSearchParams(searchParams.toString());
        if (version === null) query.delete('version');
        else query.set('version', String(version));
        const suffix = query.toString();
        router.push(`/workflow/${workflowId}${suffix ? `?${suffix}` : ''}`, { scroll: false });
        setIsVersionPanelOpen(false);
    }, [router, searchParams, workflowId]);

    const handleSelectVersion = useCallback((version: WorkflowVersionResponse) => {
        const isCurrentVersion = version.status === 'draft'
            || (version.status === 'published' && !hasDraft);
        // Opening a version remounts the editor, so unsaved settings would be lost.
        confirmNavigate(() => navigateToVersion(isCurrentVersion ? null : version.version_number));
    }, [confirmNavigate, hasDraft, navigateToVersion]);

    // Determine if we are viewing a historical (non-current) version.
    // The "current" version is the draft if one exists, otherwise the published version.
    // Anything else (archived, or published while a draft exists) is historical.
    const isViewingHistoricalVersion = useMemo(() => {
        // Explicit version links open a saved snapshot, including the latest release.
        if (initialSelectedVersion?.id === activeVersionId) return true;
        if (!activeVersionId || versions.length === 0) return false;
        const activeVersion = versions.find((v) => v.id === activeVersionId);
        if (!activeVersion) return false;
        if (activeVersion.status === "draft") return false;
        if (activeVersion.status === "published" && !hasDraft) return false;
        return true;
    }, [activeVersionId, versions, hasDraft, initialSelectedVersion]);

    const canvasReadOnly = isViewingHistoricalVersion || isCandidatePreview;
    const handleTargetChange = useCallback((snapshot: Snapshot, canReload: boolean) => {
        if (isViewingHistoricalVersion || !onDraftAdopted) return;
        const changed = savedSnapshotKey({
            name: snapshot.name,
            workflow_json: { nodes: snapshot.workflow_json.nodes ?? [], edges: snapshot.workflow_json.edges ?? [] },
            workflow_configurations: snapshot.workflow_configurations ?? {},
            template_context_variables: snapshot.template_context_variables ?? {},
        }) !== savedDraftKey;
        setExternalDraftChanged(changed);
        setWorkbenchCanReload(canReload);
        if (changed && canReload && !isDirty && !hasDirtyChanges && !settingsSaving && !reloadingDraft.current) {
            reloadingDraft.current = true;
            onDraftAdopted();
        }
    }, [isViewingHistoricalVersion, savedDraftKey, isDirty, hasDirtyChanges, settingsSaving, onDraftAdopted]);

    useEffect(() => {
        if (!isViewingHistoricalVersion) {
            return;
        }
        setActiveRuntimeNodeId(null);
    }, [isViewingHistoricalVersion]);

    // Return to the draft version, creating one from published if needed
    const handleBackToDraft = useCallback(async () => {
        if (initialSelectedVersion) {
            navigateToVersion(null);
            return;
        }
        const existingDraft = versions.find((v) => v.status === "draft");
        if (existingDraft) {
            handleSelectVersion(existingDraft);
            return;
        }

        // No draft exists — ask the backend to create one from published
        const response = await createWorkflowDraftApiV1WorkflowWorkflowIdCreateDraftPost({
            path: { workflow_id: workflowId },
        });
        const draft = response.data;
        if (draft) {
            setCurrentVersionNumber(draft.version_number);
            setCurrentVersionStatus(draft.status);
            // Load draft nodes/edges via the Zustand store (same approach as handleSelectVersion)
            const flowNodes = (draft.workflow_json?.nodes ?? []) as FlowNode[];
            const flowEdges = (draft.workflow_json?.edges ?? []) as FlowEdge[];
            setNodes(flowNodes);
            setEdges(flowEdges);
            setActiveVersionId(draft.id);
            setIsDirty(false);
            // Refresh the version list so the new draft appears
            fetchVersions(true);
        }
    }, [versions, handleSelectVersion, workflowId, setNodes, setEdges, setIsDirty, fetchVersions, initialSelectedVersion, navigateToVersion]);

    // After a successful publish, refresh the version list and update status
    const handlePublished = useCallback(() => {
        setCurrentVersionStatus("published");
        fetchVersions(true);
    }, [fetchVersions]);

    // A save can turn a published workflow into a draft. If the versions list
    // has been fetched (user interacted with versioning or published), refresh
    // it so that activeVersionId points to the correct version: otherwise it
    // would still point to the old published version, making
    // isViewingHistoricalVersion true and locking the editor read-only.
    const applySavedVersion = useCallback(async (version: SavedVersion) => {
        if (versionsFetched.current) {
            await fetchVersions(true);
            return;
        }
        if (version.versionNumber != null) setCurrentVersionNumber(version.versionNumber);
        if (version.versionStatus) setCurrentVersionStatus(version.versionStatus);
    }, [fetchVersions]);
    applySavedVersionRef.current = (version) => { void applySavedVersion(version); };

    // Compute version label for the header.
    // Uses currentVersionNumber/Status which update immediately from save responses,
    // falling back to the versions list for history navigation.
    const activeVersionLabel = useMemo(() => {
        // When viewing a version from the history panel, use the versions list
        if (activeVersionId) {
            const v = versions.find((ver) => ver.id === activeVersionId)
                ?? (initialSelectedVersion?.id === activeVersionId ? initialSelectedVersion : undefined);
            if (v) {
                const statusSuffix = v.status === "draft" ? " (Draft)" : v.status === "published" ? " (Published)" : "";
                return `v${v.version_number}${statusSuffix}`;
            }
        }
        // Otherwise use the immediately-available version info from save responses
        if (currentVersionNumber != null) {
            const statusSuffix = currentVersionStatus === "draft" ? " (Draft)" : currentVersionStatus === "published" ? " (Published)" : "";
            return `v${currentVersionNumber}${statusSuffix}`;
        }
        return undefined;
    }, [activeVersionId, versions, currentVersionNumber, currentVersionStatus, initialSelectedVersion]);

    const testerDisabledReason = useMemo(() => {
        if (isViewingHistoricalVersion) {
            return "Return to the draft before starting a new test session.";
        }
        if (isDirty) {
            return "Save the latest draft before testing so the session uses the workflow you are looking at.";
        }
        if (settingsSaving) {
            return "Wait for the model settings to finish saving so the session uses them.";
        }
        if (workflowValidationErrors.length > 0) {
            return "Resolve the current validation errors before starting another test.";
        }
        return null;
    }, [isDirty, isViewingHistoricalVersion, settingsSaving, workflowValidationErrors.length]);

    const handleOpenTester = useCallback(() => {
        setWorkbenchOpen(false);
        if (window.innerWidth >= 1280) {
            setIsTesterRailOpen(true);
            return;
        }
        setIsTesterSheetOpen(true);
    }, [setWorkbenchOpen]);

    const shouldShowWebCallOnboarding = useMemo(() => {
        return (initialTotalRuns ?? 0) === 0 && !hasCompletedAction('web_call_started');
    }, [hasCompletedAction, initialTotalRuns]);

    useEffect(() => {
        const syncViewport = () => {
            setIsDesktopViewport(window.innerWidth >= 1280);
        };

        syncViewport();
        window.addEventListener('resize', syncViewport);
        return () => window.removeEventListener('resize', syncViewport);
    }, []);

    useEffect(() => {
        if (workbenchOpen || hasAutoOpenedTester.current || !openTesterOnLoad || !shouldShowWebCallOnboarding || testerDisabledReason) {
            return;
        }

        handleOpenTester();
        hasAutoOpenedTester.current = true;
    }, [handleOpenTester, openTesterOnLoad, shouldShowWebCallOnboarding, testerDisabledReason, workbenchOpen]);

    // Fetch documents, tools, and recordings once for the entire workflow
    useEffect(() => {
        const fetchData = async () => {
            try {
                // Fetch documents
                const documentsResponse = await listDocumentsApiV1KnowledgeBaseDocumentsGet({
                    query: { limit: 100 },
                });
                if (documentsResponse.data) {
                    setDocuments(documentsResponse.data.documents);
                }

                // Fetch tools
                const toolsResponse = await listToolsApiV1ToolsGet({});
                if (toolsResponse.data) {
                    setTools(toolsResponse.data);
                }

                // Fetch org-level recordings
                try {
                    const recordingsResponse = await listRecordingsApiV1WorkflowRecordingsGet({
                        query: {},
                    });
                    if (recordingsResponse.data) {
                        setRecordings(recordingsResponse.data.recordings);
                    }
                } catch {
                    // Recordings API may not be available yet; silently ignore
                }
            } catch (error) {
                console.error('Failed to fetch documents and tools:', error);
            }
        };

        fetchData();
    }, [workflowId]);

    // Memoize defaultEdgeOptions to prevent unnecessary re-renders
    const defaultEdgeOptions = useMemo(() => ({
        animated: true,
        type: "custom"
    }), []);

    const displayNodes = useMemo(
        () =>
            (isCandidatePreview ? previewNodes : nodes).map((node) =>
                !isCandidatePreview && node.id === activeRuntimeNodeId
                    ? {
                          ...node,
                          data: {
                              ...node.data,
                              runtime_active: true,
                          },
                      }
                    : node,
            ),
        [activeRuntimeNodeId, nodes, previewNodes, isCandidatePreview],
    );

    const handleRuntimeNodeTransition = useCallback(
        (transition: WorkflowRuntimeNodeTransition) => {
            const nodeId = transition.nodeId;
            const instance = rfInstance.current;
            if (!nodeId || !instance) {
                return;
            }

            setActiveRuntimeNodeId(nodeId);

            if (!instance.viewportInitialized) {
                return;
            }

            void instance.fitView({
                nodes: [{ id: nodeId }],
                duration: 350,
                padding: 0.45,
                maxZoom: 0.9,
            });
        },
        [rfInstance],
    );

    // Guard saveWorkflow so it's a no-op when viewing a historical version.
    // This is the single safety net that covers every save path: header button,
    // Cmd+S, node edit dialogs, stale doc/tool cleanup, etc.
    // Uses the save response to immediately update version label and hasDraft.
    const guardedSaveWorkflow = useCallback(async (updateWorkflowDefinition?: boolean) => {
        if (canvasReadOnly) return;
        const result = await saveWorkflow(updateWorkflowDefinition);
        if (result) await applySavedVersion(result);
    }, [saveWorkflow, canvasReadOnly, applySavedVersion]);

    const renameWorkflow = useCallback(async (newName: string) => {
        // The header doesn't render the pencil until the page has mounted with
        // initial data, so workflowConfigurations is non-null by the time this
        // runs. Throw rather than silently sending fallback workflow configurations,
        // which would overwrite the saved server-side config.
        if (!workflowConfigurations) {
            throw new Error("Workflow configurations not loaded");
        }
        await saveWorkflowConfigurations(workflowConfigurations, newName);
    }, [saveWorkflowConfigurations, workflowConfigurations]);

    const updateTool = useCallback(
        (toolUuid: string, updater: (tool: ToolResponse) => ToolResponse) => {
            setTools((prev) =>
                prev?.map((tool) =>
                    tool.tool_uuid === toolUuid ? updater(tool) : tool,
                ),
            );
        },
        [],
    );

    // Memoize the context value to prevent unnecessary re-renders
    const workflowContextValue = useMemo(() => ({
        saveWorkflow: guardedSaveWorkflow,
        documents,
        tools,
        updateTool,
        recordings,
        readOnly: canvasReadOnly,
    }), [
        guardedSaveWorkflow,
        documents,
        tools,
        updateTool,
        recordings,
        canvasReadOnly,
    ]);

    return (
        <WorkflowProvider value={workflowContextValue}>
            <div className="flex flex-col h-full min-h-0 min-w-0">
                <HireExpertNudge workflowId={workflowId} />
                {/* New Workflow Editor Header */}
                <WorkflowEditorHeader
                    workflowName={candidatePreview && isCandidatePreview ? candidatePreview.name : workflowName}
                    isDirty={isDirty}
                    workflowValidationErrors={isCandidatePreview ? [] : workflowValidationErrors}
                    rfInstance={rfInstance}
                    workflowId={workflowId}
                    workflowUuid={workflowUuid}
                    saveWorkflow={guardedSaveWorkflow}
                    user={user}
                    onPhoneCallClick={() => setIsPhoneCallDialogOpen(true)}
                    onTestAgentClick={handleOpenTester}
                    onWorkbenchClick={() => {
                        setIsTesterSheetOpen(false);
                        setWorkbenchOpen(!workbenchOpen);
                    }}
                    workbenchOpen={workbenchOpen}
                    isCandidatePreview={isCandidatePreview}
                    onHistoryClick={handleOpenVersionPanel}
                    activeVersionLabel={activeVersionLabel}
                    isViewingHistoricalVersion={isViewingHistoricalVersion}
                    onBackToDraft={handleBackToDraft}
                    hasDraft={hasDraft}
                    savingSettings={settingsSaving}
                    onPublished={handlePublished}
                    renameWorkflow={renameWorkflow}
                />
                {externalDraftChanged && (
                    <div role="status" className="flex items-center justify-between gap-3 border-b bg-muted px-4 py-2 text-sm">
                        <span>草稿已在其他页面更新。请先复制保留本地修改和未发送内容，并结束语音，再同步最新草稿。</span>
                        <Button size="sm" variant="outline" disabled={!workbenchCanReload} onClick={() => confirmNavigate(() => onDraftAdopted?.())}>
                            同步最新草稿
                        </Button>
                    </div>
                )}

                {/* Workflow Canvas */}
                <div className="flex-1 min-h-0">
                    <div className="relative flex h-full min-w-0">
                        <div className="relative min-w-0 flex-1">
                            <ReactFlow
                                key={isCandidatePreview ? candidatePreview.id : activeVersionId ?? 'current'}
                                nodes={displayNodes}
                                edges={isCandidatePreview ? previewEdges : edges}
                                onNodesChange={isCandidatePreview ? changes => setPreviewNodes(current => applyNodeChanges(changes, current)) : onNodesChange}
                                onEdgesChange={isCandidatePreview ? changes => setPreviewEdges(current => applyEdgeChanges(changes, current)) : onEdgesChange}
                                onDelete={canvasReadOnly ? undefined : onDelete}
                                nodeTypes={nodeTypes}
                                edgeTypes={edgeTypes}
                                onConnect={canvasReadOnly ? undefined : onConnect}
                                minZoom={0.05}
                                onInit={(instance) => {
                                    rfInstance.current = instance;
                                    // Center the workflow on load
                                    setTimeout(() => {
                                        instance.fitView({ padding: 0.2, duration: 200, maxZoom: 0.75 });
                                    }, 0);
                                }}
                                defaultEdgeOptions={defaultEdgeOptions}
                                defaultViewport={initialFlow?.viewport}
                                nodesDraggable={!canvasReadOnly}
                                nodesConnectable={!canvasReadOnly}
                                edgesReconnectable={!canvasReadOnly}
                                zoomOnDoubleClick={false}
                                deleteKeyCode={canvasReadOnly ? null : "Backspace"}
                            >
                                <Background
                                    variant={BackgroundVariant.Dots}
                                    gap={16}
                                    size={1}
                                    color="#94a3b8"
                                />

                                {/* Top-right controls (hidden when viewing history) */}
                                {!canvasReadOnly && (
                                    <Panel position="top-right">
                                        <TooltipProvider>
                                            <div className="flex flex-col gap-2">
                                                <Tooltip>
                                                    <TooltipTrigger asChild>
                                                        <Button
                                                            variant="default"
                                                            size="icon"
                                                            onClick={() => setIsAddNodePanelOpen(true)}
                                                            className="shadow-md hover:shadow-lg"
                                                        >
                                                            <Plus className="h-4 w-4" />
                                                        </Button>
                                                    </TooltipTrigger>
                                                    <TooltipContent side="left">
                                                        <p>Add node</p>
                                                    </TooltipContent>
                                                </Tooltip>
                                            </div>
                                        </TooltipProvider>
                                    </Panel>
                                )}
                            </ReactFlow>

                            {isCandidatePreview && (
                                <div role="status" className="absolute top-4 left-4 right-4 z-10 rounded-lg border border-border bg-background/95 px-4 py-3 text-sm shadow-sm">
                                    候选版本只读预览 · 应用到草稿后可编辑
                                </div>
                            )}

                            {/* Bottom-left controls - horizontal layout with custom buttons */}
                            <div className="absolute bottom-12 left-8 z-10 flex gap-2">
                                <TooltipProvider>
                                    <Tooltip>
                                        <TooltipTrigger asChild>
                                            <Button
                                                variant="outline"
                                                size="icon"
                                                aria-label="Zoom in"
                                                onClick={() => rfInstance.current?.zoomIn()}
                                                className="bg-white shadow-sm hover:shadow-md h-8 w-8"
                                            >
                                                <Plus className="h-4 w-4" />
                                            </Button>
                                        </TooltipTrigger>
                                        <TooltipContent side="top">
                                            <p>Zoom in</p>
                                        </TooltipContent>
                                    </Tooltip>

                                    <Tooltip>
                                        <TooltipTrigger asChild>
                                            <Button
                                                variant="outline"
                                                size="icon"
                                                aria-label="Zoom out"
                                                onClick={() => rfInstance.current?.zoomOut()}
                                                className="bg-white shadow-sm hover:shadow-md h-8 w-8"
                                            >
                                                <Minus className="h-4 w-4" />
                                            </Button>
                                        </TooltipTrigger>
                                        <TooltipContent side="top">
                                            <p>Zoom out</p>
                                        </TooltipContent>
                                    </Tooltip>

                                    <Tooltip>
                                        <TooltipTrigger asChild>
                                            <Button
                                                variant="outline"
                                                size="icon"
                                                aria-label="Fit view"
                                                onClick={() => rfInstance.current?.fitView()}
                                                className="bg-white shadow-sm hover:shadow-md h-8 w-8"
                                            >
                                                <Maximize2 className="h-4 w-4" />
                                            </Button>
                                        </TooltipTrigger>
                                        <TooltipContent side="top">
                                            <p>Fit view</p>
                                        </TooltipContent>
                                    </Tooltip>

                                    {!isViewingHistoricalVersion && (
                                        <Tooltip>
                                            <TooltipTrigger asChild>
                                                <Button
                                                    variant="outline"
                                                    size="icon"
                                                    aria-label="Tidy up"
                                                    onClick={() => {
                                                        if (isCandidatePreview) {
                                                            setPreviewNodes(layoutNodes(previewNodes, previewEdges, 'TB', rfInstance));
                                                        } else {
                                                            setNodes(layoutNodes(nodes, edges, 'TB', rfInstance));
                                                            setIsDirty(true);
                                                        }
                                                    }}
                                                    className="bg-white shadow-sm hover:shadow-md h-8 w-8"
                                                >
                                                    <BrushCleaning className="h-4 w-4" />
                                                </Button>
                                            </TooltipTrigger>
                                            <TooltipContent side="top">
                                                <p>Tidy Up</p>
                                            </TooltipContent>
                                        </Tooltip>
                                    )}
                                </TooltipProvider>
                            </div>

                            {/* View switcher (Canvas / Model / Variables / Settings); the other views cover the canvas */}
                            {!isViewingHistoricalVersion && (
                                <div className={isCandidatePreview ? "hidden" : undefined}>
                                <EditorSetup
                                    view={editorView}
                                    onViewChange={setEditorView}
                                    onSavingChange={setSettingsSaving}
                                    workflowId={workflowId}
                                    workflowName={workflowName}
                                    workflowUuid={workflowUuid}
                                    nodes={nodes}
                                    edges={edges}
                                    workflowConfigurations={workflowConfigurations}
                                    defaultCallDispositions={defaultCallDispositions}
                                    defaultAnswerClassifierPrompt={defaultAnswerClassifierPrompt}
                                    textChatInactivityTimeoutConstraints={textChatInactivityTimeoutConstraints}
                                    widgetTextDefaults={widgetTextDefaults}
                                    templateContextVariables={templateContextVariables}
                                    dictionary={dictionary}
                                    saveWorkflowConfigurations={saveWorkflowConfigurations}
                                    saveTemplateContextVariables={saveTemplateContextVariables}
                                    saveDictionary={saveDictionary}
                                />
                                </div>
                            )}
                        </div>

                        {workbenchOpen && (
                            <aside aria-label="Agent 工作台" className="absolute inset-y-0 right-0 z-20 h-full w-full border-l border-border bg-background shadow-xl md:static md:w-[440px] md:shrink-0 md:shadow-none 2xl:w-[480px]">
                                <AgentWorkbench
                                    key={`${workflowId}:${searchParams.get('session') || 'latest'}`}
                                    workflowId={workflowId}
                                    sessionId={searchParams.get('session') || undefined}
                                    onPreviewChange={handlePreviewChange}
                                    onSessionReady={handleSessionReady}
                                    onAdopted={onDraftAdopted}
                                    onTargetChange={handleTargetChange}
                                    onClose={() => setWorkbenchOpen(false)}
                                    adoptDisabledReason={isDirty || hasDirtyChanges || settingsSaving
                                        ? "草稿有未保存的修改，请先返回当前草稿保存，再应用候选版本。"
                                        : isViewingHistoricalVersion ? "请先返回当前草稿，再应用候选版本。" : undefined}
                                />
                            </aside>
                        )}
                        {!workbenchOpen && isTesterRailOpen && (
                            <aside className="hidden h-full w-[400px] shrink-0 border-l border-border xl:block">
                                <WorkflowTesterPanel
                                    workflowId={workflowId}
                                    initialContextVariables={templateContextVariables}
                                    disabled={testerDisabledReason !== null}
                                    disabledReason={testerDisabledReason}
                                    showWebCallOnboarding={shouldShowWebCallOnboarding}
                                    isVisible={isDesktopViewport}
                                    onClose={() => setIsTesterRailOpen(false)}
                                    onRuntimeNodeTransition={handleRuntimeNodeTransition}
                                />
                            </aside>
                        )}
                    </div>

                    <Sheet open={isTesterSheetOpen} onOpenChange={setIsTesterSheetOpen}>
                        <SheetContent side="right" className="w-full max-w-none p-0 sm:max-w-xl xl:hidden">
                            <WorkflowTesterPanel
                                workflowId={workflowId}
                                initialContextVariables={templateContextVariables}
                                disabled={testerDisabledReason !== null}
                                disabledReason={testerDisabledReason}
                                showWebCallOnboarding={shouldShowWebCallOnboarding}
                                isVisible={isTesterSheetOpen}
                                onRuntimeNodeTransition={handleRuntimeNodeTransition}
                            />
                        </SheetContent>
                    </Sheet>
                </div>

                <AddNodePanel
                    isOpen={isAddNodePanelOpen && !canvasReadOnly}
                    onNodeSelect={handleNodeSelect}
                    onClose={() => setIsAddNodePanelOpen(false)}
                    nodes={nodes}
                />

                <VersionHistoryPanel
                    workflowId={workflowId}
                    isOpen={isVersionPanelOpen}
                    onClose={() => setIsVersionPanelOpen(false)}
                    versions={versions}
                    loading={versionsLoading}
                    activeVersionId={activeVersionId}
                    onSelectVersion={handleSelectVersion}
                    onCompareVersion={handleCompareVersion}
                    comparingVersionId={comparingVersionId}
                    hasMore={versionsHasMore}
                    loadingMore={versionsLoadingMore}
                    onLoadMore={handleLoadMoreVersions}
                    onVersionUpdated={(metadata) => setVersions((previous) => previous.map((version) => (
                        version.id === metadata.id ? { ...version, ...metadata } : version
                    )))}
                />

                {versionDiffPair && (
                    <WorkflowVersionDiffDialog
                        open
                        onOpenChange={handleVersionDiffOpenChange}
                        previousVersion={versionDiffPair.previousVersion}
                        selectedVersion={versionDiffPair.selectedVersion}
                    />
                )}

                <PhoneCallDialog
                    open={isPhoneCallDialogOpen}
                    onOpenChange={setIsPhoneCallDialogOpen}
                    workflowId={workflowId}
                    user={user}
                />
            </div>
        </WorkflowProvider>
    );
}

// Memoize the component to prevent unnecessary re-renders when parent re-renders
export default React.memo(RenderWorkflow, (prevProps, nextProps) => {
    // Only re-render if these specific props change
    return (
        prevProps.workflowId === nextProps.workflowId &&
        prevProps.initialWorkflowName === nextProps.initialWorkflowName &&
        prevProps.user.id === nextProps.user.id
        // Note: We intentionally don't compare initialFlow, initialTemplateContextVariables,
        // or initialWorkflowConfigurations because they're only used for initialization
    );
});

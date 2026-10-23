import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, expect, it, vi } from 'vitest';

import type { WorkflowVersionResponse } from '@/client/types.gen';
import type { Candidate } from "@/components/agent-workbench/types";
import type { FlowEdge } from '@/components/flow/types';

import RenderWorkflow from './RenderWorkflow';

const mocks = vi.hoisted(() => ({
    query: 'version=3&source=campaign',
    push: vi.fn(),
    replace: vi.fn(),
    versions: vi.fn(),
    updateMetadata: vi.fn(),
    save: vi.fn(),
    workbench: vi.fn(),
    flow: vi.fn(),
    state: {
        rfInstance: { current: null },
        nodes: [], edges: [] as FlowEdge[], workflowValidationErrors: [],
        workflowName: 'Agent', isDirty: false, templateContextVariables: {},
        setNodes: vi.fn(), setEdges: vi.fn(), setIsDirty: vi.fn(), setIsAddNodePanelOpen: vi.fn(),
    },
}));
vi.mock('@xyflow/react/dist/style.css', () => ({}));
vi.mock('next/navigation', () => ({
    useRouter: () => ({ push: mocks.push, replace: mocks.replace }),
    useSearchParams: () => new URLSearchParams(mocks.query),
}));
vi.mock('@/client', () => ({
    getWorkflowVersionsApiV1WorkflowWorkflowIdVersionsGet: mocks.versions,
    createWorkflowDraftApiV1WorkflowWorkflowIdCreateDraftPost: vi.fn(),
    listDocumentsApiV1KnowledgeBaseDocumentsGet: async () => ({ data: { documents: [] } }),
    listToolsApiV1ToolsGet: async () => ({ data: [] }),
    listRecordingsApiV1WorkflowRecordingsGet: async () => ({ data: { recordings: [] } }),
}));
vi.mock('@/client/sdk.gen', () => ({
    updateWorkflowVersionMetadataApiV1WorkflowWorkflowIdVersionsDefinitionIdMetadataPatch: mocks.updateMetadata,
}));
vi.mock('./hooks/useWorkflowState', () => ({ useWorkflowState: () => ({ ...mocks.state, saveWorkflow: mocks.save }) }));
vi.mock('@/components/agent-workbench/AgentWorkbench', () => ({ AgentWorkbench: (props: {
    onPreviewChange: (candidate?: Candidate) => void; onClose: () => void;
    onAdopted: () => void; adoptDisabledReason?: string;
}) => {
    mocks.workbench(props);
    return <div>
        <button onClick={() => props.onPreviewChange({id: 'candidate-1', name: 'Candidate', summary: '', code: '', fingerprint: 'abc', snapshot: {
            name: 'Candidate', workflow_configurations: {}, template_context_variables: {},
            workflow_json: {nodes: [{id: 'c1', type: 'agentNode', position: {x: 0, y: 0}, data: {name: 'Candidate node'}}], edges: []},
        }})}>Preview candidate</button>
        <button onClick={() => props.onPreviewChange(undefined)}>Current draft</button>
        <button onClick={props.onClose}>Close workbench</button>
        <button disabled={!!props.adoptDisabledReason} onClick={props.onAdopted}>Apply candidate</button>
    </div>;
} }));
vi.mock('@/components/flow/renderer' , () => ({ useNodeSpecs: () => ({ specs: [] }) }));
vi.mock('@/context/OnboardingContext', () => ({ useOnboarding: () => ({ hasCompletedAction: () => true }) }));
vi.mock('@/components/lead-forms/HireExpertNudge', () => ({ HireExpertNudge: () => null }));
vi.mock('@/components/flow/AddNodePanel', () => ({ default: () => null }));
vi.mock('@/components/flow/edges/CustomEdge', () => ({ default: () => null }));
vi.mock('@/components/flow/nodes/GenericNode', () => ({ GenericNode: () => null }));
vi.mock('./components/PhoneCallDialog', () => ({ PhoneCallDialog: () => null }));
vi.mock('./components/EditorSetup', () => ({
    isEditorView: (value: string | null) => value === 'model',
    EditorSetup: ({ view, onViewChange }: { view: string; onViewChange: (view: string) => void }) => (
        <button onClick={() => onViewChange(view === 'model' ? 'canvas' : 'model')}>View: {view}</button>
    ),
}));
vi.mock('./components/WorkflowVersionDiffDialog', () => ({ WorkflowVersionDiffDialog: () => null }));
vi.mock('./components/WorkflowTesterPanel', () => ({
    WorkflowTesterPanel: ({ disabled }: { disabled: boolean }) => <div data-testid="tester" data-disabled={disabled} />,
}));
vi.mock('@xyflow/react', () => ({
    Background: () => null,
    BackgroundVariant: { Dots: 'dots' },
    Panel: ({ children }: { children: ReactNode }) => children,
    ReactFlow: (props: { nodesDraggable: boolean; children: ReactNode }) => { mocks.flow(props); return <div data-testid="canvas" data-editable={props.nodesDraggable}>{props.children}</div>; },
}));
vi.mock('./components/WorkflowEditorHeader', () => ({
    WorkflowEditorHeader: ({ activeVersionLabel, onHistoryClick, onBackToDraft, saveWorkflow }: {
        activeVersionLabel: string;
        onHistoryClick: () => void;
        onBackToDraft: () => void;
        saveWorkflow: () => void;
    }) => <div>
        <span data-testid="version-label">{activeVersionLabel}</span>
        <button onClick={onHistoryClick}>History</button>
        <button onClick={onBackToDraft}>Back to draft</button>
        <button onClick={saveWorkflow}>Save</button>
    </div>,
}));

const version = (number: number, status: string): WorkflowVersionResponse => ({
    id: 100 + number, version_number: number, status,
    workflow_json: { nodes: [], edges: [] }, created_at: '2026-01-01T00:00:00Z',
});

beforeEach(() => {
    vi.clearAllMocks();
    mocks.state.isDirty = false;
    mocks.state.edges = [];
    mocks.query = 'version=3&source=campaign';
    mocks.versions.mockResolvedValue({ data: [version(21, 'draft'), version(20, 'published')] });
});

it.each(['archived', 'published'])('opens a linked %s version read-only before history loads', async status => {
    render(<RenderWorkflow workflowId={12} initialWorkflowName="Agent" user={{ id: '1' }} initialSelectedVersion={version(3, status)} />);
    expect(screen.getByTestId('canvas').getAttribute('data-editable')).toBe('false');
    expect(screen.getByTestId('tester').getAttribute('data-disabled')).toBe('true');
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(mocks.save).not.toHaveBeenCalled();
    expect(mocks.versions).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'History' }));
    await screen.findByRole('button', { name: /^v20/ });
    expect(screen.getByTestId('version-label').textContent).toContain('v3');
    expect(screen.getByTestId('canvas').getAttribute('data-editable')).toBe('false');
});

it('updates version metadata in history while preserving the selected historical version', async () => {
    mocks.updateMetadata.mockResolvedValueOnce({ data: { id: 120, version_name: 'Updated release', change_description: 'Corrected notes' } });
    render(<RenderWorkflow workflowId={12} initialWorkflowName="Agent" user={{ id: '1' }} initialSelectedVersion={version(3, 'archived')} />);
    fireEvent.click(screen.getByRole('button', { name: 'History' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Edit details for v20' }));
    fireEvent.change(screen.getByLabelText('Version name (optional)'), { target: { value: 'Updated release' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
    await screen.findByText('Updated release');
    expect(screen.getByText('Corrected notes')).toBeTruthy();
    expect(screen.getByTestId('version-label').textContent).toContain('v3');
    expect(mocks.push).not.toHaveBeenCalled();
    expect(mocks.versions).toHaveBeenCalledOnce();
});

it('updates shareable URLs from history and clears only the version when returning to the draft', async () => {
    render(<RenderWorkflow workflowId={12} initialWorkflowName="Agent" user={{ id: '1' }} initialSelectedVersion={version(3, 'archived')} />);
    fireEvent.click(screen.getByRole('button', { name: 'History' }));
    fireEvent.click(await screen.findByRole('button', { name: /^v20/ }));
    expect(mocks.push).toHaveBeenCalledWith('/workflow/12?version=20&source=campaign', { scroll: false });
    fireEvent.click(screen.getByRole('button', { name: 'Back to draft' }));
    await waitFor(() => expect(mocks.push).toHaveBeenCalledWith('/workflow/12?source=campaign', { scroll: false }));
});

it.each(['current', 'historical'])('selects the editable published version from the %s view when no draft exists', async view => {
    mocks.query = view === 'current' ? 'source=campaign' : 'version=3&source=campaign';
    mocks.versions.mockResolvedValue({ data: [version(20, 'published'), version(3, 'archived')] });
    render(<RenderWorkflow
        workflowId={12}
        initialWorkflowName="Agent"
        initialVersionNumber={20}
        initialVersionStatus="published"
        initialSelectedVersion={view === 'historical' ? version(3, 'archived') : undefined}
        user={{ id: '1' }}
    />);

    fireEvent.click(screen.getByRole('button', { name: 'History' }));
    fireEvent.click(await screen.findByRole('button', { name: /^v20/ }));

    expect(mocks.push).toHaveBeenCalledWith('/workflow/12?source=campaign', { scroll: false });
});

it.each([
    ['source=campaign', 'canvas', '/workflow/12?source=campaign&view=model'],
    ['source=campaign&view=model', 'model', '/workflow/12?source=campaign'],
])('keeps the editor view in the URL (%s)', (query, view, next) => {
    mocks.query = query;
    mocks.versions.mockResolvedValue({ data: [version(20, 'draft')] });
    render(<RenderWorkflow workflowId={12} initialWorkflowName="Agent" initialVersionNumber={20} initialVersionStatus="draft" user={{ id: '1' }} />);

    fireEvent.click(screen.getByRole('button', { name: `View: ${view}` }));

    expect(mocks.replace).toHaveBeenCalledWith(next, { scroll: false });
});

it('previews candidates on the original canvas without changing the draft, then restores editing', () => {
    mocks.query = 'workbench=1&session=existing';
    render(<RenderWorkflow workflowId={12} initialWorkflowName="Agent" user={{id: '1'}} />);
    expect(screen.queryByTestId('tester')).toBeNull();
    expect(mocks.workbench.mock.lastCall?.[0].sessionId).toBe('existing');
    fireEvent.click(screen.getByText('Preview candidate'));
    expect(screen.getAllByTestId('canvas')).toHaveLength(1);
    expect(screen.getByTestId('canvas').getAttribute('data-editable')).toBe('false');
    const flow = mocks.flow.mock.lastCall?.[0];
    expect(flow.nodes[0].type).toBe('agentNode');
    expect(flow.nodeTypes.agentNode).toBeDefined();
    expect(flow.edgeTypes.custom).toBeDefined();
    expect(flow.deleteKeyCode).toBeNull();
    expect(flow.onConnect).toBeUndefined();
    fireEvent.click(screen.getByText('Save'));
    expect(mocks.save).not.toHaveBeenCalled();
    expect(mocks.state.setNodes).not.toHaveBeenCalled();
    expect(mocks.state.setEdges).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText('Current draft'));
    expect(screen.getByTestId('canvas').getAttribute('data-editable')).toBe('true');
    expect(mocks.flow.mock.lastCall?.[0].nodes).toEqual(mocks.state.nodes);
    fireEvent.click(screen.getByText('Close workbench'));
    expect(mocks.replace).toHaveBeenCalledWith('/workflow/12?session=existing', {scroll: false});
});

it('blocks adoption over unsaved draft changes, and reloads the editor after adoption', () => {
    mocks.query = 'workbench=1';
    mocks.state.isDirty = true;
    const onDraftAdopted = vi.fn();
    const view = render(<RenderWorkflow workflowId={12} initialWorkflowName="Agent" user={{id:'1'}} onDraftAdopted={onDraftAdopted} />);
    fireEvent.click(screen.getByText('Apply candidate'));
    expect(onDraftAdopted).not.toHaveBeenCalled();
    expect(mocks.workbench.mock.lastCall?.[0].adoptDisabledReason).toContain('未保存');
    mocks.state.isDirty = false;
    view.rerender(<RenderWorkflow workflowId={12} initialWorkflowName="Saved Agent" user={{id:'1'}} onDraftAdopted={onDraftAdopted} />);
    fireEvent.click(screen.getByText('Apply candidate'));
    expect(onDraftAdopted).toHaveBeenCalledOnce();
});

it('previews every legacy parallel edge with a unique identity without mutating the candidate', () => {
    mocks.query = 'workbench=1';
    render(<RenderWorkflow workflowId={12} initialWorkflowName="Agent" user={{id: '1'}} />);
    const edges = [
        {id: '3-35', source: '3', target: '35', data: {label: 'accept', condition: 'Agreed'}},
        {id: '3-35', source: '3', target: '35', data: {label: 'retry', condition: 'Try again'}},
    ];
    act(() => mocks.workbench.mock.lastCall?.[0].onPreviewChange({
        id: 'legacy', snapshot: {workflow_json: {nodes: [], edges}},
    }));
    const preview = mocks.flow.mock.lastCall?.[0].edges;
    expect(preview.map((edge: {id: string}) => edge.id)).toEqual(['3-35', '3-35__2']);
    expect(preview.map((edge: {data: unknown}) => edge.data)).toEqual(edges.map(edge => edge.data));
    expect(edges.map(edge => edge.id)).toEqual(['3-35', '3-35']);
    expect(mocks.state.setEdges).not.toHaveBeenCalled();
});

it('normalizes already-mounted legacy edges before React Flow renders them', () => {
    const edges: FlowEdge[] = [
        {id: '3-35', source: '3', target: '35', data: {label: 'first', condition: 'First branch'}},
        {id: '3-35', source: '3', target: '35', data: {label: 'second', condition: 'Second branch'}},
    ];
    mocks.state.edges = edges;
    const view = render(<RenderWorkflow workflowId={12} initialWorkflowName="Agent" user={{id:'1'}} />);
    const normalized = mocks.flow.mock.lastCall?.[0].edges;
    expect(normalized.map((edge: FlowEdge) => edge.id)).toEqual(['3-35', '3-35__2']);
    expect(normalized.map((edge: FlowEdge) => edge.data)).toEqual(edges.map(edge => edge.data));
    view.rerender(<RenderWorkflow workflowId={12} initialWorkflowName="Renamed" user={{id:'1'}} />);
    expect(mocks.flow.mock.lastCall?.[0].edges).toBe(normalized);
    expect(edges.map(edge => edge.id)).toEqual(['3-35', '3-35']);
});

it('reloads an externally adopted draft only once, after its saved snapshot actually changes', () => {
    mocks.query = 'workbench=1';
    const reload = vi.fn();
    render(<RenderWorkflow workflowId={12} initialWorkflowName="Agent" user={{id:'1'}} onDraftAdopted={reload} />);
    const snapshot = { template_context_variables: {}, workflow_configurations: {}, workflow_json: {edges: [], nodes: []}, name: 'Agent' };
    act(() => mocks.workbench.mock.lastCall?.[0].onTargetChange(snapshot, true));
    expect(reload).not.toHaveBeenCalled();
    act(() => mocks.workbench.mock.lastCall?.[0].onTargetChange({ ...snapshot, template_context_variables: {company: 'Provided'} }, true));
    act(() => mocks.workbench.mock.lastCall?.[0].onTargetChange({ ...snapshot, template_context_variables: {company: 'Provided'} }, true));
    expect(reload).toHaveBeenCalledOnce();
});

it.each(['dirty', 'composing', 'historical'])('preserves %s state when another tab updates the draft', kind => {
    mocks.query = kind === 'historical' ? 'workbench=1&version=3' : 'workbench=1';
    mocks.state.isDirty = kind === 'dirty';
    const reload = vi.fn();
    render(<RenderWorkflow workflowId={12} initialWorkflowName="Agent" user={{id:'1'}} onDraftAdopted={reload}
        initialSelectedVersion={kind === 'historical' ? version(3, 'archived') : undefined} />);
    act(() => mocks.workbench.mock.lastCall?.[0].onTargetChange({
        name: 'Built agent', workflow_json: {nodes: [], edges: []}, workflow_configurations: {}, template_context_variables: {},
    }, kind !== 'composing'));
    expect(reload).not.toHaveBeenCalled();
    if (kind === 'composing') expect(screen.getByRole('button', {name: '同步最新草稿'}).hasAttribute('disabled')).toBe(true);
});

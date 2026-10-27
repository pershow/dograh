import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { Position, ReactFlowProvider, useReactFlow } from '@xyflow/react';
import { memo, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { beforeEach, expect, it, vi } from 'vitest';

import { WorkflowProvider } from '@/app/workflow/[workflowId]/contexts/WorkflowContext';
import type { FlowEdge } from '@/components/flow/types';

import CustomEdge from './CustomEdge';

const mocks = vi.hoisted(() => ({ updateEdge: vi.fn(), deleteEdge: vi.fn(), save: vi.fn() }));
vi.mock('@/app/workflow/[workflowId]/stores/workflowStore', () => ({
    useWorkflowStore: (selector: (state: typeof mocks) => unknown) => selector(mocks),
}));
vi.mock('@/components/flow/TextOrAudioInput', () => ({
    StaticTextWarning: () => null,
    TextOrAudioInput: ({ children }: { children: ReactNode }) => children,
}));
vi.mock('@xyflow/react', async importOriginal => ({
    ...await importOriginal<typeof import('@xyflow/react')>(),
    // jsdom has no measured React Flow viewport; keep the actual paths, hooks
    // and store, and provide only the portal container normally supplied by it.
    EdgeLabelRenderer: ({ children }: { children: ReactNode }) => createPortal(children, document.body),
}));

const edges: FlowEdge[] = Array.from({ length: 7 }, (_, index) => ({
    id: `branch-${index}`, source: 'source', target: 'target', type: 'custom',
    data: { label: `Route ${index}`, condition: `Condition ${index}` },
}));
const MemoEdge = memo(CustomEdge);

function DrawEdge({ edge }: { edge: FlowEdge }) {
    return <MemoEdge {...edge} sourceX={950} sourceY={700} targetX={350} targetY={900}
        sourcePosition={Position.Bottom} targetPosition={Position.Top}
        selected={false} animated={false} selectable deletable />;
}

function ChangeSiblings() {
    const { setEdges } = useReactFlow();
    return <>
        <button onClick={() => setEdges(edges.slice(0, 3))}>Add sibling</button>
        <button onClick={() => setEdges(edges.slice(0, 2))}>Remove sibling</button>
    </>;
}

function label(id: string) {
    return document.querySelector<HTMLElement>(`[data-edge-label-id="${id}"]`)!;
}

beforeEach(() => vi.clearAllMocks());

it('renders seven distinct paths and reachable labels without changing the workflow', () => {
    const before = structuredClone(edges);
    const { container } = render(<ReactFlowProvider initialEdges={edges}>
        <WorkflowProvider value={{ saveWorkflow: mocks.save }}>
            <svg>{edges.map(edge => <DrawEdge key={edge.id} edge={edge} />)}</svg>
        </WorkflowProvider>
    </ReactFlowProvider>);
    const paths = [...container.querySelectorAll('.react-flow__edge-path')];
    expect(paths).toHaveLength(7);
    expect(new Set(paths.map(path => path.getAttribute('d'))).size).toBe(7);
    expect(new Set(edges.map(edge => label(edge.id).style.transform)).size).toBe(7);
    edges.forEach(edge => expect(label(edge.id).textContent).toBe(edge.data.label));
    expect(edges).toEqual(before);
    expect(mocks.save).not.toHaveBeenCalled();
    expect(mocks.updateEdge).not.toHaveBeenCalled();
});

it('reroutes an unchanged edge when siblings are added or removed', async () => {
    const { container } = render(<ReactFlowProvider defaultEdges={edges.slice(0, 2)}>
        <WorkflowProvider value={{ saveWorkflow: mocks.save }}>
            <svg><DrawEdge edge={edges[0]} /></svg><ChangeSiblings />
        </WorkflowProvider>
    </ReactFlowProvider>);
    const path = () => container.querySelector('.react-flow__edge-path')!.getAttribute('d');
    const initialPath = path();
    const initialPosition = label(edges[0].id).style.transform;
    fireEvent.click(screen.getByText('Add sibling'));
    await waitFor(() => expect(path()).not.toBe(initialPath));
    expect(label(edges[0].id).style.transform).not.toBe(initialPosition);
    fireEvent.click(screen.getByText('Remove sibling'));
    await waitFor(() => expect(path()).toBe(initialPath));
    expect(label(edges[0].id).style.transform).toBe(initialPosition);
    expect(mocks.save).not.toHaveBeenCalled();
});

it.each([false, true])('opens the correct parallel condition with readOnly=%s', readOnly => {
    render(<ReactFlowProvider initialEdges={edges}>
        <WorkflowProvider value={{ saveWorkflow: mocks.save, readOnly }}>
            <svg>{edges.map(edge => <DrawEdge key={edge.id} edge={edge} />)}</svg>
        </WorkflowProvider>
    </ReactFlowProvider>);
    fireEvent.doubleClick(label('branch-3'));
    const inputs = screen.getAllByRole('textbox') as HTMLInputElement[];
    expect(inputs[0].value).toBe('Route 3');
    expect(inputs[1].value).toBe('Condition 3');
    const save = screen.getByRole('button', { name: readOnly ? 'Read Only' : 'Save' });
    if (readOnly) {
        expect(save.hasAttribute('disabled')).toBe(true);
        fireEvent.click(save);
        expect(mocks.save).not.toHaveBeenCalled();
        expect(mocks.updateEdge).not.toHaveBeenCalled();
    } else {
        fireEvent.change(inputs[0], { target: { value: 'Renamed route' } });
        fireEvent.click(save);
        expect(mocks.updateEdge).toHaveBeenCalledWith('branch-3', {
            data: expect.objectContaining({ label: 'Renamed route', condition: 'Condition 3' }),
        });
        expect(mocks.save).toHaveBeenCalledOnce();
    }
});

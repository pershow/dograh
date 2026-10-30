import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';

import { CapabilityEditor } from '../CapabilityEditor';

const mocks = vi.hoisted(() => ({
  create: vi.fn(),
  auth: {user: {id: 'user'}, loading: false},
}));
vi.mock('@/client', () => ({createWorkbenchCapabilityCandidate: mocks.create}));
vi.mock('@/lib/auth', () => ({useAuth: () => mocks.auth}));
vi.mock('../workbench.module.css', () => ({default: {}}));
const snapshot = {
  name: 'Agent', workflow_json: {nodes: [], edges: []}, template_context_variables: {},
  workflow_configurations: {agent_behavior: {enabled: true, goal: 'Help customers', confirmation_phrases: ['确认执行']}},
};
beforeEach(() => {
  vi.clearAllMocks();
  mocks.auth.loading = false;
  mocks.create.mockResolvedValue({data: {data: {id: 'new-candidate'}}});
});

it('creates a separate candidate from the selected version and preserves existing confirmation policy', async () => {
  const created = vi.fn();
  render(<CapabilityEditor session="session" snapshot={snapshot} candidateId="base-candidate" fingerprint={'a'.repeat(64)} disabled={false} onCreated={created}/>);
  fireEvent.change(screen.getByLabelText('任务目标'), {target: {value: 'Resolve the request'}});
  fireEvent.click(screen.getByRole('button', {name: '生成能力配置候选版本'}));
  await waitFor(() => expect(created).toHaveBeenCalledWith('new-candidate'));
  expect(mocks.create.mock.calls[0][0]).toMatchObject({path: {session_id: 'session'}, body: {
    candidate_id: 'base-candidate', expected_fingerprint: 'a'.repeat(64),
    agent_behavior: {goal: 'Resolve the request', confirmation_phrases: ['确认执行']},
  }});
  expect(snapshot.workflow_configurations.agent_behavior.goal).toBe('Help customers');
});

it('renders structured validation errors without reporting candidate creation', async () => {
  mocks.create.mockResolvedValue({error: {detail: [{loc: ['body', 'agent_behavior'], msg: 'Unknown fact', type: 'value_error'}]}});
  const created = vi.fn();
  render(<CapabilityEditor session="session" snapshot={snapshot} fingerprint={'a'.repeat(64)} disabled={false} onCreated={created}/>);
  fireEvent.click(screen.getByRole('button', {name: '生成能力配置候选版本'}));
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('Unknown fact'));
  expect(created).not.toHaveBeenCalled();
});

it('does not save while authentication is loading', () => {
  mocks.auth.loading = true;
  render(<CapabilityEditor session="session" snapshot={snapshot} fingerprint={'a'.repeat(64)} disabled={false} onCreated={vi.fn()}/>);
  fireEvent.click(screen.getByRole('button', {name: '生成能力配置候选版本'}));
  expect(mocks.create).not.toHaveBeenCalled();
});

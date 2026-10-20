import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';

import { AgentWorkbench } from '../AgentWorkbench';
import type { WorkbenchState } from '../types';

const mocks = vi.hoisted(() => ({
  replace: vi.fn(), capabilities: vi.fn(), create: vi.fn(), session: vi.fn(), events: vi.fn(),
  candidate: vi.fn(), adopt: vi.fn(), voice: vi.fn(), dependencies: vi.fn(),
  auth: { user: { id: '1' }, loading: false, getAccessToken: vi.fn(), redirectToLogin: vi.fn() },
}));
vi.mock('../workbench.module.css', () => ({ default: {} }));
vi.mock('next/navigation', () => ({ useRouter: () => ({ replace: mocks.replace }) }));
vi.mock('@/lib/auth', () => ({ useAuth: () => mocks.auth }));
vi.mock('../useWorkbenchVoice', () => ({ useWorkbenchVoice: mocks.voice }));
vi.mock('@/client/sdk.gen', () => ({
  workbenchCapabilitiesApiV1AgentWorkbenchCapabilitiesGet: mocks.capabilities,
  workbenchCreateSessionApiV1AgentWorkbenchSessionsPost: mocks.create,
  workbenchSessionApiV1AgentWorkbenchSessionsSessionIdGet: mocks.session,
  workbenchEventsApiV1AgentWorkbenchSessionsSessionIdEventsGet: mocks.events,
  workbenchCandidateApiV1AgentWorkbenchSessionsSessionIdCandidatesCandidateIdGet: mocks.candidate,
  workbenchAdoptApiV1AgentWorkbenchSessionsSessionIdCandidatesCandidateIdAdoptPost: mocks.adopt,
  workbenchDependenciesApiV1AgentWorkbenchSessionsSessionIdDependenciesGet: mocks.dependencies,
}));
const response = (data: unknown) => ({ data: { data } });
const snapshot = { name: 'Agent', workflow_json: { nodes: [], edges: [] }, workflow_configurations: {}, template_context_variables: {} };
const candidate = { id: 'candidate', name: 'Candidate', summary: 'Summary', code: '', fingerprint: 'abc', snapshot };
const state: WorkbenchState = {
  session: { id: 'existing', workflow_id: 7, model_protocol: 'chat' }, target: snapshot,
  candidates: [candidate], assets: [{ id: 'asset', name: 'SOP.txt', status: 'ready', byte_size: 20 }],
  jobs: [{ id: 'job', kind: 'build', status: 'failed', input: { asset_ids: ['asset'] }, output: {} }],
  suites: [], evaluations: [],
};
beforeEach(() => {
  vi.clearAllMocks();
  mocks.auth.loading = false;
  mocks.capabilities.mockResolvedValue(response({ enabled: true }));
  mocks.create.mockResolvedValue(response(state.session));
  mocks.session.mockResolvedValue(response(state));
  mocks.events.mockResolvedValue(response({ events: [] }));
  mocks.candidate.mockResolvedValue(response(candidate));
  mocks.adopt.mockResolvedValue(response({}));
  mocks.dependencies.mockResolvedValue(response({ ready: true, issues: [], services: [] }));
  mocks.voice.mockReturnValue({ status: 'off', start: vi.fn(), stop: vi.fn() });
  Element.prototype.scrollIntoView = vi.fn();
});

it('redirects old workbench bookmarks into the original editor with the same session', async () => {
  render(<AgentWorkbench sessionId="existing" />);
  await waitFor(() => expect(mocks.replace).toHaveBeenCalledWith('/workflow/7?workbench=1&session=existing'));
  expect(mocks.create).not.toHaveBeenCalled();
});

it('restores attachments for voice and text, previews in the host canvas, and reports adoption', async () => {
  const preview = vi.fn();
  const adopted = vi.fn();
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={preview} onAdopted={adopted} />);
  await waitFor(() => expect(preview).toHaveBeenCalledWith(candidate));
  expect(mocks.voice.mock.lastCall?.[2]).toEqual(['asset']);
  expect(screen.getByRole('textbox', { name: '构建需求' })).toBeTruthy();
  expect(screen.getByRole('button', { name: '实时语音' })).toBeTruthy();
  expect(mocks.replace).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '应用到草稿' }));
  await waitFor(() => expect(adopted).toHaveBeenCalledOnce());
  expect(preview.mock.lastCall?.[0]).toBeUndefined();
  expect(mocks.adopt).toHaveBeenCalledWith({ path: { session_id: 'existing', candidate_id: 'candidate' } });
});

it('ignores late candidate loads after returning to the current draft', async () => {
  let resolveCandidate!: (value: unknown) => void;
  mocks.candidate.mockReturnValue(new Promise(resolve => { resolveCandidate = resolve; }));
  const preview = vi.fn();
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={preview} />);
  await waitFor(() => expect(mocks.candidate).toHaveBeenCalledOnce());
  fireEvent.change(screen.getByRole('combobox', { name: '候选版本' }), { target: { value: '' } });
  await act(async () => { resolveCandidate(response(candidate)); });
  expect(preview).not.toHaveBeenCalledWith(candidate);
  expect(screen.getByRole('button', { name: '应用到草稿' }).hasAttribute('disabled')).toBe(true);
});

it('does not restore a session from a different workflow', async () => {
  const preview = vi.fn();
  render(<AgentWorkbench workflowId={8} sessionId="existing" onPreviewChange={preview} />);
  await screen.findByRole('alert');
  expect(screen.getByRole('alert').textContent).toContain('不属于当前 Agent');
  expect(mocks.candidate).not.toHaveBeenCalled();
});

it('keeps an adopted candidate in the draft when replaying task history', async () => {
  mocks.events.mockResolvedValueOnce(response({ events: [
    {sequence: 1, type: 'task.completed', payload: {output: {candidate_id: 'candidate'}}, created_at: ''},
    {sequence: 2, type: 'candidate.adopted', payload: {}, created_at: ''},
  ] }));
  const preview = vi.fn();
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={preview} />);
  await screen.findByRole('combobox', {name: '候选版本'});
  await waitFor(() => expect(mocks.events).toHaveBeenCalled());
  expect((screen.getByRole('combobox', {name: '候选版本'}) as HTMLSelectElement).value).toBe('');
  expect(preview).not.toHaveBeenCalledWith(candidate);
});

it('defers host draft synchronization while composing or using voice', async () => {
  const targetChanged = vi.fn();
  const preview = vi.fn();
  const view = render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={preview} onTargetChange={targetChanged} />);
  await waitFor(() => expect(targetChanged).toHaveBeenLastCalledWith(snapshot, true));
  fireEvent.change(screen.getByRole('textbox', {name: '构建需求'}), {target: {value: 'Unsent requirements'}});
  expect(targetChanged).toHaveBeenLastCalledWith(snapshot, false);
  fireEvent.change(screen.getByRole('textbox', {name: '构建需求'}), {target: {value: ''}});
  expect(targetChanged).toHaveBeenLastCalledWith(snapshot, true);
  mocks.voice.mockReturnValue({status: 'listening', start: vi.fn(), stop: vi.fn()});
  view.rerender(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={preview} onTargetChange={targetChanged} />);
  expect(targetChanged).toHaveBeenLastCalledWith(snapshot, false);
});

it('previews candidate defaults and labels unresolved business values', async () => {
  mocks.candidate.mockResolvedValue(response({...candidate, snapshot: {
    ...snapshot, template_context_variables: {company: 'Provided company'},
    workflow_json: {nodes: [{id: 'start', data: {prompt: '{{customer}} {{current_weekday}}'}}], edges: []},
  }}));
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={vi.fn()} />);
  await screen.findByText('Provided company');
  expect(screen.getByText('customer')).toBeTruthy();
  expect(screen.getByText('待填写 / 每次通话传入')).toBeTruthy();
  expect(screen.queryByText('current_weekday')).toBeNull();
});

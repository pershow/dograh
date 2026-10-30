import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';

import { AgentWorkbench } from '../AgentWorkbench';
import type { WorkbenchState } from '../types';

const mocks = vi.hoisted(() => ({
  replace: vi.fn(), capabilities: vi.fn(), create: vi.fn(), session: vi.fn(), sessions: vi.fn(), events: vi.fn(),
  runs: vi.fn(), instruct: vi.fn(), candidate: vi.fn(), adopt: vi.fn(), voice: vi.fn(), dependencies: vi.fn(), tune: vi.fn(), evaluation: vi.fn(), suite: vi.fn(), pages: vi.fn(),
  auth: { user: { id: '1' }, loading: false, getAccessToken: vi.fn(), redirectToLogin: vi.fn() },
}));
vi.mock('../workbench.module.css', () => ({ default: {} }));
vi.mock('next/navigation', () => ({ useRouter: () => ({ replace: mocks.replace }) }));
vi.mock('@/lib/auth', () => ({ useAuth: () => mocks.auth }));
vi.mock('../useWorkbenchVoice', () => ({ useWorkbenchVoice: mocks.voice }));
vi.mock('../BuildModelSettings', () => ({ BuildModelSettings: ({onSaved}: {onSaved: () => void}) => <button onClick={onSaved}>保存构建模型</button> }));
vi.mock('@/client/sdk.gen', () => ({
  getWorkbenchEvaluation: mocks.evaluation,
  getWorkbenchSuite: mocks.suite,
  getWorkbenchTestResults: mocks.pages,
  workbenchAgentRunsApiV1AgentWorkbenchSessionsSessionIdAgentRunsGet: mocks.runs,
  workbenchInstructionApiV1AgentWorkbenchSessionsSessionIdInstructionsPost: mocks.instruct,
  workbenchCapabilitiesApiV1AgentWorkbenchCapabilitiesGet: mocks.capabilities,
  workbenchCreateSessionApiV1AgentWorkbenchSessionsPost: mocks.create,
  workbenchSessionApiV1AgentWorkbenchSessionsSessionIdGet: mocks.session,
  workbenchSessionsApiV1AgentWorkbenchSessionsGet: mocks.sessions,
  workbenchEventsApiV1AgentWorkbenchSessionsSessionIdEventsGet: mocks.events,
  workbenchCandidateApiV1AgentWorkbenchSessionsSessionIdCandidatesCandidateIdGet: mocks.candidate,
  workbenchAdoptApiV1AgentWorkbenchSessionsSessionIdCandidatesCandidateIdAdoptPost: mocks.adopt,
  workbenchDependenciesApiV1AgentWorkbenchSessionsSessionIdDependenciesGet: mocks.dependencies,
  workbenchTuneApiV1AgentWorkbenchSessionsSessionIdTunePost: mocks.tune,
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
  mocks.sessions.mockResolvedValue(response({sessions: [{...state.session, created_at: '2026-10-22T00:00:00Z'}]}));
  mocks.events.mockResolvedValue(response({ events: [] }));
  mocks.candidate.mockResolvedValue(response(candidate));
  mocks.adopt.mockResolvedValue(response({}));
  mocks.instruct.mockResolvedValue(response({}));
  mocks.tune.mockResolvedValue(response({}));
  mocks.runs.mockResolvedValue(response({runs: [{id: 42, created_at: "2026-10-20", state: "completed", has_transcript: true}]}));
  mocks.dependencies.mockResolvedValue(response({ ready: true, issues: [], services: [] }));
  mocks.voice.mockReturnValue({ status: 'off', start: vi.fn(), stop: vi.fn() });
  Element.prototype.scrollIntoView = vi.fn();
});

it('tunes the complete task even when the selected suite is its last small batch', async () => {
  const suites = [{id: 'first', payload: {name: 'First', cases: [{}, {}, {}]}}, {id: 'last', payload: {name: 'Last', cases: [{}]}}];
  mocks.session.mockResolvedValue(response({...state, suites, jobs: [{id: 'full-test', kind: 'build', status: 'completed', input: {}, output: {outcome: 'test', candidate_id: 'candidate', suite_ids: ['first', 'last'], suite_id: 'last', summary: {total: 4}}}], evaluations: []}));
  mocks.events.mockResolvedValue(response({events: [{sequence: 1, type: 'task.completed', payload: {output: {candidate_id: 'candidate', suite_id: 'last'}}, created_at: ''}]}));
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', {name: '文本仿真'}));
  expect(await screen.findByText(/完整测试任务 · 2 批 \/ 4 条/)).toBeTruthy();
  fireEvent.click(screen.getByRole('button', {name: '开始调优'}));
  await waitFor(() => expect(mocks.tune).toHaveBeenCalledOnce());
  expect(mocks.tune.mock.calls[0][0].body.test_job_id).toBe('full-test');
  expect(mocks.tune.mock.calls[0][0].body.suite_id).toBeUndefined();
  expect(mocks.adopt).not.toHaveBeenCalled();
});

it('prepares every batch from the selected task without running or adopting it', async () => {
  const suites = [{id: 'first', payload: {name: 'First', cases: [{}, {}]}}, {id: 'last', payload: {name: 'Last', cases: [{}]}}];
  mocks.session.mockResolvedValue(response({...state, suites, jobs: [{id: 'test', kind: 'build', status: 'completed', input: {}, output: {outcome: 'test', candidate_id: 'candidate', suite_ids: ['first', 'last'], suite_id: 'last', summary: {total: 3}}}]}));
  mocks.events.mockResolvedValue(response({events: [{sequence: 1, type: 'task.completed', payload: {output: {candidate_id: 'candidate', suite_id: 'last'}}, created_at: ''}]}));
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', {name: '文本仿真'}));
  fireEvent.click(screen.getByRole('button', {name: '准备完整场景测试'}));
  await waitFor(() => expect(mocks.instruct).toHaveBeenCalledOnce());
  expect(mocks.instruct.mock.calls[0][0].body).toMatchObject({mode: 'test', test_mode: 'dynamic', prepare_only: true, rerun_test_job_id: 'test'});
  expect(mocks.adopt).not.toHaveBeenCalled();
  expect(mocks.tune).not.toHaveBeenCalled();
});

it('shows preparation gaps and runs the complete frozen scope without presenting a new agent', async () => {
  const output = {outcome: 'test_preparation', candidate_id: 'candidate', suite_ids: ['s1','s2'], compilation: {total: 3, ready: 2, needs_input: 1, preparation_error: 0, rows: [{name: 'Conflicting case', source: {row: 4}, status: 'needs_input', requirement_count: 5, questions: ['Which source rule applies?']}]}};
  mocks.events.mockResolvedValue(response({events: [{sequence: 1, type: 'task.completed', payload: {job_id: 'prepared', output}, created_at: ''}]}));
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={vi.fn()} />);
  const preparation = await screen.findByRole('region', {name: '测试准备结果'});
  expect(within(preparation).getByText('Which source rule applies?')).toBeTruthy();
  expect(screen.queryByRole('region', {name: '候选版本 1 · Candidate'})).toBeNull();
  fireEvent.click(screen.getByRole('button', {name: '运行已准备的完整测试集'}));
  await waitFor(() => expect(mocks.instruct).toHaveBeenCalledOnce());
  expect(mocks.instruct.mock.calls[0][0].body.prepared_test_job_id).toBe('prepared');
  expect(mocks.instruct.mock.calls[0][0].body.test_suite_ids).toBeUndefined();
  expect(mocks.instruct.mock.calls[0][0].body.candidate_id).toBeUndefined();
  expect(mocks.adopt).not.toHaveBeenCalled();
});

it('opens the exact full-task baseline receipt from a blocked tuning task', async () => {
  const result = (text: string) => ({name: 'Case', status: 'inconclusive', transcript: [{role: 'assistant', text}], assertions: [], tool_calls: []});
  mocks.session.mockResolvedValue(response({...state,
    suites: [{id: 'unrelated', payload: {name: 'Other', cases: []}}, {id: 'frozen', payload: {name: 'Frozen', cases: []}}],
    evaluations: [
      {id: 'newer', candidate_id: 'candidate', suite_id: 'frozen', status: 'completed', summary: {}, results: [result('Unrelated newer reply')]},
      {id: 'baseline', candidate_id: 'candidate', suite_id: 'frozen', status: 'completed', summary: {}, results: [result('Frozen baseline reply')]},
    ],
    jobs: [{id: 'tune', kind: 'tune', status: 'needs_attention', input: {test_job_id: 'test'}, output: {candidate_id: 'candidate', evaluation_ids: ['baseline'], blocked_reason: 'baseline_incomplete', verification_issue: 'Missing evidence', tuning_scope: {suite_count: 2, case_count: 4}}}],
  }));
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', {name: /^调优$/}));
  fireEvent.click(screen.getByRole('button', {name: '查看基线结果'}));
  expect(await screen.findByText('Frozen baseline reply')).toBeTruthy();
  expect(screen.queryByText('Unrelated newer reply')).toBeNull();
  expect((screen.getByLabelText('测试集') as HTMLSelectElement).value).toBe('frozen');
  expect(mocks.adopt).not.toHaveBeenCalled();
  expect(mocks.instruct).not.toHaveBeenCalled();
});

it('resumes the saved build without starting a new generation or adopting a candidate', async () => {
  mocks.session.mockResolvedValue(response({...state, jobs: [{...state.jobs[0], output: {
    outcome: 'build', candidate_id: 'candidate', suite_id: 'frozen',
  }}]}));
  mocks.events.mockResolvedValue(response({events: [{sequence: 1, type: 'task.failed', payload: {job_id: 'job', error: 'Model connection interrupted.'}, created_at: ''}]}));
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', {name: '从断点继续调优'}));
  await waitFor(() => expect(mocks.instruct).toHaveBeenCalled());
  const body = mocks.instruct.mock.calls[0][0].body;
  expect(body.mode).toBe('build');
  expect(body.resume_build_job_id).toBe('job');
  expect(body.candidate_id).toBeUndefined();
  expect(mocks.adopt).not.toHaveBeenCalled();
});

it('opens the latest page, prepends older messages without duplicates or jumping, and polls only deltas', async () => {
  const interval = vi.spyOn(globalThis, 'setInterval');
  const event = (sequence: number) => ({sequence, type: 'assistant.message', payload: {text: `Message ${sequence}`}, created_at: ''});
  mocks.events.mockResolvedValueOnce(response({events: [event(100), event(101)], before: 100, after: 101}));
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={vi.fn()} />);
  await screen.findByText('Message 101');
  expect(mocks.events.mock.calls[0][0].query).toEqual({after: undefined, limit: 60});
  const pane = screen.getByRole('region', {name: '构建对话'});
  pane.scrollTop = 20;
  vi.spyOn(pane, 'getBoundingClientRect').mockReturnValue({top: 100} as DOMRect);
  const anchor = screen.getByText('Message 100').closest('[data-event-sequence]')!;
  vi.spyOn(anchor, 'getBoundingClientRect').mockImplementation(() => ({
    top: screen.queryByText('Message 99') ? 340 : 140, bottom: 400,
  } as DOMRect));
  mocks.events.mockResolvedValueOnce(response({events: [event(99), event(100)], before: 99, after: 100}));
  fireEvent.click(screen.getByRole('button', {name: '加载更早消息'}));
  await screen.findByText('Message 99');
  expect(screen.getAllByText('Message 100')).toHaveLength(1);
  expect(pane.scrollTop).toBe(220);
  expect(mocks.events.mock.calls[1][0].query).toEqual({before: 100, limit: 60});
  const sessionReads = mocks.session.mock.calls.length;
  mocks.events.mockResolvedValueOnce(response({events: [event(102)], after: 102}));
  const poll = interval.mock.calls.find(call => call[1] === 2000)![0] as () => void;
  await act(async () => { await poll(); });
  await screen.findByText('Message 102');
  expect(mocks.events.mock.lastCall![0].query).toEqual({after: 101, limit: 200});
  expect(mocks.session.mock.calls.length).toBe(sessionReads);
  expect(pane.scrollTop).toBe(220);
  interval.mockRestore();
});

it('catches up all pending event pages on return without reloading old history', async () => {
  const event = (sequence: number) => ({sequence, type: 'assistant.message', payload: {text: `Catchup ${sequence}`}, created_at: ''});
  mocks.events.mockResolvedValueOnce(response({events: [event(100)], before: 100, after: 100}));
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={vi.fn()} />);
  await screen.findByText('Catchup 100');
  const sessionReads = mocks.session.mock.calls.length;
  mocks.events.mockResolvedValueOnce(response({events: [event(101)], after: 101, has_more: true}));
  mocks.events.mockResolvedValueOnce(response({events: [event(102)], after: 102, has_more: false}));
  fireEvent(document, new Event('visibilitychange'));
  await screen.findByText('Catchup 102');
  expect(mocks.events.mock.calls.slice(-2).map(call => call[0].query)).toEqual([
    {after: 100, limit: 200}, {after: 101, limit: 200},
  ]);
  expect(mocks.session.mock.calls.length).toBe(sessionReads);
  expect(screen.getByRole('button', {name: '加载更早消息'})).toBeTruthy();
});

it('switches independent sessions within an Agent and protects unsent input', async () => {
  mocks.sessions.mockResolvedValue(response({sessions: [
    {...state.session, created_at: '2026-10-22T00:00:00Z'},
    {...state.session, id: 'another', created_at: '2026-10-21T00:00:00Z'},
  ]}));
  mocks.create.mockResolvedValue(response({...state.session, id: 'new-session'}));
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', {name: '工作台会话'}));
  const older = await screen.findByRole('button', {name: /anothe/});
  fireEvent.change(screen.getByRole('textbox', {name: '构建需求'}), {target: {value: 'Unsent'}});
  expect(older.hasAttribute('disabled')).toBe(true);
  expect(screen.getByRole('button', {name: '新建会话'}).hasAttribute('disabled')).toBe(true);
  fireEvent.change(screen.getByRole('textbox', {name: '构建需求'}), {target: {value: ''}});
  fireEvent.click(older);
  expect(mocks.replace).toHaveBeenLastCalledWith('/workflow/7?workbench=1&session=another');
  fireEvent.click(screen.getByRole('button', {name: '工作台会话'}));
  fireEvent.click(await screen.findByRole('button', {name: '新建会话'}));
  await waitFor(() => expect(mocks.create).toHaveBeenCalledWith({body: {
    workflow_id: 7, model_protocol: 'chat', new_session: true,
  }}));
  await waitFor(() => expect(mocks.replace).toHaveBeenLastCalledWith('/workflow/7?workbench=1&session=new-session'));
});

it('shows live statuses across sessions and refreshes only while the list is open', async () => {
  const interval = vi.spyOn(globalThis, 'setInterval');
  const clear = vi.spyOn(globalThis, 'clearInterval');
  const items = [
    {...state.session, created_at: '2026-10-22T00:00:00Z', active_job_status: null},
    {...state.session, id: 'running-session', created_at: '2026-10-21T00:00:00Z', active_job_status: 'running'},
    {...state.session, id: 'queued-session', created_at: '2026-10-20T00:00:00Z', active_job_status: 'queued'},
  ];
  mocks.sessions.mockResolvedValue(response({sessions: items}));
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={vi.fn()} />);
  await screen.findByRole('button', {name: '工作台会话'});
  expect(mocks.sessions).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', {name: '工作台会话'}));
  expect(await screen.findByRole('button', {name: /runnin.*运行中/})).toBeTruthy();
  expect(screen.getByRole('button', {name: /queued.*排队中/})).toBeTruthy();
  expect(screen.getByRole('button', {name: /当前.*未运行/})).toBeTruthy();
  const index = interval.mock.calls.findIndex(call => call[1] === 3000);
  const poll = interval.mock.calls[index][0] as () => void;
  const timer = interval.mock.results[index].value;
  const historyReads = mocks.events.mock.calls.length;
  const sessionReads = mocks.session.mock.calls.length;
  mocks.sessions.mockResolvedValue(response({sessions: items.map(item => ({...item, active_job_status: null}))}));
  await act(async () => { poll(); });
  expect(await screen.findByRole('button', {name: /runnin.*未运行/})).toBeTruthy();
  expect(screen.queryByText('正在加载会话…')).toBeNull();
  expect(mocks.events.mock.calls.length).toBe(historyReads);
  expect(mocks.session.mock.calls.length).toBe(sessionReads);
  mocks.sessions.mockResolvedValue({error: {detail: 'temporarily unavailable'}});
  await act(async () => { poll(); });
  expect(await screen.findByText(/会话状态更新失败/)).toBeTruthy();
  mocks.sessions.mockResolvedValue(response({sessions: items}));
  await act(async () => { poll(); });
  await screen.findByRole('button', {name: /runnin.*运行中/});
  expect(screen.queryByText(/会话状态更新失败/)).toBeNull();
  fireEvent.click(screen.getByRole('button', {name: '工作台会话'}));
  expect(clear).toHaveBeenCalledWith(timer);
  interval.mockRestore();
  clear.mockRestore();
});

it('analyzes a selected Agent Run without asking for a candidate and surfaces actual evidence', async () => {
  mocks.events.mockResolvedValueOnce(response({ events: [
    {sequence: 1, type: 'evidence.read', payload: {source: 'agent_runs', run_id: 42, section: 'transcript'}},
    {sequence: 2, type: 'task.completed', payload: {output: {outcome: 'analysis'}}},
  ]}));
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', {name: 'Agent Runs 运行记录'}));
  fireEvent.click(await screen.findByRole('button', {name: '分析'}));
  await waitFor(() => expect(mocks.instruct).toHaveBeenCalledOnce());
  expect(mocks.instruct.mock.calls[0][0].body).toMatchObject({mode: 'analyze', asset_ids: []});
  expect(mocks.instruct.mock.calls[0][0].body.text).toContain('#42');
  expect(await screen.findByText('分析已完成。可以继续追问，或提出修改要求。')).toBeTruthy();
  fireEvent.click(screen.getByText('已读取运行 #42 · 1 项资料'));
  expect(screen.getByRole('link', {name: /已读取运行 #42/}).getAttribute('href')).toBe('/workflow/7/run/42');
});

it('folds consecutive evidence and distinguishes reuse without merging separate turns', async () => {
  mocks.events.mockResolvedValueOnce(response({events: [
    ...['overview', 'transcript', 'variables', 'logs', 'logs_raw'].map((section, index) => ({sequence: index + 1, type: 'evidence.read', payload: {source: 'agent_runs', run_id: 76, section}})),
    {sequence: 6, type: 'assistant.message', payload: {text: 'First analysis'}},
    {sequence: 7, type: 'evidence.read', payload: {source: 'agent_runs', run_id: 76, section: 'transcript', reused: true}},
  ]}));
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={vi.fn()} />);
  const summary = await screen.findByText('已读取运行 #76 · 5 项资料');
  expect(summary.closest('details')?.open).toBe(false);
  expect(screen.getByText('复用运行 #76 · 1 项资料')).toBeTruthy();
  fireEvent.click(summary);
  expect(screen.getAllByRole('link', {name: /已读取运行 #76/})).toHaveLength(5);
});

it('clears attachment focus after sending while retaining the session library', async () => {
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', {name: '资料附件，已选择 0 个'}));
  fireEvent.click(await screen.findByRole('checkbox', {name: 'SOP.txt'}));
  fireEvent.keyDown(screen.getByRole('dialog', {name: '资料附件'}), {key: 'Escape'});
  await waitFor(() => expect(screen.queryByRole('dialog', {name: '资料附件'})).toBeNull());
  fireEvent.change(screen.getByRole('textbox', {name: '构建需求'}), {target: {value: 'Use this rule'}});
  fireEvent.click(screen.getByRole('button', {name: '发送构建需求'}));
  await waitFor(() => expect(mocks.instruct).toHaveBeenCalledOnce());
  expect(mocks.instruct.mock.calls[0][0].body.asset_ids).toEqual(['asset']);
  await screen.findByRole('button', {name: '资料附件，已选择 0 个'});
  expect(mocks.voice.mock.lastCall?.[2]).toEqual([]);
});

it('redirects old workbench bookmarks into the original editor with the same session', async () => {
  render(<AgentWorkbench sessionId="existing" />);
  await waitFor(() => expect(mocks.replace).toHaveBeenCalledWith('/workflow/7?workbench=1&session=existing'));
  expect(mocks.create).not.toHaveBeenCalled();
});

it('retains the attachment library without restoring previous focus, previews in the host canvas, and reports adoption', async () => {
  const preview = vi.fn();
  const adopted = vi.fn();
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={preview} onAdopted={adopted} />);
  await waitFor(() => expect(preview).toHaveBeenCalledWith(candidate));
  expect(mocks.voice.mock.lastCall?.[2]).toEqual([]);
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

it.each(['build', 'evaluate', 'needs_attention', 'awaiting_input'])('opens the candidate and matching test results from its ended %s task', async (kind) => {
  mocks.session.mockResolvedValue(response({...state,
    suites: [{id: 'suite', payload: {name: 'Frozen suite', cases: []}}],
    evaluations: [{id: 'evaluation', candidate_id: 'candidate', suite_id: 'suite', status: 'completed',
      summary: {passed: 1, failed: 1, total: 2}, results: []}],
  }));
  mocks.events.mockResolvedValue(response({events: [
    {sequence: 1, type: ['needs_attention', 'awaiting_input'].includes(kind) ? `task.${kind}` : 'task.completed', payload: {output: kind === 'evaluate' ? {id: 'evaluation'} : {
      candidate_id: 'candidate', suite_id: 'suite', evaluation_id: 'evaluation',
      verification: {status: 'failed', passed: 1, total: 2},
    }}, created_at: ''},
  ]}));
  const preview = vi.fn();
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={preview} />);
  const card = await screen.findByRole('region', {name: '候选版本 1 · Candidate'});
  expect(within(card).getByText('Summary')).toBeTruthy();
  expect(within(card).getByText('自动测试：1/2 项通过')).toBeTruthy();
  fireEvent.change(screen.getByRole('combobox', {name: '候选版本'}), {target: {value: ''}});
  fireEvent.click(within(card).getByRole('button', {name: '查看候选'}));
  await waitFor(() => expect(preview).toHaveBeenLastCalledWith(candidate));
  fireEvent.click(within(card).getByRole('button', {name: '查看测试结果'}));
  expect(await screen.findByRole('main', {name: '文本仿真结果'})).toBeTruthy();
  expect(mocks.adopt).not.toHaveBeenCalled();
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
  fireEvent.click(await screen.findByRole('button', {name: '候选版本说明'}));
  await screen.findByText('Provided company');
  expect(screen.getByText('customer')).toBeTruthy();
  expect(screen.getByText('待填写 / 每次通话传入')).toBeTruthy();
  expect(screen.queryByText('current_weekday')).toBeNull();
});

it('opens settings and attachments without losing the input or voice context', async () => {
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={vi.fn()} />);
  const input = await screen.findByRole('textbox', {name: '构建需求'});
  fireEvent.change(input, {target: {value: 'Keep this unsent requirement'}});
  expect(screen.queryByRole('combobox', {name: '构建模型协议'})).toBeNull();
  fireEvent.click(screen.getByRole('button', {name: '工作台设置'}));
  expect((await screen.findByRole('combobox', {name: '构建模型协议'}) as HTMLSelectElement).value).toBe('chat');
  const dependencyReads = mocks.dependencies.mock.calls.length;
  fireEvent.click(screen.getByRole('button', {name: '保存构建模型'}));
  await waitFor(() => expect(mocks.dependencies.mock.calls.length).toBeGreaterThan(dependencyReads));
  fireEvent.keyDown(screen.getByRole('dialog', {name: '工作台设置'}), {key: 'Escape'});
  await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', {name: '工作台设置'})));
  fireEvent.click(screen.getByRole('button', {name: '资料附件，已选择 0 个'}));
  const attachment = await screen.findByRole('checkbox', {name: 'SOP.txt'});
  expect((attachment as HTMLInputElement).checked).toBe(false);
  fireEvent.click(attachment);
  expect(mocks.voice.mock.lastCall?.[2]).toEqual(['asset']);
  fireEvent.keyDown(screen.getByRole('dialog', {name: '资料附件'}), {key: 'Escape'});
  await waitFor(() => expect(screen.queryByRole('dialog', {name: '资料附件'})).toBeNull());
  fireEvent.click(screen.getByRole('button', {name: '文本仿真'}));
  expect((screen.getByRole('textbox', {name: '构建需求'}) as HTMLTextAreaElement).value).toBe('Keep this unsent requirement');
  expect(mocks.voice.mock.lastCall?.[2]).toEqual(['asset']);
  expect(mocks.create).not.toHaveBeenCalled();
});

it('keeps cancellation available in every view without the large background-task banner', async () => {
  mocks.session.mockResolvedValue(response({...state, jobs: [{...state.jobs[0], status: 'running'}]}));
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={vi.fn()} />);
  await screen.findByRole('button', {name: '取消任务'});
  expect(screen.queryByText(/离开页面不会取消/)).toBeNull();
  fireEvent.click(screen.getByRole('button', {name: '文本仿真'}));
  expect(screen.getByRole('button', {name: '取消任务'})).toBeTruthy();
  expect(screen.queryByRole('textbox', {name: '构建需求'})).toBeNull();
  fireEvent.click(screen.getByRole('button', {name: /向助手提出修改/}));
  expect(screen.getByRole('textbox', {name: '构建需求'})).toBeTruthy();
  fireEvent.click(screen.getByRole('button', {name: '收起输入框'}));
  expect(screen.queryByRole('textbox', {name: '构建需求'})).toBeNull();
  fireEvent.click(screen.getByRole('button', {name: '调优'}));
  expect(screen.getByRole('button', {name: '取消任务'})).toBeTruthy();
});

it('shows the recommended result with the original suite and lets the reader select any case', async () => {
  mocks.session.mockResolvedValue(response({...state,
    suites: [{id: 'new-suite', payload: {name: 'New suite', cases: []}}, {id: 'original-suite', payload: {name: 'Original suite', cases: []}}],
    jobs: [{id: 'tune', kind: 'tune', status: 'completed', input: {suite_id: 'original-suite'}, output: {candidate_id: 'candidate', improved: true}}],
    evaluations: [{id: 'evaluation', candidate_id: 'candidate', suite_id: 'original-suite', status: 'completed',
      summary: {passed: 1, total: 2}, results: [
        {name: 'First case', status: 'passed', transcript: [{role: 'assistant', text: 'First response'}], assertions: [], tool_calls: []},
        {name: 'Second case', status: 'failed', transcript: [{role: 'assistant', text: 'Second response'}], assertions: [{kind: 'contains', value: 'Goodbye', passed: false}], tool_calls: []},
      ]}],
  }));
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', {name: '调优'}));
  fireEvent.click(screen.getByRole('button', {name: '查看推荐版本'}));
  expect((screen.getByRole('combobox', {name: '测试集'}) as HTMLSelectElement).value).toBe('original-suite');
  expect(screen.getByText('First response')).toBeTruthy();
  fireEvent.change(screen.getByRole('combobox', {name: '测试场景'}), {target: {value: '1'}});
  const transcript = screen.getByRole('region', {name: '仿真对话与断言'});
  expect(within(transcript).getByText('Second response')).toBeTruthy();
  expect(within(transcript).getByText(/Goodbye/)).toBeTruthy();
});

it('opens saved evidence across versions and repeated runs while a new evaluation is empty', async () => {
  const older = {...candidate, id: 'tested', name: 'Tested version'};
  mocks.candidate.mockImplementation(({path}) => Promise.resolve(response(path.candidate_id === 'tested' ? older : candidate)));
  mocks.session.mockResolvedValue(response({...state, candidates: [candidate, older],
    suites: [{id: 'suite', payload: {name: 'Batch', cases: []}}],
    jobs: [{id: 'active', kind: 'evaluate', status: 'running', input: {}, output: {}}, {id: 'previous', kind: 'evaluate', status: 'completed', input: {}, output: {}}],
    evaluations: [
      {id: 'new-run', job_id: 'active', candidate_id: 'tested', suite_id: 'suite', status: 'running', summary: {}, results: []},
      {id: 'saved-run', job_id: 'previous', candidate_id: 'tested', suite_id: 'suite', status: 'completed', summary: {total: 1, passed: 1}, results: [
        {name: 'Saved case', status: 'passed', transcript: [{role: 'assistant', text: 'Persisted reply'}], assertions: [], tool_calls: []},
      ]},
    ],
  }));
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', {name: '查看已保存结果'}));
  expect(screen.getByText('当前版本与测试集没有匹配结果')).toBeTruthy();
  fireEvent.change(screen.getByLabelText('结果所属任务'), {target: {value: 'previous'}});
  fireEvent.click(screen.getByRole('button', {name: '查看 Saved case 的结果'}));
  expect(await screen.findByText('Persisted reply')).toBeTruthy();
  expect(document.activeElement).toBe(screen.getByRole('region', {name: '测试用例详情'}));
  expect((screen.getByLabelText('候选版本') as HTMLSelectElement).value).toBe('tested');
  expect(screen.getByText(/测试记录 saved-ru/)).toBeTruthy();
  expect(mocks.adopt).not.toHaveBeenCalled();
  expect(mocks.instruct).not.toHaveBeenCalled();
});

it('keeps the selected case when concurrent results arrive before it in the saved array', async () => {
  const interval = vi.spyOn(globalThis, 'setInterval');
  const saved = {name: 'Selected', status: 'passed', transcript: [{role: 'assistant', text: 'Selected reply'}], assertions: [], tool_calls: []};
  const e = {id: 'e', candidate_id: 'candidate', suite_id: 'suite', status: 'running', summary: {}, results: [saved]};
  const s = {...state, suites: [{id: 'suite', payload: {name: 'Batch', cases: []}}], evaluations: [e]};
  mocks.session.mockResolvedValue(response(s));
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', {name: '文本仿真'}));
  fireEvent.click(screen.getByRole('button', {name: '查看 Selected 的结果'}));
  mocks.session.mockResolvedValue(response({...s, evaluations: [{...e, results: [
    {...saved, name: 'Earlier', transcript: [{role: 'assistant', text: 'Earlier reply'}]}, saved,
  ]}]}));
  mocks.events.mockResolvedValue(response({events: [{sequence: 1, type: 'evaluation.updated', payload: {}, created_at: ''}], after: 1}));
  const poll = interval.mock.calls.find(call => call[1] === 2000)![0] as () => void;
  await act(async () => { await poll(); });
  const transcript = screen.getByRole('region', {name: '仿真对话与断言'});
  expect(within(transcript).getByText('Selected reply')).toBeTruthy();
  expect(within(transcript).queryByText('Earlier reply')).toBeNull();
  expect((screen.getByLabelText('测试场景') as HTMLSelectElement).selectedOptions[0].textContent).toContain('Selected');
  interval.mockRestore();
});

it('preserves the composing session when expanding the workbench', async () => {
  const preview = vi.fn();
  const expand = vi.fn();
  const props = {workflowId: 7, sessionId: 'existing', onPreviewChange: preview, onExpandedChange: expand};
  const view = render(<AgentWorkbench {...props} expanded={false} />);
  const input = await screen.findByRole('textbox', {name: '构建需求'});
  fireEvent.change(input, {target: {value: 'Unsent changes'}});
  fireEvent.click(screen.getByRole('button', {name: '展开工作台'}));
  expect(expand).toHaveBeenCalledWith(true);
  view.rerender(<AgentWorkbench {...props} expanded />);
  expect((screen.getByRole('textbox', {name: '构建需求'}) as HTMLTextAreaElement).value).toBe('Unsent changes');
  expect(mocks.voice.mock.lastCall?.[2]).toEqual([]);
  expect(screen.getByRole('button', {name: '收起工作台'})).toBeTruthy();
  expect(mocks.create).not.toHaveBeenCalled();
  expect(mocks.candidate).toHaveBeenCalledOnce();
});

it('submits the explicit test-only mode without adopting the candidate', async () => {
  mocks.instruct.mockResolvedValue(response({id: 'test', status: 'queued'}));
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={vi.fn()} />);
  const input = await screen.findByRole('textbox', {name: '构建需求'});
  fireEvent.change(screen.getByRole('combobox', {name: '助手任务模式'}), {target: {value: 'test'}});
  fireEvent.change(input, {target: {value: '只执行附件用例'}});
  fireEvent.click(screen.getByRole('button', {name: '发送构建需求'}));
  await waitFor(() => expect(mocks.instruct).toHaveBeenCalledWith(expect.objectContaining({body: expect.objectContaining({mode: 'test', text: '只执行附件用例'})})));
  expect(mocks.adopt).not.toHaveBeenCalled();
});

it('continues a pending test clarification using its exact task reference', async () => {
  mocks.instruct.mockResolvedValue(response({id: 'continued', status: 'queued'}));
  mocks.events.mockResolvedValue(response({events: [{sequence: 1, type: 'task.completed', payload: {job_id: 'pending', output: {outcome: 'needs_input', task_mode: 'test', question: '选用哪一列对话？', candidate_id: 'candidate'}}, created_at: ''}]}));
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={vi.fn()} />);
  const answer = await screen.findByRole('textbox', {name: '补充回答'});
  expect(screen.queryByRole('region', {name: '候选版本 1 · Candidate'})).toBeNull();
  fireEvent.change(answer, {target: {value: '调整后对话'}});
  fireEvent.click(screen.getByRole('button', {name: '补充并继续'}));
  await waitFor(() => expect(mocks.instruct).toHaveBeenCalledWith(expect.objectContaining({body: expect.objectContaining({mode: 'test', continue_test_job_id: 'pending', text: '调整后对话'})})));
  expect(mocks.adopt).not.toHaveBeenCalled();
});

it('shows all test batches without representing them as a new candidate', async () => {
  mocks.events.mockResolvedValue(response({events: [{sequence: 1, type: 'task.completed', payload: {job_id: 'test', output: {outcome: 'test', candidate_id: 'candidate', suite_ids: ['s1','s2'], summary: {executed: 22, total: 22, passed: 18, failed: 4, error: 0, inconclusive: 0}}}, created_at: ''}]}));
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={vi.fn()} />);
  const card = await screen.findByRole('region', {name: '只运行测试结果'});
  expect(within(card).getByText('测试结果 · 18/22 项通过')).toBeTruthy();
  expect(within(card).getByRole('button', {name: '第 2 批结果'})).toBeTruthy();
  expect(screen.queryByRole('region', {name: '候选版本 1 · Candidate'})).toBeNull();
  expect(screen.getByText(/22\/22 条已实际运行/)).toBeTruthy();
});

it('keeps the tested version when resource loading finishes after its completion event', async () => {
  let resolveResources!: (value: unknown) => void;
  mocks.session.mockResolvedValueOnce(response(state)).mockImplementationOnce(() => new Promise(resolve => { resolveResources = resolve; }));
  mocks.events.mockResolvedValueOnce(response({events: [{sequence: 1, type: 'task.completed', payload: {output: {outcome: 'test', candidate_id: 'candidate'}}, created_at: ''}]}));
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={vi.fn()} />);
  await waitFor(() => expect(mocks.session).toHaveBeenCalledTimes(2));
  await act(async () => { resolveResources(response({...state, candidates: [{...candidate, id: 'newer'}, candidate]})); });
  await waitFor(() => expect((screen.getByRole('combobox', {name: '候选版本'}) as HTMLSelectElement).value).toBe('candidate'));
  expect(mocks.adopt).not.toHaveBeenCalled();
});

it('resumes a cancelled test using its original task instead of starting a build', async () => {
  mocks.instruct.mockResolvedValue(response({id: 'resumed', status: 'queued'}));
  mocks.session.mockResolvedValue(response({...state, jobs: [{id: 'paused-test', kind: 'build', status: 'cancelled', input: {mode: 'test'}, output: {intent: 'test', evaluation_ids: ['e'], summary: {executed: 10, total: 20}}}]}));
  mocks.events.mockResolvedValue(response({events: [{sequence: 1, type: 'task.cancelled', payload: {job_id: 'paused-test'}, created_at: ''}]}));
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', {name: '继续未完成或异常测试'}));
  await waitFor(() => expect(mocks.instruct).toHaveBeenCalledWith(expect.objectContaining({body: expect.objectContaining({mode: 'test', continue_test_job_id: 'paused-test'})})));
  expect(mocks.adopt).not.toHaveBeenCalled();
});

it('loads full saved evidence and full suite before editing compact context entries', async () => {
  const fullSuite = {id: 'saved-suite', payload: {name: 'Frozen source', cases: [{name: 'Saved case', user_messages: ['original user'], assertions: [{kind: 'completed', value: true}]}]}};
  const index = {id: 'saved-evaluation', job_id: 'saved-test', candidate_id: 'candidate', suite_id: 'saved-suite', status: 'completed', summary_only: true, summary: {}, results: [{name: 'Saved case', status: 'failed', transcript: [], assertions: [], tool_calls: []}]};
  const full = {...index, summary_only: undefined, results: [{...index.results[0], transcript: [{role: 'assistant', text: 'Complete saved reply'}], assertions: [{kind: 'completed', passed: false}]}]};
  mocks.session.mockResolvedValue(response({...state, suites: [{...fullSuite, summary_only: true, payload: {...fullSuite.payload, cases: [{name: 'Saved case'}]}}], evaluations: [index]}));
  mocks.evaluation.mockResolvedValue(response(full));
  mocks.suite.mockResolvedValue(response(fullSuite));
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', {name: '文本仿真'}));
  expect(await screen.findByText('Complete saved reply')).toBeTruthy();
  expect(mocks.evaluation.mock.calls[0][0].path).toEqual({session_id: 'existing', evaluation_id: 'saved-evaluation'});
  fireEvent.click(screen.getByRole('button', {name: '编辑测试集'}));
  await waitFor(() => expect(mocks.suite).toHaveBeenCalledOnce());
  expect(mocks.suite.mock.calls[0][0].path).toEqual({session_id: 'existing', suite_id: 'saved-suite'});
  expect((await screen.findByLabelText('测试集 JSON') as HTMLTextAreaElement).value).toBe(JSON.stringify(fullSuite.payload, null, 2));
  expect(mocks.adopt).not.toHaveBeenCalled();
});

it('retains partial preparation counts and continuation after cancellation before any replay', async () => {
  const compilation = {total: 5, processed: 2, ready: 1, needs_input: 0, preparation_error: 1, preparing: 1, pending: 2,
    rows: [
      {name: 'Accepted', source: {}, status: 'ready', requirement_count: 2, questions: []},
      {name: 'Broken mapping', source: {}, status: 'preparation_error', requirement_count: 2, questions: [], error: 'Invalid source link'},
      {name: 'Saved draft', source: {}, status: 'preparing', requirement_count: 2, questions: []},
      {name: 'Waiting row', source: {}, status: 'pending', requirement_count: 0, questions: []},
    ]};
  mocks.session.mockResolvedValue(response({...state, jobs: [{id: 'stopped', kind: 'build', status: 'cancelled', input: {}, output: {intent: 'test', preparation_receipts: [{name: 'Saved draft', suite_id: 'checkpoint'}], compilation}}], evaluations: []}));
  mocks.events.mockResolvedValue(response({events: [{sequence: 1, type: 'task.cancelled', payload: {job_id: 'stopped'}, created_at: ''}]}));
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={vi.fn()} />);
  const region = await screen.findByRole('region', {name: '测试准备结果'});
  expect(within(region).getByText(/共 5 条 · 可执行 1 · 资料待确认 0 · 准备异常 1 · 准备中 1 · 待处理 2/)).toBeTruthy();
  expect(within(region).getByText(/Saved draft · 准备中/)).toBeTruthy();
  expect(within(region).getByText('Invalid source link')).toBeTruthy();
  expect(mocks.instruct).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', {name: '继续未完成或异常测试'}));
  await waitFor(() => expect(mocks.instruct).toHaveBeenCalledOnce());
  expect(mocks.instruct.mock.calls[0][0].body.continue_test_job_id).toBe('stopped');
  expect(mocks.adopt).not.toHaveBeenCalled();
  expect(mocks.tune).not.toHaveBeenCalled();
});

it('retains the historical question but hides its already-answered continuation form', async () => {
  mocks.session.mockResolvedValue(response({...state, jobs: [
    {id: 'latest', kind: 'build', status: 'failed', input: {continue_test_job_id: 'pending'}, output: {intent: 'test'}},
  ]}));
  mocks.events.mockResolvedValue(response({events: [
    {sequence: 1, type: 'task.completed', payload: {job_id: 'pending', output: {outcome: 'needs_input', task_mode: 'test', question: '选择资料列？'}}, created_at: ''},
    {sequence: 2, type: 'task.failed', payload: {job_id: 'latest', error: 'Model stream interrupted before completion; incomplete output was not accepted.'}, created_at: ''},
  ]}));
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={vi.fn()} />);
  expect(await screen.findByText(/选择资料列/)).toBeTruthy();
  expect(screen.queryByRole('textbox', {name: '补充回答'})).toBeNull();
  expect(screen.queryByRole('button', {name: '补充并继续'})).toBeNull();
  expect(await screen.findByText(/模型响应流中断/)).toBeTruthy();
  expect(screen.queryByText(/构建服务异常/)).toBeNull();
});

it('can resume preparation interrupted before its first case without authorizing execution', async () => {
  mocks.session.mockResolvedValue(response({...state, jobs: [
    {id: 'preparing', kind: 'build', status: 'failed', input: {prepare_only: true}, output: {intent: 'test', test_operation: 'prepare'}},
  ]}));
  mocks.events.mockResolvedValue(response({events: [
    {sequence: 1, type: 'task.failed', payload: {job_id: 'preparing', error: 'Model stream interrupted before completion'}, created_at: ''},
  ]}));
  mocks.instruct.mockResolvedValue(response({id: 'resumed', status: 'queued'}));
  render(<AgentWorkbench workflowId={7} sessionId="existing" onPreviewChange={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', {name: '继续准备测试集'}));
  await waitFor(() => expect(mocks.instruct).toHaveBeenCalledWith(expect.objectContaining({body: expect.objectContaining({
    mode: 'test', continue_test_job_id: 'preparing', text: expect.stringContaining('不执行 Agent 对话'),
  })})));
  expect(mocks.tune).not.toHaveBeenCalled();
  expect(mocks.adopt).not.toHaveBeenCalled();
});

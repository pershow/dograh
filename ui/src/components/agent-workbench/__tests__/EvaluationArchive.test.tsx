import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';

import { EvaluationArchive } from '../EvaluationArchive';
import type { CaseResult, Evaluation, WorkbenchState } from '../types';

vi.mock('../workbench.module.css', () => ({ default: {} }));
const pages = vi.hoisted(() => vi.fn());
vi.mock('@/client/sdk.gen', () => ({getWorkbenchTestResults: pages}));
beforeEach(() => { pages.mockReset(); });

const result = (name: string, status: string): CaseResult => ({name, status, transcript: [], assertions: [], tool_calls: []});
const evaluation = (id: string, job: string, results: CaseResult[]): Evaluation => ({id, job_id: job, candidate_id: 'tested-version', suite_id: id, status: 'running', summary: {}, results});
const state = {
  evaluations: [evaluation('empty', 'current', []), evaluation('batch-two', 'current', [result('B', 'inconclusive'), result('C', 'running')]), evaluation('batch-one', 'current', [result('A', 'failed')]), evaluation('older', 'previous', [result('Historical', 'passed')])],
  jobs: [{id: 'current', status: 'running'}, {id: 'previous', status: 'cancelled'}],
  suites: [],
} as unknown as WorkbenchState;

it('shows finished cases across all batches while the task is running and opens exact evidence', () => {
  const onOpen = vi.fn();
  render(<EvaluationArchive state={state} onOpen={onOpen} />);
  expect(screen.getByText(/已完成 2 条 · 进行中 1 条 · 共 3 个批次/)).toBeTruthy();
  expect(screen.queryByRole('button', {name: '查看 C 的结果'})).toBeNull();
  fireEvent.click(screen.getByRole('button', {name: '查看 A 的结果'}));
  expect(onOpen).toHaveBeenCalledWith(state.evaluations[2], 'A');
  fireEvent.change(screen.getByLabelText('结果筛选'), {target: {value: 'running'}});
  expect(screen.getByRole('button', {name: '查看 C 的结果'})).toBeTruthy();
  expect(screen.queryByRole('button', {name: '查看 A 的结果'})).toBeNull();
});

it('keeps historical results accessible after cancellation or a new task with no saved cases', () => {
  const next = {...state, evaluations: [state.evaluations[0], state.evaluations[3]]};
  render(<EvaluationArchive state={next} onOpen={vi.fn()} />);
  expect(screen.getByText(/本次任务尚未保存用例结果/)).toBeTruthy();
  fireEvent.change(screen.getByLabelText('结果所属任务'), {target: {value: 'previous'}});
  expect(screen.getByRole('button', {name: '查看 Historical 的结果'})).toBeTruthy();
  expect(within(screen.getByLabelText('结果所属任务')).getByText(/已取消，保留已有结果/)).toBeTruthy();
});

it('filters by name and status without mixing batches or changing selection on refresh', () => {
  const {rerender} = render(<EvaluationArchive state={state} onOpen={vi.fn()} />);
  fireEvent.change(screen.getByLabelText('结果所属任务'), {target: {value: 'previous'}});
  fireEvent.change(screen.getByLabelText('搜索测试用例'), {target: {value: 'histor'}});
  rerender(<EvaluationArchive state={{...state, evaluations: [evaluation('newest', 'current', [result('New result', 'passed')]), ...state.evaluations]}} onOpen={vi.fn()} />);
  expect(screen.getByRole('button', {name: '查看 Historical 的结果'})).toBeTruthy();
  expect(screen.queryByRole('button', {name: '查看 New result 的结果'})).toBeNull();
  fireEvent.change(screen.getByLabelText('结果筛选'), {target: {value: 'failed'}});
  expect(screen.getByText(/没有符合筛选条件的结果/)).toBeTruthy();
});

it('loads every saved batch beyond the recent context and retries a failed page', async () => {
  const all = Array.from({length: 113}, (_, i) => ({...evaluation(`batch-${i}`, 'whole', [result(`Case ${i}`, 'passed')]), status: 'completed'}));
  const next = {...state, revision: 1, evaluations: all.slice(-100), jobs: [{id: 'whole', status: 'completed', output: {evaluation_ids: all.map(e => e.id)}}]} as unknown as WorkbenchState;
  pages.mockResolvedValueOnce({error: {detail: 'temporary'}});
  pages.mockImplementation(async ({query}: {query: {offset: number; limit: number}}) => ({data: {data: {evaluations: all.slice(query.offset, query.offset + query.limit), next_offset: query.offset + query.limit < all.length ? query.offset + query.limit : null}}}));
  const open = vi.fn();
  render(<EvaluationArchive sessionId="session" state={next} onOpen={open} />);
  expect((await screen.findByRole('alert')).textContent).toContain('测试结果分页加载失败');
  fireEvent.click(screen.getByRole('button', {name: '重新加载结果'}));
  expect(await screen.findByText(/已完成 113 条.*共 113 个批次/)).toBeTruthy();
  await waitFor(() => expect(screen.queryByText(/正在读取该任务/)).toBeNull());
  expect(screen.queryByText(/当前列表尚未完整/)).toBeNull();
  expect(pages.mock.calls.slice(1).map(c => c[0].query.offset)).toEqual([0, 50, 100]);
  fireEvent.click(screen.getByRole('button', {name: '查看 Case 0 的结果'}));
  expect(open).toHaveBeenCalledWith(all[0], 'Case 0');
});

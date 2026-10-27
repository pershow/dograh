import { fireEvent, render, screen, within } from '@testing-library/react';
import { expect, it, vi } from 'vitest';

import { EvaluationArchive } from '../EvaluationArchive';
import type { CaseResult, Evaluation, WorkbenchState } from '../types';

vi.mock('../workbench.module.css', () => ({ default: {} }));

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

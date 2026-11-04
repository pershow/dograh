import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';

import type { Evaluation } from '../types';
import { useEvaluationDetail } from '../useEvaluationDetail';

const load = vi.hoisted(() => vi.fn());
vi.mock('@/client/sdk.gen', () => ({getWorkbenchEvaluation: load}));
const evidence = (text: string) => [{name: 'Case', status: 'passed', transcript: [{role: 'assistant' as const, text}], assertions: [], tool_calls: []}];
const index: Evaluation = {id: 'evaluation', candidate_id: 'c', suite_id: 's', status: 'running', summary_only: true, detail_revision: 'v1', summary: {}, results: []};
const response = (data: Evaluation) => ({data: {data}});
const deferred = () => {
  let resolve!: (value: ReturnType<typeof response>) => void;
  return {promise: new Promise<ReturnType<typeof response>>(r => {resolve = r;}), resolve};
};
beforeEach(() => {load.mockReset();});

it('renders before a session or result is selected without fetching', () => {
  const {result} = renderHook(() => useEvaluationDetail(undefined, undefined, undefined, true));
  expect(result.current.evaluation).toBeUndefined();
  expect(load).not.toHaveBeenCalled();
});

it('retains evidence through refresh and failure, retries explicitly, and ignores unrelated revisions', async () => {
  load.mockResolvedValueOnce(response({...index, summary_only: false, results: evidence('Saved reply')}));
  const hook = renderHook(({item, revision}) => useEvaluationDetail('session', item, revision, true), {initialProps: {item: index, revision: 'session1'}});
  await waitFor(() => expect(hook.result.current.evaluation?.results).toEqual(evidence('Saved reply')));
  hook.rerender({item: {...index}, revision: 'session2'});
  expect(load).toHaveBeenCalledOnce();

  const pending = deferred();
  load.mockReturnValueOnce(pending.promise);
  hook.rerender({item: {...index, detail_revision: 'v2'}, revision: 'session3'});
  expect(hook.result.current.loading).toBe(true);
  expect(hook.result.current.evaluation?.results).toEqual(evidence('Saved reply'));
  // A different batch progressing must not cancel the selected batch's request.
  const signal = load.mock.lastCall![0].signal;
  hook.rerender({item: {...index, detail_revision: 'v2'}, revision: 'session4'});
  expect(signal.aborted).toBe(false);
  expect(load).toHaveBeenCalledTimes(2);
  await act(async () => pending.resolve(response({...index, detail_revision: 'v2', results: evidence('Updated reply')})));
  expect(hook.result.current.evaluation?.results).toEqual(evidence('Updated reply'));

  load.mockResolvedValueOnce({error: {detail: 'Connection unavailable'}});
  hook.rerender({item: {...index, detail_revision: 'v3'}, revision: 'session5'});
  await waitFor(() => expect(hook.result.current.error).toBe('Connection unavailable'));
  expect(hook.result.current.evaluation?.results).toEqual(evidence('Updated reply'));
  hook.rerender({item: {...index, detail_revision: 'v3'}, revision: 'session6'});
  expect(load).toHaveBeenCalledTimes(3);
  load.mockResolvedValueOnce(response({...index, detail_revision: 'v3', results: evidence('Final reply')}));
  act(() => hook.result.current.retry());
  await waitFor(() => expect(hook.result.current.evaluation?.results).toEqual(evidence('Final reply')));
  expect(hook.result.current.error).toBe('');
});

it('rejects stale responses across versions, selections and sessions, including clients that ignore abort', async () => {
  const first = deferred(), second = deferred(), other = deferred();
  load.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise).mockReturnValueOnce(other.promise);
  const hook = renderHook(({item, session}) => useEvaluationDetail(session, item, 'revision', true), {initialProps: {item: index, session: 's1'}});
  const signal = load.mock.calls[0][0].signal;
  hook.rerender({item: {...index, detail_revision: 'v2'}, session: 's1'});
  expect(signal.aborted).toBe(true);
  await act(async () => second.resolve(response({...index, detail_revision: 'v2', results: evidence('New reply')})));
  await act(async () => first.resolve(response({...index, results: evidence('Stale reply')})));
  expect(hook.result.current.evaluation?.results).toEqual(evidence('New reply'));
  hook.rerender({item: {...index, id: 'other'}, session: 's1'});
  expect(hook.result.current.evaluation?.results).toEqual([]);
  load.mockResolvedValueOnce(response({...index, results: evidence('Other session')}));
  hook.rerender({item: index, session: 's2'});
  await waitFor(() => expect(hook.result.current.evaluation?.results).toEqual(evidence('Other session')));
  await act(async () => other.resolve(response({...index, id: 'other', results: evidence('Old selection')})));
  expect(hook.result.current.evaluation?.results).toEqual(evidence('Other session'));
});

it('waits for auth and supports running indexes from older servers without clearing evidence', async () => {
  const legacy = {...index, detail_revision: undefined};
  load.mockResolvedValue(response({...legacy, results: evidence('Legacy reply')}));
  const hook = renderHook(({enabled, revision}) => useEvaluationDetail('session', legacy, revision, enabled), {initialProps: {enabled: false, revision: 'r1'}});
  expect(load).not.toHaveBeenCalled();
  hook.rerender({enabled: true, revision: 'r1'});
  await waitFor(() => expect(hook.result.current.evaluation?.results).toEqual(evidence('Legacy reply')));
  load.mockReturnValueOnce(new Promise(() => {}));
  hook.rerender({enabled: true, revision: 'r2'});
  expect(load).toHaveBeenCalledTimes(2);
  expect(hook.result.current.evaluation?.results).toEqual(evidence('Legacy reply'));
});

it('keeps a pinned batch completed when its running summary has left the compact index', async () => {
  const historical = {...index, detail_revision: undefined};
  load.mockResolvedValue(response({...historical, status: 'completed', summary: {passed: 1}, results: evidence('Final reply')}));
  const hook = renderHook(({revision}) => useEvaluationDetail('session', historical, revision, true), {initialProps: {revision: 'r1'}});
  await waitFor(() => expect(hook.result.current.evaluation?.status).toBe('completed'));
  hook.rerender({revision: 'r2'});
  expect(load).toHaveBeenCalledOnce();
  expect(hook.result.current.evaluation?.summary).toEqual({passed: 1});
  expect(hook.result.current.evaluation?.results).toEqual(evidence('Final reply'));
});

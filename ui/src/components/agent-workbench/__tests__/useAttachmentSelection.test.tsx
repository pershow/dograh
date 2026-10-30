import { act, renderHook } from '@testing-library/react';
import { beforeEach, expect, it } from 'vitest';
import { useAttachmentSelection } from '../useAttachmentSelection';

beforeEach(() => sessionStorage.clear());

it('retains unsent uploads across remounts, isolates chats and clears only after send', () => {
  const first = renderHook(() => useAttachmentSelection('first'));
  act(() => first.result.current[1](['uploaded-file']));
  first.unmount();
  const again = renderHook(({session}) => useAttachmentSelection(session), {initialProps: {session: 'first'}});
  expect(again.result.current[0]).toEqual(['uploaded-file']);
  again.rerender({session: 'second'});
  expect(again.result.current[0]).toEqual([]);
  again.rerender({session: 'first'});
  expect(again.result.current[0]).toEqual(['uploaded-file']);
  act(() => again.result.current[1]([]));
  again.unmount();
  expect(renderHook(() => useAttachmentSelection('first')).result.current[0]).toEqual([]);
});

it('tolerates unavailable or malformed stored selections', () => {
  sessionStorage.setItem('workbench:attachments:broken', '{');
  expect(renderHook(() => useAttachmentSelection('broken')).result.current[0]).toEqual([]);
  sessionStorage.setItem('workbench:attachments:types', JSON.stringify([null, 'valid', {}]));
  expect(renderHook(() => useAttachmentSelection('types')).result.current[0]).toEqual(['valid']);
});

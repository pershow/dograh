// @vitest-environment node
import { describe, expect, it, vi } from 'vitest';

import { createClient } from '@/client/client';

import { setupAuthInterceptor } from './apiClient';

describe('SDK authentication lifecycle', () => {
  it('uses the latest provider after a remount without duplicate interceptors', async () => {
    const fetchMock = vi.fn().mockImplementation(() => Response.json({}));
    const client = createClient({ baseUrl: 'http://backend.test', fetch: fetchMock });
    setupAuthInterceptor(client, async () => 'old');
    await client.get({ url: '/first' });
    setupAuthInterceptor(client, async () => 'new');
    await client.get({ url: '/second' });
    expect(fetchMock.mock.calls.map(([request]) => request.headers.get('Authorization')))
      .toEqual(['Bearer old', 'Bearer new']);
    expect(client.interceptors.request.fns.filter(Boolean)).toHaveLength(1);
    expect(client.interceptors.response.fns.filter(Boolean)).toHaveLength(1);
  });

  it('recovers a rejected current session', async () => {
    const onUnauthorized = vi.fn();
    const client = createClient({
      baseUrl: 'http://backend.test',
      fetch: async () => Response.json({ detail: 'Invalid token' }, { status: 401 }),
    });
    setupAuthInterceptor(client, async () => 'current', onUnauthorized);
    const result = await client.get({ url: '/protected' });
    expect(onUnauthorized).toHaveBeenCalledOnce();
    expect(result.error).toEqual({ detail: 'Invalid token' });
  });

  it('ignores a delayed 401 from the previous session', async () => {
    const onUnauthorized = vi.fn();
    let finish!: (response: Response) => void;
    let started!: () => void;
    const fetching = new Promise<void>(resolve => { started = resolve; });
    const client = createClient({
      baseUrl: 'http://backend.test',
      fetch: () => { started(); return new Promise(resolve => { finish = resolve; }); },
    });
    setupAuthInterceptor(client, async () => 'old', onUnauthorized);
    const pending = client.get({ url: '/protected' });
    await fetching;
    setupAuthInterceptor(client, async () => 'new', onUnauthorized);
    finish(new Response(null, { status: 401 }));
    await pending;
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it.each([403, 500, 503])('does not sign out on status %s', async status => {
    const onUnauthorized = vi.fn();
    const client = createClient({
      baseUrl: 'http://backend.test',
      fetch: async () => new Response(null, { status }),
    });
    setupAuthInterceptor(client, async () => 'current', onUnauthorized);
    await client.get({ url: '/protected' });
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it('preserves explicit credentials and does not sign out for their failures', async () => {
    const onUnauthorized = vi.fn();
    const fetchMock = vi.fn().mockImplementation(() => new Response(null, { status: 401 }));
    const client = createClient({ baseUrl: 'http://backend.test', fetch: fetchMock });
    setupAuthInterceptor(client, async () => 'current', onUnauthorized);
    await client.get({ url: '/public', headers: { Authorization: 'Bearer explicit' } });
    expect(fetchMock.mock.calls[0][0].headers.get('Authorization')).toBe('Bearer explicit');
    expect(onUnauthorized).not.toHaveBeenCalled();
  });

  it('does not attach an empty token or redirect on an unauthenticated login failure', async () => {
    const onUnauthorized = vi.fn();
    const fetchMock = vi.fn().mockImplementation(() => new Response(null, { status: 401 }));
    const client = createClient({ baseUrl: 'http://backend.test', fetch: fetchMock });
    setupAuthInterceptor(client, async () => '', onUnauthorized);
    await client.post({ url: '/login' });
    expect(fetchMock.mock.calls[0][0].headers.has('Authorization')).toBe(false);
    expect(onUnauthorized).not.toHaveBeenCalled();
  });
});

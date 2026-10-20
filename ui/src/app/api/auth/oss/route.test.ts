// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  getCookie: vi.fn(),
  getAuthProvider: vi.fn(),
}));
vi.mock('next/headers', () => ({ cookies: async () => ({ get: mocks.getCookie }) }));
vi.mock('@/lib/auth/config', () => ({ getAuthProvider: mocks.getAuthProvider }));

import { OSS_TOKEN_COOKIE } from '@/lib/auth/cookies';

import { GET } from './route';

describe('GET /api/auth/oss', () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubEnv('BACKEND_URL', 'http://backend.test:8016');
    vi.stubGlobal('fetch', fetchMock);
    mocks.getAuthProvider.mockResolvedValue('local');
    // A stale or malformed user cookie must not supply the authenticated identity.
    mocks.getCookie.mockImplementation(name => ({
      value: name === OSS_TOKEN_COOKIE ? 'stored-token' : '{broken-user',
    }));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it('verifies with the configured backend and returns its current user', async () => {
    fetchMock.mockResolvedValue(Response.json({
      id: 12, email: 'current@example.com', name: 'Current User',
      organization_id: 34, provider_id: 'local-12',
    }));
    const response = await GET();
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      token: 'stored-token',
      user: {
        id: '12', email: 'current@example.com', name: 'Current User',
        organizationId: '34', provider_id: 'local-12', provider: 'local',
      },
    });
    const request = fetchMock.mock.calls[0][0] as Request;
    expect(request.url).toBe('http://backend.test:8016/api/v1/auth/me');
    expect(request.headers.get('Authorization')).toBe('Bearer stored-token');
    expect(request.cache).toBe('no-store');
    expect(response.headers.get('Cache-Control')).toBe('no-store');
  });

  it('rejects an invalid JWT even when both cookies exist', async () => {
    fetchMock.mockResolvedValue(Response.json({ detail: 'Invalid or expired token' }, { status: 401 }));
    const response = await GET();
    expect(response.status).toBe(401);
    expect(await response.json()).not.toHaveProperty('token');
  });

  it('requires a token and skips verification when it is missing', async () => {
    mocks.getCookie.mockReturnValue(undefined);
    expect((await GET()).status).toBe(401);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([500, 502, 503])('keeps the session on backend status %s', async status => {
    fetchMock.mockResolvedValue(new Response('Unavailable', { status }));
    const response = await GET();
    expect(response.status).toBe(503);
    expect(response.headers.get('Set-Cookie')).toBeNull();
    expect(await response.json()).not.toHaveProperty('token');
  });

  it('returns a retryable error on network failure', async () => {
    fetchMock.mockRejectedValue(new TypeError('fetch failed'));
    expect((await GET()).status).toBe(503);
  });

  it('does not run local validation for Stack auth', async () => {
    mocks.getAuthProvider.mockResolvedValue('stack');
    expect((await GET()).status).toBe(400);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

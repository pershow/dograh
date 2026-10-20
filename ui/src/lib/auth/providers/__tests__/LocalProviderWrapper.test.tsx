import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { LocalUser } from '@/lib/auth/types';

vi.mock('@/lib/logger', () => ({
  default: { info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

import { useAuth } from '../AuthProvider';
import { LocalProviderWrapper } from '../LocalProviderWrapper';

function Probe() {
  const auth = useAuth();
  return <div>{auth.loading ? 'Loading' : (auth.user as LocalUser | null)?.email || 'Signed out'}</div>;
}

describe('LocalProviderWrapper', () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    vi.stubGlobal('fetch', fetchMock);
    fetchMock.mockReset();
    window.history.replaceState({}, '', '/workflow/create');
  });
  afterEach(() => vi.unstubAllGlobals());

  it('lets the user retry a failed validation without discarding their session', async () => {
    fetchMock.mockResolvedValueOnce(Response.json({ error: 'Unavailable' }, { status: 503 }))
      .mockResolvedValueOnce(Response.json({
        token: 'verified-token', user: { id: '1', email: 'user@example.com', provider: 'local' },
      }));
    render(<LocalProviderWrapper><Probe /></LocalProviderWrapper>);
    expect(await screen.findByRole('alert')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('user@example.com')).toBeTruthy();
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual(['/api/auth/oss', '/api/auth/oss']);
  });

  it('leaves the login form accessible with an invalid cookie', async () => {
    window.history.replaceState({}, '', '/auth/login');
    fetchMock.mockResolvedValue(Response.json({ error: 'Invalid session' }, { status: 401 }));
    render(<LocalProviderWrapper><Probe /></LocalProviderWrapper>);
    expect(await screen.findByText('Signed out')).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it('aborts validation when the provider unmounts', async () => {
    fetchMock.mockReturnValue(new Promise(() => {}));
    const rendered = render(<LocalProviderWrapper><Probe /></LocalProviderWrapper>);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
    const signal = fetchMock.mock.calls[0][1].signal as AbortSignal;
    rendered.unmount();
    expect(signal.aborted).toBe(true);
  });
});

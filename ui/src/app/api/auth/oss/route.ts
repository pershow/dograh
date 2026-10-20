import { cookies } from 'next/headers';
import { NextResponse } from 'next/server';

import { getCurrentUserApiV1AuthMeGet } from '@/client/sdk.gen';
import { getServerBackendUrl } from '@/lib/apiClient';
import { getAuthProvider } from '@/lib/auth/config';
import { OSS_TOKEN_COOKIE } from '@/lib/auth/cookies';

const headers = { 'Cache-Control': 'no-store' };

export async function GET() {
  const authProvider = await getAuthProvider();

  if (authProvider !== 'local') {
    return NextResponse.json({ error: 'Not in OSS mode' }, { status: 400, headers });
  }

  const cookieStore = await cookies();
  const token = cookieStore.get(OSS_TOKEN_COOKIE)?.value;

  if (!token) {
    return NextResponse.json({ error: 'Not authenticated' }, { status: 401, headers });
  }

  // A cookie can outlive its JWT or belong to a different backend instance.
  // Let the configured backend verify it, and use its current user identity.
  const result = await getCurrentUserApiV1AuthMeGet({
    baseUrl: getServerBackendUrl(),
    headers: { Authorization: `Bearer ${token}` },
    cache: 'no-store',
    signal: AbortSignal.timeout(5000),
  });
  if (result.response?.status === 401) {
    return NextResponse.json({ error: 'Invalid session. Please sign in again.' }, { status: 401, headers });
  }
  if (result.error || !result.data) {
    // An outage is not an invalid session. Keep the cookies so the user can retry.
    return NextResponse.json({ error: 'Unable to verify your session. Please try again.' }, { status: 503, headers });
  }

  const user = result.data;
  return NextResponse.json({
    token,
    user: {
      id: String(user.id),
      email: user.email ?? undefined,
      name: user.name ?? undefined,
      organizationId: user.organization_id == null ? undefined : String(user.organization_id),
      provider_id: user.provider_id ?? undefined,
      provider: 'local',
    },
  }, { headers });
}

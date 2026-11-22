const CODE_KEY = 'sourcing-access-code';

function storedCode(): string {
  try { return localStorage.getItem(CODE_KEY) ?? ''; } catch { return ''; }
}

export function setAccessCode(code: string) {
  try { if (code) localStorage.setItem(CODE_KEY, code); else localStorage.removeItem(CODE_KEY); } catch { /* storage unavailable */ }
}

export class AccessDenied extends Error {}

export async function api<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  const code = storedCode();
  if (code) headers['X-Access-Code'] = code;
  const response = await fetch(`/api${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const result = await response.json();
  if (response.status === 401) throw new AccessDenied(result.error?.message ?? 'Passcode required');
  if (!response.ok) throw new Error([result.error?.message, ...(result.error?.fields ?? [])].filter(Boolean).join(': ') || `HTTP ${response.status}`);
  return result as T;
}

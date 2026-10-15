export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      ...options,
      headers: { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers },
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new Error('서버에 연결할 수 없습니다. 서버 상태와 네트워크를 확인해 주세요.');
  }
  const data: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = data && typeof data === 'object' && 'detail' in data ? data.detail : null;
    const message = typeof detail === 'string' ? detail : detail && typeof detail === 'object' && 'message' in detail && typeof detail.message === 'string' ? detail.message : Array.isArray(detail)
      ? detail.map((item: { msg?: string }) => item.msg || '입력값을 확인해 주세요.').join(' ')
      : `요청을 처리하지 못했습니다 (${response.status}). 다시 시도해 주세요.`;
    throw new Error(message);
  }
  if (data === null) throw new Error('서버 응답을 읽을 수 없습니다. 다시 시도해 주세요.');
  return data as T;
}

export const errorMessage = (error: unknown) => error instanceof Error ? error.message : '요청을 처리하지 못했습니다.';

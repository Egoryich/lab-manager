import createClient from 'openapi-fetch';
import type { paths, components } from '@lab/shared-types';

let csrfToken: string | null = null;
export const setCsrfToken = (token: string | null) => {
  csrfToken = token;
};
export const api = createClient<paths>({ baseUrl: '', credentials: 'same-origin' });
api.use({
  onRequest({ request }) {
    if (csrfToken && !['GET', 'HEAD', 'OPTIONS'].includes(request.method)) {
      request.headers.set('X-CSRF-Token', csrfToken);
    }
    return request;
  },
});
export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}
export function unwrap<T>(result: {
  data?: T;
  error?: components['schemas']['ErrorView'];
  response: Response;
}): T {
  if (!result.response.ok) {
    throw new ApiError(
      result.response.status,
      result.error?.code ?? 'REQUEST_FAILED',
      result.error?.message ?? 'Не удалось выполнить запрос. Попробуйте ещё раз.',
    );
  }
  return result.data as T;
}

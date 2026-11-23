import {resolveApiUrl} from './config.js';
// 独立前端直连配置的后端；会话由 HttpOnly Cookie 管理，不自动重试写操作。
export function apiUrl(path) { return resolveApiUrl(path, __API_BASE_URL__); }
export class ApiError extends Error {
  constructor(message, status = 0, code = '', requestId = '') {
    super(message); Object.assign(this, {status, code, requestId});
  }
}
export async function request(path, {csrf, method = 'GET', body, timeout = 15000, signal} = {}) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal?.addEventListener('abort', abort, {once:true});
  if (signal?.aborted) controller.abort();
  const timer = setTimeout(abort, timeout);
  try {
    const headers = {Accept:'application/json'};
    if (csrf && !['GET','HEAD'].includes(method)) headers['X-CSRF-Token'] = csrf;
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const response = await fetch(apiUrl(path), {method, headers, body:body === undefined ? undefined : JSON.stringify(body), signal:controller.signal, cache:'no-store', credentials:'include'});
    if (response.status === 204) return null;
    const data = await response.json().catch(() => null);
    if (!response.ok) throw new ApiError(data?.error?.message || `请求失败（${response.status}）`, response.status, data?.error?.code, data?.request_id || response.headers.get('X-Request-ID'));
    if (data === null) throw new ApiError('服务返回了无法识别的内容');
    return data;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (signal?.aborted) throw new ApiError('连接已取消', 0, 'cancelled');
    throw new ApiError(error.name === 'AbortError' ? '请求超时，请刷新确认结果后再操作' : '无法连接服务，请检查服务是否已启动');
  } finally { clearTimeout(timer); signal?.removeEventListener('abort', abort); }
}

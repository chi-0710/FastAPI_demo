export function validateApiBase(value) {
  const url = new URL(value);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password ||
      !['', '/'].includes(url.pathname) || url.search || url.hash) {
    throw new Error('VITE_API_BASE_URL 必须为不含凭据、路径或查询参数的 HTTP(S) 服务地址');
  }
  return url.origin;
}

export function resolveApiUrl(path, base) {
  if (!path.startsWith('/') || path.startsWith('//') || path.includes('\\')) {
    throw new Error('API path 必须是后端服务内的绝对路径');
  }
  return new URL(path, validateApiBase(base)).href;
}

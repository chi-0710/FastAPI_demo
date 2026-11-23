import test from 'node:test';
import assert from 'node:assert/strict';
import {request} from '../assets/api.js';

globalThis.__API_BASE_URL__ = 'http://127.0.0.1:8000';

test('登录凭证由 Cookie 携带，写请求额外携带 CSRF，读取不携带 CSRF', async t => {
  const calls = [];
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({url, options});
    return new Response(JSON.stringify({ok:true}), {status:200});
  });
  await request('/api/v1/todos', {csrf:'test-token', method:'POST', body:{title:'test'}});
  await request('/api/v1/auth/me', {csrf:'test-token'});
  assert.equal(calls[0].options.credentials, 'include');
  assert.equal(calls[0].options.headers['X-CSRF-Token'], 'test-token');
  assert.equal(calls[0].options.headers['X-API-Key'], undefined);
  assert.equal(calls[1].options.headers['X-CSRF-Token'], undefined);
  assert.equal(calls[1].options.credentials, 'include');
});

test('会话失效和跨站拒绝保留状态码供界面处理', async t => {
  for (const status of [401,403,429]) {
    t.mock.method(globalThis, 'fetch', async () => new Response(
      JSON.stringify({error:{message:'请重新登录',code:'auth_error'},request_id:'test-request'}), {status}
    ));
    await assert.rejects(request('/api/v1/auth/me'), error =>
      error.status === status && error.requestId === 'test-request');
  }
});

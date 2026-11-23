import test from 'node:test';
import assert from 'node:assert/strict';
import {validateApiBase, resolveApiUrl} from '../assets/config.js';

test('API 地址与路径正确拼接，文档指向后端', () => {
  assert.equal(validateApiBase('http://127.0.0.1:8000/'), 'http://127.0.0.1:8000');
  assert.equal(resolveApiUrl('/api/v1/todos?limit=8', 'https://api.example.com'), 'https://api.example.com/api/v1/todos?limit=8');
  assert.equal(resolveApiUrl('/docs', 'http://localhost:8000'), 'http://localhost:8000/docs');
});
test('拒绝不安全配置和跳转到外部服务的路径', () => {
  for (const value of ['javascript:alert(1)', 'https://user:secret@example.com', 'https://example.com/api']) {
    assert.throws(() => validateApiBase(value));
  }
  for (const path of ['//evil.example', 'https://evil.example', '/\\evil.example']) {
    assert.throws(() => resolveApiUrl(path, 'https://api.example.com'));
  }
});

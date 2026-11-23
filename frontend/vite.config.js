import {defineConfig, loadEnv} from 'vite';
import {validateApiBase} from './assets/config.js';

export default defineConfig(({mode}) => {
  const env = loadEnv(mode, process.cwd(), 'VITE_');
  const apiBase = validateApiBase(env.VITE_API_BASE_URL || 'http://127.0.0.1:8000');
  return {
    define: {__API_BASE_URL__: JSON.stringify(apiBase)},
    server: {host: '127.0.0.1', port: 5173, strictPort: true},
    preview: {host: '127.0.0.1', port: 5173, strictPort: true},
    build: {target: 'es2022'},
  };
});

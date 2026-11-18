import { defineConfig } from 'vite';

const proxy = { '/api': 'http://127.0.0.1:8011' };
export default defineConfig({ server: { port: 5174, strictPort: true, proxy }, preview: { port: 5174, strictPort: true, proxy } });

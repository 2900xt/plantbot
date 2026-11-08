import { defineConfig } from 'vite';
export default defineConfig({ root: 'app', server: { proxy: { '/api': 'http://127.0.0.1:8080' } }, build: { outDir: '../dist', emptyOutDir: true,rollupOptions:{input:['app/index.html','app/capture.html']} } });

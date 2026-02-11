import { defineConfig } from 'vite';

export default defineConfig({
    root: '.',
    server: {
        port: 5173,
        proxy: {
            '/api': {
                target: 'http://127.0.0.1:8741',
                changeOrigin: true,
                // Disable proxy buffering for SSE streaming
                configure: (proxy) => {
                    proxy.on('proxyRes', (proxyRes) => {
                        // Prevent http-proxy from buffering chunked responses
                        if (proxyRes.headers['content-type']?.includes('text/event-stream')) {
                            proxyRes.headers['cache-control'] = 'no-cache';
                            proxyRes.headers['x-accel-buffering'] = 'no';
                        }
                    });
                },
            },
        },
    },
    build: {
        outDir: 'dist',
    },
});

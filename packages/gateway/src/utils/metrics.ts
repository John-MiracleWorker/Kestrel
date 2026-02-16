import { FastifyInstance } from 'fastify';
import client from 'prom-client';

// Create a Registry
const register = new client.Registry();
client.collectDefaultMetrics({ register });

// Custom metrics
export const httpRequestDuration = new client.Histogram({
    name: 'gateway_http_request_duration_seconds',
    help: 'HTTP request duration in seconds',
    labelNames: ['method', 'route', 'status_code'],
    buckets: [0.01, 0.05, 0.1, 0.5, 1, 5],
    registers: [register],
});

export const wsConnectionsGauge = new client.Gauge({
    name: 'gateway_ws_connections_total',
    help: 'Number of active WebSocket connections',
    registers: [register],
});

export const grpcRequestDuration = new client.Histogram({
    name: 'gateway_grpc_request_duration_seconds',
    help: 'gRPC request duration in seconds',
    labelNames: ['method', 'status'],
    buckets: [0.01, 0.05, 0.1, 0.5, 1, 5, 10],
    registers: [register],
});

export const messageCounter = new client.Counter({
    name: 'gateway_messages_total',
    help: 'Total messages processed',
    labelNames: ['channel', 'direction'],
    registers: [register],
});

/**
 * Set up Prometheus metrics endpoint on the Fastify app.
 */
export function setupMetrics(app: FastifyInstance): void {
    // Metrics endpoint
    app.get('/metrics', async (req, reply) => {
        reply.header('Content-Type', register.contentType);
        return register.metrics();
    });

    // Hook into request lifecycle for HTTP metrics
    app.addHook('onResponse', async (request, reply) => {
        const route = request.routeOptions?.url || request.url;
        httpRequestDuration
            .labels(request.method, route, reply.statusCode.toString())
            .observe(reply.elapsedTime / 1000);
    });
}

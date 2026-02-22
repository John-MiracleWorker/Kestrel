/**
 * Direct PostgreSQL connection pool for gateway.
 * Used for MCP tool CRUD operations that bypass gRPC.
 */
import { Pool } from 'pg';
import { logger } from '../utils/logger';

let pool: Pool | null = null;

export function getPool(): Pool {
    if (!pool) {
        pool = new Pool({
            host: process.env.POSTGRES_HOST || 'localhost',
            port: parseInt(process.env.POSTGRES_PORT || '5432', 10),
            database: process.env.POSTGRES_DB || 'kestrel',
            user: process.env.POSTGRES_USER || 'kestrel',
            password: process.env.POSTGRES_PASSWORD || 'changeme',
            max: 5,
            idleTimeoutMillis: 30000,
        });
        pool.on('error', (err: Error) => {
            logger.error('Unexpected PG pool error:', err);
        });
        logger.info('Gateway PG pool created');
    }
    return pool;
}

export async function closePool(): Promise<void> {
    if (pool) {
        await pool.end();
        pool = null;
        logger.info('Gateway PG pool closed');
    }
}

/**
 * File upload route for chat attachments.
 * Files are saved to a local `uploads/` directory and served statically.
 */
import { FastifyInstance } from 'fastify';
import { randomUUID } from 'crypto';
import path from 'path';
import fs from 'fs';
import { requireAuth } from '../auth/middleware';

const UPLOAD_DIR = process.env.UPLOAD_DIR || '/app/uploads';
const MAX_FILE_SIZE = 10 * 1024 * 1024; // 10 MB
const ALLOWED_MIME_PREFIXES = [
    'image/',
    'text/',
    'application/pdf',
    'application/json',
    'application/javascript',
    'application/xml',
    'application/x-yaml',
    'application/octet-stream',
];

export default async function uploadRoutes(app: FastifyInstance) {
    // Ensure upload directory exists
    fs.mkdirSync(UPLOAD_DIR, { recursive: true });

    // ── POST /api/upload ────────────────────────────────────────────
    app.post('/api/upload', {
        preHandler: [requireAuth],
    }, async (req, reply) => {
        const parts = req.parts();
        const uploaded: Array<{
            id: string;
            filename: string;
            mimeType: string;
            size: number;
            url: string;
        }> = [];

        for await (const part of parts) {
            if (part.type !== 'file' || !part.filename) continue;

            // Validate MIME type
            const mime = part.mimetype || 'application/octet-stream';
            const allowed = ALLOWED_MIME_PREFIXES.some(p => mime.startsWith(p));
            if (!allowed) {
                return reply.status(400).send({
                    error: `Unsupported file type: ${mime}`,
                });
            }

            // Generate unique filename
            const ext = path.extname(part.filename) || '';
            const id = randomUUID();
            const storedName = `${id}${ext}`;
            const filePath = path.join(UPLOAD_DIR, storedName);

            // Stream to disk with size check
            const chunks: Buffer[] = [];
            let totalSize = 0;

            for await (const chunk of part.file) {
                totalSize += chunk.length;
                if (totalSize > MAX_FILE_SIZE) {
                    return reply.status(413).send({
                        error: `File too large. Max size: ${MAX_FILE_SIZE / 1024 / 1024}MB`,
                    });
                }
                chunks.push(chunk);
            }

            fs.writeFileSync(filePath, Buffer.concat(chunks));

            uploaded.push({
                id,
                filename: part.filename,
                mimeType: mime,
                size: totalSize,
                url: `/uploads/${storedName}`,
            });
        }

        if (uploaded.length === 0) {
            return reply.status(400).send({ error: 'No files uploaded' });
        }

        return { files: uploaded };
    });

    // ── Static file serving for uploads ─────────────────────────────
    app.register(import('@fastify/static'), {
        root: UPLOAD_DIR,
        prefix: '/uploads/',
        decorateReply: false,
    });
}

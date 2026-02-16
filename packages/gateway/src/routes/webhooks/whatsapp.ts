import { FastifyInstance } from 'fastify';
import { logger } from '../../utils/logger';

interface WebhookDeps {
    whatsappAdapter: import('../../channels/whatsapp').WhatsAppAdapter;
}

/**
 * WhatsApp / Twilio webhook route.
 * Receives inbound messages from Twilio, validates the signature,
 * and routes them to the WhatsAppAdapter for processing.
 *
 * Endpoint: POST /webhooks/whatsapp
 */
export default async function whatsappWebhookRoutes(
    app: FastifyInstance,
    deps: WebhookDeps,
): Promise<void> {
    const { whatsappAdapter } = deps;

    app.post('/webhooks/whatsapp', async (req, reply) => {
        try {
            const body = req.body as Record<string, string>;

            // Validate Twilio signature
            const signature = req.headers['x-twilio-signature'] as string;
            if (!signature) {
                return reply.code(401).send({ error: 'Missing Twilio signature' });
            }

            // Build the full URL for signature validation
            const proto = req.headers['x-forwarded-proto'] || 'https';
            const host = req.headers['x-forwarded-host'] || req.headers.host;
            const fullUrl = `${proto}://${host}${req.url}`;

            const valid = whatsappAdapter.validateSignature(fullUrl, body, signature);
            if (!valid) {
                logger.warn('WhatsApp webhook signature validation failed');
                return reply.code(403).send({ error: 'Invalid signature' });
            }

            // Process asynchronously — respond 200 with TwiML
            setImmediate(() => {
                whatsappAdapter.processWebhook(body).catch((err) => {
                    logger.error('Failed to process WhatsApp webhook', {
                        messageSid: body.MessageSid,
                        error: (err as Error).message,
                    });
                });
            });

            // Respond with empty TwiML (we send replies via REST API)
            reply
                .code(200)
                .header('Content-Type', 'text/xml')
                .send('<Response></Response>');

        } catch (err) {
            logger.error('WhatsApp webhook error', { error: (err as Error).message });
            return reply.code(500).send({ error: 'Internal error' });
        }
    });

    // Status callback endpoint for delivery receipts
    app.post('/webhooks/whatsapp/status', async (req, reply) => {
        const body = req.body as Record<string, string>;
        logger.info('WhatsApp status callback', {
            messageSid: body.MessageSid,
            status: body.MessageStatus,
            to: body.To,
        });
        return reply.code(200).send({ ok: true });
    });
}

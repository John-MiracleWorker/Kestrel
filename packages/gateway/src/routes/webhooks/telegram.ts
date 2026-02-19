import { FastifyInstance } from 'fastify';
import { z } from 'zod';
import { ZodTypeProvider } from 'fastify-type-provider-zod';
import { logger } from '../../utils/logger';

interface WebhookDeps {
    telegramAdapter: import('../../channels/telegram').TelegramAdapter;
}

/**
 * Telegram webhook route.
 * Receives updates from the Telegram Bot API and routes them
 * to the TelegramAdapter for processing.
 *
 * Endpoint: POST /webhooks/telegram
 */
export default async function telegramWebhookRoutes(
    app: FastifyInstance,
    deps: WebhookDeps,
): Promise<void> {
    const { telegramAdapter } = deps;
    const typedApp = app.withTypeProvider<ZodTypeProvider>();

    const telegramUpdateSchema = z.object({
        update_id: z.number(),
    }).passthrough();
    type TelegramUpdate = z.infer<typeof telegramUpdateSchema>;

    typedApp.post('/webhooks/telegram', {
        schema: { body: telegramUpdateSchema }
    }, async (req, reply) => {
        try {
            const update = req.body as TelegramUpdate;

            // Process asynchronously — respond 200 immediately

            // so Telegram doesn't retry
            setImmediate(() => {
                telegramAdapter.processUpdate(update).catch((err) => {
                    logger.error('Failed to process Telegram update', {
                        updateId: update.update_id,
                        error: (err as Error).message,
                    });
                });
            });

            return reply.code(200).send({ ok: true });

        } catch (err) {
            logger.error('Telegram webhook error', { error: (err as Error).message });
            return reply.code(500).send({ error: 'Internal error' });
        }
    });
}

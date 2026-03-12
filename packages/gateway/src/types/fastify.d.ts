import { Role } from '../auth/middleware';

declare module 'fastify' {
    interface FastifyRequest {
        user?: {
            id: string;
            email: string;
            workspaces: Array<{
                id: string;
                role: Role;
            }>;
            authType?: 'jwt' | 'api_key';
            apiKeyId?: string;
            actorUserId?: string;
        };
        workspace?: {
            id: string;
            role: Role;
        };
    }
}

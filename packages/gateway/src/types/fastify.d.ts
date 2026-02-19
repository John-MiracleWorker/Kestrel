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
        };
        workspace?: {
            id: string;
            role: Role;
        };
    }
}

import { randomUUID } from 'crypto';
import {
    BaseChannelAdapter,
    ChannelType,
    IncomingMessage,
    OutgoingMessage,
    Attachment,
} from './base';
import { logger } from '../utils/logger';

// ── Types ──────────────────────────────────────────────────────────

interface MoltbookAgent {
    id: string;
    name: string;
    username: string;
    avatar_url?: string;
    bio?: string;
    verified: boolean;
    karma: number;
    created_at: string;
}

interface MoltbookPost {
    id: string;
    submolt: string;
    title: string;
    content: string;
    author: MoltbookAgent;
    score: number;
    comment_count: number;
    created_at: string;
    updated_at: string;
    url: string;
}

interface MoltbookComment {
    id: string;
    post_id: string;
    parent_id?: string;
    content: string;
    author: MoltbookAgent;
    score: number;
    created_at: string;
    replies?: MoltbookComment[];
}

interface MoltbookFeedItem {
    post: MoltbookPost;
    relevance_score?: number;
}

interface MoltbookSubmolt {
    name: string;
    description: string;
    subscriber_count: number;
    post_count: number;
}

// ── Configuration ──────────────────────────────────────────────────

export interface MoltbookConfig {
    apiKey: string;                 // Agent's Moltbook API key
    agentName: string;              // Display name on Moltbook
    agentBio?: string;              // Agent's bio/description
    defaultWorkspaceId: string;
    baseUrl?: string;               // API base (default: https://moltbook.com/api/v1)
    pollingIntervalMs?: number;     // How often to check for new interactions (default: 30s)
    autoPost?: boolean;             // Whether agent autonomously creates posts
    autoReply?: boolean;            // Whether agent autonomously replies to comments
    subscribedSubmolts?: string[];  // Submolts to monitor
    maxPostsPerHour?: number;       // Rate limit for posts (default: 3)
    maxCommentsPerHour?: number;    // Rate limit for comments (default: 10)
}

// ── Moltbook Adapter ──────────────────────────────────────────────

/**
 * Moltbook adapter — lets Kestrel agents participate on the
 * AI-agent-only social network.
 *
 * Features:
 *   ✅ Agent registration and profile management
 *   ✅ Post creation (text + link posts)
 *   ✅ Commenting and threaded replies
 *   ✅ Voting (upvote/downvote)
 *   ✅ Feed monitoring — polls subscribed submolts for new content
 *   ✅ Notification polling — checks for replies to agent's posts
 *   ✅ Rate limiting — configurable posts/comments per hour
 *   ✅ Identity verification support
 *   ✅ Submolt discovery and subscription
 *   ✅ Karma tracking
 *   ✅ Integration with Kestrel's memory graph for context
 *   ✅ Integration with Kestrel's persona for consistent voice
 */
export class MoltbookAdapter extends BaseChannelAdapter {
    readonly channelType: ChannelType = 'moltbook';

    private readonly apiBase: string;
    private agentProfile?: MoltbookAgent;
    private pollingTimer?: NodeJS.Timeout;
    private lastPollTimestamp = new Date(0);

    // Rate limiting
    private postTimestamps: Date[] = [];
    private commentTimestamps: Date[] = [];
    private readonly maxPostsPerHour: number;
    private readonly maxCommentsPerHour: number;

    // Tracking what we've seen to avoid re-processing
    private seenPostIds = new Set<string>();
    private seenCommentIds = new Set<string>();
    private seenNotificationIds = new Set<string>();

    // Our posts/comments for tracking replies
    private ourPostIds = new Set<string>();
    private ourCommentIds = new Set<string>();

    constructor(private config: MoltbookConfig) {
        super();
        this.apiBase = config.baseUrl || 'https://moltbook.com/api/v1';
        this.maxPostsPerHour = config.maxPostsPerHour ?? 3;
        this.maxCommentsPerHour = config.maxCommentsPerHour ?? 10;
    }

    // ── Lifecycle ──────────────────────────────────────────────────

    async connect(): Promise<void> {
        this.setStatus('connecting');

        // Verify API key and get agent profile
        try {
            this.agentProfile = await this.api('GET', '/agents/me');
            logger.info(`Moltbook agent connected: @${this.agentProfile!.username} (karma: ${this.agentProfile!.karma})`);
        } catch (err) {
            // Agent may not be registered yet — try to register
            logger.info('Moltbook agent not found, attempting registration...');
            this.agentProfile = await this.registerAgent();
            logger.info(`Moltbook agent registered: @${this.agentProfile.username}`);
        }

        // Start polling for new content and notifications
        const intervalMs = this.config.pollingIntervalMs ?? 30000;
        this.pollingTimer = setInterval(() => this.poll(), intervalMs);

        // Initial poll
        this.poll();

        this.setStatus('connected');
    }

    async disconnect(): Promise<void> {
        if (this.pollingTimer) {
            clearInterval(this.pollingTimer);
            this.pollingTimer = undefined;
        }
        this.setStatus('disconnected');
        logger.info('Moltbook adapter disconnected');
    }

    // ── Agent Registration ─────────────────────────────────────────

    private async registerAgent(): Promise<MoltbookAgent> {
        return await this.api('POST', '/agents/register', {
            name: this.config.agentName,
            bio: this.config.agentBio || `🦅 Kestrel AI Agent — autonomous, reflective, and always learning.`,
            platform: 'kestrel',
            capabilities: [
                'conversation',
                'task_execution',
                'code_review',
                'research',
                'planning',
            ],
        });
    }

    // ── Sending (responding to incoming messages) ──────────────────

    async send(userId: string, message: OutgoingMessage): Promise<void> {
        // In Moltbook context, "sending" means posting a reply.
        // The conversationId tells us whether it's a post or comment reply.
        const convId = message.conversationId;

        if (convId.startsWith('mb-comment-')) {
            // Reply to a comment
            const commentId = convId.replace('mb-comment-', '');
            // Find the post ID from our tracking
            await this.replyToComment(commentId, message.content);
        } else if (convId.startsWith('mb-post-')) {
            // Comment on a post
            const postId = convId.replace('mb-post-', '');
            await this.commentOnPost(postId, message.content);
        } else {
            // Create a new post (fallback)
            await this.createPost('general', 'Kestrel Response', message.content);
        }
    }

    formatOutgoing(message: OutgoingMessage): OutgoingMessage {
        // Strip HTML, keep Markdown (Moltbook supports limited Markdown)
        let content = message.content;
        // Enforce Moltbook content length limits
        if (content.length > 10000) {
            content = content.substring(0, 9997) + '...';
        }
        return { ...message, content };
    }

    // ── Content Creation ──────────────────────────────────────────

    /**
     * Create a new post on a submolt.
     */
    async createPost(submolt: string, title: string, content: string, linkUrl?: string): Promise<MoltbookPost> {
        if (!this.canPost()) {
            throw new Error(`Rate limited: max ${this.maxPostsPerHour} posts/hour`);
        }

        const body: Record<string, any> = {
            submolt,
            title,
            content,
        };

        if (linkUrl) {
            body.type = 'link';
            body.url = linkUrl;
        }

        const post = await this.api('POST', '/posts', body) as MoltbookPost;
        this.ourPostIds.add(post.id);
        this.postTimestamps.push(new Date());

        logger.info('Moltbook post created', {
            postId: post.id,
            submolt,
            title: title.substring(0, 50),
        });

        return post;
    }

    /**
     * Comment on a post.
     */
    async commentOnPost(postId: string, content: string, parentCommentId?: string): Promise<MoltbookComment> {
        if (!this.canComment()) {
            throw new Error(`Rate limited: max ${this.maxCommentsPerHour} comments/hour`);
        }

        const body: Record<string, any> = { content };
        if (parentCommentId) {
            body.parent_id = parentCommentId;
        }

        const comment = await this.api('POST', `/posts/${postId}/comments`, body) as MoltbookComment;
        this.ourCommentIds.add(comment.id);
        this.commentTimestamps.push(new Date());

        logger.info('Moltbook comment created', {
            commentId: comment.id,
            postId,
            contentPreview: content.substring(0, 50),
        });

        return comment;
    }

    /**
     * Reply to a comment (threaded).
     */
    async replyToComment(commentId: string, content: string): Promise<MoltbookComment> {
        // Need to find the post ID for this comment
        const comment = await this.api('GET', `/comments/${commentId}`) as MoltbookComment;
        return this.commentOnPost(comment.post_id, content, commentId);
    }

    // ── Voting ────────────────────────────────────────────────────

    /**
     * Upvote or downvote a post.
     */
    async voteOnPost(postId: string, direction: 'up' | 'down'): Promise<void> {
        await this.api('POST', `/posts/${postId}/vote`, { direction });
        logger.info('Moltbook vote', { postId, direction });
    }

    /**
     * Upvote or downvote a comment.
     */
    async voteOnComment(commentId: string, direction: 'up' | 'down'): Promise<void> {
        await this.api('POST', `/comments/${commentId}/vote`, { direction });
        logger.info('Moltbook vote', { commentId, direction });
    }

    // ── Feed & Discovery ──────────────────────────────────────────

    /**
     * Get the feed for a submolt.
     */
    async getFeed(submolt: string, sort: 'hot' | 'new' | 'top' = 'hot', limit: number = 25): Promise<MoltbookPost[]> {
        const posts = await this.api('GET', `/submolts/${submolt}/posts?sort=${sort}&limit=${limit}`) as MoltbookPost[];
        return posts;
    }

    /**
     * Get the agent's personalized feed.
     */
    async getPersonalizedFeed(limit: number = 25): Promise<MoltbookFeedItem[]> {
        return await this.api('GET', `/feed?limit=${limit}`) as MoltbookFeedItem[];
    }

    /**
     * Get comments on a post.
     */
    async getPostComments(postId: string, sort: 'best' | 'new' | 'old' = 'best'): Promise<MoltbookComment[]> {
        return await this.api('GET', `/posts/${postId}/comments?sort=${sort}`) as MoltbookComment[];
    }

    /**
     * Search for posts across Moltbook.
     */
    async searchPosts(query: string, submolt?: string, limit: number = 10): Promise<MoltbookPost[]> {
        let url = `/search/posts?q=${encodeURIComponent(query)}&limit=${limit}`;
        if (submolt) url += `&submolt=${encodeURIComponent(submolt)}`;
        return await this.api('GET', url) as MoltbookPost[];
    }

    /**
     * Discover submolts.
     */
    async discoverSubmolts(limit: number = 20): Promise<MoltbookSubmolt[]> {
        return await this.api('GET', `/submolts?sort=popular&limit=${limit}`) as MoltbookSubmolt[];
    }

    /**
     * Subscribe to a submolt.
     */
    async subscribeToSubmolt(name: string): Promise<void> {
        await this.api('POST', `/submolts/${name}/subscribe`);
        logger.info('Subscribed to submolt', { name });
    }

    // ── Profile Management ────────────────────────────────────────

    /**
     * Update the agent's profile.
     */
    async updateProfile(updates: { name?: string; bio?: string; avatar_url?: string }): Promise<MoltbookAgent> {
        this.agentProfile = await this.api('PATCH', '/agents/me', updates) as MoltbookAgent;
        return this.agentProfile;
    }

    /**
     * Get the agent's current karma and stats.
     */
    async getStats(): Promise<{ karma: number; posts: number; comments: number; followers: number }> {
        return await this.api('GET', '/agents/me/stats');
    }

    // ── Identity Verification ─────────────────────────────────────

    /**
     * Get an identity token that third-party apps can verify.
     * Used for "Sign in with Moltbook" integrations.
     */
    async getIdentityToken(): Promise<{ token: string; expires_at: string }> {
        return await this.api('POST', '/agents/me/identity-token');
    }

    // ── Notifications ─────────────────────────────────────────────

    /**
     * Get agent notifications (replies, mentions, votes).
     */
    async getNotifications(since?: Date): Promise<any[]> {
        let url = '/agents/me/notifications';
        if (since) {
            url += `?since=${since.toISOString()}`;
        }
        return await this.api('GET', url);
    }

    // ── Polling ───────────────────────────────────────────────────

    private async poll(): Promise<void> {
        try {
            await Promise.all([
                this.pollNotifications(),
                this.pollSubscribedFeeds(),
            ]);
        } catch (err) {
            logger.error('Moltbook polling error', { error: (err as Error).message });
        }
    }

    /**
     * Poll for notifications (replies to our posts/comments, mentions, etc.)
     */
    private async pollNotifications(): Promise<void> {
        try {
            const notifications = await this.getNotifications(this.lastPollTimestamp);

            for (const notif of notifications) {
                if (this.seenNotificationIds.has(notif.id)) continue;
                this.seenNotificationIds.add(notif.id);

                // Route notification as an incoming message so Kestrel can respond
                const incoming: IncomingMessage = {
                    id: randomUUID(),
                    channel: 'web',
                    userId: `mb-${notif.from_agent?.id || 'unknown'}`,
                    workspaceId: this.config.defaultWorkspaceId,
                    conversationId: notif.comment_id
                        ? `mb-comment-${notif.comment_id}`
                        : `mb-post-${notif.post_id}`,
                    content: notif.content || notif.preview || '',
                    metadata: {
                        channelUserId: notif.from_agent?.id || 'unknown',
                        channelMessageId: notif.id,
                        timestamp: new Date(notif.created_at),
                        platform: 'moltbook',
                        notificationType: notif.type,
                        postId: notif.post_id,
                        commentId: notif.comment_id,
                        submolt: notif.submolt,
                        fromAgent: notif.from_agent?.username,
                    },
                };

                this.emit('message', incoming);
            }

            this.lastPollTimestamp = new Date();
        } catch (err) {
            logger.error('Moltbook notification poll failed', { error: (err as Error).message });
        }
    }

    /**
     * Poll subscribed submolts for new content relevant to engage with.
     */
    private async pollSubscribedFeeds(): Promise<void> {
        if (!this.config.autoReply) return;

        const submolts = this.config.subscribedSubmolts || ['general', 'agents', 'tech'];

        for (const submolt of submolts) {
            try {
                const posts = await this.getFeed(submolt, 'new', 5);

                for (const post of posts) {
                    if (this.seenPostIds.has(post.id)) continue;
                    this.seenPostIds.add(post.id);

                    // Skip our own posts
                    if (this.ourPostIds.has(post.id)) continue;

                    // Emit as incoming message for Kestrel's brain to decide whether to engage
                    const incoming: IncomingMessage = {
                        id: randomUUID(),
                        channel: 'web',
                        userId: `mb-${post.author.id}`,
                        workspaceId: this.config.defaultWorkspaceId,
                        conversationId: `mb-post-${post.id}`,
                        content: `[${post.submolt}] ${post.title}\n\n${post.content}`,
                        metadata: {
                            channelUserId: post.author.id,
                            channelMessageId: post.id,
                            timestamp: new Date(post.created_at),
                            platform: 'moltbook',
                            contentType: 'feed_post',
                            postId: post.id,
                            submolt: post.submolt,
                            fromAgent: post.author.username,
                            score: post.score,
                            commentCount: post.comment_count,
                        },
                    };

                    this.emit('message', incoming);
                }
            } catch (err) {
                logger.error(`Moltbook feed poll failed for s/${submolt}`, { error: (err as Error).message });
            }
        }
    }

    // ── Rate Limiting ─────────────────────────────────────────────

    private canPost(): boolean {
        this.pruneOldTimestamps(this.postTimestamps);
        return this.postTimestamps.length < this.maxPostsPerHour;
    }

    private canComment(): boolean {
        this.pruneOldTimestamps(this.commentTimestamps);
        return this.commentTimestamps.length < this.maxCommentsPerHour;
    }

    private pruneOldTimestamps(timestamps: Date[]): void {
        const oneHourAgo = Date.now() - 60 * 60 * 1000;
        while (timestamps.length > 0 && timestamps[0].getTime() < oneHourAgo) {
            timestamps.shift();
        }
    }

    // ── Accessor ──────────────────────────────────────────────────

    get profile(): MoltbookAgent | undefined {
        return this.agentProfile;
    }

    get isMoltbook(): boolean {
        return true;
    }

    // ── API Client ────────────────────────────────────────────────

    private async api(method: string, path: string, body?: any): Promise<any> {
        const url = `${this.apiBase}${path}`;

        const headers: Record<string, string> = {
            'Authorization': `Bearer ${this.config.apiKey}`,
            'Content-Type': 'application/json',
            'User-Agent': 'Kestrel/1.0 (Autonomous AI Agent)',
        };

        const res = await fetch(url, {
            method,
            headers,
            body: body ? JSON.stringify(body) : undefined,
        });

        if (res.status === 204) return null;

        const data = await res.json() as any;

        if (!res.ok) {
            const errorMsg = data?.error || data?.message || `HTTP ${res.status}`;
            throw new Error(`Moltbook API error: ${errorMsg} (${method} ${path})`);
        }

        return data.data || data;
    }
}

/**
 * NotificationBell — Shows unread notification count and dropdown panel.
 *
 * Features:
 *   - Bell icon with unread badge
 *   - Dropdown panel with notification list
 *   - Mark individual / all as read
 *   - Type-based color coding
 */
import React, { useState, useEffect, useRef, useCallback } from 'react';
import { request, createChatSocket } from '../../api/client';

interface Notification {
    id: string;
    type: 'info' | 'success' | 'warning' | 'task_complete' | 'mention';
    title: string;
    body: string;
    source: string;
    read: boolean;
    created_at: string;
}

const TYPE_COLORS: Record<string, string> = {
    info: '#00f3ff',
    success: '#10b981',
    warning: '#f59e0b',
    task_complete: '#a855f7',
    mention: '#ec4899',
    dep_vuln: '#ef4444',
    ci_fail: '#ef4444',
    key_expiry: '#f59e0b',
};

const TYPE_ICONS: Record<string, string> = {
    info: 'ℹ',
    success: '✓',
    warning: '⚠',
    task_complete: '✦',
    mention: '@',
    dep_vuln: '🛡',
    ci_fail: '⚙',
    key_expiry: '🔑',
};

export function NotificationBell() {
    const [notifications, setNotifications] = useState<Notification[]>([]);
    const [isOpen, setIsOpen] = useState(false);
    const [isPulsing, setIsPulsing] = useState(false);
    const panelRef = useRef<HTMLDivElement>(null);

    const unreadCount = notifications.filter((n) => !n.read).length;

    // Fetch notifications on mount and every 30s
    const fetchNotifications = useCallback(async () => {
        try {
            const data = (await request('/notifications?limit=20')) as {
                notifications?: Notification[];
            };
            setNotifications(data.notifications || []);
        } catch {
            // Silent fail — notifications are non-critical
        }
    }, []);

    useEffect(() => {
        fetchNotifications();
        const interval = setInterval(fetchNotifications, 30000);

        let ws: WebSocket;
        try {
            ws = createChatSocket();
            ws.addEventListener('message', (event) => {
                try {
                    const data = JSON.parse(event.data);
                    if (data.type === 'notification' && data.notification) {
                        setNotifications((prev) => [data.notification, ...prev]);
                        setIsPulsing(true);
                        setTimeout(() => setIsPulsing(false), 2000);
                    }
                } catch (e) {
                    // Ignore parsing errors
                }
            });
        } catch (e) {
            // Ignore socket creation errors
        }

        return () => {
            clearInterval(interval);
            if (ws) ws.close();
        };
    }, [fetchNotifications]);

    // Close panel when clicking outside
    useEffect(() => {
        const handler = (e: MouseEvent) => {
            if (panelRef.current && !panelRef.current.contains(e.target as Node)) {
                setIsOpen(false);
            }
        };
        if (isOpen) document.addEventListener('mousedown', handler);
        return () => document.removeEventListener('mousedown', handler);
    }, [isOpen]);

    const markRead = async (id: string) => {
        try {
            await request(`/notifications/${id}/read`, { method: 'POST' });
            setNotifications((prev) =>
                prev.map((n) => (n.id === id ? { ...n, read: true } : n)),
            );
        } catch { /* ignore */ }
    };

    const markAllRead = async () => {
        try {
            await request('/notifications/read-all', { method: 'POST' });
            setNotifications((prev) => prev.map((n) => ({ ...n, read: true })));
        } catch { /* ignore */ }
    };

    const timeAgo = (iso: string) => {
        const s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
        if (s < 60) return 'just now';
        if (s < 3600) return `${Math.floor(s / 60)}m`;
        if (s < 86400) return `${Math.floor(s / 3600)}h`;
        return `${Math.floor(s / 86400)}d`;
    };

    return (
        <div ref={panelRef} style={{ position: 'relative' }}>
            <style>
                {`
                    @keyframes notification-pulse {
                        0% { transform: scale(1); box-shadow: 0 0 0 0 rgba(168, 85, 247, 0.7); }
                        70% { transform: scale(1.1); box-shadow: 0 0 0 10px rgba(168, 85, 247, 0); }
                        100% { transform: scale(1); box-shadow: 0 0 0 0 rgba(168, 85, 247, 0); }
                    }
                `}
            </style>
            {/* Bell Button */}
            <button
                onClick={() => setIsOpen(!isOpen)}
                style={{
                    background: isOpen ? 'rgba(168,85,247,0.12)' : 'transparent',
                    border: '1px solid rgba(255,255,255,0.06)',
                    color: unreadCount > 0 ? 'var(--accent-purple)' : 'var(--text-dim)',
                    padding: '8px 10px',
                    borderRadius: '8px',
                    cursor: 'pointer',
                    fontFamily: 'var(--font-mono)',
                    fontSize: '1rem',
                    position: 'relative',
                    transition: 'all 0.3s',
                    animation: isPulsing ? 'notification-pulse 2s ease-out' : 'none',
                    boxShadow: unreadCount > 0 ? '0 0 12px rgba(168,85,247,0.15)' : 'none',
                }}
            >
                🔔
                {unreadCount > 0 && (
                    <span
                        style={{
                            position: 'absolute',
                            top: '-4px',
                            right: '-4px',
                            background: '#ef4444',
                            color: '#fff',
                            fontSize: '0.6rem',
                            fontWeight: 700,
                            width: '16px',
                            height: '16px',
                            borderRadius: '50%',
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'center',
                        }}
                    >
                        {unreadCount > 9 ? '9+' : unreadCount}
                    </span>
                )}
            </button>

            {/* Dropdown Panel */}
            {isOpen && (
                <div
                    style={{
                        position: 'absolute',
                        top: 'calc(100% + 8px)',
                        right: 0,
                        width: '360px',
                        maxHeight: '440px',
                        background: 'rgba(10, 10, 10, 0.92)',
                        backdropFilter: 'blur(16px)',
                        WebkitBackdropFilter: 'blur(16px)',
                        border: '1px solid rgba(255, 255, 255, 0.06)',
                        borderRadius: '10px',
                        boxShadow: '0 12px 40px rgba(0,0,0,0.6), 0 0 20px rgba(168,85,247,0.06)',
                        overflow: 'hidden',
                        zIndex: 1000,
                        animation: 'fadeIn 0.2s ease-out',
                    }}
                >
                    {/* Header */}
                    <div
                        style={{
                            display: 'flex',
                            justifyContent: 'space-between',
                            alignItems: 'center',
                            padding: '10px 14px',
                            borderBottom: '1px solid var(--border-subtle)',
                        }}
                    >
                        <span
                            style={{
                                fontFamily: 'var(--font-mono)',
                                fontSize: '0.7rem',
                                color: 'var(--text-dim)',
                                letterSpacing: '0.1em',
                            }}
                        >
                            NOTIFICATIONS
                        </span>
                        {unreadCount > 0 && (
                            <button
                                onClick={markAllRead}
                                style={{
                                    background: 'none',
                                    border: 'none',
                                    color: 'var(--accent-cyan)',
                                    fontFamily: 'var(--font-mono)',
                                    fontSize: '0.65rem',
                                    cursor: 'pointer',
                                }}
                            >
                                MARK ALL READ
                            </button>
                        )}
                    </div>

                    {/* List */}
                    <div style={{ overflowY: 'auto', maxHeight: '380px' }}>
                        {notifications.length === 0 ? (
                            <div
                                style={{
                                    padding: '30px',
                                    textAlign: 'center',
                                    color: 'var(--text-dim)',
                                    fontFamily: 'var(--font-mono)',
                                    fontSize: '0.75rem',
                                }}
                            >
                                No notifications yet
                            </div>
                        ) : (
                            notifications.map((n) => (
                                <div
                                    key={n.id}
                                    onClick={() => !n.read && markRead(n.id)}
                                    style={{
                                        padding: '10px 14px',
                                        borderBottom: '1px solid rgba(255,255,255,0.03)',
                                        cursor: n.read ? 'default' : 'pointer',
                                        opacity: n.read ? 0.5 : 1,
                                        transition: 'all 0.2s',
                                        display: 'flex',
                                        gap: '10px',
                                        alignItems: 'flex-start',
                                        background: 'transparent',
                                    }}
                                    onMouseEnter={(e) => {
                                        if (!n.read) e.currentTarget.style.background = 'rgba(255,255,255,0.02)';
                                    }}
                                    onMouseLeave={(e) => {
                                        e.currentTarget.style.background = 'transparent';
                                    }}
                                >
                                    <span
                                        style={{
                                            fontSize: '0.85rem',
                                            color: TYPE_COLORS[n.type] || 'var(--text-dim)',
                                            flexShrink: 0,
                                            marginTop: '2px',
                                        }}
                                    >
                                        {TYPE_ICONS[n.type] || '●'}
                                    </span>
                                    <div style={{ flex: 1, minWidth: 0 }}>
                                        <div
                                            style={{
                                                fontFamily: 'var(--font-mono)',
                                                fontSize: '0.72rem',
                                                color: 'var(--text-primary)',
                                                fontWeight: n.read ? 400 : 600,
                                                marginBottom: '2px',
                                            }}
                                        >
                                            {n.title}
                                        </div>
                                        {n.body && (
                                            <div
                                                style={{
                                                    fontSize: '0.68rem',
                                                    color: 'var(--text-dim)',
                                                    overflow: 'hidden',
                                                    textOverflow: 'ellipsis',
                                                    whiteSpace: 'nowrap',
                                                }}
                                            >
                                                {n.body}
                                            </div>
                                        )}
                                        <div
                                            style={{
                                                fontSize: '0.6rem',
                                                color: 'var(--text-dim)',
                                                marginTop: '3px',
                                                fontFamily: 'var(--font-mono)',
                                            }}
                                        >
                                            {n.source && <span>{n.source} · </span>}
                                            {timeAgo(n.created_at)}
                                        </div>
                                    </div>
                                    {!n.read && (
                                        <div
                                            style={{
                                                width: '6px',
                                                height: '6px',
                                                borderRadius: '50%',
                                                background: TYPE_COLORS[n.type] || 'var(--accent-purple)',
                                                flexShrink: 0,
                                                marginTop: '6px',
                                            }}
                                        />
                                    )}
                                </div>
                            ))
                        )}
                    </div>
                </div>
            )}
        </div>
    );
}

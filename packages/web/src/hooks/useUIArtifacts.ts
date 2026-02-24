/**
 * useUIArtifacts — fetches and manages UI artifacts for a workspace.
 */
import { useState, useEffect, useCallback, useRef } from 'react';
import { uiArtifacts, type UIArtifactItem } from '../api/client';

interface UseUIArtifactsReturn {
    artifacts: UIArtifactItem[];
    loading: boolean;
    error: string | null;
    refresh: () => void;
    updateArtifact: (artifactId: string, instruction: string) => void;
}

export function useUIArtifacts(workspaceId: string | null): UseUIArtifactsReturn {
    const [artifacts, setArtifacts] = useState<UIArtifactItem[]>([]);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const wsRef = useRef(workspaceId);
    wsRef.current = workspaceId;

    const refresh = useCallback(() => {
        const ws = wsRef.current;
        if (!ws) return;
        setLoading(true);
        setError(null);
        uiArtifacts
            .list(ws)
            .then((data) => {
                setArtifacts(data);
            })
            .catch((err) => {
                setError(err instanceof Error ? err.message : 'Failed to load artifacts');
            })
            .finally(() => {
                setLoading(false);
            });
    }, []);

    useEffect(() => {
        refresh();
    }, [refresh, workspaceId]);

    const updateArtifact = useCallback((artifactId: string, instruction: string) => {
        const ws = wsRef.current;
        if (!ws) return;
        uiArtifacts
            .update(ws, artifactId, instruction)
            .then((updated) => {
                setArtifacts((prev) => prev.map((a) => (a.id === artifactId ? updated : a)));
            })
            .catch((err) => {
                setError(err instanceof Error ? err.message : 'Failed to update artifact');
            });
    }, []);

    return { artifacts, loading, error, refresh, updateArtifact };
}

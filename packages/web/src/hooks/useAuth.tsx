import { createContext, useContext, useState, useEffect, useCallback, type ReactNode } from 'react';
import { auth, setTokens, loadTokens, clearTokens, setOnAuthExpired } from '../api/client';

interface User {
    id: string;
    email: string;
    displayName: string;
}

interface AuthContextType {
    user: User | null;
    isLoading: boolean;
    isAuthenticated: boolean;
    login: (email: string, password: string) => Promise<void>;
    register: (email: string, password: string, displayName?: string) => Promise<void>;
    logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
    const [user, setUser] = useState<User | null>(null);
    const [isLoading, setIsLoading] = useState(true);

    const logout = useCallback(async () => {
        try {
            await auth.logout();
        } catch {
            // Ignore errors on logout
        }
        clearTokens();
        setUser(null);
    }, []);

    // Auto-expire handler
    useEffect(() => {
        setOnAuthExpired(() => {
            clearTokens();
            setUser(null);
        });
    }, []);

    // Try to restore session from stored tokens
    useEffect(() => {
        (async () => {
            if (loadTokens()) {
                try {
                    const me = await auth.me();
                    setUser(me);
                } catch {
                    clearTokens();
                }
            }
            setIsLoading(false);
        })();
    }, []);

    const login = useCallback(async (email: string, password: string) => {
        const result = await auth.login(email, password);
        setTokens(result.accessToken, result.refreshToken);
        const me = await auth.me();
        setUser(me);
    }, []);

    const register = useCallback(async (email: string, password: string, displayName?: string) => {
        const result = await auth.register(email, password, displayName);
        setTokens(result.accessToken, result.refreshToken);
        const me = await auth.me();
        setUser(me);
    }, []);

    return (
        <AuthContext.Provider value={{
            user,
            isLoading,
            isAuthenticated: !!user,
            login,
            register,
            logout,
        }}>
            {children}
        </AuthContext.Provider>
    );
}

export function useAuth(): AuthContextType {
    const ctx = useContext(AuthContext);
    if (!ctx) throw new Error('useAuth must be used within AuthProvider');
    return ctx;
}

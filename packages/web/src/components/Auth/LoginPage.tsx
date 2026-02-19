import { useState, FormEvent } from 'react';
import { useAuth } from '../../hooks/useAuth';

function OAuthNotice({ onClose }: { onClose: () => void }) {
    return (
        <div style={{
            position: 'fixed',
            inset: 0,
            zIndex: 10000,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            background: 'rgba(0,0,0,0.6)',
            backdropFilter: 'blur(4px)',
        }} onClick={onClose}>
            <div className="card animate-scale-in" style={{ maxWidth: 360, padding: 'var(--space-6)', textAlign: 'center' }} onClick={e => e.stopPropagation()}>
                <div style={{ fontSize: '2rem', marginBottom: 'var(--space-3)' }}>🔐</div>
                <h3 style={{ fontWeight: 600, marginBottom: 'var(--space-2)' }}>OAuth Coming Soon</h3>
                <p style={{ color: 'var(--color-text-secondary)', fontSize: '0.875rem', marginBottom: 'var(--space-4)' }}>
                    Social login is not yet configured. Please sign in with your email and password for now.
                </p>
                <button className="btn btn-primary" onClick={onClose} style={{ width: '100%' }}>Got it</button>
            </div>
        </div>
    );
}

export function LoginPage() {
    const { login, register } = useAuth();
    const [mode, setMode] = useState<'login' | 'register'>('login');
    const [email, setEmail] = useState('');
    const [password, setPassword] = useState('');
    const [displayName, setDisplayName] = useState('');
    const [error, setError] = useState('');
    const [showOAuthNotice, setShowOAuthNotice] = useState(false);
    const [loading, setLoading] = useState(false);

    async function handleSubmit(e: FormEvent) {
        e.preventDefault();
        setError('');
        setLoading(true);

        try {
            if (mode === 'login') {
                await login(email, password);
            } else {
                await register(email, password, displayName || undefined);
            }
        } catch (err) {
            setError(err instanceof Error ? err.message : 'Something went wrong');
        } finally {
            setLoading(false);
        }
    }

    return (
        <div className="auth-page">
            <div className="auth-card card-glass">
                {/* Logo */}
                <div style={{
                    textAlign: 'center',
                    marginBottom: 'var(--space-6)',
                }}>
                    <div style={{
                        width: 48,
                        height: 48,
                        background: 'linear-gradient(135deg, var(--color-brand), #a855f7)',
                        borderRadius: 'var(--radius-md)',
                        display: 'inline-flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        fontSize: '1.5rem',
                        marginBottom: 'var(--space-3)',
                        boxShadow: 'var(--shadow-glow)',
                    }}>
                        🪶
                    </div>
                </div>

                <h1>{mode === 'login' ? 'Welcome back' : 'Create account'}</h1>
                <p>
                    {mode === 'login'
                        ? 'Sign in to your Kestrel workspace'
                        : 'Get started with Kestrel'}
                </p>

                <form className="auth-form" onSubmit={handleSubmit}>
                    {mode === 'register' && (
                        <div className="form-group">
                            <label htmlFor="displayName">Display Name</label>
                            <input
                                id="displayName"
                                className="input"
                                type="text"
                                placeholder="Your name"
                                value={displayName}
                                onChange={(e) => setDisplayName(e.target.value)}
                                autoComplete="name"
                            />
                        </div>
                    )}

                    <div className="form-group">
                        <label htmlFor="email">Email</label>
                        <input
                            id="email"
                            className="input"
                            type="email"
                            placeholder="you@example.com"
                            value={email}
                            onChange={(e) => setEmail(e.target.value)}
                            required
                            autoComplete="email"
                        />
                    </div>

                    <div className="form-group">
                        <label htmlFor="password">Password</label>
                        <input
                            id="password"
                            className="input"
                            type="password"
                            placeholder="••••••••"
                            value={password}
                            onChange={(e) => setPassword(e.target.value)}
                            required
                            minLength={8}
                            autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
                        />
                    </div>

                    {error && <p className="error-text">{error}</p>}

                    <button
                        className="btn btn-primary btn-lg"
                        type="submit"
                        disabled={loading}
                        style={{ width: '100%' }}
                    >
                        {loading ? 'Please wait...' : mode === 'login' ? 'Sign In' : 'Create Account'}
                    </button>
                </form>

                <div className="auth-divider">or</div>

                {/* OAuth buttons — social login not yet configured */}
                <div style={{ display: 'flex', gap: 'var(--space-3)' }}>
                    <button className="btn btn-secondary" style={{ flex: 1, opacity: 0.7 }} onClick={() => setShowOAuthNotice(true)}>
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
                            <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 01-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z" fill="#4285F4" />
                            <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853" />
                            <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05" />
                            <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335" />
                        </svg>
                        Google
                    </button>
                    <button className="btn btn-secondary" style={{ flex: 1, opacity: 0.7 }} onClick={() => setShowOAuthNotice(true)}>
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
                            <path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z" />
                        </svg>
                        GitHub
                    </button>
                </div>
                {showOAuthNotice && <OAuthNotice onClose={() => setShowOAuthNotice(false)} />}

                {/* Toggle mode */}
                <p style={{
                    textAlign: 'center',
                    marginTop: 'var(--space-6)',
                    fontSize: '0.875rem',
                    color: 'var(--color-text-secondary)',
                }}>
                    {mode === 'login' ? "Don't have an account?" : 'Already have an account?'}{' '}
                    <a
                        href="#"
                        onClick={(e) => {
                            e.preventDefault();
                            setMode(mode === 'login' ? 'register' : 'login');
                            setError('');
                        }}
                    >
                        {mode === 'login' ? 'Sign up' : 'Sign in'}
                    </a>
                </p>
            </div>
        </div>
    );
}

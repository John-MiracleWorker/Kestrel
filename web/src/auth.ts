const API_TOKEN_STORAGE_KEY = "kestrel.apiToken";

export function getApiToken(): string {
  try {
    return (
      window.sessionStorage.getItem(API_TOKEN_STORAGE_KEY)?.trim() ||
      window.localStorage.getItem(API_TOKEN_STORAGE_KEY)?.trim() ||
      ""
    );
  } catch {
    return "";
  }
}

export function setApiToken(token: string, persist = false): void {
  const trimmed = token.trim();
  try {
    window.sessionStorage.removeItem(API_TOKEN_STORAGE_KEY);
    window.localStorage.removeItem(API_TOKEN_STORAGE_KEY);
    if (trimmed) {
      const store = persist ? window.localStorage : window.sessionStorage;
      store.setItem(API_TOKEN_STORAGE_KEY, trimmed);
    }
  } catch {
    // Storage can be unavailable in locked-down browser contexts; keep runtime auth in memory via caller state.
  }
}

export function apiAuthHeaders(): Record<string, string> {
  const token = getApiToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

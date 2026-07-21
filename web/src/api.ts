import { apiAuthHeaders, getApiToken } from "./auth";

export class ApiResponseError extends Error {
  constructor(
    message: string,
    readonly status: number
  ) {
    super(message);
    this.name = "ApiResponseError";
  }
}

export class ApiAuthError extends ApiResponseError {

  constructor(message = "Kestrel API token required.") {
    super(message, 401);
    this.name = "ApiAuthError";
  }
}

export async function getJson<T>(path: string, options: { signal?: AbortSignal } = {}): Promise<T> {
  const response = await fetch(path, { headers: apiAuthHeaders(), signal: options.signal });
  return parseResponse<T>(response);
}

export async function postJson<T>(path: string, body: unknown = {}): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    headers: { ...apiAuthHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  return parseResponse<T>(response);
}

export async function putJson<T>(path: string, body: unknown = {}): Promise<T> {
  const response = await fetch(path, {
    method: "PUT",
    headers: { ...apiAuthHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  return parseResponse<T>(response);
}

export async function deleteJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { method: "DELETE", headers: apiAuthHeaders() });
  return parseResponse<T>(response);
}

async function parseResponse<T>(response: Response): Promise<T> {
  const text = await response.text();
  const payload = text ? safeParse(text) : {};
  if (!response.ok) {
    if (response.status === 401) {
      throw new ApiAuthError(errorMessage(payload, text, response.statusText));
    }
    throw new ApiResponseError(errorMessage(payload, text, response.statusText), response.status);
  }
  return payload as T;
}

function errorMessage(payload: unknown, text: string, fallback: string): string {
  if (typeof payload === "object" && payload !== null && "detail" in payload) {
    const detail = (payload as { detail: unknown }).detail;
    return typeof detail === "string" ? detail : JSON.stringify(detail);
  }
  return text || fallback;
}

function safeParse(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export async function getLearningDashboard<T>(since = "all"): Promise<T> {
  return getJson<T>(`/api/learning/dashboard${queryString({ since })}`);
}

export function queryString(params: Record<string, string | number | boolean | null | undefined>): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== null && value !== undefined && String(value).trim() !== "") {
      search.set(key, String(value));
    }
  });
  const rendered = search.toString();
  return rendered ? `?${rendered}` : "";
}

export function subscribeJsonEvents<T>(
  path: string,
  eventTypes: string[],
  onEvent: (event: T) => void,
  onError: (error: unknown) => void
): () => void {
  if (!getApiToken() && typeof EventSource !== "undefined") {
    const source = new EventSource(path);
    const handleEvent = (event: MessageEvent) => onEvent(JSON.parse(event.data) as T);
    source.onmessage = handleEvent;
    eventTypes.forEach((type) => source.addEventListener(type, handleEvent));
    return () => source.close();
  }

  const controller = new AbortController();
  void readEventStream<T>(path, controller.signal, onEvent).catch((error) => {
    if (!controller.signal.aborted) onError(error);
  });
  return () => controller.abort();
}

async function readEventStream<T>(path: string, signal: AbortSignal, onEvent: (event: T) => void): Promise<void> {
  const response = await fetch(path, { headers: apiAuthHeaders(), signal });
  if (!response.ok) {
    await parseResponse<unknown>(response);
    return;
  }
  if (!response.body) return;

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split(/\r?\n\r?\n/);
    buffer = parts.pop() ?? "";
    parts.forEach((part) => emitSsePart(part, onEvent));
  }
  buffer += decoder.decode();
  if (buffer.trim()) emitSsePart(buffer, onEvent);
}

function emitSsePart<T>(part: string, onEvent: (event: T) => void): void {
  const data = part
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n");
  if (data) onEvent(JSON.parse(data) as T);
}

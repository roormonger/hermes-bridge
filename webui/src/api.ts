export const API_BASE = "";

export const getToken = () => localStorage.getItem("hermes_token") || "";

export const setToken = (token: string) => {
  if (token) localStorage.setItem("hermes_token", token);
  else localStorage.removeItem("hermes_token");
};

export const apiFetch = async (path: string, options: RequestInit = {}) => {
  const token = getToken();
  const res = await fetch(API_BASE + path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...options.headers,
    },
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
  }
  return res.json();
};

export type SseEvent =
  | { type: "text"; text: string }
  | { type: "gate_interrupt"; gate_id: string; options?: string[]; prompt?: string }
  | { type: "process_exit" };

export const streamEvents = async (
  url: string,
  body: object,
  onEvent: (event: SseEvent) => void
) => {
  const token = getToken();
  const res = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(res.statusText);
  const reader = res.body?.getReader();
  if (!reader) throw new Error("no response body");
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      const trimmed = line.trim();
      if (trimmed.startsWith("data: ")) {
        const data = trimmed.slice(6);
        if (data === "[DONE]") continue;
        try {
          const event = JSON.parse(data) as SseEvent;
          onEvent(event);
        } catch (e) {
          console.error("failed to parse SSE event", data, e);
        }
      }
    }
  }
};

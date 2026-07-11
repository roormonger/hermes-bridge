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
  | { type: "gate_interrupt"; gate_id: string; gate_kind?: string; options?: string[]; prompt?: string; context?: Record<string, unknown> }
  | { type: "process_exit" }
  | { type: "turn_complete" }
  | { type: "tool_start"; tool_id: string; name: string; context?: string }
  | { type: "tool_progress"; tool_id: string; name: string; text: string }
  | { type: "tool_complete"; tool_id: string; name: string; summary?: string; duration_s?: number }
  | { type: "session_title"; title: string; session_id?: string }
  | { type: "session_info"; model?: string; provider?: string; gateway?: string; api_provider?: string; reasoning_effort?: string; service_tier?: string; fast?: boolean; yolo?: boolean }
  | { type: "error"; message: string };

export const getModels = () => apiFetch("/v1/models");

export const getAnalyticsModels = () => apiFetch("/v1/analytics/models");

export const getCurrentModel = (chatId?: string) =>
  apiFetch(chatId ? `/v1/model?chat_id=${encodeURIComponent(chatId)}` : "/v1/model");

export const setModel = (
  chatId: string,
  model: string,
  provider?: string,
  confirmExpensiveModel?: boolean,
) =>
  apiFetch("/v1/model", {
    method: "POST",
    body: JSON.stringify({
      chat_id: chatId,
      model,
      provider: provider || "",
      confirm_expensive_model: !!confirmExpensiveModel,
    }),
  });

export const undoLastTurn = (chatId: string) =>
  apiFetch("/v1/chat/undo", {
    method: "POST",
    body: JSON.stringify({ chat_id: chatId }),
  });

export const streamEvents = async (
  url: string,
  body: object,
  onEvent: (event: SseEvent) => void,
  signal?: AbortSignal,
) => {
  const token = getToken();
  const res = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok) throw new Error(res.statusText);
  const reader = res.body?.getReader();
  if (!reader) throw new Error("no response body");
  const decoder = new TextDecoder();
  let buffer = "";
  try {
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
  } catch (e) {
    if ((e as Error).name === "AbortError") return;
    throw e;
  } finally {
    reader.cancel().catch(() => {});
  }
};

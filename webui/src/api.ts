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

type SseEventPayload =
  | { type: "text"; text: string }
  | { type: "gate_interrupt"; gate_id: string; gate_kind?: string; options?: string[]; prompt?: string; context?: Record<string, unknown> }
  | { type: "process_exit" }
  | { type: "turn_complete" }
  | { type: "tool_start"; tool_id: string; name: string; context?: string }
  | { type: "tool_progress"; tool_id: string; name: string; text: string }
  | { type: "tool_complete"; tool_id: string; name: string; summary?: string; duration_s?: number; result?: unknown; artifact?: unknown }
  | { type: "session_title"; title: string; session_id?: string }
  | { type: "session_info"; model?: string; provider?: string; gateway?: string; api_provider?: string; reasoning_effort?: string; service_tier?: string; fast?: boolean; yolo?: boolean; context_window?: number; input_tokens?: number; output_tokens?: number; cache_read_tokens?: number; reasoning_tokens?: number; total_tokens?: number }
  | { type: "error"; message: string };

export type SseEvent = SseEventPayload & { seq?: number; run_id?: string };

export type ChatRun = {
  run_id: string;
  chat_id: string;
  assistant_message_id: number;
  status: "starting" | "running" | "waiting_for_gate" | "complete" | "error" | "cancelled";
  last_seq: number;
  pending_gate?: Extract<SseEventPayload, { type: "gate_interrupt" }> | null;
};

export const startChatRun = (chatId: string, message: string, assistantMessageId: string) =>
  apiFetch("/v1/chat/runs", {
    method: "POST",
    body: JSON.stringify({
      chat_id: chatId,
      message,
      assistant_message_id: Number(assistantMessageId),
    }),
  }) as Promise<ChatRun>;

export const getActiveChatRun = (chatId: string) =>
  apiFetch(`/v1/chat/runs/active?chat_id=${encodeURIComponent(chatId)}`) as Promise<{
    active: boolean;
    protocol_version: number;
    run?: ChatRun | null;
  }>;

export const getChatRun = (runId: string) =>
  apiFetch(`/v1/chat/runs/${encodeURIComponent(runId)}`) as Promise<ChatRun>;

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

export const getUsage = (chatId: string) =>
  apiFetch(`/v1/usage?chat_id=${encodeURIComponent(chatId)}`);

export const undoLastTurn = (chatId: string) =>
  apiFetch("/v1/chat/undo", {
    method: "POST",
    body: JSON.stringify({ chat_id: chatId }),
  });

export const getChatUsage = (chatId: string) =>
  apiFetch(`/api/chats/${chatId}/usage`);

export const saveChatUsage = (chatId: string, usage: any, contextWindow?: number) =>
  apiFetch(`/api/chats/${chatId}/usage`, {
    method: "PUT",
    body: JSON.stringify({ usage, context_window: contextWindow }),
  });

export const saveMessageUsage = (chatId: string, messageId: string, usage: any) =>
  apiFetch(`/api/chats/${chatId}/messages/${messageId}/usage`, {
    method: "PUT",
    body: JSON.stringify({ usage }),
  });

export const getVoiceDeps = () => apiFetch("/api/plugins/hermes-chat/deps");
export const getVoiceConfig = () => apiFetch("/api/plugins/hermes-chat/voice-config");

export const transcribeAudio = async (blob: Blob): Promise<string> => {
  const token = getToken();
  const formData = new FormData();
  formData.append("file", blob, "audio.webm");
  const res = await fetch("/v1/audio/transcribe", {
    method: "POST",
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: formData,
  });
  if (!res.ok) throw new Error(await res.text() || res.statusText);
  const data = await res.json();
  return data.text as string;
};

function stripMarkdown(md: string): string {
  return md
    .replace(/```[\s\S]*?```/g, "")              // fenced code blocks
    .replace(/`[^`]*`/g, "")                      // inline code
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "")       // images
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")      // links → label only
    .replace(/^#{1,6}\s+/gm, "")                  // headings
    .replace(/(\*\*|__)(.*?)\1/g, "$2")           // bold
    .replace(/(\*|_)(.*?)\1/g, "$2")              // italic
    .replace(/~~(.*?)~~/g, "$1")                  // strikethrough
    .replace(/^\s*[-*+]\s+/gm, "")               // unordered list markers
    .replace(/^\s*\d+\.\s+/gm, "")               // ordered list markers
    .replace(/^\s*>\s+/gm, "")                    // blockquotes
    .replace(/^[-*_]{3,}\s*$/gm, "")             // horizontal rules
    .replace(/\n{3,}/g, "\n\n")                   // collapse excess newlines
    .trim();
}

export const speakText = async (text: string, lang?: string, voice?: string): Promise<string> => {
  const token = getToken();
  const clean = stripMarkdown(text);
  const res = await fetch("/v1/audio/speak", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ text: clean, lang, voice }),
  });
  if (!res.ok) throw new Error(await res.text() || res.statusText);
  const blob = await res.blob();
  return URL.createObjectURL(blob);
};

const _SSE_MAX_RETRIES = 5;
const _SSE_BASE_DELAY_MS = 500;

class SseHttpError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "SseHttpError";
  }
}

export const streamEvents = async (
  url: string,
  body: object,
  onEvent: (event: SseEvent) => void,
  signal?: AbortSignal,
  method: "GET" | "POST" = "POST",
) => {
  let attempt = 0;
  while (true) {
    if (signal?.aborted) return;
    const token = getToken();
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    try {
      const res = await fetch(url, {
        method,
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        ...(method === "POST" ? { body: JSON.stringify(body) } : {}),
        signal,
      });
      if (!res.ok) {
        const body = await res.text();
        let detail = body;
        try {
          detail = JSON.parse(body)?.detail || body;
        } catch {}
        throw new SseHttpError(res.status, detail || `${res.status} ${res.statusText}`);
      }
      reader = res.body?.getReader();
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
      return; // clean finish
    } catch (e) {
      if ((e as Error).name === "AbortError") return;
      if (e instanceof SseHttpError) throw e;
      attempt++;
      if (attempt > _SSE_MAX_RETRIES) throw e;
      const delay = _SSE_BASE_DELAY_MS * Math.pow(2, attempt - 1);
      console.warn(`[SSE] network error, retrying in ${delay}ms (attempt ${attempt}/${_SSE_MAX_RETRIES})`, e);
      await new Promise<void>((resolve, reject) => {
        const t = setTimeout(resolve, delay);
        signal?.addEventListener("abort", () => { clearTimeout(t); reject(new DOMException("Aborted", "AbortError")); }, { once: true });
      });
    } finally {
      reader?.cancel().catch(() => {});
    }
  }
};

export const streamChatRun = (
  runId: string,
  after: number,
  onEvent: (event: SseEvent) => void,
  signal?: AbortSignal,
) => streamEvents(
  `/v1/chat/runs/${encodeURIComponent(runId)}/events?after=${Math.max(0, after)}`,
  {},
  onEvent,
  signal,
  "GET",
);

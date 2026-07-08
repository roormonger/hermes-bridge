import { useEffect, useMemo, useRef, useState, useCallback } from "react";
import {
  AssistantRuntimeProvider,
  useExternalStoreRuntime,
  type AppendMessage,
  type ThreadMessageLike,
} from "@assistant-ui/react";
import { Thread } from "@/components/assistant-ui/thread";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Plus, Trash2, Pencil, MessageSquare, Check, X } from "lucide-react";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type Chat = {
  chat_id: string;
  title: string;
  created_at: string;
  updated_at: string;
};

type Gate = {
  gateId: string;
  options: string[];
  prompt: string;
};

type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  status?: "running" | "complete";
  gate?: Gate | null;
};

type SseEvent =
  | { type: "text"; text: string }
  | { type: "gate_interrupt"; gate_id: string; options?: string[]; prompt?: string }
  | { type: "process_exit" };

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

const API_BASE = "";

const apiFetch = async (path: string, options: RequestInit = {}) => {
  const res = await fetch(API_BASE + path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...options.headers,
    },
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
  }
  return res.json();
};

const streamEvents = async (
  url: string,
  body: object,
  onEvent: (event: SseEvent) => void
) => {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
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

const generateId = () => crypto.randomUUID();

const toThreadMessage = (msg: ChatMessage): ThreadMessageLike => ({
  id: msg.id,
  role: msg.role,
  content: [{ type: "text", text: msg.content }],
  status: msg.status
    ? msg.status === "running"
      ? { type: "running" }
      : { type: "complete", reason: "stop" }
    : undefined,
  metadata: msg.gate ? { custom: { gate: msg.gate } } : undefined,
});

const convertMessage = (msg: ThreadMessageLike): ThreadMessageLike => msg;

const getAppendText = (msg: AppendMessage): string => {
  if (typeof msg.content === "string") return msg.content;
  return msg.content
    .filter((p: any) => p.type === "text")
    .map((p: any) => p.text)
    .join("");
};

// ---------------------------------------------------------------------------
// Gate dialog
// ---------------------------------------------------------------------------

function GateDialog({
  pendingGate,
  onChoice,
}: {
  pendingGate: Gate | null;
  onChoice: (choice: string) => void;
}) {
  if (!pendingGate) return null;
  return (
    <Dialog open={!!pendingGate} onOpenChange={() => {}}>
      <DialogContent showCloseButton={false}>
        <DialogHeader>
          <DialogTitle>Hermes needs your input</DialogTitle>
        </DialogHeader>
        <p className="text-muted-foreground">
          {pendingGate.prompt || "Choose an option:"}
        </p>
        <div className="flex flex-col gap-2">
          {pendingGate.options.map((option) => (
            <Button key={option} onClick={() => onChoice(option)}>
              {option}
            </Button>
          ))}
        </div>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Sidebar
// ---------------------------------------------------------------------------

function ChatSidebar({
  chats,
  currentChatId,
  onSelect,
  onNew,
  onRename,
  onDelete,
}: {
  chats: Chat[];
  currentChatId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  onRename: (id: string, title: string) => void;
  onDelete: (id: string) => void;
}) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState("");

  return (
    <div className="w-64 border-r bg-card flex flex-col h-full">
      <div className="p-4 border-b flex items-center justify-between">
        <h2 className="font-semibold text-card-foreground">Chats</h2>
        <Button variant="ghost" size="icon" onClick={onNew}>
          <Plus className="size-4" />
        </Button>
      </div>
      <div className="flex-1 overflow-y-auto p-2 space-y-1">
        {chats.map((chat) => (
          <div
            key={chat.chat_id}
            className={cn(
              "group flex items-center gap-2 rounded-md px-2 py-2 text-sm cursor-pointer",
              currentChatId === chat.chat_id
                ? "bg-accent text-accent-foreground"
                : "hover:bg-muted"
            )}
            onClick={() => onSelect(chat.chat_id)}
          >
            <MessageSquare className="size-4 shrink-0" />
            {editingId === chat.chat_id ? (
              <>
                <Input
                  value={editTitle}
                  onChange={(e) => setEditTitle(e.target.value)}
                  className="h-7 flex-1"
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      onRename(chat.chat_id, editTitle);
                      setEditingId(null);
                    }
                  }}
                  onClick={(e) => e.stopPropagation()}
                  autoFocus
                />
                <Button
                  variant="ghost"
                  size="icon"
                  className="size-6"
                  onClick={(e) => {
                    e.stopPropagation();
                    onRename(chat.chat_id, editTitle);
                    setEditingId(null);
                  }}
                >
                  <Check className="size-3" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  className="size-6"
                  onClick={(e) => {
                    e.stopPropagation();
                    setEditingId(null);
                  }}
                >
                  <X className="size-3" />
                </Button>
              </>
            ) : (
              <>
                <span className="flex-1 truncate">{chat.title}</span>
                <Button
                  variant="ghost"
                  size="icon"
                  className="size-6 opacity-0 group-hover:opacity-100"
                  onClick={(e) => {
                    e.stopPropagation();
                    setEditingId(chat.chat_id);
                    setEditTitle(chat.title);
                  }}
                >
                  <Pencil className="size-3" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  className="size-6 opacity-0 group-hover:opacity-100"
                  onClick={(e) => {
                    e.stopPropagation();
                    onDelete(chat.chat_id);
                  }}
                >
                  <Trash2 className="size-3" />
                </Button>
              </>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main app
// ---------------------------------------------------------------------------

export default function App() {
  const [chats, setChats] = useState<Chat[]>([]);
  const [currentChatId, setCurrentChatId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isRunning, setIsRunning] = useState(false);
  const [pendingGate, setPendingGate] = useState<Gate | null>(null);
  const [error, setError] = useState<string | null>(null);

  const assistantIdRef = useRef<string | null>(null);
  const assistantContentRef = useRef("");
  const currentChatIdRef = useRef(currentChatId);

  useEffect(() => {
    currentChatIdRef.current = currentChatId;
  }, [currentChatId]);

  const loadChats = useCallback(async () => {
    try {
      const data = await apiFetch("/api/chats");
      setChats(data);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  const loadMessages = useCallback(async (chatId: string) => {
    try {
      const data = await apiFetch(`/api/chats/${chatId}/messages`);
      setMessages(
        data.map((m: any) => ({
          id: String(m.id),
          role: m.role,
          content: m.content,
          status: "complete" as const,
        }))
      );
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    loadChats();
  }, [loadChats]);

  useEffect(() => {
    if (currentChatId) loadMessages(currentChatId);
    else setMessages([]);
  }, [currentChatId, loadMessages]);

  const createChat = async () => {
    try {
      const data = await apiFetch("/api/chats", {
        method: "POST",
        body: JSON.stringify({ title: "New chat" }),
      });
      setChats((prev) => [data, ...prev]);
      setCurrentChatId(data.chat_id);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const renameChat = async (chatId: string, title: string) => {
    try {
      await apiFetch(`/api/chats/${chatId}`, {
        method: "PATCH",
        body: JSON.stringify({ title }),
      });
      setChats((prev) =>
        prev.map((c) => (c.chat_id === chatId ? { ...c, title } : c))
      );
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const deleteChat = async (chatId: string) => {
    try {
      await apiFetch(`/api/chats/${chatId}`, { method: "DELETE" });
      setChats((prev) => prev.filter((c) => c.chat_id !== chatId));
      if (currentChatId === chatId) {
        setCurrentChatId(null);
        setMessages([]);
      }
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const createBackendMessage = async (
    chatId: string,
    role: "user" | "assistant",
    content: string
  ): Promise<string> => {
    const data = await apiFetch(`/api/chats/${chatId}/messages`, {
      method: "POST",
      body: JSON.stringify({ role, content }),
    });
    return String(data.id);
  };

  const updateBackendMessage = async (
    chatId: string,
    messageId: string,
    content: string
  ) => {
    await apiFetch(`/api/chats/${chatId}/messages/${messageId}`, {
      method: "PUT",
      body: JSON.stringify({ content }),
    });
  };

  const handleStreamEvent = useCallback(
    (event: SseEvent, chatId: string, assistantId: string) => {
      if (event.type === "text") {
        assistantContentRef.current += event.text;
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantId
              ? { ...m, content: assistantContentRef.current }
              : m
          )
        );
      } else if (event.type === "gate_interrupt") {
        const gate: Gate = {
          gateId: event.gate_id,
          options: event.options || [],
          prompt: event.prompt || "",
        };
        assistantContentRef.current +=
          "\n\n🚦 Hermes needs your input: " + (event.prompt || "");
        setPendingGate(gate);
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantId
              ? {
                  ...m,
                  content: assistantContentRef.current,
                  status: "running",
                  gate,
                }
              : m
          )
        );
        updateBackendMessage(chatId, assistantId, assistantContentRef.current);
      } else if (event.type === "process_exit") {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantId
              ? { ...m, content: assistantContentRef.current, status: "complete", gate: null }
              : m
          )
        );
        setIsRunning(false);
        updateBackendMessage(chatId, assistantId, assistantContentRef.current);
      }
    },
    []
  );

  const streamAssistant = async (chatId: string, userText: string) => {
    setIsRunning(true);
    setError(null);
    assistantContentRef.current = "";
    const assistantId = await createBackendMessage(chatId, "assistant", "");
    assistantIdRef.current = assistantId;
    setMessages((prev) => [
      ...prev,
      { id: assistantId, role: "assistant", content: "", status: "running" },
    ]);

    try {
      await streamEvents(
        "/v1/chat",
        { chat_id: chatId, message: userText },
        (event) => handleStreamEvent(event, chatId, assistantId)
      );
    } catch (e) {
      setError((e as Error).message);
      setIsRunning(false);
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantId ? { ...m, status: "complete", gate: null } : m
        )
      );
    }
  };

  const handleGateChoice = async (choice: string) => {
    const chatId = currentChatIdRef.current;
    if (!chatId || !pendingGate) return;
    const gate = pendingGate;
    setPendingGate(null);
    setError(null);
    setIsRunning(true);
    setMessages((prev) =>
      prev.map((m) =>
        m.id === assistantIdRef.current ? { ...m, gate: null } : m
      )
    );

    try {
      await apiFetch("/v1/gate/resolve", {
        method: "POST",
        body: JSON.stringify({
          chat_id: chatId,
          gate_id: gate.gateId,
          choice,
        }),
      });
      const assistantId = assistantIdRef.current;
      if (!assistantId) return;
      await streamEvents(
        "/v1/chat/drain",
        { chat_id: chatId, message: "" },
        (event) => handleStreamEvent(event, chatId, assistantId)
      );
    } catch (e) {
      setError((e as Error).message);
      setIsRunning(false);
    }
  };

  const threadMessages = useMemo(
    () => messages.map(toThreadMessage),
    [messages]
  );

  const runtime = useExternalStoreRuntime({
    messages: threadMessages,
    isRunning,
    convertMessage,
    onNew: async (message: AppendMessage) => {
      const chatId = currentChatIdRef.current;
      if (!chatId || isRunning) return;
      const text = getAppendText(message);
      if (!text.trim()) return;
      await createBackendMessage(chatId, "user", text);
      setMessages((prev) => [
        ...prev,
        { id: generateId(), role: "user", content: text, status: "complete" },
      ]);
      await streamAssistant(chatId, text);
    },
  });

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-background">
      <ChatSidebar
        chats={chats}
        currentChatId={currentChatId}
        onSelect={setCurrentChatId}
        onNew={createChat}
        onRename={renameChat}
        onDelete={deleteChat}
      />
      <div className="flex flex-1 flex-col h-full relative">
        {error && (
          <div className="absolute top-2 right-2 z-50 rounded-md bg-destructive px-4 py-2 text-sm text-destructive-foreground shadow">
            {error}
          </div>
        )}
        <AssistantRuntimeProvider runtime={runtime}>
          <Thread />
        </AssistantRuntimeProvider>
        <GateDialog pendingGate={pendingGate} onChoice={handleGateChoice} />
      </div>
    </div>
  );
}

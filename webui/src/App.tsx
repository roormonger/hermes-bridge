import { useEffect, useMemo, useRef, useState, useCallback } from "react";
import {
  AssistantRuntimeProvider,
  useExternalStoreRuntime,
  CompositeAttachmentAdapter,
  SimpleImageAttachmentAdapter,
  SimpleTextAttachmentAdapter,
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
import { Plus, Trash2, Pencil, MessageSquare, Check, X, LogOut, PanelLeftClose, PanelLeftOpen, Menu } from "lucide-react";
import { cn } from "@/lib/utils";
import { apiFetch, streamEvents, type SseEvent } from "./api";
import { useAuth, AuthProvider, AuthGuard } from "./auth";

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
  gateKind: string;
  options: string[];
  prompt: string;
};

type ToolStep = {
  name: string;
  context?: string;
  summary?: string;
  durationS?: number;
  status: "running" | "done";
};

type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  status?: "running" | "complete";
  gate?: Gate | null;
  toolSteps?: ToolStep[];
  createdAt?: number;
};

const generateId = () => {
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
};

const toThreadMessage = (msg: ChatMessage): ThreadMessageLike => ({
  id: msg.id,
  role: msg.role,
  content: [{ type: "text", text: msg.content }],
  status:
    msg.role === "assistant" && msg.status
      ? msg.status === "running"
        ? { type: "running" }
        : { type: "complete", reason: "stop" }
      : undefined,
  metadata: {
    custom: {
      ...(msg.gate ? { gate: msg.gate } : {}),
      toolSteps: msg.toolSteps ?? [],
      createdAt: msg.createdAt ?? Date.now(),
    },
  },
});

const convertMessage = (msg: ThreadMessageLike): ThreadMessageLike => {
  if (msg.role !== "assistant" && msg.status) {
    const { status, ...rest } = msg;
    return rest;
  }
  return msg;
};

const getAppendText = (msg: AppendMessage): string => {
  const parts = typeof msg.content === "string" ? [{ type: "text", text: msg.content }] : msg.content;
  const textParts = (parts as any[])
    .filter((p) => p.type === "text")
    .map((p) => p.text)
    .join("");
  // Text/document file attachments are inlined; images are uploaded separately
  const documentParts: string[] = [];
  for (const att of (msg.attachments ?? []) as any[]) {
    for (const part of (att.content ?? []) as any[]) {
      if (part.type === "text") {
        documentParts.push(`[File: ${att.name ?? "attachment"}]\n${part.text ?? ""}`);
      }
    }
  }
  return [textParts, ...documentParts].filter(Boolean).join("\n\n");
};

const getAppendImages = (msg: AppendMessage): Array<{ data: string; name: string }> => {
  const results: Array<{ data: string; name: string }> = [];
  for (const att of (msg.attachments ?? []) as any[]) {
    const name: string = att.name ?? "image";
    for (const part of (att.content ?? []) as any[]) {
      if (part.type === "image" && part.image) {
        results.push({ data: part.image as string, name });
      }
    }
  }
  return results;
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
  const [freeText, setFreeText] = useState("");
  const isFreeText = !!pendingGate && pendingGate.options.length === 0;

  if (!pendingGate) return null;

  const handleFreeTextSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const val = freeText.trim();
    if (!val) return;
    setFreeText("");
    onChoice(val);
  };

  return (
    <Dialog open={!!pendingGate} onOpenChange={() => {}}>
      <DialogContent showCloseButton={false}>
        <DialogHeader>
          <DialogTitle>Hermes needs your input</DialogTitle>
        </DialogHeader>
        <p className="text-muted-foreground">
          {pendingGate.prompt || "Choose an option:"}
        </p>
        {pendingGate.options.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {pendingGate.options.map((option) => (
              <Button key={option} variant="outline" onClick={() => onChoice(option)}>
                {option}
              </Button>
            ))}
          </div>
        )}
        <form onSubmit={handleFreeTextSubmit} className="flex gap-2">
          <Input
            autoFocus={isFreeText}
            value={freeText}
            onChange={(e) => setFreeText(e.target.value)}
            placeholder={isFreeText ? "Type your answer…" : "Or type a custom answer…"}
            className="flex-1"
          />
          <Button type="submit" disabled={!freeText.trim()}>
            Send
          </Button>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Sidebar
// ---------------------------------------------------------------------------

function formatGroup(date: Date): string {
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const startOfYesterday = new Date(startOfToday);
  startOfYesterday.setDate(startOfYesterday.getDate() - 1);
  const startOfWeek = new Date(startOfToday);
  startOfWeek.setDate(startOfWeek.getDate() - 7);

  if (date >= startOfToday) return "Today";
  if (date >= startOfYesterday) return "Yesterday";
  if (date >= startOfWeek) return "Previous 7 days";
  return "Older";
}

function groupChats(chats: Chat[]): { label: string; items: Chat[] }[] {
  const groups = new Map<string, Chat[]>();
  for (const chat of chats) {
    const label = formatGroup(new Date(chat.updated_at || chat.created_at));
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label)!.push(chat);
  }
  const order = ["Today", "Yesterday", "Previous 7 days", "Older"];
  return order
    .filter((label) => groups.has(label))
    .map((label) => ({ label, items: groups.get(label)! }));
}

function ChatSidebar({
  chats,
  currentChatId,
  onSelect,
  onNew,
  onRename,
  onDelete,
  username,
  onLogout,
  collapsed,
  onToggleCollapse,
  mobileOpen,
  onMobileClose,
}: {
  chats: Chat[];
  currentChatId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  onRename: (id: string, title: string) => void;
  onDelete: (id: string) => void;
  username: string;
  onLogout: () => void;
  collapsed: boolean;
  onToggleCollapse: () => void;
  mobileOpen: boolean;
  onMobileClose: () => void;
}) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const [search, setSearch] = useState("");
  const filteredChats = search.trim()
    ? chats.filter((c) => c.title.toLowerCase().includes(search.toLowerCase()))
    : chats;
  const groups = groupChats(filteredChats);

  return (
    <>
      {/* Mobile backdrop */}
      {mobileOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/50 md:hidden"
          onClick={onMobileClose}
        />
      )}
      <div
        className={cn(
          "bg-card flex flex-col h-full transition-all duration-200",
          /* Desktop: inline, collapsible */
          "hidden md:flex border-r",
          collapsed ? "w-16 items-center" : "w-72",
          /* Mobile: fixed slide-over */
          mobileOpen && "!flex fixed inset-y-0 left-0 z-50 w-72 shadow-xl",
        )}
      >
      <div className={cn("w-full flex items-center border-b p-2", collapsed ? "justify-center" : "justify-between")}>
        {!collapsed && (
          <div className="flex items-center gap-2 overflow-hidden">
            <img
              src="/static/hermes-logo.png"
              alt="Hermes"
              className="size-8 rounded-md object-contain shrink-0"
            />
            <span className="font-semibold text-sm truncate">Hermes Chat</span>
          </div>
        )}
        {collapsed && (
          <img
            src="/static/hermes-logo.png"
            alt="Hermes"
            className="size-8 rounded-md object-contain"
          />
        )}
        {mobileOpen ? (
          <Button
            variant="ghost"
            size="icon"
            className="size-8 shrink-0"
            onClick={onMobileClose}
            title="Close sidebar"
          >
            <X className="size-4" />
          </Button>
        ) : !collapsed ? (
          <Button
            variant="ghost"
            size="icon"
            className="size-8 shrink-0"
            onClick={onToggleCollapse}
            title="Collapse sidebar"
          >
            <PanelLeftClose className="size-4" />
          </Button>
        ) : (
          <Button
            variant="ghost"
            size="icon"
            className="size-8 shrink-0"
            onClick={onToggleCollapse}
            title="Expand sidebar"
          >
            <PanelLeftOpen className="size-4" />
          </Button>
        )}
      </div>

      <div className="p-2 w-full flex flex-col gap-2">
        <Button
          variant="outline"
          className={cn("h-10", collapsed ? "w-10 px-0 justify-center" : "w-full justify-start gap-2")}
          onClick={onNew}
          title="New Chat"
        >
          <Plus className="size-4" />
          {!collapsed && "New Chat"}
        </Button>
        {!collapsed && (
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search chats…"
            className="h-8 text-sm"
          />
        )}
      </div>

      <div className="flex-1 overflow-y-auto px-3 py-2 space-y-4 w-full">
        {groups.length === 0 && !collapsed && (
          <p className="text-sm text-muted-foreground px-2">
            {search.trim() ? "No matching chats." : "No chats yet."}
          </p>
        )}
        {groups.map(({ label, items }) => (
          <div key={label} className={cn("space-y-1", collapsed && "space-y-2")}>
            {!collapsed && (
              <h3 className="px-2 text-xs font-semibold text-muted-foreground uppercase tracking-wide">
                {label}
              </h3>
            )}
            {items.map((chat) => (
              <div
                key={chat.chat_id}
                className={cn(
                  "group relative flex items-center rounded-lg text-sm cursor-pointer transition-colors",
                  collapsed ? "justify-center size-10 p-0 mx-auto" : "gap-3 px-2 py-2",
                  currentChatId === chat.chat_id
                    ? "bg-accent text-accent-foreground"
                    : "hover:bg-muted"
                )}
                onClick={() => onSelect(chat.chat_id)}
                title={chat.title}
              >
                <MessageSquare className="size-4 shrink-0 text-muted-foreground" />
                {!collapsed && (
                  <>
                    {editingId === chat.chat_id ? (
                      <div className="flex flex-1 items-center gap-1">
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
                      </div>
                    ) : (
                      <>
                        <span className="flex-1 truncate">{chat.title}</span>
                        <div
                          className={cn(
                            "flex items-center gap-0.5",
                            currentChatId === chat.chat_id ? "opacity-100" : "opacity-0 group-hover:opacity-100"
                          )}
                        >
                          <Button
                            variant="ghost"
                            size="icon"
                            className="size-7"
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
                            className="size-7 text-destructive hover:text-destructive"
                            onClick={(e) => {
                              e.stopPropagation();
                              onDelete(chat.chat_id);
                            }}
                          >
                            <Trash2 className="size-3" />
                          </Button>
                        </div>
                      </>
                    )}
                  </>
                )}
              </div>
            ))}
          </div>
        ))}
      </div>

      <div className={cn("border-t w-full", collapsed ? "p-2 flex justify-center" : "p-3")}>
        <div
          className={cn(
            "flex items-center rounded-lg hover:bg-muted cursor-pointer",
            collapsed ? "justify-center size-10 p-0" : "justify-between gap-2 px-2 py-2"
          )}
          onClick={onLogout}
          title="Logout"
        >
          <div className="flex items-center gap-2 min-w-0">
            <div className="size-8 rounded-full bg-primary/10 flex items-center justify-center shrink-0">
              <span className="text-sm font-medium text-primary">
                {username.slice(0, 1).toUpperCase()}
              </span>
            </div>
            {!collapsed && <span className="text-sm font-medium truncate">{username}</span>}
          </div>
          {!collapsed && <LogOut className="size-4 shrink-0 text-muted-foreground" />}
        </div>
      </div>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// Main app
// ---------------------------------------------------------------------------

function ChatApp() {
  const { user, logout } = useAuth();
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
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
          status: m.role === "assistant" ? ("complete" as const) : undefined,
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
      } else if (event.type === "tool_start") {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantId
              ? {
                  ...m,
                  toolSteps: [
                    ...(m.toolSteps ?? []),
                    { name: event.name, context: event.context, status: "running" as const },
                  ],
                }
              : m
          )
        );
      } else if (event.type === "tool_complete") {
        setMessages((prev) =>
          prev.map((m) => {
            if (m.id !== assistantId) return m;
            const steps = [...(m.toolSteps ?? [])];
            const idx = steps.map((s) => s.name).lastIndexOf(event.name);
            if (idx !== -1) {
              steps[idx] = {
                ...steps[idx],
                summary: event.summary,
                durationS: event.duration_s,
                status: "done" as const,
              };
            }
            return { ...m, toolSteps: steps };
          })
        );
      } else if (event.type === "gate_interrupt") {
        const gate: Gate = {
          gateId: event.gate_id,
          gateKind: event.gate_kind || "approval",
          options: event.options || [],
          prompt: event.prompt || "",
        };
        assistantContentRef.current +=
          "\n\n🚦 Hermes needs your input: " + (event.prompt || "");
        setPendingGate(gate);
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantId
              ? { ...m, content: assistantContentRef.current, status: "running", gate }
              : m
          )
        );
        updateBackendMessage(chatId, assistantId, assistantContentRef.current);
      } else if (event.type === "turn_complete") {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantId
              ? { ...m, content: assistantContentRef.current, status: "complete", gate: null }
              : m
          )
        );
        setIsRunning(false);
        updateBackendMessage(chatId, assistantId, assistantContentRef.current);
      } else if (event.type === "session_title") {
        if (event.title) {
          setChats((prev) =>
            prev.map((c) => (c.chat_id === chatId ? { ...c, title: event.title } : c))
          );
          apiFetch(`/api/chats/${chatId}`, {
            method: "PATCH",
            body: JSON.stringify({ title: event.title }),
          }).catch(() => {});
        }
      } else if (event.type === "process_exit" || event.type === "error") {
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
    [setChats]
  );

  const streamAssistant = async (chatId: string, userText: string) => {
    setIsRunning(true);
    setError(null);
    assistantContentRef.current = "";
    const assistantId = await createBackendMessage(chatId, "assistant", "");
    assistantIdRef.current = assistantId;
    setMessages((prev) => [
      ...prev,
      { id: assistantId, role: "assistant", content: "", status: "running", toolSteps: [], createdAt: Date.now() },
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
          gate_kind: gate.gateKind || "approval",
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

  const attachmentAdapter = useMemo(
    () => new CompositeAttachmentAdapter([
      new SimpleImageAttachmentAdapter(),
      new SimpleTextAttachmentAdapter(),
    ]),
    []
  );

  const runtime = useExternalStoreRuntime({
    messages: threadMessages,
    isRunning,
    convertMessage,
    adapters: { attachments: attachmentAdapter },
    onNew: async (message: AppendMessage) => {
      if (isRunning) return;
      const text = getAppendText(message);
      const images = getAppendImages(message);
      if (!text.trim() && images.length === 0) return;

      let chatId = currentChatIdRef.current;
      if (!chatId) {
        const data = await apiFetch("/api/chats", {
          method: "POST",
          body: JSON.stringify({ title: "New chat" }),
        });
        setChats((prev) => [data, ...prev]);
        setCurrentChatId(data.chat_id);
        chatId = data.chat_id;
      }

      // Upload images to the gateway before prompt.submit so they arrive
      // as native multimodal content parts rather than text workarounds.
      for (const img of images) {
        try {
          await apiFetch("/v1/image/attach", {
            method: "POST",
            body: JSON.stringify({
              chat_id: chatId,
              content_base64: img.data,
              filename: img.name,
            }),
          });
        } catch (e) {
          setError(`Failed to attach image: ${(e as Error).message}`);
          return;
        }
      }

      await createBackendMessage(chatId, "user", text);
      setMessages((prev) => [
        ...prev,
        { id: generateId(), role: "user", content: text },
      ]);
      await streamAssistant(chatId, text);
    },
  });

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-background">
      <ChatSidebar
        chats={chats}
        currentChatId={currentChatId}
        onSelect={(id) => { setCurrentChatId(id); setMobileSidebarOpen(false); }}
        onNew={() => { createChat(); setMobileSidebarOpen(false); }}
        onRename={renameChat}
        onDelete={deleteChat}
        username={user?.username || ""}
        onLogout={logout}
        collapsed={sidebarCollapsed}
        onToggleCollapse={() => setSidebarCollapsed((v) => !v)}
        mobileOpen={mobileSidebarOpen}
        onMobileClose={() => setMobileSidebarOpen(false)}
      />
      <div className="flex flex-1 flex-col h-full relative min-w-0">
        {/* Mobile top bar */}
        <div className="md:hidden flex items-center gap-2 border-b px-3 py-2 bg-card shrink-0">
          <Button
            variant="ghost"
            size="icon"
            className="size-8"
            onClick={() => setMobileSidebarOpen(true)}
          >
            <Menu className="size-4" />
          </Button>
          <div className="flex items-center gap-2 flex-1 min-w-0">
            <img src="/static/hermes-logo.png" alt="Hermes" className="size-6 rounded-md object-contain shrink-0" />
            <span className="font-semibold text-sm truncate">
              {chats.find((c) => c.chat_id === currentChatId)?.title ?? "Hermes Chat"}
            </span>
          </div>
          <Button
            variant="ghost"
            size="icon"
            className="size-8 shrink-0"
            onClick={createChat}
            title="New Chat"
          >
            <Plus className="size-4" />
          </Button>
        </div>
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

export default function App() {
  return (
    <AuthProvider>
      <AuthGuard>
        <ChatApp />
      </AuthGuard>
    </AuthProvider>
  );
}

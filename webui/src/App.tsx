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
import { Plus, Trash2, Pencil, MessageSquare, Check, X, LogOut, PanelLeftClose, PanelLeftOpen, Menu, Undo2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { apiFetch, getModels, getCurrentModel, setModel, undoLastTurn, streamEvents, type SseEvent } from "./api";
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
  images?: string[];  // data URLs for user-attached images
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
  content: [
    ...(msg.images ?? []).map((src) => ({ type: "image" as const, image: src })),
    { type: "text", text: msg.content },
  ],
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

type ModelOption = {
  id: string;
  name: string;
  provider: string;
  providerName: string;
};

function flattenModelOptions(payload: any): ModelOption[] {
  const providers = payload?.providers ?? [];
  const options: ModelOption[] = [];
  for (const provider of providers) {
    const models = provider?.models ?? [];
    for (const model of models) {
      const id = model?.id || model?.model || String(model);
      const name = model?.name || model?.id || String(model);
      options.push({
        id,
        name,
        provider: provider.slug || provider.id || "unknown",
        providerName: provider.name || provider.slug || "Unknown",
      });
    }
  }
  return options;
}

function ModelPicker({
  chatId,
  open,
  onOpenChange,
  onModelChange,
}: {
  chatId: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onModelChange?: (model: string, provider: string) => void;
}) {
  const [catalog, setCatalog] = useState<any>(null);
  const [current, setCurrent] = useState<{ model?: string; provider?: string; gateway?: string; api_provider?: string }>({});
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [pending, setPending] = useState<{ model: string; provider: string; message: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    setError(null);
    Promise.all([getModels(), chatId ? getCurrentModel(chatId) : getCurrentModel()])
      .then(([modelsData, currentData]) => {
        setCatalog(modelsData);
        setCurrent({
          model: currentData?.model || "",
          provider: currentData?.provider || "",
          gateway: currentData?.gateway || "",
          api_provider: currentData?.api_provider || "",
        });
      })
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false));
  }, [open, chatId]);

  const options = useMemo(() => flattenModelOptions(catalog), [catalog]);
  const filtered = useMemo(() => {
    const term = search.trim().toLowerCase();
    if (!term) return options;
    return options.filter(
      (o) =>
        o.name.toLowerCase().includes(term) ||
        o.providerName.toLowerCase().includes(term) ||
        o.provider.toLowerCase().includes(term)
    );
  }, [options, search]);

  const grouped = useMemo(() => {
    const map = new Map<string, ModelOption[]>();
    for (const opt of filtered) {
      if (!map.has(opt.providerName)) map.set(opt.providerName, []);
      map.get(opt.providerName)!.push(opt);
    }
    return Array.from(map.entries()).sort((a, b) => a[0].localeCompare(b[0]));
  }, [filtered]);

  const handleSelect = async (model: string, provider: string) => {
    if (!chatId) return;
    setError(null);
    try {
      const result = await setModel(chatId, model, provider);
      if (result?.confirm_required) {
        setPending({ model, provider, message: result.confirm_message || result.warning || "Confirm model switch?" });
        setConfirmOpen(true);
        return;
      }
      setCurrent({ model, provider });
      onModelChange?.(model, provider);
      onOpenChange(false);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const handleConfirm = async () => {
    if (!pending || !chatId) return;
    try {
      await setModel(chatId, pending.model, pending.provider, true);
      setCurrent({ model: pending.model, provider: pending.provider });
      onModelChange?.(pending.model, pending.provider);
      setConfirmOpen(false);
      setPending(null);
      onOpenChange(false);
    } catch (e) {
      setError((e as Error).message);
      setConfirmOpen(false);
    }
  };

  const currentProviderLabel = current.gateway || current.api_provider || current.provider || "";
  const currentLabel = current.model
    ? `${currentProviderLabel ? `${currentProviderLabel} / ` : ""}${current.model}`
    : "Loading…";

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="max-w-md max-h-[80vh] flex flex-col">
          <DialogHeader>
            <DialogTitle>Switch Model</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">Current: {currentLabel}</p>
          <p className="text-xs text-muted-foreground/70">
            Lists all models available through your configured Hermes providers.
          </p>
          <Input
            placeholder="Search models…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="mt-2"
          />
          {error && <p className="text-sm text-destructive mt-2">{error}</p>}
          <div className="flex-1 overflow-y-auto mt-2 space-y-3 pr-1 min-h-[200px]">
            {loading && <p className="text-sm text-muted-foreground">Loading models…</p>}
            {!loading && grouped.length === 0 && (
              <p className="text-sm text-muted-foreground">No models found.</p>
            )}
            {grouped.map(([providerName, items]) => (
              <div key={providerName}>
                <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-1 sticky top-0 bg-background py-1">
                  {providerName}
                </h4>
                <div className="space-y-1">
                  {items.map((opt) => (
                    <button
                      key={`${opt.provider}:${opt.id}`}
                      onClick={() => handleSelect(opt.id, opt.provider)}
                      disabled={!chatId}
                      className={cn(
                        "w-full text-left px-2 py-1.5 rounded-md text-sm hover:bg-accent hover:text-accent-foreground transition-colors",
                        current.model === opt.id && current.provider === opt.provider
                          ? "bg-accent text-accent-foreground"
                          : ""
                      )}
                    >
                      {opt.name}
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Confirm Model Switch</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground whitespace-pre-wrap">{pending?.message}</p>
          <div className="flex justify-end gap-2 mt-4">
            <Button variant="outline" onClick={() => setConfirmOpen(false)}>
              Cancel
            </Button>
            <Button onClick={handleConfirm}>Switch</Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}

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
  const [modelPickerOpen, setModelPickerOpen] = useState(false);
  const [currentModelDisplay, setCurrentModelDisplay] = useState<string>("");
  const [sessionInfo, setSessionInfo] = useState<{
    model?: string;
    provider?: string;
    reasoning_effort?: string;
    service_tier?: string;
    fast?: boolean;
    yolo?: boolean;
  }>({});

  const assistantIdRef = useRef<string | null>(null);
  const assistantContentRef = useRef("");
  const currentChatIdRef = useRef(currentChatId);
  const abortControllerRef = useRef<AbortController | null>(null);

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
          images: m.images?.length ? m.images : undefined,
          status: m.role === "assistant" ? ("complete" as const) : undefined,
          createdAt: m.created_at ? m.created_at * 1000 : Date.now(),
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
    if (currentChatId) {
      loadMessages(currentChatId);
      setSessionInfo({});
    } else {
      setMessages([]);
      setSessionInfo({});
    }
  }, [currentChatId, loadMessages]);

  useEffect(() => {
    if (!currentChatId) {
      setCurrentModelDisplay("");
      return;
    }
    getCurrentModel(currentChatId)
      .then((data) => {
        const model = data?.model || "";
        const provider = data?.gateway || data?.api_provider || data?.provider || "";
        setCurrentModelDisplay(provider ? `${provider} / ${model}` : model);
      })
      .catch(() => setCurrentModelDisplay(""));
  }, [currentChatId]);

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

  const handleUndo = async () => {
    const chatId = currentChatIdRef.current;
    if (!chatId || isRunning) return;
    try {
      await undoLastTurn(chatId);
      await loadMessages(chatId);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const createBackendMessage = async (
    chatId: string,
    role: "user" | "assistant",
    content: string,
    images?: string[]
  ): Promise<string> => {
    const data = await apiFetch(`/api/chats/${chatId}/messages`, {
      method: "POST",
      body: JSON.stringify({ role, content, images: images ?? [] }),
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
      } else if (event.type === "session_info") {
        setSessionInfo({
          model: event.model,
          provider: event.gateway || event.api_provider || event.provider,
          reasoning_effort: event.reasoning_effort,
          service_tier: event.service_tier,
          fast: event.fast,
          yolo: event.yolo,
        });
        if (event.model) {
          const provider = event.gateway || event.api_provider || event.provider || "";
          const providerLower = provider.toLowerCase();
          const modelLower = event.model.toLowerCase();
          // Hermes sometimes reports the model vendor ("deepseek") as provider
          // while the real gateway ("openrouter") lives elsewhere. Avoid
          // overwriting the top-bar gateway label with a vendor prefix.
          const isVendorOnly = providerLower && modelLower.startsWith(`${providerLower}/`);
          if (!isVendorOnly) {
            setCurrentModelDisplay(provider ? `${provider} / ${event.model}` : event.model);
          }
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

    const ac = new AbortController();
    abortControllerRef.current = ac;
    try {
      await streamEvents(
        "/v1/chat",
        { chat_id: chatId, message: userText },
        (event) => handleStreamEvent(event, chatId, assistantId),
        ac.signal,
      );
    } catch (e) {
      if ((e as Error).name === "AbortError") return;
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
    onCancel: async () => {
      abortControllerRef.current?.abort();
      abortControllerRef.current = null;
      const chatId = currentChatIdRef.current;
      if (chatId) {
        try {
          await apiFetch("/v1/chat/cancel", {
            method: "POST",
            body: JSON.stringify({ chat_id: chatId }),
          });
        } catch (e) {
          console.warn("cancel request failed:", e);
        }
      }
      const assistantId = assistantIdRef.current;
      setIsRunning(false);
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantId ? { ...m, status: "complete" as const, gate: null } : m
        )
      );
      if (assistantId && chatId) {
        updateBackendMessage(chatId, assistantId, assistantContentRef.current).catch(() => {});
      }
    },
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

      await createBackendMessage(chatId, "user", text, images.map((img) => img.data));
      const now = Date.now();
      setMessages((prev) => [
        ...prev,
        {
          id: generateId(),
          role: "user",
          content: text,
          images: images.map((img) => img.data),
          createdAt: now,
        },
      ]);
      await streamAssistant(chatId, text);
    },
    onEdit: async (message: AppendMessage) => {
      const chatId = currentChatIdRef.current;
      const sourceId = (message as any).sourceId;
      if (!chatId || !sourceId) return;
      const text = getAppendText(message);
      try {
        await updateBackendMessage(chatId, sourceId, text);
        setMessages((prev) =>
          prev.map((m) =>
            m.id === sourceId ? { ...m, content: text } : m
          )
        );
      } catch (e) {
        setError((e as Error).message);
      }
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
          {currentChatId && (
            <Button
              variant="ghost"
              size="sm"
              className="shrink-0 text-xs truncate max-w-[140px]"
              onClick={() => setModelPickerOpen(true)}
              disabled={isRunning}
              title="Switch model"
            >
              {currentModelDisplay || "Model"}
            </Button>
          )}
          {currentChatId && (
            <Button
              variant="ghost"
              size="icon"
              className="size-8 shrink-0"
              onClick={handleUndo}
              disabled={isRunning}
              title="Undo last turn"
            >
              <Undo2 className="size-4" />
            </Button>
          )}
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

        {/* Desktop header */}
        <div className="hidden md:flex items-center justify-between border-b px-4 py-2 bg-card shrink-0">
          <div className="flex items-center gap-2 min-w-0">
            <span className="font-semibold text-sm truncate">
              {chats.find((c) => c.chat_id === currentChatId)?.title ?? "Hermes Chat"}
            </span>
          </div>
          <div className="flex items-center gap-2">
            {currentChatId && (
              <Button
                variant="outline"
                size="sm"
                className="text-xs truncate max-w-[240px]"
                onClick={() => setModelPickerOpen(true)}
                disabled={isRunning}
                title={[
                  currentModelDisplay || "Model",
                  sessionInfo.reasoning_effort && `reasoning: ${sessionInfo.reasoning_effort}`,
                  sessionInfo.service_tier && `tier: ${sessionInfo.service_tier}`,
                  sessionInfo.fast && "fast",
                  sessionInfo.yolo && "yolo",
                ].filter(Boolean).join(" · ")}
              >
                {currentModelDisplay || "Model"}
              </Button>
            )}
            {currentChatId && sessionInfo.reasoning_effort && (
              <span className="hidden md:inline-flex items-center rounded-full border border-border/50 bg-muted/60 px-2 py-0.5 text-[10px] text-muted-foreground uppercase tracking-wide">
                {sessionInfo.reasoning_effort}
              </span>
            )}
            {currentChatId && sessionInfo.service_tier && (
              <span className="hidden md:inline-flex items-center rounded-full border border-border/50 bg-muted/60 px-2 py-0.5 text-[10px] text-muted-foreground uppercase tracking-wide">
                {sessionInfo.service_tier}
              </span>
            )}
            {currentChatId && (sessionInfo.fast || sessionInfo.yolo) && (
              <span className="hidden md:inline-flex items-center gap-1 rounded-full border border-border/50 bg-muted/60 px-2 py-0.5 text-[10px] text-muted-foreground uppercase tracking-wide">
                {sessionInfo.fast && <span>fast</span>}
                {sessionInfo.yolo && <span>yolo</span>}
              </span>
            )}
            {currentChatId && (
              <Button
                variant="ghost"
                size="icon"
                className="size-8 shrink-0"
                onClick={handleUndo}
                disabled={isRunning}
                title="Undo last turn"
              >
                <Undo2 className="size-4" />
              </Button>
            )}
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
        </div>

        {error && (
          <div className="absolute top-2 right-2 z-50 rounded-md bg-destructive px-4 py-2 text-sm text-destructive-foreground shadow">
            {error}
          </div>
        )}
        <div className="flex flex-col flex-1 min-h-0 overflow-hidden">
          <AssistantRuntimeProvider runtime={runtime}>
            <Thread />
          </AssistantRuntimeProvider>
        </div>
        <GateDialog pendingGate={pendingGate} onChoice={handleGateChoice} />
        <ModelPicker
          chatId={currentChatId}
          open={modelPickerOpen}
          onOpenChange={setModelPickerOpen}
          onModelChange={(model, provider) =>
            setCurrentModelDisplay(provider ? `${provider} / ${model}` : model)
          }
        />
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

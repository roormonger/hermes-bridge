import {
  ComposerAddAttachment,
  ComposerAttachments,
  UserMessageAttachments,
} from "@/components/assistant-ui/attachment";
import { MarkdownText } from "@/components/assistant-ui/markdown-text";
import { TooltipIconButton } from "@/components/assistant-ui/tooltip-icon-button";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { ContextDisplay } from "@/components/context-display";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import type { ThreadTokenUsage } from "@assistant-ui/react-ai-sdk";
import {
  ActionBarMorePrimitive,
  ActionBarPrimitive,
  AuiIf,
  type AssistantState,
  BranchPickerPrimitive,
  ComposerPrimitive,
  ErrorPrimitive,
  MessagePrimitive,
  SuggestionPrimitive,
  ThreadPrimitive,
  groupPartByType,
  useAuiState,
  useComposerRuntime,
} from "@assistant-ui/react";
import {
  ArrowDownIcon,
  ArrowUpIcon,
  CheckIcon,
  ChevronDownIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  CopyIcon,
  DownloadIcon,
  Loader2Icon,
  MicIcon,
  MoreHorizontalIcon,
  PencilIcon,
  RefreshCwIcon,
  SquareIcon,
  Undo2Icon,
  Volume2Icon,
  VolumeXIcon,
  WrenchIcon,
} from "lucide-react";
import { type FC, type ReactNode, useState, useCallback, useEffect, useRef, Children, createContext, useContext } from "react";
import { createPortal } from "react-dom";
import { getToken } from "../../api";
import { useVoiceRecorder } from "../../hooks/useVoiceRecorder";
import type { VoiceCapabilities } from "../../hooks/useVoiceCapabilities";

const VoiceCapsContext = createContext<VoiceCapabilities>({ ttsAvailable: true, sttAvailable: true, ttsVoice: undefined });

// ---------------------------------------------------------------------------
// Lightbox
// ---------------------------------------------------------------------------

const Lightbox: FC<{ src: string; onClose: () => void }> = ({ src, onClose }) => {
  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  return createPortal(
    <div
      className="fixed inset-0 z-[9999] flex items-center justify-center bg-black/80 backdrop-blur-sm"
      onClick={onClose}
    >
      <img
        src={src}
        alt="preview"
        className="max-h-[90vh] max-w-[90vw] rounded-xl object-contain shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      />
      <button
        className="absolute right-4 top-4 rounded-full bg-white/10 p-2 text-white hover:bg-white/20"
        onClick={onClose}
      >
        ✕
      </button>
    </div>,
    document.body,
  );
};

const ImageWithLightbox: FC<{ image: string }> = ({ image }) => {
  const [lb, setLb] = useState<string | null>(null);
  return (
    <>
      <img
        src={image}
        alt="attachment"
        className="mb-2 max-h-64 max-w-full cursor-zoom-in rounded-lg object-contain"
        onClick={(e) => { e.stopPropagation(); setLb(image); }}
      />
      {lb && <Lightbox src={lb} onClose={() => setLb(null)} />}
    </>
  );
};

// ---------------------------------------------------------------------------
// File path detection & download / inline preview
// ---------------------------------------------------------------------------

const FILE_PATH_RE = /(?:^|[\s("'])(\/(?:[^\s"'\\<>\x00-\x1f]+\/)+[^\s"'\\<>\x00-\x1f]+\.[a-zA-Z0-9]{1,10})(?=[\s"'\\).,!?]|$)/gm;
const IMAGE_EXTS = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"]);

function extractFilePaths(text: string): string[] {
  const found = new Set<string>();
  let m: RegExpExecArray | null;
  FILE_PATH_RE.lastIndex = 0;
  while ((m = FILE_PATH_RE.exec(text)) !== null) {
    found.add(m[1]);
  }
  return Array.from(found);
}

function isImagePath(p: string): boolean {
  const dot = p.lastIndexOf(".");
  return dot !== -1 && IMAGE_EXTS.has(p.slice(dot).toLowerCase());
}

function previewUrl(path: string): string {
  const token = getToken();
  return `/v1/file/preview?path=${encodeURIComponent(path)}${token ? `&token=${encodeURIComponent(token)}` : ""}`;
}

const FileDownloadLinks: FC<{ text: string }> = ({ text }) => {
  const [downloading, setDownloading] = useState<string | null>(null);
  const [lightbox, setLightbox] = useState<string | null>(null);
  const paths = extractFilePaths(text);
  if (paths.length === 0) return null;

  const download = useCallback(async (path: string) => {
    setDownloading(path);
    try {
      const token = getToken();
      const url = `/v1/file/download?path=${encodeURIComponent(path)}`;
      const res = await fetch(url, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!res.ok) {
        const msg = await res.text();
        alert(`Download failed: ${msg || res.statusText}`);
        return;
      }
      const blob = await res.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = path.split("/").pop() ?? "file";
      a.click();
      URL.revokeObjectURL(a.href);
    } finally {
      setDownloading(null);
    }
  }, []);

  const images = paths.filter(isImagePath);
  const others = paths.filter((p) => !isImagePath(p));

  return (
    <div className="mt-3 space-y-3">
      {images.map((p) => (
        <div key={p} className="overflow-hidden rounded-xl border border-border/40 bg-muted/20">
          <img
            src={previewUrl(p)}
            alt={p.split("/").pop()}
            className="max-h-96 w-auto max-w-full cursor-zoom-in object-contain"
            loading="lazy"
            onClick={() => setLightbox(previewUrl(p))}
          />
          <div className="flex items-center justify-between border-t border-border/30 px-3 py-1.5">
            <span className="max-w-[20rem] truncate font-mono text-xs text-muted-foreground">
              {p.split("/").pop()}
            </span>
            <button
              onClick={() => download(p)}
              disabled={downloading === p}
              className="inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:opacity-50"
            >
              <DownloadIcon className="size-3" />
              Download
            </button>
          </div>
        </div>
      ))}
      {lightbox && <Lightbox src={lightbox} onClose={() => setLightbox(null)} />}
      {others.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {others.map((p) => (
            <button
              key={p}
              onClick={() => download(p)}
              disabled={downloading === p}
              className="inline-flex items-center gap-1.5 rounded-lg border border-border/50 bg-muted/60 px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:opacity-50"
            >
              <DownloadIcon className="size-3 shrink-0" />
              <span className="max-w-[24rem] truncate font-mono">{p.split("/").pop()}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
};

const isNewChatView = (s: AssistantState) =>
  s.thread.messages.length === 0 &&
  (!s.thread.isLoading || s.threads.isLoading);

export const Thread: FC<{
  onUndo?: () => void;
  contextWindow?: number;
  threadUsage?: ThreadTokenUsage;
  autoSpeak?: boolean;
  onAutoSpeakToggle?: () => void;
  voiceCaps?: VoiceCapabilities;
  ttsVoice?: string;
}> = ({ onUndo, contextWindow, threadUsage, autoSpeak, onAutoSpeakToggle, voiceCaps, ttsVoice }) => {
  const caps = { ...(voiceCaps ?? { ttsAvailable: true, sttAvailable: true }), ttsVoice };
  const isEmpty = useAuiState(isNewChatView);

  return (
    <VoiceCapsContext.Provider value={caps}>
    <ThreadPrimitive.Root
      className="aui-root aui-thread-root bg-background @container flex h-full flex-col"
      style={{
        ["--thread-max-width" as string]: "44rem",
        ["--composer-bg" as string]:
          "color-mix(in oklab, var(--color-muted) 30%, var(--color-background))",
        ["--composer-radius" as string]: "1.5rem",
        ["--composer-padding" as string]: "8px",
      }}
    >
      <ThreadPrimitive.Viewport
        turnAnchor="top"
        data-slot="aui_thread-viewport"
        className="relative flex flex-1 flex-col overflow-x-auto overflow-y-scroll scroll-smooth"
      >
        <div
          className={cn(
            "mx-auto flex w-full max-w-(--thread-max-width) flex-1 flex-col px-4 pt-4",
            isEmpty && "justify-center",
          )}
        >
          <AuiIf condition={isNewChatView}>
            <ThreadWelcome />
          </AuiIf>

          <div
            data-slot="aui_message-group"
            className="mb-14 flex flex-col gap-y-6 empty:hidden"
          >
            <ThreadPrimitive.Messages>
              {() => <ThreadMessage />}
            </ThreadPrimitive.Messages>
          </div>

          <ThreadPrimitive.ViewportFooter
            className={cn(
              "aui-thread-viewport-footer bg-background flex flex-col gap-4 overflow-visible pb-4 md:pb-6",
              !isEmpty &&
                "sticky bottom-0 mt-auto rounded-t-(--composer-radius)",
            )}
          >
            <ThreadScrollToBottom />
            <Composer onUndo={onUndo} contextWindow={contextWindow} threadUsage={threadUsage} autoSpeak={autoSpeak} onAutoSpeakToggle={onAutoSpeakToggle} />
            <AuiIf condition={(s) => isNewChatView(s) && s.composer.isEmpty}>
              <ThreadSuggestions />
            </AuiIf>
          </ThreadPrimitive.ViewportFooter>
        </div>
      </ThreadPrimitive.Viewport>
    </ThreadPrimitive.Root>
    </VoiceCapsContext.Provider>
  );
};

const ThreadMessage: FC = () => {
  const role = useAuiState((s) => s.message.role);
  const isEditing = useAuiState((s) => s.message.composer.isEditing);

  if (isEditing) return <EditComposer />;
  if (role === "user") return <UserMessage />;
  return <AssistantMessage />;
};

const ThreadScrollToBottom: FC = () => {
  return (
    <ThreadPrimitive.ScrollToBottom asChild>
      <TooltipIconButton
        tooltip="Scroll to bottom"
        variant="outline"
        className="aui-thread-scroll-to-bottom dark:border-border dark:bg-background dark:hover:bg-accent absolute -top-12 z-10 self-center rounded-full p-4 disabled:invisible"
      >
        <ArrowDownIcon />
      </TooltipIconButton>
    </ThreadPrimitive.ScrollToBottom>
  );
};

const ThreadWelcome: FC = () => {
  return (
    <div className="aui-thread-welcome-root mb-6 flex flex-col items-center px-4 text-center">
      <h1 className="aui-thread-welcome-message-inner fade-in slide-in-from-bottom-1 animate-in fill-mode-both text-2xl font-semibold duration-200">
        How can I help you today?
      </h1>
    </div>
  );
};

const ThreadSuggestions: FC = () => {
  return (
    <div className="aui-thread-welcome-suggestions flex w-full flex-wrap items-center justify-center gap-2 px-4">
      <ThreadPrimitive.Suggestions>
        {() => <ThreadSuggestionItem />}
      </ThreadPrimitive.Suggestions>
    </div>
  );
};

const ThreadSuggestionItem: FC = () => {
  return (
    <div className="aui-thread-welcome-suggestion-display fade-in slide-in-from-bottom-2 animate-in fill-mode-both duration-200">
      <SuggestionPrimitive.Trigger send asChild>
        <Button
          variant="ghost"
          className="aui-thread-welcome-suggestion text-foreground hover:bg-muted border-border/60 h-auto gap-1.5 rounded-full border px-3.5 py-1.5 text-sm font-normal whitespace-nowrap transition-colors"
        >
          <SuggestionPrimitive.Title className="aui-thread-welcome-suggestion-text-1" />
          <SuggestionPrimitive.Description className="aui-thread-welcome-suggestion-text-2 empty:hidden" />
        </Button>
      </SuggestionPrimitive.Trigger>
    </div>
  );
};

const Composer: FC<{
  onUndo?: () => void;
  contextWindow?: number;
  threadUsage?: ThreadTokenUsage;
  autoSpeak?: boolean;
  onAutoSpeakToggle?: () => void;
}> = ({ onUndo, contextWindow, threadUsage, autoSpeak, onAutoSpeakToggle }) => {
  const composerRuntime = useComposerRuntime();
  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      composerRuntime.send();
    }
  };

  return (
    <ComposerPrimitive.Root className="aui-composer-root relative flex w-full flex-col">
      <ComposerPrimitive.AttachmentDropzone asChild>
        <div
          data-slot="aui_composer-shell"
          className="border-border/60 data-[dragging=true]:border-ring focus-within:border-border dark:border-muted-foreground/15 dark:focus-within:border-muted-foreground/30 flex w-full flex-col gap-2 rounded-(--composer-radius) border bg-(--composer-bg) p-(--composer-padding) shadow-[0_4px_16px_-8px_rgba(0,0,0,0.08),0_1px_2px_rgba(0,0,0,0.04)] transition-[border-color,box-shadow] focus-within:shadow-[0_6px_24px_-8px_rgba(0,0,0,0.12),0_1px_2px_rgba(0,0,0,0.05)] data-[dragging=true]:border-dashed data-[dragging=true]:bg-[color-mix(in_oklab,var(--color-accent)_50%,var(--color-background))] dark:shadow-none"
        >
          <ComposerAttachments />
          <ComposerPrimitive.Input
            placeholder="Send a message..."
            className="aui-composer-input placeholder:text-muted-foreground/80 max-h-32 min-h-10 w-full resize-none bg-transparent px-2.5 py-1 text-base outline-none"
            rows={1}
            autoFocus
            aria-label="Message input"
            onKeyDown={handleKeyDown}
          />
          <ComposerAction onUndo={onUndo} contextWindow={contextWindow} threadUsage={threadUsage} autoSpeak={autoSpeak} onAutoSpeakToggle={onAutoSpeakToggle} />
        </div>
      </ComposerPrimitive.AttachmentDropzone>
    </ComposerPrimitive.Root>
  );
};

const ComposerAction: FC<{
  onUndo?: () => void;
  contextWindow?: number;
  threadUsage?: ThreadTokenUsage;
  autoSpeak?: boolean;
  onAutoSpeakToggle?: () => void;
}> = ({ onUndo, contextWindow, threadUsage, autoSpeak, onAutoSpeakToggle }) => {
  const canUndo = useAuiState((s) => s.thread.messages.length > 0);
  const isRunning = useAuiState((s) => s.thread.isRunning);
  const composerRuntime = useComposerRuntime();
  const composerText = useAuiState((s) => s.composer.text);

  const handleTranscript = useCallback((text: string) => {
    const current = composerText ?? "";
    const next = current ? current + " " + text : text;
    composerRuntime.setText(next);
  }, [composerText, composerRuntime]);

  const { recording, transcribing, start, stop } = useVoiceRecorder(handleTranscript);
  const voiceCaps = useContext(VoiceCapsContext);

  const handleMicClick = () => {
    if (recording) stop();
    else start();
  };

  return (
    <div className="aui-composer-action-wrapper relative flex items-center justify-between">
      <div className="flex items-center gap-1">
        <ComposerAddAttachment />
        {onAutoSpeakToggle && voiceCaps.ttsAvailable && (
          <TooltipIconButton
            tooltip={autoSpeak ? "Auto-speak on (click to turn off)" : "Auto-speak off (click to turn on)"}
            side="top"
            type="button"
            variant="ghost"
            size="icon"
            className={cn("size-7 rounded-full", autoSpeak && "text-primary")}
            aria-label={autoSpeak ? "Disable auto-speak" : "Enable auto-speak"}
            onClick={onAutoSpeakToggle}
          >
            {autoSpeak ? <Volume2Icon className="size-4" /> : <VolumeXIcon className="size-4" />}
          </TooltipIconButton>
        )}
      </div>
      <div className="flex items-center gap-1.5">
        {contextWindow && contextWindow > 0 && (
          <ContextDisplay.Bar
            modelContextWindow={contextWindow}
            usage={threadUsage}
            side="top"
          />
        )}
        {onUndo && canUndo && (
          <TooltipIconButton
            tooltip="Undo last turn"
            side="bottom"
            type="button"
            variant="ghost"
            size="icon"
            className="size-7 rounded-full"
            aria-label="Undo last turn"
            onClick={onUndo}
            disabled={isRunning}
          >
            <Undo2Icon className="size-4" />
          </TooltipIconButton>
        )}
        {voiceCaps.sttAvailable && <TooltipIconButton
          tooltip={recording ? "Stop recording" : transcribing ? "Transcribing..." : "Voice input"}
          side="bottom"
          type="button"
          variant="ghost"
          size="icon"
          className={cn(
            "size-7 rounded-full",
            recording && "text-destructive",
          )}
          aria-label={recording ? "Stop recording" : "Start voice input"}
          onClick={handleMicClick}
          disabled={transcribing}
        >
          {transcribing ? (
            <Loader2Icon className="size-4 animate-spin" />
          ) : recording ? (
            <SquareIcon className="size-3.5 animate-pulse fill-current" />
          ) : (
            <MicIcon className="size-4" />
          )}
        </TooltipIconButton>}
        <AuiIf condition={(s) => !s.thread.isRunning}>
          <ComposerPrimitive.Send asChild>
            <TooltipIconButton
              tooltip="Send message"
              side="bottom"
              type="button"
              variant="default"
              size="icon"
              className="aui-composer-send size-7 rounded-full"
              aria-label="Send message"
            >
              <ArrowUpIcon className="aui-composer-send-icon size-4.5" />
            </TooltipIconButton>
          </ComposerPrimitive.Send>
        </AuiIf>
        <AuiIf condition={(s) => s.thread.isRunning}>
          <ComposerPrimitive.Cancel asChild>
            <Button
              type="button"
              variant="default"
              size="icon"
              className="aui-composer-cancel size-7 rounded-full"
              aria-label="Stop generating"
            >
              <SquareIcon className="aui-composer-cancel-icon size-3.5 fill-current" />
            </Button>
          </ComposerPrimitive.Cancel>
        </AuiIf>
      </div>
    </div>
  );
};

const MessageError: FC = () => {
  return (
    <MessagePrimitive.Error>
      <ErrorPrimitive.Root className="aui-message-error-root border-destructive bg-destructive/10 text-destructive dark:bg-destructive/5 mt-2 rounded-md border p-3 text-sm dark:text-red-200">
        <ErrorPrimitive.Message className="aui-message-error-message line-clamp-2" />
      </ErrorPrimitive.Root>
    </MessagePrimitive.Error>
  );
};


const HERMES_LOGO = "/static/hermes-logo.png";

function formatTime(ts: number): string {
  return new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

type ToolCallPart = {
  type: "tool-call";
  toolName: string;
  argsText?: string;
  status?: { type: "running" } | { type: "complete" };
  result?: string;
  durationS?: number;
};

const ToolCallRow: FC<{ step: ToolCallPart }> = ({ step }) => {
  const isRunning = step.status?.type === "running";
  return (
    <div className="flex items-start gap-2 px-3 py-2">
      <div className="mt-0.5 shrink-0">
        {isRunning ? (
          <span className="size-1.5 rounded-full bg-amber-400 animate-pulse inline-block" />
        ) : (
          <span className="size-1.5 rounded-full bg-emerald-500 inline-block" />
        )}
      </div>
      <div className="min-w-0 flex-1">
        <span className="font-medium text-foreground/80">{step.toolName}</span>
        {step.argsText && (
          <span className="text-muted-foreground"> · {step.argsText}</span>
        )}
        {step.result && (
          <div className="text-muted-foreground mt-0.5 truncate">{step.result}</div>
        )}
      </div>
      {step.durationS != null && (
        <span className="shrink-0 text-muted-foreground/70">{step.durationS.toFixed(1)}s</span>
      )}
    </div>
  );
};

const ToolStepsGroup: FC<{ children: ReactNode }> = ({ children }) => {
  const [open, setOpen] = useState(false);
  const count = Children.toArray(children).length;
  if (count === 0) return null;
  const label = `${count} step${count !== 1 ? "s" : ""}`;

  return (
    <Collapsible open={open} onOpenChange={setOpen} className="mb-2">
      <CollapsibleTrigger asChild>
        <button
          type="button"
          className="inline-flex items-center gap-1.5 rounded-full border border-border/50 bg-muted/60 px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
        >
          <WrenchIcon className="size-3" />
          <span>{label}</span>
          <ChevronDownIcon className={cn("size-3 transition-transform", open && "rotate-180")} />
        </button>
      </CollapsibleTrigger>
      <CollapsibleContent>
        <div className="mt-1.5 rounded-lg border border-border/40 bg-muted/30 divide-y divide-border/30 overflow-hidden text-xs">
          {children}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
};

const AssistantMessage: FC = () => {
  const ACTION_BAR_PT = "pt-1.5";
  const ACTION_BAR_HEIGHT = `-mb-7.5 min-h-7.5 ${ACTION_BAR_PT}`;
  const meta = useAuiState((s) => (s.message.metadata as any)?.custom ?? {});
  const createdAt: number = meta.createdAt ?? Date.now();
  const messageUsage: { totalTokens?: number } | undefined = meta.usage;

  return (
    <MessagePrimitive.Root
      data-slot="aui_assistant-message-root"
      data-role="assistant"
      className="fade-in slide-in-from-bottom-1 animate-in relative duration-150"
    >
      {/* Agent header: avatar + name + timestamp */}
      <div className="flex items-center gap-2 mb-2 px-1">
        <div className="size-7 shrink-0 rounded-full overflow-hidden bg-muted border border-border/40 flex items-center justify-center">
          <img
            src={HERMES_LOGO}
            alt="Hermes"
            className="size-full object-cover"
            onError={(e) => {
              (e.currentTarget as HTMLImageElement).style.display = "none";
              e.currentTarget.parentElement!.innerHTML = '<span class="text-xs font-bold text-primary">H</span>';
            }}
          />
        </div>
        <span className="text-sm font-semibold text-foreground/90">Hermes</span>
        <span className="text-xs text-muted-foreground/60">{formatTime(createdAt)}</span>
        {messageUsage && messageUsage.totalTokens != null && (
          <span className="text-xs text-muted-foreground/60 tabular-nums">
            · {messageUsage.totalTokens.toLocaleString()} tokens
          </span>
        )}
      </div>

      <div
        data-slot="aui_assistant-message-content"
        className="text-foreground px-2 leading-relaxed wrap-break-word [contain-intrinsic-size:auto_24px] [content-visibility:auto]"
      >
        <MessagePrimitive.GroupedParts
          groupBy={groupPartByType({ "tool-call": ["group-tool"] })}
        >
          {({ part, children }) => {
            switch (part.type) {
              case "group-tool":
                return <ToolStepsGroup>{children}</ToolStepsGroup>;
              case "text":
                return (
                  <>
                    <MarkdownText />
                    <AuiIf condition={(s) => s.message.status?.type !== "running"}>
                      <FileDownloadLinks text={(part as any).text ?? ""} />
                    </AuiIf>
                  </>
                );
              case "tool-call":
                return <ToolCallRow step={part as unknown as ToolCallPart} />;
              default:
                return null;
            }
          }}
        </MessagePrimitive.GroupedParts>
        <MessageError />
      </div>

      <div
        data-slot="aui_assistant-message-footer"
        className={cn("ms-2 flex items-center", ACTION_BAR_HEIGHT)}
      >
        <BranchPicker />
        <AssistantActionBar />
      </div>
    </MessagePrimitive.Root>
  );
};

const AssistantActionBar: FC = () => {
  const voiceCaps = useContext(VoiceCapsContext);
  const [speaking, setSpeaking] = useState(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const audioUrlRef = useRef<string | null>(null);

  const messageText = useAuiState((s) =>
    s.message.content
      .filter((p: any) => p.type === "text")
      .map((p: any) => p.text)
      .join(" ")
  );

  const handleSpeak = useCallback(async () => {
    if (speaking) {
      audioRef.current?.pause();
      setSpeaking(false);
      return;
    }
    if (!messageText?.trim()) return;
    setSpeaking(true);
    try {
      const { speakText } = await import("../../api");
      if (audioUrlRef.current) URL.revokeObjectURL(audioUrlRef.current);
      const url = await speakText(messageText, undefined, voiceCaps.ttsVoice);
      audioUrlRef.current = url;
      const audio = new Audio(url);
      audioRef.current = audio;
      audio.onended = () => setSpeaking(false);
      audio.onerror = () => setSpeaking(false);
      await audio.play();
    } catch (e) {
      console.error("TTS failed:", e);
      setSpeaking(false);
    }
  }, [speaking, messageText]);

  return (
    <ActionBarPrimitive.Root
      hideWhenRunning
      autohide="not-last"
      className="aui-assistant-action-bar-root text-muted-foreground animate-in fade-in col-start-3 row-start-2 -ms-1 flex gap-1 duration-200"
    >
      {voiceCaps.ttsAvailable && <TooltipIconButton
        tooltip={speaking ? "Stop speaking" : "Read aloud"}
        onClick={handleSpeak}
      >
        {speaking ? (
          <SquareIcon className="size-3.5 fill-current" />
        ) : (
          <Volume2Icon className="size-4" />
        )}
      </TooltipIconButton>}
      <ActionBarPrimitive.Copy asChild>
        <TooltipIconButton tooltip="Copy">
          <AuiIf condition={(s) => s.message.isCopied}>
            <CheckIcon className="animate-in zoom-in-50 fade-in duration-200 ease-out" />
          </AuiIf>
          <AuiIf condition={(s) => !s.message.isCopied}>
            <CopyIcon className="animate-in zoom-in-75 fade-in duration-150" />
          </AuiIf>
        </TooltipIconButton>
      </ActionBarPrimitive.Copy>
      <ActionBarPrimitive.Reload asChild>
        <TooltipIconButton tooltip="Refresh">
          <RefreshCwIcon />
        </TooltipIconButton>
      </ActionBarPrimitive.Reload>
      <ActionBarMorePrimitive.Root>
        <ActionBarMorePrimitive.Trigger asChild>
          <TooltipIconButton
            tooltip="More"
            className="data-[state=open]:bg-accent"
          >
            <MoreHorizontalIcon />
          </TooltipIconButton>
        </ActionBarMorePrimitive.Trigger>
        <ActionBarMorePrimitive.Content
          side="bottom"
          align="start"
          sideOffset={6}
          className="aui-action-bar-more-content bg-popover/95 text-popover-foreground data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95 data-[state=open]:animate-in data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95 data-[state=closed]:animate-out data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2 z-50 min-w-[8rem] overflow-hidden rounded-xl border p-1.5 shadow-lg backdrop-blur-sm"
        >
          <ActionBarPrimitive.ExportMarkdown asChild>
            <ActionBarMorePrimitive.Item className="aui-action-bar-more-item hover:bg-accent hover:text-accent-foreground focus:bg-accent focus:text-accent-foreground flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-sm outline-none select-none">
              <DownloadIcon className="size-4" />
              Export as Markdown
            </ActionBarMorePrimitive.Item>
          </ActionBarPrimitive.ExportMarkdown>
        </ActionBarMorePrimitive.Content>
      </ActionBarMorePrimitive.Root>
    </ActionBarPrimitive.Root>
  );
};

const UserMessage: FC = () => {
  const meta = useAuiState((s) => (s.message.metadata as any)?.custom ?? {});
  const createdAt: number = meta.createdAt ?? Date.now();

  return (
    <MessagePrimitive.Root
      data-slot="aui_user-message-root"
      className="fade-in slide-in-from-bottom-1 animate-in grid auto-rows-auto grid-cols-[minmax(72px,1fr)_auto] content-start gap-y-1 px-2 duration-150 [contain-intrinsic-size:auto_60px] [content-visibility:auto] [&:where(>*)]:col-start-2"
      data-role="user"
    >
      <UserMessageAttachments />

      <div className="aui-user-message-content-wrapper relative col-start-2 min-w-0">
        <div className="aui-user-message-content peer bg-muted text-foreground rounded-xl px-4 py-2 wrap-break-word empty:hidden">
          <MessagePrimitive.Parts
            components={{
              Image: ImageWithLightbox,
            }}
          />
        </div>
        <div className="aui-user-action-bar-wrapper absolute start-0 top-1/2 -translate-x-full -translate-y-1/2 pe-2 peer-empty:hidden rtl:translate-x-full">
          <UserActionBar />
        </div>
      </div>

      <div className="col-start-2 text-right">
        <span className="text-[10px] text-muted-foreground/60 px-1">{formatTime(createdAt)}</span>
      </div>

      <BranchPicker
        data-slot="aui_user-branch-picker"
        className="col-span-full col-start-1 row-start-4 -me-1 justify-end"
      />
    </MessagePrimitive.Root>
  );
};

const UserActionBar: FC = () => {
  return (
    <ActionBarPrimitive.Root
      hideWhenRunning
      autohide="not-last"
      className="aui-user-action-bar-root flex flex-col items-end"
    >
      <ActionBarPrimitive.Copy asChild>
        <TooltipIconButton tooltip="Copy" className="aui-user-action-copy">
          <AuiIf condition={(s) => s.message.isCopied}>
            <CheckIcon className="animate-in zoom-in-50 fade-in duration-200 ease-out" />
          </AuiIf>
          <AuiIf condition={(s) => !s.message.isCopied}>
            <CopyIcon className="animate-in zoom-in-75 fade-in duration-150" />
          </AuiIf>
        </TooltipIconButton>
      </ActionBarPrimitive.Copy>
      <ActionBarPrimitive.Edit asChild>
        <TooltipIconButton tooltip="Edit" className="aui-user-action-edit">
          <PencilIcon />
        </TooltipIconButton>
      </ActionBarPrimitive.Edit>
    </ActionBarPrimitive.Root>
  );
};

const EditComposer: FC = () => {
  const composerRuntime = useComposerRuntime();
  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      composerRuntime.send();
    }
  };

  return (
    <MessagePrimitive.Root
      data-slot="aui_edit-composer-wrapper"
      className="flex flex-col px-2"
    >
      <ComposerPrimitive.Root className="aui-edit-composer-root border-border/60 dark:border-muted-foreground/15 ms-auto flex w-full max-w-[85%] flex-col rounded-(--composer-radius) border bg-(--composer-bg) shadow-[0_4px_16px_-8px_rgba(0,0,0,0.08),0_1px_2px_rgba(0,0,0,0.04)] dark:shadow-none">
        <ComposerPrimitive.Input
          className="aui-edit-composer-input text-foreground min-h-14 w-full resize-none bg-transparent px-4 pt-3 pb-1 text-base outline-none"
          autoFocus
          onKeyDown={handleKeyDown}
        />
        <div className="aui-edit-composer-footer mx-2.5 mb-2.5 flex items-center gap-1.5 self-end">
          <ComposerPrimitive.Cancel asChild>
            <Button
              variant="ghost"
              size="sm"
              className="h-8 rounded-full px-3.5"
            >
              Cancel
            </Button>
          </ComposerPrimitive.Cancel>
          <ComposerPrimitive.Send asChild>
            <Button size="sm" className="h-8 rounded-full px-3.5">
              Update
            </Button>
          </ComposerPrimitive.Send>
        </div>
      </ComposerPrimitive.Root>
    </MessagePrimitive.Root>
  );
};

const BranchPicker: FC<BranchPickerPrimitive.Root.Props> = ({
  className,
  ...rest
}) => {
  return (
    <BranchPickerPrimitive.Root
      hideWhenSingleBranch
      className={cn(
        "aui-branch-picker-root text-muted-foreground -ms-2 me-2 inline-flex items-center text-xs",
        className,
      )}
      {...rest}
    >
      <BranchPickerPrimitive.Previous asChild>
        <TooltipIconButton tooltip="Previous">
          <ChevronLeftIcon />
        </TooltipIconButton>
      </BranchPickerPrimitive.Previous>
      <span className="aui-branch-picker-state font-medium">
        <BranchPickerPrimitive.Number /> / <BranchPickerPrimitive.Count />
      </span>
      <BranchPickerPrimitive.Next asChild>
        <TooltipIconButton tooltip="Next">
          <ChevronRightIcon />
        </TooltipIconButton>
      </BranchPickerPrimitive.Next>
    </BranchPickerPrimitive.Root>
  );
};

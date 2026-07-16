"use client";

import { memo, type FC, type PropsWithChildren } from "react";
import { BrainIcon, ChevronDownIcon } from "lucide-react";
import {
  useAuiState,
  type ReasoningMessagePartProps,
} from "@assistant-ui/react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import { useState } from "react";

type ReasoningRootProps = PropsWithChildren<{
  streaming?: boolean;
  className?: string;
  defaultOpen?: boolean;
}>;

export const ReasoningRoot: FC<ReasoningRootProps> = ({
  streaming = false,
  className,
  defaultOpen,
  children,
}) => {
  const [manualOpen, setManualOpen] = useState<boolean | null>(null);
  const open = manualOpen ?? (streaming ? true : Boolean(defaultOpen));

  return (
    <Collapsible
      open={open}
      onOpenChange={(next) => setManualOpen(next)}
      className={cn("mb-2", className)}
    >
      {children}
    </Collapsible>
  );
};

export const ReasoningTrigger: FC<{ active?: boolean; className?: string }> = ({
  active = false,
  className,
}) => (
  <CollapsibleTrigger asChild>
    <button
      type="button"
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border border-border/50 bg-muted/60 px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground",
        active && "border-amber-500/40 text-amber-700 dark:text-amber-300",
        className,
      )}
    >
      <BrainIcon className={cn("size-3", active && "animate-pulse")} />
      <span>{active ? "Thinking…" : "Thoughts"}</span>
      <ChevronDownIcon className="size-3 [[data-state=open]_&]:rotate-180 transition-transform" />
    </button>
  </CollapsibleTrigger>
);

export const ReasoningContent: FC<PropsWithChildren<{ className?: string; "aria-busy"?: boolean }>> = ({
  children,
  className,
  ...props
}) => (
  <CollapsibleContent
    className={cn(
      "mt-1.5 rounded-lg border border-border/40 bg-muted/30 p-3 text-xs text-muted-foreground",
      className,
    )}
    {...props}
  >
    {children}
  </CollapsibleContent>
);

export const ReasoningText: FC<PropsWithChildren<{ className?: string }>> = ({
  children,
  className,
}) => (
  <div className={cn("whitespace-pre-wrap break-words leading-relaxed space-y-2", className)}>
    {children}
  </div>
);

const ReasoningImpl: FC<ReasoningMessagePartProps> = ({ text }) => {
  const trimmed = (text ?? "").trim();
  if (!trimmed) return null;
  return <div className="whitespace-pre-wrap break-words">{trimmed}</div>;
};

export const Reasoning = memo(ReasoningImpl);
Reasoning.displayName = "Reasoning";

/** True when the current message is still producing reasoning tokens. */
export function useReasoningStreaming(): boolean {
  return useAuiState((s) => {
    if (s.message.status?.type !== "running") return false;
    const parts = s.message.content;
    if (!parts.length) return false;
    return parts[parts.length - 1]?.type === "reasoning";
  });
}

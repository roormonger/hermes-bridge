# Hermes Chat - Assistant Message Component

... [existing content] ...

## Reload Functionality

Added `ActionBarPrimitive.Reload` to handle message re-submission:

```tsx
// In assistant message footer
<ActionBarPrimitive.Reload
  hideWhenRunning
  onReload={() => {
    // Resubmit last user message
    const lastUserMessage = useAuiState(() => s.thread.messages.find(m => m.role === "user"));
    if (lastUserMessage) {
      composerRuntime.setText(lastUserMessage.content);
      composerRuntime.send();
    }
  }},
  className="aui-assistant-action-bar-reload"
/>
```
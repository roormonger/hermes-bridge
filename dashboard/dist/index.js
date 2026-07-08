(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const { React, hooks, components } = SDK;
  const { useState, useEffect, useCallback } = hooks;
  const { Card, CardHeader, CardTitle, CardContent, CardDescription, Button, Badge, Separator, Tabs, TabsList, TabsTrigger, TabsContent } = components;
  const h = React.createElement;

  const API_PREFIX = "/api/plugins/hermes-bridge";

  function fetchJSON(path, opts) {
    return SDK.fetchJSON(API_PREFIX + path, opts);
  }

  function cn(...classes) {
    return classes.filter(Boolean).join(" ");
  }

  function StatusBadge({ running, healthy }) {
    if (!running) {
      return h(Badge, { variant: "secondary" }, "Stopped");
    }
    return h(Badge, { variant: healthy ? "default" : "destructive" }, healthy ? "Running" : "Unhealthy");
  }

  function BridgeDashboard() {
    const [status, setStatus] = useState(null);
    const [logs, setLogs] = useState("");
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(null);
    const [activeTab, setActiveTab] = useState("status");

    const loadStatus = useCallback(async () => {
      try {
        const data = await fetchJSON("/status");
        setStatus(data);
        setError(null);
      } catch (err) {
        setError(String(err));
      }
    }, []);

    const loadLogs = useCallback(async () => {
      try {
        const data = await fetchJSON("/logs?tail=100");
        setLogs(data.log);
      } catch (err) {
        setLogs("Failed to load logs: " + err);
      }
    }, []);

    useEffect(() => {
      loadStatus();
      const id = setInterval(loadStatus, 5000);
      return () => clearInterval(id);
    }, [loadStatus]);

    useEffect(() => {
      if (activeTab === "logs") {
        loadLogs();
      }
    }, [activeTab, loadLogs]);

    async function act(promise, refresh) {
      setLoading(true);
      try {
        await promise;
        if (refresh) await refresh();
      } finally {
        setLoading(false);
      }
    }

    const onStart = () => act(fetchJSON("/start", { method: "POST" }), loadStatus);
    const onStop = () => act(fetchJSON("/stop", { method: "POST" }), loadStatus);
    const onRestart = () => act(fetchJSON("/restart", { method: "POST" }), loadStatus);
    const onInstallDeps = () => act(fetchJSON("/install-deps", { method: "POST" }), loadStatus);
    const onRefreshLogs = () => act(loadLogs(), null);

    return h("div", { className: "space-y-4 p-4" },
      h("div", { className: "flex items-center justify-between" },
        h("div", null,
          h("h2", { className: "text-2xl font-bold tracking-tight" }, "Hermes Bridge"),
          h("p", { className: "text-muted-foreground" }, "Control the Open WebUI bridge daemon.")
        ),
        h(StatusBadge, {
          running: status?.running || false,
          healthy: status?.healthy || false
        })
      ),
      error && h("div", { className: "rounded-md bg-destructive/15 p-3 text-destructive text-sm" }, error),
      h(Tabs, { value: activeTab, onValueChange: setActiveTab, className: "w-full" },
        h(TabsList, { className: "grid w-full grid-cols-2 max-w-md" },
          h(TabsTrigger, { value: "status" }, "Status & Controls"),
          h(TabsTrigger, { value: "logs" }, "Daemon Logs")
        ),
        h(TabsContent, { value: "status", className: "space-y-4" },
          h(Card, null,
            h(CardHeader, null,
              h(CardTitle, null, "Daemon Status"),
              h(CardDescription, null, "Current bridge process state and configuration.")
            ),
            h(CardContent, { className: "space-y-4" },
              h("div", { className: "grid grid-cols-2 gap-4 text-sm" },
                h("div", null,
                  h("div", { className: "text-muted-foreground" }, "Running"),
                  h("div", { className: "font-medium" }, status ? (status.running ? "Yes" : "No") : "—")
                ),
                h("div", null,
                  h("div", { className: "text-muted-foreground" }, "Healthy"),
                  h("div", { className: "font-medium" }, status ? (status.healthy ? "Yes" : "No") : "—")
                ),
                h("div", null,
                  h("div", { className: "text-muted-foreground" }, "PID"),
                  h("div", { className: "font-medium" }, status?.pid ?? "—")
                ),
                h("div", null,
                  h("div", { className: "text-muted-foreground" }, "Port"),
                  h("div", { className: "font-medium" }, status?.config?.port ?? "—")
                )
              ),
              h(Separator),
              h("div", null,
                h("div", { className: "text-muted-foreground text-sm mb-2" }, "Config"),
                h("pre", { className: "rounded-md bg-muted p-3 text-xs overflow-auto" },
                  status ? JSON.stringify(status.config, null, 2) : "Loading..."
                )
              )
            )
          ),
          h(Card, null,
            h(CardHeader, null,
              h(CardTitle, null, "Controls"),
              h(CardDescription, null, "Start, stop, or restart the bridge daemon.")
            ),
            h(CardContent, null,
              h("div", { className: "flex flex-wrap gap-2" },
                h(Button, { onClick: onStart, disabled: loading || status?.running }, "Start"),
                h(Button, { onClick: onStop, disabled: loading || !status?.running, variant: "destructive" }, "Stop"),
                h(Button, { onClick: onRestart, disabled: loading, variant: "outline" }, "Restart"),
                h(Button, { onClick: onInstallDeps, disabled: loading, variant: "secondary" }, "Install Dependencies")
              )
            )
          )
        ),
        h(TabsContent, { value: "logs" },
          h(Card, null,
            h(CardHeader, null,
              h(CardTitle, null, "Daemon Logs"),
              h(CardDescription, null, "Last 100 lines from the bridge daemon log.")
            ),
            h(CardContent, { className: "space-y-2" },
              h("div", { className: "flex justify-end" },
                h(Button, { onClick: onRefreshLogs, disabled: loading, variant: "outline", size: "sm" }, "Refresh")
              ),
              h("pre", { className: "rounded-md bg-muted p-3 text-xs h-96 overflow-auto font-mono" },
                logs || "No logs yet."
              )
            )
          )
        )
      )
    );
  }

  window.__HERMES_PLUGINS__.register("hermes-bridge", BridgeDashboard);
})();

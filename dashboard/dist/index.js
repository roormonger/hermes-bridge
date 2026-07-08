(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__ || {};
  const React = SDK.React;
  const hooks = SDK.hooks || {};
  const components = SDK.components || {};
  const { useState, useEffect, useCallback } = hooks;
  const { Card, CardHeader, CardTitle, CardContent, Button } = components;
  const h = React ? React.createElement : function () { return null; };

  if (!React || !Card || !Button) {
    window.__HERMES_PLUGINS__.register("hermes-bridge", function () {
      return h("div", { className: "p-4" }, "Hermes Bridge dashboard could not load: SDK components are missing.");
    });
    return;
  }

  const API_PREFIX = "/api/plugins/hermes-bridge";

  async function fetchJSON(path, opts) {
    const url = API_PREFIX + path;
    if (typeof SDK.fetchJSON === "function") {
      return SDK.fetchJSON(url, opts);
    }
    const res = await fetch(url, {
      ...opts,
      headers: { "Content-Type": "application/json", ...(opts && opts.headers) },
      credentials: "same-origin",
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    return res.json();
  }

  function StatusPill({ running, healthy }) {
    const color = !running ? "bg-muted text-muted-foreground" : healthy ? "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-100" : "bg-destructive/15 text-destructive";
    const text = !running ? "Stopped" : healthy ? "Running" : "Unhealthy";
    return h("span", { className: "inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium " + color }, text);
  }

  function BridgeDashboard() {
    const [status, setStatus] = useState(null);
    const [logs, setLogs] = useState("");
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(null);

    const loadStatus = useCallback(async () => {
      try {
        const data = await fetchJSON("/status");
        setStatus(data);
        setError(null);
      } catch (err) {
        setError("Status failed: " + String(err));
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
      loadLogs();
      const id = setInterval(() => { loadStatus(); loadLogs(); }, 5000);
      return () => clearInterval(id);
    }, [loadStatus, loadLogs]);

    async function act(promise, refresh) {
      setLoading(true);
      try {
        const result = await promise;
        if (refresh) await refresh();
        return result;
      } catch (err) {
        setError("Action failed: " + String(err));
      } finally {
        setLoading(false);
      }
    }

    const onStart = () => act(fetchJSON("/start", { method: "POST" }), loadStatus);
    const onStop = () => act(fetchJSON("/stop", { method: "POST" }), loadStatus);
    const onRestart = () => act(fetchJSON("/restart", { method: "POST" }), loadStatus);
    const onInstallDeps = () => act(fetchJSON("/install-deps", { method: "POST" }), loadStatus);
    const onRefreshLogs = () => act(loadLogs(), null);

    return h("div", { className: "space-y-4 p-4 max-w-5xl mx-auto" },
      h("div", { className: "flex items-center justify-between" },
        h("div", null,
          h("h2", { className: "text-2xl font-bold tracking-tight" }, "Hermes Bridge"),
          h("p", { className: "text-muted-foreground" }, "Control the Open WebUI bridge daemon.")
        ),
        h(StatusPill, { running: status?.running || false, healthy: status?.healthy || false })
      ),
      error && h("div", { className: "rounded-md bg-destructive/15 p-3 text-destructive text-sm" }, error),
      h(Card, null,
        h(CardHeader, null,
          h(CardTitle, null, "Daemon Status"),
          h("p", { className: "text-sm text-muted-foreground" }, "Current bridge process state and configuration.")
        ),
        h(CardContent, { className: "space-y-4" },
          h("div", { className: "grid grid-cols-2 md:grid-cols-4 gap-4 text-sm" },
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
          h("p", { className: "text-sm text-muted-foreground" }, "Start, stop, or restart the bridge daemon.")
        ),
        h(CardContent, null,
          h("div", { className: "flex flex-wrap gap-2" },
            h(Button, { onClick: onStart, disabled: loading || status?.running }, "Start"),
            h(Button, { onClick: onStop, disabled: loading || !status?.running }, "Stop"),
            h(Button, { onClick: onRestart, disabled: loading }, "Restart"),
            h(Button, { onClick: onInstallDeps, disabled: loading }, "Install Dependencies")
          )
        )
      ),
      h(Card, null,
        h(CardHeader, null,
          h(CardTitle, null, "Daemon Logs"),
          h("div", { className: "flex items-center justify-between" },
            h("p", { className: "text-sm text-muted-foreground" }, "Last 100 lines from the bridge daemon log."),
            h(Button, { onClick: onRefreshLogs, disabled: loading }, "Refresh")
          )
        ),
        h(CardContent, null,
          h("pre", { className: "rounded-md bg-muted p-3 text-xs h-96 overflow-auto font-mono" },
            logs || "No logs yet."
          )
        )
      )
    );
  }

  window.__HERMES_PLUGINS__.register("hermes-bridge", BridgeDashboard);
})();

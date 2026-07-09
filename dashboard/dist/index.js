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

  const { useRef } = hooks;

  function BridgeDashboard() {
    const [status, setStatus] = useState(null);
    const [logs, setLogs] = useState("");
    const [logsPaused, setLogsPaused] = useState(false);
    const [users, setUsers] = useState([]);
    const [newUsername, setNewUsername] = useState("");
    const [newPassword, setNewPassword] = useState("");
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(null);
    const logsPausedRef = useRef(false);
    const logsBoxRef = useRef(null);

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
        if (!logsPausedRef.current) {
          setLogs(data.log);
        }
      } catch (err) {
        if (!logsPausedRef.current) setLogs("Failed to load logs: " + err);
      }
    }, []);

    const loadUsers = useCallback(async () => {
      try {
        const data = await fetchJSON("/users");
        setUsers(data.users || []);
      } catch (err) {
        setError("Users failed: " + String(err));
      }
    }, []);

    useEffect(() => {
      loadStatus();
      loadLogs();
      loadUsers();
      const id = setInterval(() => { loadStatus(); loadLogs(); }, 5000);
      return () => clearInterval(id);
    }, [loadStatus, loadLogs, loadUsers]);

    useEffect(() => {
      if (!logsPaused && logsBoxRef.current) {
        logsBoxRef.current.scrollTop = logsBoxRef.current.scrollHeight;
      }
    }, [logs, logsPaused]);

    const toggleLogsPause = () => {
      const next = !logsPausedRef.current;
      logsPausedRef.current = next;
      setLogsPaused(next);
      if (!next) loadLogs();
    };

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

    const onCreateUser = async (e) => {
      e.preventDefault();
      await act(
        fetchJSON("/users", {
          method: "POST",
          body: JSON.stringify({ username: newUsername, password: newPassword }),
        }),
        loadUsers
      );
      setNewUsername("");
      setNewPassword("");
    };

    const onDeleteUser = (userId) =>
      act(fetchJSON("/users/" + userId, { method: "DELETE" }), loadUsers);

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
          h(CardTitle, null, "Chat Users"),
          h("p", { className: "text-sm text-muted-foreground" }, "Add or remove users for the standalone chat UI.")
        ),
        h(CardContent, { className: "space-y-4" },
          h("form", { onSubmit: onCreateUser, className: "flex flex-col sm:flex-row gap-2" },
            h("input", {
              type: "text",
              placeholder: "Username",
              value: newUsername,
              onChange: (e) => setNewUsername(e.target.value),
              required: true,
              className: "flex-1 rounded-md border border-input bg-background px-3 py-2 text-sm"
            }),
            h("input", {
              type: "password",
              placeholder: "Password",
              value: newPassword,
              onChange: (e) => setNewPassword(e.target.value),
              required: true,
              className: "flex-1 rounded-md border border-input bg-background px-3 py-2 text-sm"
            }),
            h(Button, { type: "submit", disabled: loading }, "Add User")
          ),
          h("div", { className: "space-y-2" },
            users.length === 0 && h("p", { className: "text-sm text-muted-foreground" }, "No users yet."),
            users.map((u) => h("div", {
              key: u.user_id,
              className: "flex items-center justify-between rounded-md border px-3 py-2 text-sm"
            },
              h("span", null, u.username),
              h(Button, {
                variant: "destructive",
                size: "sm",
                onClick: () => onDeleteUser(u.user_id),
                disabled: loading
              }, "Delete")
            ))
          )
        )
      ),
      h(Card, null,
        h(CardHeader, null,
          h("div", { className: "flex items-center justify-between" },
            h(CardTitle, null, "Daemon Logs"),
            h("div", { className: "flex gap-2" },
              h(Button, {
                onClick: toggleLogsPause,
                variant: logsPaused ? "default" : "outline",
                size: "sm"
              }, logsPaused ? "▶ Resume" : "⏸ Pause"),
              h(Button, { onClick: onRefreshLogs, disabled: loading, size: "sm", variant: "outline" }, "Refresh")
            )
          ),
          h("p", { className: "text-sm text-muted-foreground" },
            logsPaused
              ? "⏸ Log updates paused — scroll freely or copy."
              : "Last 100 lines from the bridge daemon log. Auto-scrolls to bottom."
          )
        ),
        h(CardContent, null,
          h("pre", {
            ref: logsBoxRef,
            className: "rounded-md bg-muted p-3 text-xs h-96 overflow-auto font-mono whitespace-pre-wrap break-all"
          },
            logs || "No logs yet."
          )
        )
      )
    );
  }

  window.__HERMES_PLUGINS__.register("hermes-bridge", BridgeDashboard);
})();

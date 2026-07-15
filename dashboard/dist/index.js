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
    window.__HERMES_PLUGINS__.register("hermes-chat", function () {
      return h("div", { className: "p-4" }, "Hermes Chat dashboard could not load: SDK components are missing.");
    });
    return;
  }

  // Derive the correct API prefix. Try in order:
  //  1. SDK.apiPrefix  — Hermes may inject this directly
  //  2. SDK.pluginName — build prefix from the registered plugin name
  //  3. URL of a <script> tag whose src contains /plugins/<name>/
  //  4. Hard-coded fallback (correct after plugin is renamed)
  function _resolveApiPrefix() {
    if (SDK.apiPrefix) return SDK.apiPrefix;
    if (SDK.pluginName) return "/api/plugins/" + SDK.pluginName;
    try {
      const scripts = Array.from(document.querySelectorAll("script[src]"));
      for (const s of scripts) {
        const m = s.src.match(/\/plugins\/([^/]+)\//);
        if (m) return "/api/plugins/" + m[1];
      }
    } catch (_) {}
    return "/api/plugins/hermes-chat";
  }
  const API_PREFIX = _resolveApiPrefix();

  async function fetchJSON(path, opts) {
    const url = API_PREFIX + path;
    const res = await fetch(url, {
      ...opts,
      headers: { "Content-Type": "application/json", ...(opts && opts.headers) },
      credentials: "same-origin",
    });
    if (!res.ok) {
      let detail = "HTTP " + res.status;
      try { const j = await res.json(); detail = j.detail || detail; } catch (_) {}
      throw new Error(detail);
    }
    return res.json();
  }

  function StatusPill({ running, healthy }) {
    const color = !running ? "bg-muted text-muted-foreground" : healthy ? "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-100" : "bg-destructive/15 text-destructive";
    const text = !running ? "Stopped" : healthy ? "Running" : "Unhealthy";
    return h("span", { className: "inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium " + color }, text);
  }

  const { useRef } = hooks;

  function HermesChatDashboard() {
    const [status, setStatus] = useState(null);
    const [logs, setLogs] = useState("");
    const [logsPaused, setLogsPaused] = useState(false);
    const [users, setUsers] = useState([]);
    const [newUsername, setNewUsername] = useState("");
    const [newPassword, setNewPassword] = useState("");
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(null);
    const [notice, setNotice] = useState(null);
    const [missingDeps, setMissingDeps] = useState([]);
    const [missingOptionalDeps, setMissingOptionalDeps] = useState([]);
    const [voiceEnabled, setVoiceEnabled] = useState(true);
    const [defaultTtsVoice, setDefaultTtsVoice] = useState("en-US-AriaNeural");
    const [voiceDepsAvailable, setVoiceDepsAvailable] = useState({ tts: true, stt: true });
    const [settingsHost, setSettingsHost] = useState("");
    const [settingsPort, setSettingsPort] = useState("");
    const [pendingRestart, setPendingRestart] = useState(false);
    const [dashboardUrl, setDashboardUrl] = useState("");
    const [dashboardUrlSource, setDashboardUrlSource] = useState("");
    const [dashboardUrlVerify, setDashboardUrlVerify] = useState(null);
    const [dashboardUrlLoading, setDashboardUrlLoading] = useState(false);
    const logsPausedRef = useRef(false);
    const logsBoxRef = useRef(null);

    const loadStatus = useCallback(async () => {
      try {
        const data = await fetchJSON("/status");
        setStatus(data);
        setError(null);
        if (data.config) {
          setSettingsHost(prev => prev === "" ? (data.config.host || "") : prev);
          setSettingsPort(prev => prev === "" ? String(data.config.port || "") : prev);
        }
      } catch (err) {
        setError("Status failed: " + String(err));
      }
    }, []);

    const loadDeps = useCallback(async () => {
      try {
        const data = await fetchJSON("/deps");
        setMissingDeps(data.missing || []);
        setMissingOptionalDeps(data.missing_optional || []);
      } catch (err) {
        setError("Deps check failed: " + String(err));
      }
    }, []);

    const loadVoiceConfig = useCallback(async () => {
      try {
        const data = await fetchJSON("/voice-config");
        setVoiceEnabled(data.voice_enabled);
        setDefaultTtsVoice(data.default_tts_voice);
        setVoiceDepsAvailable({ tts: data.tts_available, stt: data.stt_available });
      } catch (_) {}
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

    const loadDashboardUrl = useCallback(async () => {
      try {
        const data = await fetchJSON("/dashboard-url");
        setDashboardUrl(data.url || "");
        setDashboardUrlSource(data.source || "");
        setDashboardUrlVerify(data.verify || null);
      } catch (err) {
        setDashboardUrlVerify({ ok: false, error: String(err) });
      }
    }, []);

    useEffect(() => {
      loadStatus();
      loadDeps();
      loadLogs();
      loadUsers();
      loadDashboardUrl();
      loadVoiceConfig();
      const id = setInterval(() => { loadStatus(); loadLogs(); }, 5000);
      return () => clearInterval(id);
    }, [loadStatus, loadDeps, loadLogs, loadUsers, loadDashboardUrl, loadVoiceConfig]);

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
    const onInstallDeps = async () => {
      setLoading(true);
      setError(null);
      setNotice(null);
      try {
        const result = await fetchJSON("/install-deps", { method: "POST" });
        if (result.status === "error") {
          setError("Install failed: " + (result.message || "unknown error"));
        } else {
          const installed = result.installed && result.installed.length > 0
            ? "Installed: " + result.installed.join(", ") + "."
            : "All dependencies are already present.";
          setNotice(installed);
          await loadDeps();
          await loadStatus();
        }
      } catch (err) {
        setError("Install deps failed: " + String(err));
      } finally {
        setLoading(false);
      }
    };
    const onRefreshLogs = () => act(loadLogs(), null);

    const TTS_VOICES = [
      { label: "Aria (US Female)", value: "en-US-AriaNeural" },
      { label: "Jenny (US Female)", value: "en-US-JennyNeural" },
      { label: "Guy (US Male)", value: "en-US-GuyNeural" },
      { label: "Eric (US Male)", value: "en-US-EricNeural" },
      { label: "Sonia (UK Female)", value: "en-GB-SoniaNeural" },
      { label: "Ryan (UK Male)", value: "en-GB-RyanNeural" },
      { label: "Natasha (AU Female)", value: "en-AU-NatashaNeural" },
      { label: "William (AU Male)", value: "en-AU-WilliamNeural" },
    ];

    const onSaveVoiceSettings = async () => {
      setLoading(true);
      setError(null);
      setNotice(null);
      try {
        await fetchJSON("/config", {
          method: "POST",
          body: JSON.stringify({ voice_enabled: voiceEnabled, default_tts_voice: defaultTtsVoice }),
        });
        setNotice("Voice settings saved.");
        await loadVoiceConfig();
      } catch (err) {
        setError("Save voice settings failed: " + String(err));
      } finally {
        setLoading(false);
      }
    };

    const onSaveSettings = async (e) => {
      e.preventDefault();
      setLoading(true);
      setError(null);
      setNotice(null);
      try {
        const port = parseInt(settingsPort, 10);
        if (isNaN(port) || port < 1 || port > 65535) {
          setError("Port must be a number between 1 and 65535.");
          return;
        }
        await fetchJSON("/config", {
          method: "POST",
          body: JSON.stringify({ host: settingsHost.trim(), port }),
        });
        setPendingRestart(true);
        setNotice("Settings saved. Restart the daemon for changes to take effect.");
        await loadStatus();
      } catch (err) {
        setError("Save settings failed: " + String(err));
      } finally {
        setLoading(false);
      }
    };

    const onRestartAfterSettings = async () => {
      await act(fetchJSON("/restart", { method: "POST" }), loadStatus);
      setPendingRestart(false);
      setNotice("Daemon restarted with new settings.");
    };

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

    const onVerifyDashboardUrl = async () => {
      setDashboardUrlLoading(true);
      setError(null);
      try {
        const data = await fetchJSON("/dashboard-url/verify", {
          method: "POST",
          body: JSON.stringify({ url: dashboardUrl.trim() }),
        });
        setDashboardUrlVerify(data);
      } catch (err) {
        setDashboardUrlVerify({ ok: false, error: String(err) });
      } finally {
        setDashboardUrlLoading(false);
      }
    };

    const onSaveDashboardUrl = async (e) => {
      e.preventDefault();
      setLoading(true);
      setError(null);
      setNotice(null);
      try {
        await fetchJSON("/config", {
          method: "POST",
          body: JSON.stringify({ hermes_dashboard_url: dashboardUrl.trim() }),
        });
        setNotice("Dashboard URL saved. Restart the daemon for changes to take effect.");
        await loadDashboardUrl();
      } catch (err) {
        setError("Save dashboard URL failed: " + String(err));
      } finally {
        setLoading(false);
      }
    };

    const onResetDashboardUrl = async () => {
      setLoading(true);
      setError(null);
      setNotice(null);
      try {
        await fetchJSON("/config", {
          method: "POST",
          body: JSON.stringify({ hermes_dashboard_url: "" }),
        });
        setNotice("Dashboard URL override cleared. Auto-discovery will be used.");
        await loadDashboardUrl();
      } catch (err) {
        setError("Reset dashboard URL failed: " + String(err));
      } finally {
        setLoading(false);
      }
    };

    const onDeleteUser = (userId) =>
      act(fetchJSON("/users/" + userId, { method: "DELETE" }), loadUsers);

    return h("div", { className: "space-y-4 p-4 max-w-5xl mx-auto" },
      h("div", { className: "flex items-center justify-between" },
        h("div", null,
          h("h2", { className: "text-2xl font-bold tracking-tight" }, "Hermes Chat"),
          h("p", { className: "text-muted-foreground" }, "Control the Hermes Chat daemon.")
        ),
        h(StatusPill, { running: status?.running || false, healthy: status?.healthy || false })
      ),
      missingDeps.length > 0 && h("div", { className: "rounded-md border border-yellow-400 bg-yellow-50 dark:bg-yellow-950 dark:border-yellow-700 p-3 text-sm flex items-start justify-between gap-3" },
        h("div", null,
          h("span", { className: "font-semibold text-yellow-800 dark:text-yellow-200" }, "Missing dependencies: "),
          h("span", { className: "text-yellow-700 dark:text-yellow-300" }, missingDeps.join(", ")),
          h("p", { className: "text-yellow-600 dark:text-yellow-400 text-xs mt-1" }, "The daemon cannot start until these are installed.")
        ),
        h(Button, { onClick: onInstallDeps, disabled: loading, size: "sm", variant: "outline" }, loading ? "Installing..." : "Install Now")
      ),
      missingOptionalDeps.length > 0 && missingDeps.length === 0 && h("div", { className: "rounded-md border border-blue-400 bg-blue-50 dark:bg-blue-950 dark:border-blue-700 p-3 text-sm flex items-start justify-between gap-3" },
        h("div", null,
          h("span", { className: "font-semibold text-blue-800 dark:text-blue-200" }, "Voice support not installed: "),
          h("span", { className: "text-blue-700 dark:text-blue-300" }, missingOptionalDeps.map(d => d.requirement).join(", ")),
          h("p", { className: "text-blue-600 dark:text-blue-400 text-xs mt-1" }, missingOptionalDeps.map(d => d.feature).join(", ") + ". Install to enable voice input and output.")
        ),
        h(Button, { onClick: onInstallDeps, disabled: loading, size: "sm", variant: "outline" }, loading ? "Installing..." : "Install Voice")
      ),
      notice && h("div", { className: "rounded-md border border-green-400 bg-green-50 dark:bg-green-950 dark:border-green-700 p-3 text-sm flex items-center justify-between gap-3" },
        h("span", { className: "text-green-800 dark:text-green-200" }, "✓ " + notice),
        h("button", { onClick: () => setNotice(null), className: "text-green-600 dark:text-green-400 hover:opacity-70 text-xs" }, "✕")
      ),
      pendingRestart && h("div", { className: "rounded-md border border-orange-400 bg-orange-50 dark:bg-orange-950 dark:border-orange-700 p-3 text-sm flex items-center justify-between gap-3" },
        h("span", { className: "text-orange-800 dark:text-orange-200" }, "⚠ Restart required for network settings to take effect."),
        h(Button, { onClick: onRestartAfterSettings, disabled: loading, size: "sm" }, loading ? "Restarting..." : "Restart Now")
      ),
      error && h("div", { className: "rounded-md bg-destructive/15 p-3 text-destructive text-sm" }, error),
      h(Card, null,
        h(CardHeader, { className: "flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between" },
          h("div", null,
            h(CardTitle, null, "Daemon Status"),
            h("p", { className: "text-sm text-muted-foreground" }, "Current Hermes Chat process state and configuration.")
          ),
          h("div", { className: "flex flex-wrap gap-2" },
            h(Button, { onClick: onStart, disabled: loading || status?.running, size: "sm" }, "Start"),
            h(Button, { onClick: onStop, disabled: loading || !status?.running, size: "sm" }, "Stop"),
            h(Button, { onClick: onRestart, disabled: loading, size: "sm", variant: "outline" }, "Restart"),
            h(Button, { onClick: onInstallDeps, disabled: loading, size: "sm", variant: "outline" }, "Install Dependencies")
          )
        ),
        h(CardContent, null,
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
            ),
            h("div", null,
              h("div", { className: "text-muted-foreground" }, "Bind Address"),
              h("div", { className: "font-medium" }, status?.config?.host ?? "—")
            ),
            h("div", null,
              h("div", { className: "text-muted-foreground" }, "Log Level"),
              h("div", { className: "font-medium" }, status?.config?.log_level ?? "—")
            ),
            h("div", null,
              h("div", { className: "text-muted-foreground" }, "Auto Start"),
              h("div", { className: "font-medium" }, status?.config?.auto_start ? "Yes" : "No")
            ),
            h("div", null,
              h("div", { className: "text-muted-foreground" }, "Debug"),
              h("div", { className: "font-medium" }, status?.config?.debug ? "Yes" : "No")
            )
          )
        )
      ),
      h(Card, null,
        h(CardHeader, null,
          h(CardTitle, null, "Network Settings"),
          h("p", { className: "text-sm text-muted-foreground" }, "Bind address and port. Changes take effect after a daemon restart.")
        ),
        h(CardContent, null,
          h("form", { onSubmit: onSaveSettings, className: "flex flex-col sm:flex-row gap-3 items-end" },
            h("div", { className: "flex flex-col gap-1 flex-1" },
              h("label", { className: "text-xs text-muted-foreground font-medium" }, "Bind Address"),
              h("input", {
                type: "text",
                value: settingsHost,
                onChange: (e) => setSettingsHost(e.target.value),
                placeholder: "0.0.0.0",
                className: "rounded-md border border-input bg-background px-3 py-2 text-sm"
              })
            ),
            h("div", { className: "flex flex-col gap-1 w-32" },
              h("label", { className: "text-xs text-muted-foreground font-medium" }, "Port"),
              h("input", {
                type: "number",
                value: settingsPort,
                onChange: (e) => setSettingsPort(e.target.value),
                placeholder: "6969",
                min: "1",
                max: "65535",
                className: "rounded-md border border-input bg-background px-3 py-2 text-sm"
              })
            ),
            h(Button, { type: "submit", disabled: loading }, "Save")
          )
        )
      ),
      h(Card, null,
        h(CardHeader, null,
          h("div", { className: "flex items-center justify-between" },
            h(CardTitle, null, "Hermes Dashboard URL"),
            dashboardUrlSource && h("span", { className: "text-xs text-muted-foreground" }, "Source: " + dashboardUrlSource)
          ),
          h("p", { className: "text-sm text-muted-foreground" }, "Used to show your recently-used Hermes models in the chat model picker. Leave empty to auto-detect.")
        ),
        h(CardContent, { className: "space-y-3" },
          h("form", { onSubmit: onSaveDashboardUrl, className: "flex flex-col gap-3" },
            h("input", {
              type: "text",
              value: dashboardUrl,
              onChange: (e) => setDashboardUrl(e.target.value),
              placeholder: "http://127.0.0.1:9119",
              className: "rounded-md border border-input bg-background px-3 py-2 text-sm"
            }),
            h("div", { className: "flex flex-wrap gap-2" },
              h(Button, { type: "submit", disabled: loading, size: "sm" }, "Save Override"),
              h(Button, { type: "button", variant: "outline", size: "sm", onClick: onVerifyDashboardUrl, disabled: dashboardUrlLoading || !dashboardUrl.trim() }, dashboardUrlLoading ? "Verifying..." : "Verify"),
              h(Button, { type: "button", variant: "ghost", size: "sm", onClick: onResetDashboardUrl, disabled: loading }, "Use Auto-Detect")
            )
          ),
          dashboardUrlVerify && (
            dashboardUrlVerify.ok
              ? h("div", { className: "rounded-md border border-green-400 bg-green-50 dark:bg-green-950 dark:border-green-700 p-3 text-sm" }, "✓ Reached dashboard (" + (dashboardUrlVerify.model_count ?? "?") + " models found).")
              : h("div", { className: "rounded-md border border-yellow-400 bg-yellow-50 dark:bg-yellow-950 dark:border-yellow-700 p-3 text-sm" },
                  h("div", { className: "font-semibold text-yellow-800 dark:text-yellow-200" }, "⚠ Could not verify dashboard URL"),
                  h("p", { className: "text-yellow-700 dark:text-yellow-300 text-xs mt-1" }, dashboardUrlVerify.error || "Unknown error"),
                  h("p", { className: "text-yellow-600 dark:text-yellow-400 text-xs mt-1" }, "The chat model picker will fall back to the full model catalog until this is fixed.")
                )
          ),
          !dashboardUrlVerify && !dashboardUrl && h("div", { className: "rounded-md border border-orange-400 bg-orange-50 dark:bg-orange-950 dark:border-orange-700 p-3 text-sm" },
            h("span", { className: "text-orange-800 dark:text-orange-200" }, "⚠ No dashboard URL detected. Profile-style model switching in chat will not work until the Hermes dashboard URL is set or auto-detected.")
          )
        )
      ),
      h(Card, null,
        h(CardHeader, null,
          h(CardTitle, null, "Voice Settings"),
          h("p", { className: "text-sm text-muted-foreground" }, "Enable or disable voice features and set the default TTS voice for all users.")
        ),
        h(CardContent, { className: "space-y-4" },
          !voiceDepsAvailable.tts && !voiceDepsAvailable.stt && h("div", { className: "rounded-md border border-yellow-400 bg-yellow-50 dark:bg-yellow-950 dark:border-yellow-700 p-3 text-sm text-yellow-800 dark:text-yellow-200" },
            "⚠ Voice dependencies are not installed. Install them above to enable voice features."
          ),
          h("label", { className: "flex items-center justify-between rounded-md border px-4 py-3 cursor-pointer" },
            h("div", null,
              h("div", { className: "text-sm font-medium" }, "Enable Voice"),
              h("div", { className: "text-xs text-muted-foreground" }, voiceDepsAvailable.tts || voiceDepsAvailable.stt ? "Allow users to use voice input and output." : "Requires voice dependencies to be installed.")
            ),
            h("input", {
              type: "checkbox",
              checked: voiceEnabled,
              disabled: !voiceDepsAvailable.tts && !voiceDepsAvailable.stt,
              onChange: (e) => setVoiceEnabled(e.target.checked),
              className: "h-4 w-4 cursor-pointer"
            })
          ),
          h("div", { className: "flex flex-col gap-2" },
            h("label", { className: "text-sm font-medium" }, "Default TTS Voice"),
            h("p", { className: "text-xs text-muted-foreground" }, "Used as the fallback voice when users haven't chosen one."),
            h("select", {
              value: defaultTtsVoice,
              onChange: (e) => setDefaultTtsVoice(e.target.value),
              disabled: !voiceDepsAvailable.tts,
              className: "rounded-md border border-input bg-background px-3 py-2 text-sm max-w-xs" + (!voiceDepsAvailable.tts ? " opacity-40 cursor-not-allowed" : "")
            },
              TTS_VOICES.map(v => h("option", { key: v.value, value: v.value }, v.label))
            )
          ),
          h(Button, { onClick: onSaveVoiceSettings, disabled: loading, size: "sm" }, "Save Voice Settings")
        )
      ),
      h(Card, null,
        h(CardHeader, null,
          h(CardTitle, null, "Chat Users"),
          h("p", { className: "text-sm text-muted-foreground" }, "Add or remove users for the standalone chat UI. New users can log in immediately — no restart needed.")
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
              h(Button, { onClick: onRefreshLogs, disabled: loading, size: "sm", variant: "outline" }, "Refresh"),
              h("a", {
                href: API_PREFIX + "/logs/download",
                download: "hermes-chat.log",
                className: "inline-flex items-center justify-center rounded-md border border-input bg-background px-3 py-1.5 text-xs font-medium shadow-sm hover:bg-accent hover:text-accent-foreground"
              }, "Download")
            )
          ),
          h("p", { className: "text-sm text-muted-foreground" },
            logsPaused
              ? "⏸ Log updates paused — scroll freely or copy."
              : "Last 100 lines from the Hermes Chat daemon log. Auto-scrolls to bottom."
          )
        ),
        h(CardContent, { style: { padding: "0 1rem 1rem" } },
          h("pre", {
            ref: logsBoxRef,
            className: "rounded-md bg-muted p-3 text-xs font-mono whitespace-pre-wrap break-all",
            style: { height: "320px", overflowY: "scroll" }
          },
            logs || "No logs yet."
          )
        )
      )
    );
  }

  window.__HERMES_PLUGINS__.register("hermes-chat", HermesChatDashboard);
})();

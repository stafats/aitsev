// Aitsev chat. Renders state sent by the extension; every action is a message back to it.
// All text goes through textContent (never innerHTML), so model output cannot inject markup.
(function () {
  "use strict";
  const vscode = acquireVsCodeApi();
  const app = document.getElementById("app");
  const saved = vscode.getState() || {};
  let draft = saved.draft || "";
  let state = null;
  let lastCount = 0;

  function persist() {
    vscode.setState({ draft });
  }

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "className") node.className = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2).toLowerCase(), value);
      else if (key === "disabled" || key === "checked" || key === "selected") node[key] = Boolean(value);
      else node.setAttribute(key, String(value));
    }
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function send(message) {
    vscode.postMessage(message);
  }

  /** Plain text with ``` fences and `inline code`, rendered safely. */
  function richText(text) {
    const parts = String(text).split(/```/);
    return parts.map((part, i) => {
      if (i % 2 === 1) {
        const newline = part.indexOf("\n");
        const code = newline >= 0 ? part.slice(newline + 1) : part;
        return el("pre", {}, el("code", {}, code.replace(/\n$/, "")));
      }
      return part
        .split(/\n{2,}/)
        .filter((p) => p.trim())
        .map((para) =>
          el("p", {}, ...para.split(/(`[^`\n]+`)/).map((seg) => (seg.startsWith("`") && seg.endsWith("`") && seg.length > 1 ? el("code", {}, seg.slice(1, -1)) : seg))),
        );
    });
  }

  const ACTION = { create: "+", modify: "~", delete: "−" };
  const STATUS = {
    applied: ["Applied", "good"],
    rejected: ["Rejected", "muted"],
    undone: ["Undone", "muted"],
    conflict: ["Changed locally", "warn"],
    failed: ["Failed", "bad"],
  };

  function renderEdits(entry) {
    const edits = entry.edits || [];
    if (!edits.length) return null;
    const open = edits.filter((e) => e.status === "proposed" || e.status === "conflict");
    const applied = edits.some((e) => e.status === "applied");
    return el(
      "div",
      { className: "edits" },
      edits.map((e) =>
        el(
          "div",
          { className: `edit ${e.status}` },
          el("span", { className: `action ${e.action}`, title: e.action }, ACTION[e.action] || "~"),
          el("a", { href: "#", className: "path", title: "Show diff", onClick: (ev) => { ev.preventDefault(); send({ type: "diff", entryId: entry.id, path: e.path }); } }, e.path),
          el("span", { className: "stats" }, el("span", { className: "add" }, `+${e.additions}`), " ", el("span", { className: "del" }, `−${e.deletions}`)),
          e.status === "proposed" || e.status === "conflict"
            ? el(
                "span",
                { className: "buttons" },
                e.status === "conflict" ? el("span", { className: "badge warn", title: e.note || "" }, "Changed locally") : null,
                el("button", { className: "small", onClick: () => send({ type: "accept", entryId: entry.id, path: e.path }) }, "Accept"),
                el("button", { className: "small secondary", onClick: () => send({ type: "reject", entryId: entry.id, path: e.path }) }, "Reject"),
              )
            : el("span", { className: `badge ${(STATUS[e.status] || ["", ""])[1]}`, title: e.note || "" }, (STATUS[e.status] || [e.status])[0]),
        ),
      ),
      open.length > 1 || applied
        ? el(
            "div",
            { className: "row" },
            open.length > 1 ? el("button", { className: "small", onClick: () => send({ type: "accept", entryId: entry.id }) }, "Accept all") : null,
            open.length > 1 ? el("button", { className: "small secondary", onClick: () => send({ type: "reject", entryId: entry.id }) }, "Reject all") : null,
            applied ? el("button", { className: "small secondary", onClick: () => send({ type: "undo", entryId: entry.id }) }, "Undo") : null,
          )
        : null,
      (entry.skipped || []).length ? el("div", { className: "muted small" }, `Skipped: ${entry.skipped.join("; ")}`) : null,
    );
  }

  function renderEntry(entry) {
    if (entry.role === "user") return el("div", { className: "msg user" }, richText(entry.text));
    if (entry.role === "error") return el("div", { className: "msg error", role: "alert" }, entry.text);
    const verification = {
      passed: ["Tests passed", "good"],
      repaired: ["Tests passed after a fix", "good"],
      failed: ["Tests failed", "bad"],
    }[entry.verification || ""];
    return el(
      "div",
      { className: "msg assistant" },
      el(
        "div",
        { className: "meta" },
        el("span", {}, "Aitsev"),
        entry.model ? el("span", { className: "chip" }, entry.model.split("/").pop()) : null,
        verification ? el("span", { className: `badge ${verification[1]}` }, verification[0]) : null,
        entry.autopilot && (entry.edits || []).length ? el("span", { className: "chip" }, "Autopilot") : null,
      ),
      richText(entry.text),
      renderEdits(entry),
    );
  }

  function renderSuggestions() {
    const items = state.suggestions || [];
    if (!items.length && !state.analyzing) return null;
    return el(
      "details",
      { className: "suggestions", open: items.length > 0 && items.length <= 5 },
      el("summary", {}, state.analyzing ? "Analyzing your project…" : `Suggestions (${items.length})`),
      items.map((s) =>
        el(
          "div",
          { className: "suggestion" },
          el("span", { className: `badge ${s.severity === "error" ? "bad" : s.severity === "warning" ? "warn" : "muted"}` }, s.severity),
          el("div", { className: "grow" },
            el("div", {}, s.title),
            el("a", { href: "#", className: "muted small", onClick: (ev) => { ev.preventDefault(); send({ type: "openFile", path: s.path, line: s.line }); } }, `${s.path}:${s.line}`)),
          el("button", { className: "small", onClick: () => send({ type: "fix", id: s.id }) }, "Fix"),
        ),
      ),
    );
  }

  function renderToolbar() {
    const model = el(
      "select",
      { "aria-label": "Model", title: "Model", onChange: (e) => send({ type: "setModel", model: e.target.value }) },
      state.models.map((m) => el("option", { value: m.id, selected: m.id === state.model }, m.label)),
    );
    const mode = el(
      "select",
      { "aria-label": "Mode", className: "narrow", onChange: (e) => send({ type: "setMode", mode: e.target.value }) },
      el("option", { value: "agent", selected: state.mode === "agent" }, "Agent"),
      el("option", { value: "ask", selected: state.mode === "ask" }, "Ask"),
    );
    const autopilot = el(
      "select",
      {
        "aria-label": "How edits are applied",
        className: "narrow",
        title: "Autopilot writes changes directly (you can undo). Supervised shows each change for you to accept first.",
        disabled: state.mode === "ask",
        onChange: (e) => send({ type: "setAutopilot", on: e.target.value === "autopilot" }),
      },
      el("option", { value: "supervised", selected: !state.autopilot }, "Supervised"),
      el("option", { value: "autopilot", selected: state.autopilot }, "Autopilot"),
    );
    return el("div", { className: "toolbar" }, model, mode, autopilot);
  }

  function renderInput() {
    const input = el("textarea", {
      placeholder: state.mode === "agent" ? "Describe a change, or ask about your code…" : "Ask about your code…",
      "aria-label": "Message",
      rows: "3",
      onInput: (e) => {
        draft = e.target.value;
        persist();
      },
      onKeydown: (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          submit();
        }
      },
    });
    input.value = draft;
    function submit() {
      if (!draft.trim() || state.busy) return;
      send({ type: "send", text: draft });
      draft = "";
      persist();
    }
    return el(
      "div",
      { className: "composer" },
      input,
      el("div", { className: "row" },
        el("span", { className: "muted small grow" }, "Enter to send · Shift+Enter for a new line"),
        el("button", { onClick: submit, disabled: Boolean(state.busy) }, "Send")),
    );
  }

  function render() {
    if (!state) return;
    const focused = document.activeElement && document.activeElement.tagName === "TEXTAREA";
    if (!state.signedIn) {
      app.replaceChildren(
        el("div", { className: "welcome" },
          el("h2", {}, "Aitsev"),
          el("p", {}, "Chat with your code. Several AI models write, test and cross-check every change."),
          el("button", { onClick: () => send({ type: "signIn" }) }, "Sign in with browser")),
      );
      return;
    }
    const children = [renderToolbar()];
    if (!state.hasFolder) {
      children.push(el("div", { className: "banner" }, "Open a project folder to start. ",
        el("button", { className: "small", onClick: () => send({ type: "openFolder" }) }, "Open Folder")));
    }
    const suggestions = renderSuggestions();
    if (suggestions) children.push(suggestions);
    const log = el("div", { className: "log" });
    if (!state.entries.length) {
      log.append(el("div", { className: "empty muted" },
        el("p", {}, "Ask anything about this project, or tell Aitsev what to change."),
        el("p", {}, "Try: “Explain how authentication works” or “Add input validation to the signup endpoint and tests for it”.")));
    }
    for (const entry of state.entries) log.append(renderEntry(entry));
    if (state.busy) log.append(el("div", { className: "msg assistant busy" }, el("span", { className: "spinner" }), state.busy));
    children.push(log, renderInput());
    app.replaceChildren(...children);
    if (state.entries.length !== lastCount || state.busy) log.scrollTop = log.scrollHeight;
    lastCount = state.entries.length;
    if (focused) {
      const textarea = app.querySelector("textarea");
      if (textarea) {
        textarea.focus();
        textarea.setSelectionRange(textarea.value.length, textarea.value.length);
      }
    }
  }

  window.addEventListener("message", (event) => {
    const message = event.data;
    if (message && message.type === "state") {
      state = message.state;
      render();
    }
  });
  send({ type: "ready" });
})();

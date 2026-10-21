// Aitsev sidebar. Renders state sent by the extension; every action is a message back to it.
// All text goes through textContent (never innerHTML) so server data cannot inject markup.
(function () {
  "use strict";
  const vscode = acquireVsCodeApi();
  const app = document.getElementById("app");
  const saved = vscode.getState() || {};
  let draft = saved.draft || "";
  let useOpenFiles = saved.useOpenFiles !== false;
  let size = saved.size || null;
  let state = null;

  function persist() {
    vscode.setState({ draft, useOpenFiles, size });
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
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function send(message) {
    vscode.postMessage(message);
  }

  function time(iso) {
    try {
      return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    } catch {
      return "";
    }
  }

  function badge(label, tone) {
    return el("span", { className: `badge tone-${tone}` }, label);
  }

  function renderSetup() {
    return el(
      "div",
      {},
      el("h2", {}, "Welcome to Aitsev"),
      el("p", {}, "Several AI models write, test and cross-check every change before you approve it."),
      el("button", { onClick: () => send({ type: "signIn" }) }, "Sign in with browser"),
      el("p", { className: "muted" }, "No account yet? You can create one on the page that opens."),
      el("p", { className: "muted" },
        el("a", { href: "#", onClick: (e) => { e.preventDefault(); send({ type: "setApiKey" }); } }, "Use an API key instead"),
        ` · Server: ${state.serverUrl}`),
    );
  }

  function renderForm() {
    const currentSize = size || state.defaultSize || "auto";
    const textarea = el("textarea", {
      placeholder: "Describe the change, e.g. “Add pagination to the users API and update its tests”",
      "aria-label": "Task description",
      onInput: (e) => {
        draft = e.target.value;
        persist();
      },
      onKeydown: (e) => {
        if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) submit();
      },
    });
    textarea.value = draft;
    const select = el(
      "select",
      {
        "aria-label": "Team size",
        onChange: (e) => {
          size = e.target.value;
          persist();
        },
      },
      ...[
        ["auto", "Auto team"],
        ["small", "1 model"],
        ["normal", "2 models"],
        ["large", "3 models"],
      ].map(([value, label]) => el("option", { value, selected: value === currentSize }, label)),
    );
    function submit() {
      if (!draft.trim() || state.busy) return;
      send({ type: "submit", instructions: draft, size: select.value, useOpenFiles });
      draft = "";
      persist();
    }
    return el(
      "div",
      {},
      el("h2", {}, "New task"),
      textarea,
      el(
        "label",
        { className: "check" },
        el("input", {
          type: "checkbox",
          checked: useOpenFiles,
          onChange: (e) => {
            useOpenFiles = e.target.checked;
            persist();
          },
        }),
        "Focus on open files",
      ),
      el(
        "p",
        { className: "muted" },
        `Models: ${state.models || "Automatic"} · `,
        el("a", { href: "#", onClick: (e) => { e.preventDefault(); send({ type: "chooseModels" }); } }, "Change"),
        state.account ? ` · ${state.account}` : "",
      ),
      el(
        "div",
        { className: "row" },
        select,
        el("span", { className: "grow" }),
        el("button", { onClick: submit, disabled: Boolean(state.busy), title: "Ctrl+Enter" }, "Run task"),
      ),
    );
  }

  function renderTasks() {
    if (state.tasks.length === 0) {
      return el("div", {}, el("h2", {}, "Tasks"), el("p", { className: "muted" }, "No tasks yet."));
    }
    return el(
      "div",
      {},
      el("h2", {}, "Tasks"),
      el(
        "ul",
        { className: "tasks", role: "listbox", "aria-label": "Tasks" },
        state.tasks.map((t) =>
          el(
            "li",
            {
              className: state.selected && state.selected.id === t.id ? "selected" : "",
              role: "option",
              tabindex: "0",
              title: t.title,
              onClick: () => send({ type: "select", id: t.id }),
              onKeydown: (e) => {
                if (e.key === "Enter" || e.key === " ") send({ type: "select", id: t.id });
              },
            },
            el("span", { className: "title" }, t.title),
            t.applied ? el("span", { className: "muted", title: "Applied" }, "✓") : null,
            badge(t.statusLabel, t.tone),
          ),
        ),
      ),
    );
  }

  function renderDetail(d) {
    const running = ["queued", "planning", "generating", "reviewing", "judging"].includes(d.status);
    const parts = [
      el("h3", {}, d.title),
      el(
        "div",
        { className: "row" },
        running ? el("span", { className: `spinner tone-${d.tone}` }, d.statusLabel) : badge(d.statusLabel, d.tone),
        el("span", { className: "muted" }, `${d.tokens.toLocaleString()} tokens`),
      ),
    ];
    if (d.error) parts.push(el("div", { className: "banner error" }, d.error));
    if (d.appliedNote) parts.push(el("div", { className: "banner" }, d.appliedNote));
    if (d.plan) {
      parts.push(el("h2", {}, "Plan"), el("p", {}, d.plan.summary));
      if (d.plan.steps.length) parts.push(el("ol", { className: "steps" }, d.plan.steps.map((s) => el("li", {}, s))));
    }
    if (d.candidates.length) {
      parts.push(
        el("h2", {}, "Candidates"),
        el(
          "table",
          { className: "candidates" },
          el("tr", {}, el("th", {}, ""), el("th", {}, "Model"), el("th", {}, "Result"), el("th", {}, "Score")),
          d.candidates.map((c) =>
            el(
              "tr",
              { title: c.error || "" },
              el("td", {}, c.winner ? `★ ${c.label}` : c.label),
              el("td", {}, c.model.replace(/^[^/]+\//, "")),
              el("td", {}, `${c.status}${c.attempts > 1 ? ` (${c.attempts} tries)` : ""}`),
              el("td", {}, c.score === null ? "–" : `${c.score.toFixed(1)}`),
            ),
          ),
        ),
      );
    }
    if (d.decision) parts.push(el("p", { className: "muted" }, `Winner ${d.decision.label}: ${d.decision.rationale}`));
    if (d.files.length) {
      parts.push(
        el("h2", {}, `Changes (${d.files.length})`),
        el(
          "ul",
          { className: "files" },
          d.files.map((f) =>
            el(
              "li",
              {},
              el(
                "a",
                {
                  role: "button",
                  tabindex: "0",
                  onClick: () => send({ type: "openDiff", id: d.id, path: f.path }),
                  onKeydown: (e) => {
                    if (e.key === "Enter") send({ type: "openDiff", id: d.id, path: f.path });
                  },
                },
                f.path,
              ),
              el("span", { className: "muted" }, ` ${f.action}`),
            ),
          ),
        ),
      );
    }
    const busy = Boolean(state.busy);
    const actions = [];
    if (d.canApprove && !d.applied) {
      actions.push(el("button", { disabled: busy, onClick: () => send({ type: "approve", id: d.id }) }, "Approve & apply"));
    }
    if (d.status === "awaiting_approval" && !d.local) {
      actions.push(el("span", { className: "muted" }, "Submitted elsewhere; apply it from that workspace."));
    }
    if (d.canReject) {
      actions.push(el("button", { className: "secondary", disabled: busy, onClick: () => send({ type: "reject", id: d.id }) }, "Reject"));
    }
    if (d.canRetry) {
      actions.push(
        el("button", { className: "secondary", disabled: busy, onClick: () => send({ type: "retry", id: d.id }) }, "Retry with feedback"),
      );
    }
    if (d.canCancel) {
      actions.push(el("button", { className: "secondary", disabled: busy, onClick: () => send({ type: "cancel", id: d.id }) }, "Cancel"));
    }
    if (actions.length) parts.push(el("div", { className: "actions" }, actions));
    if (d.events.length) {
      parts.push(
        el("h2", {}, "Activity"),
        el(
          "ul",
          { className: "events" },
          d.events
            .slice()
            .reverse()
            .map((e) => el("li", {}, el("span", { className: "muted" }, `${time(e.time)} `), e.message)),
        ),
      );
    }
    return el("div", { className: "detail" }, parts);
  }

  function render() {
    if (!state) return;
    const focused = document.activeElement && document.activeElement.tagName === "TEXTAREA";
    const children = [];
    if (state.busy) children.push(el("div", { className: "banner" }, el("span", { className: "spinner" }, state.busy)));
    if (state.error) children.push(el("div", { className: "banner error", role: "alert" }, state.error));
    if (state.notice) children.push(el("div", { className: "banner" }, state.notice));
    if (!state.configured) children.push(renderSetup());
    else {
      children.push(renderForm(), renderTasks());
      if (state.selected) children.push(renderDetail(state.selected));
    }
    app.replaceChildren(...children);
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

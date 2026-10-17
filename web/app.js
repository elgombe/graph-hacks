// Repo Brain web UI: streaming chat over /api/ask, evidence graph (Cytoscape), triage inbox, memory.

const KIND_COLORS = {
  Person: "#f5b14c", Issue: "#f87171", PR: "#a78bfa", File: "#2dd4bf", Dir: "#60a5fa", Repo: "#94a3b8",
  Decision: "#4ade80", Comment: "#64748b", Release: "#f472b6", Topic: "#bef264", Task: "#fb923c",
  Turn: "#cbd5e1", Fact: "#fde68a", Module: "#22d3ee", Label: "#a3a3a3", Agent: "#fb7185",
};
const LEGEND = ["Person", "Issue", "PR", "File", "Dir", "Decision", "Release", "Task"];
const SUGGESTIONS = [
  "Who should review a change to src/execution_plan in FalkorDB core, and why?",
  "Why did FalkorDB remove query replay replication?",
  "I want to change falkordb/asyncio/cluster.py in falkordb-py. What might break, and is anyone already on it?",
  "Where is knowledge in QueryWeaver concentrated in too few people?",
  "How is @Naseem77 connected to @swilly22?",
  "Who are the key people in the review network, and who bridges the teams?",
  "What did the team decide about hiredis?",
  "Triage issue falkordb/falkordb#3350",
];

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const newId = () => (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2) + Date.now()).replace(/-/g, "").slice(0, 16);

function storedUser() {
  try {
    let u = localStorage.getItem("rb-user");
    if (!u) { u = "u-" + newId().slice(0, 10); localStorage.setItem("rb-user", u); }
    return u;
  } catch { return "u-" + newId().slice(0, 10); }  // storage blocked: memory lasts for this page only
}

const state = { user: storedUser(), session: newId(), busy: false, answers: [] };

/* ---------------------------------------------------------------- graph */

function kindOf(id, nodes) { return nodes?.[id]?.kind || "Node"; }

function shortLabel(n) {
  const label = n.label || n.id;
  switch (n.kind) {
    case "Person": return "@" + label;
    case "Issue": case "PR": return trunc(label, 26);
    case "File": return label.split("/").pop();
    case "Dir": return (label.split("/").filter(Boolean).pop() || n.id.split(":")[0]) + "/";
    default: return trunc(label, 28);
  }
}
const trunc = (s, n) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

function makeGraph(container, emptyEl, onTap) {
  const cy = cytoscape({
    container, wheelSensitivity: 0.25, minZoom: 0.15, maxZoom: 3,
    style: [
      { selector: "node", style: {
        "background-color": "data(color)", label: "data(short)", color: "#cbd5e1", "font-size": 9,
        "text-valign": "bottom", "text-margin-y": 4, width: 14, height: 14,
        "text-outline-color": "#0b0f14", "text-outline-width": 2,
      } },
      { selector: "node[?cited]", style: { width: 24, height: 24, "border-width": 2, "border-color": "#ffffff", "font-size": 10, "font-weight": 600 } },
      { selector: "node.focus", style: { "border-width": 4, "border-color": "#8b6cff", "z-index": 10 } },
      { selector: "edge", style: {
        width: 1.3, "line-color": "#334155", "target-arrow-color": "#475569", "target-arrow-shape": "triangle",
        "arrow-scale": 0.7, "curve-style": "bezier", label: "data(type)", "font-size": 7, color: "#64748b",
        "text-rotation": "autorotate", "text-outline-color": "#0b0f14", "text-outline-width": 2,
      } },
      { selector: ".dim", style: { opacity: 0.15 } },
    ],
  });
  cy.on("tap", "node", (e) => onTap?.(e.target.data()));
  cy.on("tap", (e) => { if (e.target === cy) { cy.elements().removeClass("dim focus"); onTap?.(null); } });

  const toElements = (ev, cited = []) => {
    const set = new Set(cited);
    const nodes = ev.nodes.map((n) => ({ data: { ...n, short: shortLabel(n), color: KIND_COLORS[n.kind] || "#94a3b8", cited: set.has(n.id) } }));
    const ids = new Set(ev.nodes.map((n) => n.id));
    const edges = ev.edges.filter((e) => ids.has(e.source) && ids.has(e.target))
      .map((e) => ({ data: { id: `${e.source}|${e.type}|${e.target}`, ...e } }));
    return [...nodes, ...edges];
  };
  const layout = () => cy.layout({ name: "cose", animate: false, nodeRepulsion: 9000, idealEdgeLength: 80, padding: 30 }).run();

  return {
    cy,
    set(ev, cited) {
      cy.elements().remove();
      cy.add(toElements(ev, cited));
      emptyEl.classList.toggle("hidden", ev.nodes.length > 0);
      layout();
    },
    add(ev) {
      const fresh = toElements(ev).filter((el) => cy.getElementById(el.data.id).empty());
      cy.add(fresh);
      layout();
    },
    focus(id) {
      const n = cy.getElementById(id);
      if (n.empty()) return false;
      cy.elements().addClass("dim").removeClass("focus");
      n.closedNeighborhood().removeClass("dim");
      n.addClass("focus");
      cy.animate({ center: { eles: n }, zoom: Math.max(cy.zoom(), 1.1) }, { duration: 250 });
      onTap?.(n.data());
      return true;
    },
  };
}

function nodeCard(cardEl, graphFn) {
  return (data) => {
    if (!data) { cardEl.classList.add("hidden"); return; }
    cardEl.innerHTML = `
      <div class="kind" style="color:${KIND_COLORS[data.kind] || "#94a3b8"}">${esc(data.kind)}</div>
      <div>${esc(data.label)}</div>
      <div class="id">${esc(data.id)}</div>
      <div class="row">
        <button class="link" data-expand>Expand neighbours</button>
        ${data.url ? `<a href="${esc(data.url)}" target="_blank" rel="noopener">Open on GitHub ↗</a>` : ""}
      </div>`;
    cardEl.classList.remove("hidden");
    cardEl.querySelector("[data-expand]").onclick = async () => {
      const res = await fetch(`/api/node?id=${encodeURIComponent(data.id)}`);
      if (res.ok) graphFn().add(await res.json());
    };
  };
}

const graph = makeGraph($("#graph"), $("#graph-empty"), nodeCard($("#node-card"), () => graph));
const taskGraph = makeGraph($("#task-graph"), $("#task-graph-empty"), null);

$("#legend").innerHTML = LEGEND.map((k) => `<span><i style="background:${KIND_COLORS[k]}"></i>${k}</span>`).join("");

/* ---------------------------------------------------------------- chat */

function renderAnswer(text, nodes) {
  // [[id]] citations become chips; markdown is sanitised before it touches the page.
  const html = text.replace(/\[\[([^\]]+)\]\]/g, (_, id) => {
    const kind = kindOf(id, nodes);
    const label = kind === "Person" ? id : kind === "Issue" || kind === "PR" ? id.split("/").pop() :
      kind === "File" ? id.split("/").pop() : kind === "Decision" ? "decision" : id.split(":").pop() || id;
    return `<span class="cite" data-id="${esc(id)}" title="${esc(nodes?.[id]?.label || id)}" style="--c:${KIND_COLORS[kind] || "#94a3b8"}">${esc(label)}</span>`;
  });
  return DOMPurify.sanitize(marked.parse(html), { ADD_ATTR: ["data-id", "style"] });
}

function stepLine(s, i) {
  const args = Object.entries(s.args).map(([k, v]) => `${k}=${typeof v === "string" ? JSON.stringify(v) : JSON.stringify(v)}`).join(", ");
  return `<li><span class="tool">${esc(s.tool)}</span>(<span class="args">${esc(args)}</span>) → <span class="res">${s.rows} rows · ${s.ms} ms</span>${s.note ? ` <span class="note">${esc(s.note)}</span>` : ""}</li>`;
}

function showTrace(steps, meta) {
  $("#steps").innerHTML = steps.length ? steps.map(stepLine).join("") : `<li class="muted none">No tools were called.</li>`;
  $("#trace-meta").textContent = meta || "";
}

// Default view: the cited nodes plus any node that connects two of them. The full evidence is one click away.
function citedView(ev, cited) {
  const set = new Set(cited);
  if (set.size < 2) return ev;
  const touching = {};
  ev.edges.forEach((e) => {
    if (set.has(e.target)) (touching[e.source] ||= new Set()).add(e.target);
    if (set.has(e.source)) (touching[e.target] ||= new Set()).add(e.source);
  });
  const keep = new Set([...set, ...ev.nodes.filter((n) => (touching[n.id]?.size || 0) >= 2).map((n) => n.id)]);
  return { nodes: ev.nodes.filter((n) => keep.has(n.id)), edges: ev.edges.filter((e) => keep.has(e.source) && keep.has(e.target)) };
}

function selectAnswer(idx) {
  const a = state.answers[idx];
  if (!a) return;
  state.selected = idx;
  document.querySelectorAll(".msg.bot").forEach((el) => el.classList.toggle("selected", Number(el.dataset.idx) === idx));
  const view = state.fullGraph ? a.evidence : citedView(a.evidence, a.cited);
  graph.set(view, a.cited);
  const mode = $("#graph-mode");
  mode.classList.toggle("hidden", view.nodes.length === a.evidence.nodes.length && !state.fullGraph);
  mode.textContent = state.fullGraph ? "show cited paths only" : `show all ${a.evidence.nodes.length} nodes`;
  showTrace(a.steps, `${a.model} · ${a.cited.length} citations verified`);
}

$("#graph-mode").onclick = () => { state.fullGraph = !state.fullGraph; selectAnswer(state.selected); };

function outcomeBadge(outcome, unverified) {
  if (unverified?.length) return `<span class="badge bad" title="${esc(unverified.join(", "))}">${unverified.length} unverified</span>`;
  if (outcome === "answered_with_path") return `<span class="badge ok">answered with path</span>`;
  return `<span class="badge warn">${esc(outcome.replaceAll("_", " "))}</span>`;
}

function ask(question) {
  question = question.trim();
  if (!question || state.busy) return;
  state.busy = true;
  $("#send").disabled = true;
  $("#empty")?.remove();
  const box = $("#messages");
  box.insertAdjacentHTML("beforeend", `<div class="msg user">${esc(question)}</div>`);
  const bot = document.createElement("div");
  bot.className = "msg bot";
  bot.innerHTML = `<div class="thinking"><span class="dots">Walking the graph</span><div class="live"></div></div>`;
  box.appendChild(bot);
  box.scrollTop = box.scrollHeight;
  const live = [];
  showTrace([], "running…");

  const params = new URLSearchParams({ q: question, user: state.user, session: state.session });
  const es = new EventSource(`/api/ask?${params}`);
  const done = () => { es.close(); state.busy = false; $("#send").disabled = false; };

  es.addEventListener("step", (e) => {
    const s = JSON.parse(e.data);
    live.push(s);
    bot.querySelector(".live").innerHTML = live.map((x) => `→ ${esc(x.tool)} · ${x.rows} rows`).join("<br>");
    showTrace(live, "running…");
  });
  es.addEventListener("answer", (e) => {
    done();
    const a = JSON.parse(e.data);
    const nodes = Object.fromEntries(a.evidence.nodes.map((n) => [n.id, n]));
    const idx = state.answers.push({ ...a, nodes }) - 1;
    bot.dataset.idx = idx;
    bot.innerHTML = `<div class="body">${renderAnswer(a.text || "(no answer)", nodes)}</div>
      <div class="meta">${outcomeBadge(a.outcome, a.unverified)}<span>${a.steps.length} tool calls</span>
      <span>${a.evidence.nodes.length} nodes in evidence</span><span>${esc(a.model)}</span></div>`;
    bot.onclick = (ev) => {
      selectAnswer(idx);
      const chip = ev.target.closest(".cite");
      if (chip) graph.focus(chip.dataset.id);
    };
    selectAnswer(idx);
    box.scrollTop = box.scrollHeight;
  });
  es.addEventListener("error", (e) => {
    // Our own `error` event carries data; a bare error means the connection dropped.
    let message = "The connection dropped. Try again.";
    try { if (e.data) message = JSON.parse(e.data).message; } catch {}
    done();
    bot.innerHTML = `<span class="badge bad">error</span> ${esc(message)}`;
    showTrace(live, "failed");
  });
}

$("#composer").addEventListener("submit", (e) => { e.preventDefault(); const q = $("#q").value; $("#q").value = ""; ask(q); });
$("#q").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); } });
$("#suggestions").innerHTML = SUGGESTIONS.map((s) => `<button type="button">${esc(s)}</button>`).join("");
$("#suggestions").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) ask(b.textContent); });
$("#new-chat").onclick = () => { state.session = newId(); updateSessionLabel(); $("#messages").innerHTML = ""; graph.set({ nodes: [], edges: [] }, []); showTrace([], ""); };
function updateSessionLabel() { $("#session-label").textContent = `you: ${state.user} · session ${state.session.slice(0, 6)}`; }
updateSessionLabel();

/* ---------------------------------------------------------------- inbox */

async function loadInbox() {
  const tasks = await (await fetch("/api/inbox")).json();
  $("#tasks").innerHTML = tasks.length ? tasks.map((t) => `
    <div class="task" data-id="${esc(t.id)}">
      <div class="title"><span class="pill ${esc(t.status)}">${esc(t.status)}</span>
        <a href="${esc(t.issue.url)}" target="_blank" rel="noopener">${esc(t.issue.id.split("/").pop())}</a> ${esc(t.issue.title)}</div>
      <div class="meta">queued by ${esc(t.created_by)} on ${esc(t.created)}${t.claimed_by ? ` · handled by <b>${esc(t.claimed_by)}</b>` : ""}</div>
      ${t.owners.length ? `<div class="line">Suggested owners: ${t.owners.map((o) => `<span class="cite" style="--c:${KIND_COLORS.Person}">${esc(o)}</span>`).join(" ")}</div>` : ""}
      ${t.files.length ? `<div class="line muted">Likely files: ${t.files.slice(0, 4).map(esc).join(", ")}</div>` : ""}
      ${t.related.length ? `<div class="line muted">Related: ${t.related.slice(0, 5).map((r) => esc(r.split("/").pop())).join(", ")}</div>` : ""}
      ${t.decisions.length ? `<div class="line">Past decision: ${esc(t.decisions[0])}</div>` : ""}
      ${t.error ? `<div class="line" style="color:var(--bad)">${esc(t.error)}</div>` : ""}
    </div>`).join("") : `<p class="muted">No tasks yet. Ask the Curator to queue some issues.</p>`;
}

$("#tasks").addEventListener("click", async (e) => {
  const card = e.target.closest(".task");
  if (!card || e.target.closest("a")) return;
  document.querySelectorAll(".task").forEach((el) => el.classList.toggle("selected", el === card));
  const res = await fetch(`/api/node?id=${encodeURIComponent("task:" + card.dataset.id)}`);
  if (res.ok) taskGraph.set(await res.json(), ["task:" + card.dataset.id]);
});

function toast(html) { const t = $("#inbox-toast"); t.innerHTML = html; t.classList.remove("hidden"); }

async function inboxAction(button, url, describe) {
  button.disabled = true;
  try {
    const res = await fetch(url, { method: "POST" });
    toast(res.ok ? describe(await res.json()) : `Failed: ${esc(await res.text())}`);
    await loadInbox();
  } finally { button.disabled = false; }
}

$("#queue").onclick = (e) => inboxAction(e.currentTarget, "/api/inbox/queue?n=5",
  (r) => r.queued.length ? `Curator queued ${r.queued.length} new issue(s) as Task nodes.` : "No untriaged open issues left to queue.");
$("#run").onclick = (e) => inboxAction(e.currentTarget, "/api/inbox/run?n=6&workers=3", (r) => {
  if (!r.handled.length) return "No open tasks to claim.";
  const by = {};
  r.handled.forEach((h) => { (by[h.agent] ||= []).push(h.task.split("/").pop()); });
  return Object.entries(by).map(([a, ts]) => `<b>${esc(a)}</b> claimed ${ts.map(esc).join(", ")}`).join(" · ") +
    " — every task claimed exactly once.";
});

/* ---------------------------------------------------------------- memory */

async function loadMemory() {
  const m = await (await fetch(`/api/memory?user=${encodeURIComponent(state.user)}`)).json();
  const chips = (ids) => ids.map((id) => `<span class="cite" style="--c:#94a3b8">${esc(id)}</span>`).join(" ");
  $("#memory").innerHTML = `
    <div class="mem-section"><h3>Facts you asked me to remember</h3>
      ${m.facts.length ? `<ul>${m.facts.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>` : `<p class="muted">None yet. Try “Remember that I maintain falkordb/asyncio”.</p>`}</div>
    <div class="mem-section"><h3>What your questions keep touching</h3>
      ${m.interests.length ? chips(m.interests) : `<p class="muted">Ask a few questions first.</p>`}</div>
    <div class="mem-section"><h3>Sessions</h3>
      ${m.sessions.length ? m.sessions.map((s) => `<div class="mem-session"><b>Session ${esc(s.id.slice(0, 6))}</b>
        <span class="muted">· ${esc(s.started)}</span><ol>${s.turns.map((t) => `<li>${esc(t.question)}</li>`).join("")}</ol></div>`).join("")
        : `<p class="muted">No sessions yet.</p>`}</div>`;
}

/* ---------------------------------------------------------------- shell */

document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${b.dataset.tab}`));
  if (b.dataset.tab === "inbox") loadInbox();
  if (b.dataset.tab === "memory") loadMemory();
  if (b.dataset.tab === "ask") graph.cy.resize();
  if (b.dataset.tab === "inbox") taskGraph.cy.resize();
}));

fetch("/api/stats").then((r) => r.json()).then((s) => {
  const decisions = s.counts.Decision || 0;
  $("#stats").textContent = `${s.nodes.toLocaleString()} nodes · ${s.edges.toLocaleString()} relationships · ${decisions} decisions · ${s.repos.length} repos`;
  $("#tenant").textContent = `${s.tenant} org`;
}).catch(() => {});

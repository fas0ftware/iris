"use strict";
// Every value is inserted with textContent: message texts quote devices and attackers.
const $ = (id) => document.getElementById(id);
const el = (tag, text, cls) => { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (cls) e.className = cls; return e; };
const WHY = { deliver: "sent", quiet: "held (quiet hours)", below_min: "stored only", duplicate: "repeat" };
let topic = "";
try { topic = localStorage.getItem("iris.topic") || ""; } catch (_) {}

function when(ts) {
  const d = new Date(ts * 1000);
  const today = new Date().toDateString() === d.toDateString();
  return d.toLocaleString([], today ? { timeStyle: "short" } : { dateStyle: "short", timeStyle: "short" });
}

async function get(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

async function act(key, what, hours) {
  const r = await fetch(`/api/task/${encodeURIComponent(key)}/${what}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(hours ? { hours } : {}),
  });
  if (!r.ok) throw new Error(`${what}: ${r.status}`);
  return load();
}

function button(text, onClick, cls) {
  const b = el("button", text, cls || "");
  b.type = "button";
  b.addEventListener("click", () => { b.disabled = true; onClick().catch((e) => { b.disabled = false; alert(e.message); }); });
  return b;
}

const STATE = { snoozed: "snoozed", done: "done", gone: "resolved by itself" };

function taskItem(t) {
  const li = el("li", undefined, `task p-${t.priority} s-${t.state}`);
  const head = el("div", undefined, "head");
  head.append(el("span", t.priority, "prio"), el("span", t.source, "topic"), el("strong", t.title));
  if (t.reopened) head.append(el("span", "still there", "why quiet"));
  if (t.state !== "open") head.append(el("span", STATE[t.state] +
    (t.state === "snoozed" ? ` until ${when(t.snooze_until)}` : ""), "why"));
  li.append(head);
  if (t.action) li.append(el("p", t.action, "body"));
  if (t.command) {
    const code = el("code", t.command, "cmd");
    const copy = button("copy", async () => { await navigator.clipboard.writeText(t.command); copy.disabled = false; copy.textContent = "copied"; }, "small");
    const row = el("div", undefined, "cmdrow");
    row.append(code, copy);
    li.append(row);
  }
  if (t.why) { const d = el("details"); d.append(el("summary", "why", "muted"), el("p", t.why, "body muted")); li.append(d); }
  const foot = el("div", undefined, "foot");
  if (t.state === "open") {
    foot.append(button("Done", () => act(t.key, "done")), button("Snooze 1 day", () => act(t.key, "snooze", 24), "ghost"),
      button("1 week", () => act(t.key, "snooze", 168), "ghost"));
  } else if (t.state !== "gone") {
    foot.append(button("Reopen", () => act(t.key, "reopen"), "ghost"));
  }
  foot.append(el("span", `since ${when(t.first_seen)}`, "muted"));
  if (t.link) { const a = el("a", "open"); a.href = t.link; a.rel = "noopener noreferrer"; a.target = "_blank"; foot.append(a); }
  li.append(foot);
  return li;
}

function renderTasks(tasks) {
  const open = tasks.filter((t) => t.state === "open");
  const other = tasks.filter((t) => t.state !== "open");
  $("tasks-box").hidden = !tasks.length;
  $("tasks-count").textContent = open.length ? `${open.length} open` : "nothing open";
  $("tasks").replaceChildren(...open.map(taskItem));
  if (!open.length) $("tasks").append(el("li", "All done. New tasks appear here when your tools find something to do.", "muted"));
  $("tasks-other-box").hidden = !other.length;
  $("tasks-other-sum").textContent = `${other.length} snoozed, done or resolved (last 7 days)`;
  $("tasks-other").replaceChildren(...other.map(taskItem));
}

async function load() {
  const [status, all, tasks] = await Promise.all([get("/api/status"), get("/api/messages"), get("/api/tasks?state=all")]);
  renderTasks(tasks);
  $("ver").textContent = `v${status.version}`;
  const d = status.decisions || {};
  const n = (k) => (d[k] ? d[k].messages : 0);
  const repeats = Object.values(d).reduce((a, v) => a + v.repeats, 0);
  $("stats").textContent = `24 h: ${status.messages_24h} in · ${n("deliver")} sent · ${n("quiet")} held for the morning · ` +
    `${n("below_min")} stored only · ${repeats} repeats folded` + (status.undelivered ? ` · ${status.undelivered} waiting to be delivered` : "");
  const topics = [...new Set(all.map((m) => m.topic))].sort();
  const nav = $("topics");
  nav.replaceChildren();
  for (const t of ["", ...topics]) {
    const b = el("button", t || "all", t === topic ? "on" : "");
    b.addEventListener("click", () => { topic = t; try { localStorage.setItem("iris.topic", t); } catch (_) {} load(); });
    nav.append(b);
  }
  const list = $("inbox");
  list.replaceChildren();
  for (const m of all.filter((x) => !topic || x.topic === topic)) {
    const li = el("li", undefined, `p-${m.priority}`);
    const head = el("div", undefined, "head");
    head.append(el("span", m.priority, "prio"), el("span", m.topic, "topic"), el("strong", m.title), el("time", when(m.last_ts)));
    li.append(head);
    if (m.body) li.append(el("p", m.body, "body"));
    const foot = el("div", undefined, "foot");
    foot.append(el("span", WHY[m.decision] || m.decision, `why ${m.decision}`));
    if (m.repeats) foot.append(el("span", `×${m.repeats + 1}`, "rep"));
    if (m.delivered && m.delivered !== "nowhere") foot.append(el("span", `→ ${m.delivered}`, "muted"));
    if (m.decision === "deliver" && !m.delivered) foot.append(el("span", `not delivered yet (${m.attempts} tries)`, "why quiet"));
    if (m.click) { const a = el("a", "open"); a.href = m.click; a.rel = "noopener noreferrer"; a.target = "_blank"; foot.append(a); }
    li.append(foot);
    list.append(li);
  }
  if (!list.children.length) list.append(el("li", "Nothing yet. Point a tool's ntfy address at http://127.0.0.1:8090/<topic>.", "muted"));
}

document.addEventListener("DOMContentLoaded", () => {
  load().catch((e) => { $("stats").textContent = `Iris unreachable: ${e.message}`; });
  setInterval(() => load().catch(() => {}), 10000);
});

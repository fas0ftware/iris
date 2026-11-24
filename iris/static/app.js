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

async function load() {
  const [status, all] = await Promise.all([get("/api/status"), get("/api/messages")]);
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

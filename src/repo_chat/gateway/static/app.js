const form = document.querySelector("#chat-form");
const input = document.querySelector("#message");
const send = document.querySelector("#send");
const messages = document.querySelector("#messages");
const repository = document.querySelector("#repository");
const revision = document.querySelector("#revision");
const health = document.querySelector("#health");
const sessionKey = "repo-copilot-session";
let sessionId = localStorage.getItem(sessionKey);

function escapeHtml(value) {
  const element = document.createElement("div");
  element.textContent = value ?? "";
  return element.innerHTML;
}

function renderInlineMarkdown(value) {
  return escapeHtml(value)
    .replace(/`([^`\n]+)`/g, "<code class=\"inline-code\">$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>");
}

function renderProse(value) {
  return value
    .split(/\n{2,}/)
    .filter((part) => part.trim())
    .map((part) => `<p>${renderInlineMarkdown(part).replaceAll("\n", "<br>")}</p>`)
    .join("");
}

function renderAnswer(value) {
  const fence = /```([\w.+-]*)\s*\n([\s\S]*?)```/g;
  let cursor = 0;
  let html = "";
  for (const match of value.matchAll(fence)) {
    html += renderProse(value.slice(cursor, match.index));
    const language = match[1] || "code";
    html += `<section class="code-card"><header><span>${escapeHtml(language)}</span><button type="button" class="copy-code">Copy code</button></header><pre><code>${escapeHtml(match[2].trim())}</code></pre></section>`;
    cursor = match.index + match[0].length;
  }
  html += renderProse(value.slice(cursor));
  return html;
}

function evidenceCard(item) {
  if (item.location) {
    const location = `${item.location.file_path}:${item.location.start_line}-${item.location.end_line}`;
    const snapshot = item.location.snapshot;
    const snapshotDetails = snapshot
      ? `<div class="snapshot-meta"><span><small>Revision</small>${escapeHtml(item.location.revision.slice(0, 12))}</span><span><small>Captured</small>${formatTimestamp(snapshot.captured_at)}</span><span><small>Indexed</small>${formatTimestamp(snapshot.indexed_at)}</span><span><small>Index job</small>${escapeHtml(snapshot.index_job_id?.slice(0, 12) ?? "—")}</span></div><div class="snapshot-origin"><span>${snapshot.immutable ? "Immutable snapshot" : "Mutable checkout"}</span>${snapshot.repository_source ? `<code title="${escapeHtml(snapshot.repository_source)}">${escapeHtml(snapshot.repository_source)}</code>` : ""}</div>`
      : `<div class="snapshot-origin unavailable"><span>Legacy evidence · snapshot timestamp unavailable</span></div>`;
    const excerpt = item.excerpt
      ? `<pre class="evidence-excerpt"><code>${escapeHtml(item.excerpt.slice(0, 700))}</code></pre>`
      : "";
    return `<article class="evidence-card source-evidence"><div class="evidence-title"><span class="evidence-kind">source snapshot</span><strong>${escapeHtml(location)}</strong></div>${snapshotDetails}${excerpt}</article>`;
  }
  const identity = item.graph_path?.length
    ? item.graph_path.join(" → ")
    : item.entity_id ?? "Graph relationship";
  return `<article class="evidence-card graph-evidence"><div class="evidence-title"><span class="evidence-kind">graph</span><strong>${escapeHtml(identity)}</strong></div></article>`;
}

function formatTimestamp(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return escapeHtml(value);
  return escapeHtml(parsed.toLocaleString([], { dateStyle: "medium", timeStyle: "short" }));
}

function addMessage(role, content, response = null) {
  const article = document.createElement("article");
  article.className = `message ${role}`;
  const evidence = response?.evidence ?? [];
  const agents = (response?.agents_used ?? [])
    .map((agent) => `<span class="badge"><span class="badge-dot"></span>${escapeHtml(agent.replaceAll("_", " "))}</span>`)
    .join("");
  const warnings = (response?.warnings ?? [])
    .map((warning) => `<p class="warning">⚠ ${escapeHtml(warning)}</p>`)
    .join("");
  const evidenceHtml = evidence.length
    ? `<details class="evidence-panel"><summary><span>Evidence</span><span>${evidence.length} item${evidence.length === 1 ? "" : "s"}</span></summary><div class="evidence-list">${evidence.map(evidenceCard).join("")}</div></details>`
    : "";
  article.innerHTML = role === "assistant"
    ? `<div class="avatar">RC</div><div class="message-body"><div class="answer-content">${renderAnswer(content)}</div>${agents ? `<div class="response-section"><span class="section-label">Agents used</span><div class="meta">${agents}</div></div>` : ""}${warnings}${evidenceHtml}${response?.trace_id ? `<div class="trace"><div><span class="section-label">Request trace</span><code>${escapeHtml(response.trace_id)}</code></div><button type="button" class="copy-trace">Copy ID</button></div>` : ""}</div>`
    : `<div class="message-body"><p>${escapeHtml(content)}</p></div>`;
  messages.append(article);
  messages.scrollTop = messages.scrollHeight;
  return article;
}

messages.addEventListener("click", async (event) => {
  const button = event.target.closest(".copy-code, .copy-trace");
  if (!button) return;
  const value = button.classList.contains("copy-code")
    ? button.closest(".code-card").querySelector("code").textContent
    : button.closest(".trace").querySelector("code").textContent;
  await navigator.clipboard.writeText(value);
  const original = button.textContent;
  button.textContent = "Copied";
  setTimeout(() => { button.textContent = original; }, 1200);
});

async function submitQuestion(question) {
  addMessage("user", question);
  const pending = addMessage("assistant", "Gathering graph and source evidence");
  pending.classList.add("loading");
  send.disabled = true;
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        message: question,
        session_id: sessionId,
        repository_id: repository.value.trim() || null,
        revision: revision.value.trim() || null,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail ?? "The repository agents are unavailable.");
    sessionId = payload.session_id;
    localStorage.setItem(sessionKey, sessionId);
    pending.remove();
    addMessage("assistant", payload.answer, payload);
  } catch (error) {
    pending.remove();
    addMessage("assistant", `I couldn't complete that request. ${error.message}`);
  } finally {
    send.disabled = false;
    input.focus();
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const question = input.value.trim();
  if (!question || send.disabled) return;
  input.value = "";
  input.style.height = "auto";
  submitQuestion(question);
});

input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
});

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});

document.querySelectorAll(".prompts button").forEach((button) => {
  button.addEventListener("click", () => {
    input.value = button.textContent;
    input.focus();
  });
});

fetch("/health/ready")
  .then((response) => {
    health.className = `health ${response.ok ? "ready" : "error"}`;
    health.querySelector("span:last-child").textContent = response.ok ? "Agents ready" : "Agents unavailable";
  })
  .catch(() => {
    health.className = "health error";
    health.querySelector("span:last-child").textContent = "Gateway unavailable";
  });

// Minimal admin scaffold: config editor, remote control, health, HA entities.
// Phase 5 replaces the JSON editor with a proper tabbed Preact GUI.

import "./admin.css";

const editor = document.getElementById("config-editor") as HTMLTextAreaElement;
const configStatus = document.getElementById("config-status")!;
const healthSummary = document.getElementById("health-summary")!;
const healthTable = document.getElementById("health-table")!;
const entitiesTable = document.getElementById("entities-table")!;

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!,
  );
}

async function loadConfig(): Promise<void> {
  const response = await fetch("/api/config");
  editor.value = JSON.stringify(await response.json(), null, 2);
  configStatus.textContent = "";
}

async function saveConfig(): Promise<void> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(editor.value);
  } catch (err) {
    configStatus.textContent = `invalid JSON: ${(err as Error).message}`;
    configStatus.className = "error";
    return;
  }
  const response = await fetch("/api/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(parsed),
  });
  configStatus.textContent = response.ok ? "saved ✓" : `save failed (${response.status})`;
  configStatus.className = response.ok ? "ok" : "error";
}

async function refreshHealth(): Promise<void> {
  try {
    const response = await fetch("/api/health");
    const health = await response.json();
    healthSummary.textContent = `HA: ${health.ha} · ${health.ws_clients} client(s)`;
    healthTable.innerHTML =
      `<tr><th>collector</th><th>interval</th><th>stale</th><th>last success</th><th>last error</th></tr>` +
      (health.collectors ?? [])
        .map(
          (c: any) => `<tr class="${c.stale ? "stale" : ""}">
            <td>${escapeHtml(c.name)}</td>
            <td>${escapeHtml(c.interval)}s</td>
            <td>${c.stale ? "yes" : ""}</td>
            <td>${escapeHtml(c.last_success ?? "never")}</td>
            <td>${escapeHtml(c.last_error ?? "")}</td>
          </tr>`,
        )
        .join("");
  } catch {
    healthSummary.textContent = "backend unreachable";
  }
}

async function loadEntities(): Promise<void> {
  const response = await fetch("/api/ha/entities");
  const data = await response.json();
  entitiesTable.innerHTML =
    `<tr><th>entity_id</th><th>name</th><th>state</th></tr>` +
    (data.entities ?? [])
      .map(
        (e: any) => `<tr>
          <td><code>${escapeHtml(e.entity_id)}</code></td>
          <td>${escapeHtml(e.name)}</td>
          <td>${escapeHtml(e.state)}</td>
        </tr>`,
      )
      .join("");
  if (!data.entities?.length) {
    entitiesTable.innerHTML = `<tr><td>no entities (HA ${escapeHtml(data.status)})</td></tr>`;
  }
}

document.querySelectorAll<HTMLButtonElement>("#controls [data-action]").forEach((button) =>
  button.addEventListener("click", () =>
    fetch("/api/control", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: button.dataset.action }),
    }),
  ),
);

document.getElementById("config-save")!.addEventListener("click", saveConfig);
document.getElementById("config-reload")!.addEventListener("click", loadConfig);
document.getElementById("entities-load")!.addEventListener("click", loadEntities);

loadConfig();
refreshHealth();
setInterval(refreshHealth, 5000);

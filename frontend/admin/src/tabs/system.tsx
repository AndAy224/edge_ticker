import { signal } from "@preact/signals";
import { useEffect } from "preact/hooks";
import { config, control, displayState, health, livePayloads, loadConfig } from "../state";

type Version = { id: number; saved_at: string; current: boolean; changed: string[] };
const versions = signal<Version[]>([]);
const historyStatus = signal("");

async function loadHistory(): Promise<void> {
  try {
    versions.value = (await (await fetch("/api/config/history")).json()).versions ?? [];
  } catch {
    versions.value = [];
  }
}

async function restoreVersion(v: Version): Promise<void> {
  if (!confirm(`Restore the config saved ${new Date(v.saved_at).toLocaleString()}?`)) return;
  historyStatus.value = "restoring…";
  const res = await fetch("/api/config/restore", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: v.id }),
  });
  const reply = await res.json().catch(() => null);
  historyStatus.value = res.ok ? "restored ✓" : `not restored: ${reply?.error ?? res.status}`;
  if (res.ok) await loadConfig();
  await loadHistory();
}

/** One health row → a short state label. */
function collectorState(c: any): string {
  if (c.state && c.state !== "running") return c.detail ? `${c.state}: ${c.detail}` : c.state;
  if (c.stuck) return "stuck";
  if (c.overdue) return "overdue";
  if (c.stale) return "stale";
  if (c.degraded) return c.degraded;
  return "ok";
}

// Result of the last camera-takeover test: it can legitimately refuse (no
// cameras configured yet), and a button that silently does nothing reads broken.
const cameraTestResult = signal<string>("");

async function testCameraTakeover(): Promise<void> {
  cameraTestResult.value = "firing…";
  const res = await control("camera_alert_test");
  cameraTestResult.value = res?.error
    ? `✕ ${res.error}`
    : `✓ showing ${res?.event?.cameras?.length ?? 0} camera(s) for ${
        res?.event?.duration_seconds ?? 0
      }s`;
}

function downloadConfig(): void {
  const blob = new Blob([JSON.stringify(config.value, null, 2)], {
    type: "application/json",
  });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "edge-ticker-config.json";
  a.click();
  URL.revokeObjectURL(a.href);
}

async function restoreConfig(file: File | undefined): Promise<void> {
  if (!file) return;
  try {
    const parsed = JSON.parse(await file.text());
    if (typeof parsed !== "object" || parsed === null || !parsed.modules) {
      throw new Error("not an edge-ticker config (missing modules)");
    }
    config.value = parsed; // marks dirty — Save & apply pushes it live
  } catch (err) {
    alert(`Could not read backup: ${err}`);
  }
}

const CONTROLS = ["prev", "next", "pin", "blank", "wake", "reload"] as const;

function age(iso: string | null): string {
  if (!iso) return "never";
  const seconds = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000));
  if (seconds < 90) return `${seconds}s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m ago`;
  return `${Math.round(seconds / 3600)}h ago`;
}

export function SystemTab() {
  const h = health.value;
  const ds = displayState.value ?? {};
  const payloads = livePayloads.value ?? {};
  useEffect(() => {
    loadHistory();
  }, [config.value]); // a save anywhere adds a version

  return (
    <div class="tab">
      <section>
        <h2>Display control</h2>
        <div class="button-row">
          {CONTROLS.map((action) => (
            <button key={action} class="ghost" onClick={() => control(action)}>
              {action}
            </button>
          ))}
          <button class="ghost celebrate" onClick={() => control("celebrate_test")}>
            🎉 Test score alert
          </button>
          <button class="ghost celebrate" onClick={() => control("starship_test")}>
            🚀 Test Starship card
          </button>
          <button class="ghost celebrate" onClick={() => control("weather_alert_test")}>
            ⛈ Test weather alert
          </button>
          <button class="ghost celebrate" onClick={testCameraTakeover}>
            📷 Test camera takeover
          </button>
        </div>
        {cameraTestResult.value && <p class="hint">{cameraTestResult.value}</p>}
        <p class="hint">
          Score alert replays the last touchdown from the latest Packers game of
          last season. Starship test shows the fabricated flight-day card for
          10s, then the T−0 countdown board for 10s, then restores the display.
          Camera takeover replays the first door alert configured for one in the
          Home Assistant tab; if none is, it shows whatever cameras are picked
          there for 20s.
        </p>
      </section>

      <section>
        <h2>Live preview</h2>
        <div class="preview">
          <div class="preview-state">
            <span>
              showing: <strong>{ds.module ?? "—"}</strong>
            </span>
            {ds.pinned && <span class="badge">pinned</span>}
            {ds.blanked && <span class="badge">blanked</span>}
            {ds.overlay && <span class="badge">HA overlay</span>}
          </div>
          <table>
            <tr>
              <th>module</th>
              <th>updated</th>
              <th>tape items</th>
              <th></th>
            </tr>
            {Object.values(payloads).map((p: any) => (
              <tr key={p.module} class={p.stale ? "stale" : ""}>
                <td>{p.module}</td>
                <td>{age(p.updated_at)}</td>
                <td>{p.tape?.length ?? 0}</td>
                <td>{p.stale ? "⚠ stale" : ""}</td>
              </tr>
            ))}
          </table>
        </div>
      </section>

      <section>
        <h2>Config backup</h2>
        <p class="hint">
          Restore loads the file as a draft — review, then Save &amp; apply.
        </p>
        <div class="button-row">
          <button class="ghost" onClick={downloadConfig}>
            Download backup
          </button>
          <label class="ghost file-button">
            Restore from file…
            <input
              type="file"
              accept="application/json,.json"
              onChange={(e) => {
                restoreConfig(e.currentTarget.files?.[0]);
                e.currentTarget.value = "";
              }}
            />
          </label>
        </div>
      </section>

      <section>
        <h2>Config history</h2>
        <p class="hint">
          Every save is kept (last 20). Restoring applies that version at once —
          and is itself a save, so it can be undone the same way.
        </p>
        <table>
          <tr>
            <th>saved</th>
            <th>changed</th>
            <th></th>
          </tr>
          {versions.value.map((v) => (
            <tr key={v.id}>
              <td>{new Date(v.saved_at).toLocaleString()}</td>
              <td>
                <code>{v.changed.join(", ") || "—"}</code>
              </td>
              <td>
                {v.current ? (
                  <span class="badge">current</span>
                ) : (
                  <button class="ghost" onClick={() => restoreVersion(v)}>
                    Restore
                  </button>
                )}
              </td>
            </tr>
          ))}
        </table>
        {historyStatus.value && <p class="hint">{historyStatus.value}</p>}
      </section>

      <section>
        <h2>Collector health</h2>
        {h ? (
          <>
            {(h.problems ?? []).length > 0 && (
              <ul class="problems">
                {h.problems.map((p: string) => (
                  <li key={p}>{p}</li>
                ))}
              </ul>
            )}
            <table>
              <tr>
                <th>collector</th>
                <th>state</th>
                <th>interval</th>
                <th>last success</th>
                <th>failures</th>
                <th>last error</th>
              </tr>
              {(h.collectors ?? []).map((c: any) => {
                const state = collectorState(c);
                const bad = c.stale || c.stuck || c.overdue || c.state === "error" || c.state === "dead";
                return (
                  <tr key={c.name} class={bad ? "stale" : c.state && c.state !== "running" ? "idle" : ""}>
                    <td>{c.name}</td>
                    <td>{state}</td>
                    <td>{c.interval != null ? `${c.interval}s` : ""}</td>
                    <td>{c.state === "running" ? age(c.last_success) : ""}</td>
                    <td>
                      {c.failures_total
                        ? `${c.failures_total}${c.consecutive_failures ? ` (${c.consecutive_failures} in a row)` : ""}`
                        : ""}
                    </td>
                    <td>{c.last_error ?? ""}</td>
                  </tr>
                );
              })}
            </table>
            <p class="hint">
              Night: {h.night?.mode ?? "?"} at {h.night?.level ?? "?"}% via{" "}
              {h.night?.method_used ?? "(not applied yet)"} · dropped WS messages:{" "}
              {h.dropped_messages ?? 0}
            </p>
          </>
        ) : (
          <p class="hint">backend unreachable</p>
        )}
      </section>
    </div>
  );
}

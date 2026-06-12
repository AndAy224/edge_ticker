import { control, displayState, health, livePayloads } from "../state";

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
        </div>
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
        <h2>Collector health</h2>
        {h ? (
          <table>
            <tr>
              <th>collector</th>
              <th>interval</th>
              <th>last success</th>
              <th>last error</th>
            </tr>
            {(h.collectors ?? []).map((c: any) => (
              <tr key={c.name} class={c.stale ? "stale" : ""}>
                <td>{c.name}</td>
                <td>{c.interval}s</td>
                <td>{age(c.last_success)}</td>
                <td>{c.last_error ?? ""}</td>
              </tr>
            ))}
          </table>
        ) : (
          <p class="hint">backend unreachable</p>
        )}
      </section>
    </div>
  );
}

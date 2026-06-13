import { config, patch } from "../state";

// Modules that have a stage renderer and can be in the rotation.
const STAGE_MODULES = ["markets", "sports", "news", "weather", "airquality", "fantasy", "proxmox", "adsb", "astro"];

export function ModulesTab() {
  const cfg = config.value;
  const order: string[] = cfg.rotation?.order ?? [];
  const moduleIds = Object.keys(cfg.modules ?? {});
  const addable = STAGE_MODULES.filter(
    (id) => !order.includes(id) && cfg.modules?.[id]?.enabled !== false,
  );

  const move = (index: number, delta: number) =>
    patch((c) => {
      const o = c.rotation.order;
      const target = index + delta;
      if (target < 0 || target >= o.length) return;
      [o[index], o[target]] = [o[target], o[index]];
    });

  return (
    <div class="tab">
      <section>
        <h2>Rotation</h2>
        <label class="field">
          Stage interval (seconds)
          <input
            type="number"
            min={5}
            value={cfg.rotation?.interval_seconds ?? 25}
            onInput={(e) =>
              patch((c) => (c.rotation.interval_seconds = Number(e.currentTarget.value)))
            }
          />
        </label>
        <div class="order-list">
          {order.map((id, i) => (
            <div class="order-row" key={id}>
              <span class="order-pos">{i + 1}</span>
              <span class="order-name">{id}</span>
              <button class="ghost" onClick={() => move(i, -1)} disabled={i === 0}>
                ↑
              </button>
              <button
                class="ghost"
                onClick={() => move(i, 1)}
                disabled={i === order.length - 1}
              >
                ↓
              </button>
              <button
                class="ghost danger"
                onClick={() =>
                  patch((c) => c.rotation.order.splice(c.rotation.order.indexOf(id), 1))
                }
              >
                ✕
              </button>
            </div>
          ))}
        </div>
        {addable.length > 0 && (
          <label class="field">
            Add to rotation
            <select
              value=""
              onChange={(e) => {
                const id = e.currentTarget.value;
                if (id) patch((c) => c.rotation.order.push(id));
                e.currentTarget.value = "";
              }}
            >
              <option value="">choose module…</option>
              {addable.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </select>
          </label>
        )}
      </section>

      <section>
        <h2>Enabled modules</h2>
        <p class="hint">
          Disabled modules stop collecting and drop off the tape. proxmox and adsb
          also need their env vars in <code>.env</code> on the appliance.
        </p>
        <div class="toggle-grid">
          {moduleIds.map((id) => (
            <label class="toggle" key={id}>
              <input
                type="checkbox"
                checked={cfg.modules[id]?.enabled !== false}
                onChange={(e) =>
                  patch((c) => (c.modules[id].enabled = e.currentTarget.checked))
                }
              />
              {id}
            </label>
          ))}
        </div>
      </section>
    </div>
  );
}

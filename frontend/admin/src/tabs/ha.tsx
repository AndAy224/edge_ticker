import { signal } from "@preact/signals";
import { useEffect } from "preact/hooks";
import { config, entities, haStatus, loadEntities, patch } from "../state";

const search = signal("");

function entityList(domain: string) {
  const term = search.value.toLowerCase();
  return entities.value
    .filter((e) => e.domain === domain)
    .filter(
      (e) =>
        !term ||
        e.entity_id.toLowerCase().includes(term) ||
        String(e.name).toLowerCase().includes(term),
    );
}

function MultiPicker({
  title,
  domain,
  selected,
  max,
  onToggle,
}: {
  title: string;
  domain: string;
  selected: string[];
  max: number;
  onToggle: (entityId: string, add: boolean) => void;
}) {
  const available = entityList(domain);
  return (
    <section>
      <h2>
        {title}{" "}
        <span class="count">
          {selected.length}/{max}
        </span>
      </h2>
      <div class="entity-list">
        {available.map((e) => {
          const checked = selected.includes(e.entity_id);
          return (
            <label class={`entity ${checked ? "selected" : ""}`} key={e.entity_id}>
              <input
                type="checkbox"
                checked={checked}
                disabled={!checked && selected.length >= max}
                onChange={(event) => onToggle(e.entity_id, event.currentTarget.checked)}
              />
              <span class="entity-name">{e.name}</span>
              <code>{e.entity_id}</code>
            </label>
          );
        })}
        {!available.length && <p class="hint">no {domain} entities</p>}
      </div>
    </section>
  );
}

function SinglePicker({
  title,
  domain,
  selected,
  onSelect,
}: {
  title: string;
  domain: string;
  selected: string | null;
  onSelect: (entityId: string | null) => void;
}) {
  const available = entityList(domain);
  return (
    <section>
      <h2>{title}</h2>
      <div class="entity-list">
        {available.map((e) => {
          const checked = selected === e.entity_id;
          return (
            <label class={`entity ${checked ? "selected" : ""}`} key={e.entity_id}>
              <input
                type="checkbox"
                checked={checked}
                onChange={() => onSelect(checked ? null : e.entity_id)}
              />
              <span class="entity-name">{e.name}</span>
              <code>{e.entity_id}</code>
            </label>
          );
        })}
        {!available.length && <p class="hint">no {domain} entities</p>}
      </div>
    </section>
  );
}

const DOOR_CLASSES = new Set(["door", "garage_door", "garage", "window", "opening", "gate"]);

function alertDefaults(e: any): { state: string; text: string } {
  if (e.domain === "lock") return { state: "unlocked", text: `${e.name} unlocked` };
  if (e.domain === "cover") return { state: "open", text: `${e.name} open` };
  return { state: "on", text: `${e.name} open` };
}

function DoorAlertPicker({ ha }: { ha: any }) {
  const term = search.value.toLowerCase();
  const doors = entities.value
    .filter(
      (e: any) =>
        e.domain === "lock" ||
        e.domain === "cover" ||
        (e.domain === "binary_sensor" && DOOR_CLASSES.has(e.device_class)),
    )
    .filter(
      (e: any) =>
        !term ||
        e.entity_id.toLowerCase().includes(term) ||
        String(e.name).toLowerCase().includes(term),
    );
  const alerts: any[] = ha.alerts ?? [];
  return (
    <section>
      <h2>
        Door &amp; lock alerts{" "}
        <span class="count">{alerts.length ? `${alerts.length} active` : ""}</span>
      </h2>
      <p class="hint">
        Checked doors pop a banner on the display when they open or close, and
        keep an alert item on the ticker tape while open/unlocked.
      </p>
      <div class="entity-list">
        {doors.map((e: any) => {
          const checked = alerts.some((a) => a?.entity === e.entity_id);
          return (
            <label class={`entity ${checked ? "selected" : ""}`} key={e.entity_id}>
              <input
                type="checkbox"
                checked={checked}
                onChange={(event) => {
                  const add = event.currentTarget.checked;
                  patch((c) => {
                    const list: any[] = c.ha.alerts ?? (c.ha.alerts = []);
                    if (add) {
                      list.push({ entity: e.entity_id, ...alertDefaults(e) });
                    } else {
                      const i = list.findIndex((a) => a?.entity === e.entity_id);
                      if (i >= 0) list.splice(i, 1);
                    }
                  });
                }}
              />
              <span class="entity-name">{e.name}</span>
              <code>{e.entity_id}</code>
            </label>
          );
        })}
        {!doors.length && (
          <p class="hint">no door, lock, or cover entities found</p>
        )}
      </div>
    </section>
  );
}

export function HATab() {
  useEffect(() => {
    loadEntities();
  }, []);

  const ha = config.value.ha ?? {};
  if (haStatus.value !== "connected" && !entities.value.length) {
    return (
      <div class="tab">
        <p class="hint">
          Home Assistant is <strong>{haStatus.value}</strong>
          {haStatus.value === "unconfigured"
            ? " — set HA_URL and HA_TOKEN in .env on the appliance, then restart the backend."
            : " — check that HA is reachable from the appliance."}
        </p>
        <button class="ghost" onClick={loadEntities}>
          Retry
        </button>
      </div>
    );
  }

  const toggleIn = (key: "scenes" | "lights") => (entityId: string, add: boolean) =>
    patch((c) => {
      const list: string[] = c.ha[key] ?? (c.ha[key] = []);
      if (add) list.push(entityId);
      else list.splice(list.indexOf(entityId), 1);
    });

  return (
    <div class="tab">
      <label class="field">
        Filter entities
        <input
          value={search.value}
          placeholder="search by name or entity_id"
          onInput={(e) => (search.value = e.currentTarget.value)}
        />
      </label>
      <MultiPicker
        title="Scenes column"
        domain="scene"
        selected={ha.scenes ?? []}
        max={4}
        onToggle={toggleIn("scenes")}
      />
      <MultiPicker
        title="Lights grid"
        domain="light"
        selected={ha.lights ?? []}
        max={8}
        onToggle={toggleIn("lights")}
      />
      <SinglePicker
        title="Climate"
        domain="climate"
        selected={ha.climate ?? null}
        onSelect={(id) => patch((c) => (c.ha.climate = id))}
      />
      <SinglePicker
        title="Media player"
        domain="media_player"
        selected={ha.media ?? null}
        onSelect={(id) => patch((c) => (c.ha.media = id))}
      />

      <DoorAlertPicker ha={ha} />

      <section>
        <h2>Custom alerts (advanced)</h2>
        <p class="hint">
          While an entity is in the given state, an alert-colored item scrolls
          on the ticker tape and a banner pops on the transition. Doors and
          locks are easier to manage with the checklist above.
        </p>
        <div class="rows">
          {(ha.alerts ?? []).map((alert: any, i: number) => (
            <div class="row" key={i}>
              <input
                class="wide"
                list="ha-alert-entities"
                value={alert.entity}
                placeholder="entity_id"
                onInput={(e) =>
                  patch((c) => (c.ha.alerts[i].entity = e.currentTarget.value))
                }
              />
              <input
                value={alert.state}
                placeholder="state (e.g. on, open)"
                onInput={(e) =>
                  patch((c) => (c.ha.alerts[i].state = e.currentTarget.value))
                }
              />
              <input
                class="wide"
                value={alert.text ?? ""}
                placeholder="tape text (optional)"
                onInput={(e) =>
                  patch((c) => (c.ha.alerts[i].text = e.currentTarget.value))
                }
              />
              <button
                class="ghost danger"
                onClick={() => patch((c) => c.ha.alerts.splice(i, 1))}
              >
                ✕
              </button>
            </div>
          ))}
          <button
            class="ghost"
            onClick={() =>
              patch((c) => {
                (c.ha.alerts ?? (c.ha.alerts = [])).push({
                  entity: "",
                  state: "on",
                  text: "",
                });
              })
            }
          >
            + add alert
          </button>
        </div>
        <datalist id="ha-alert-entities">
          {entities.value.map((e) => (
            <option key={e.entity_id} value={e.entity_id}>
              {String(e.name)}
            </option>
          ))}
        </datalist>
      </section>
    </div>
  );
}

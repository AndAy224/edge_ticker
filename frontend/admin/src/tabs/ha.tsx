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
    </div>
  );
}

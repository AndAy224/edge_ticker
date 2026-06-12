import { signal } from "@preact/signals";
import { config, dirty, discardConfig, health, saveConfig, saveStatus } from "./state";
import { HATab } from "./tabs/ha";
import { ModulesTab } from "./tabs/modules";
import { ScheduleTab } from "./tabs/schedule";
import { SourcesTab } from "./tabs/sources";
import { SystemTab } from "./tabs/system";

const TABS = [
  { id: "modules", label: "Modules", component: ModulesTab },
  { id: "sources", label: "Sources", component: SourcesTab },
  { id: "ha", label: "HA Mapping", component: HATab },
  { id: "schedule", label: "Schedule", component: ScheduleTab },
  { id: "system", label: "System", component: SystemTab },
];

const activeTab = signal("modules");

export function App() {
  if (!config.value) {
    return <div class="loading">loading config…</div>;
  }
  const Active = TABS.find((t) => t.id === activeTab.value)!.component;
  const h = health.value;
  return (
    <div class="layout">
      <header>
        <h1>edge-ticker</h1>
        <span class="health-summary">
          {h
            ? `HA: ${h.ha} · ${h.ws_clients} client(s)` +
              (h.collectors?.some((c: any) => c.stale) ? " · ⚠ stale data" : "")
            : "backend unreachable"}
        </span>
        <div class="save-bar">
          <span class="save-status">{saveStatus.value}</span>
          {dirty.value && (
            <button class="ghost" onClick={discardConfig}>
              Discard
            </button>
          )}
          <button class="primary" disabled={!dirty.value} onClick={saveConfig}>
            Save & apply
          </button>
        </div>
      </header>
      <nav>
        {TABS.map((tab) => (
          <button
            key={tab.id}
            class={activeTab.value === tab.id ? "active" : ""}
            onClick={() => (activeTab.value = tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </nav>
      <main>
        <Active />
      </main>
    </div>
  );
}

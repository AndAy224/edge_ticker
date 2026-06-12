import { DEFAULT_LAYOUT, DEFAULT_THEME, LAYOUTS, THEMES } from "../../../shared/themes";
import { config, patch } from "../state";

const SWATCH_VARS = ["--bg", "--panel-raised", "--text", "--accent", "--up", "--down"];

function setAppearance(key: "theme" | "layout", value: string): void {
  patch((c) => {
    c.appearance = { ...(c.appearance ?? {}), [key]: value };
  });
}

export function AppearanceTab() {
  const appearance = config.value.appearance ?? {};
  const activeTheme = appearance.theme ?? DEFAULT_THEME;
  const activeLayout = appearance.layout ?? DEFAULT_LAYOUT;
  return (
    <div class="tab">
      <section>
        <h2>Theme</h2>
        <p class="hint">
          Color scheme for the display. Applies live on save — no kiosk reload
          needed.
        </p>
        <div class="theme-grid">
          {Object.entries(THEMES).map(([id, theme]) => (
            <button
              key={id}
              class={`theme-card ${activeTheme === id ? "selected" : ""}`}
              style={{ background: theme.vars["--panel"] }}
              onClick={() => setAppearance("theme", id)}
            >
              <span class="theme-name" style={{ color: theme.vars["--text"] }}>
                {theme.label}
              </span>
              <span class="swatch-row">
                {SWATCH_VARS.map((v) => (
                  <span key={v} class="swatch" style={{ background: theme.vars[v] }} />
                ))}
              </span>
            </button>
          ))}
        </div>
      </section>

      <section>
        <h2>Layout</h2>
        <p class="hint">Where the clock & weather rail sits, if anywhere.</p>
        {Object.entries(LAYOUTS).map(([id, layout]) => (
          <label key={id} class="field radio-field">
            <input
              type="radio"
              name="layout"
              checked={activeLayout === id}
              onChange={() => setAppearance("layout", id)}
            />
            <span>
              <strong>{layout.label}</strong>
              <span class="radio-desc"> — {layout.description}</span>
            </span>
          </label>
        ))}
      </section>
    </div>
  );
}

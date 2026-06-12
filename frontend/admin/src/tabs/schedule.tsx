import { config, patch } from "../state";

function TimeField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label class="field">
      {label}
      <input
        type="time"
        value={value}
        onInput={(e) => onChange(e.currentTarget.value)}
      />
    </label>
  );
}

export function ScheduleTab() {
  const night = config.value.night ?? {};
  return (
    <div class="tab">
      <section>
        <h2>Night dimming</h2>
        <p class="hint">
          DDC/CI sends <code>ddcutil setvcp 10</code> to the panel; if that fails
          (or method is software), the display applies a dim overlay instead.
        </p>
        <TimeField
          label="Dim at"
          value={night.dim_at ?? "23:00"}
          onChange={(v) => patch((c) => (c.night.dim_at = v))}
        />
        <TimeField
          label="Wake at"
          value={night.wake_at ?? "07:00"}
          onChange={(v) => patch((c) => (c.night.wake_at = v))}
        />
        <label class="field">
          Night brightness ({night.dim_level ?? 10}%)
          <input
            type="range"
            min={0}
            max={100}
            value={night.dim_level ?? 10}
            onInput={(e) => patch((c) => (c.night.dim_level = Number(e.currentTarget.value)))}
          />
        </label>
        <label class="field">
          Day brightness ({night.day_level ?? 100}%)
          <input
            type="range"
            min={10}
            max={100}
            value={night.day_level ?? 100}
            onInput={(e) => patch((c) => (c.night.day_level = Number(e.currentTarget.value)))}
          />
        </label>
        <label class="field">
          Method
          <select
            value={night.method ?? "ddc"}
            onChange={(e) => patch((c) => (c.night.method = e.currentTarget.value))}
          >
            <option value="ddc">DDC/CI (hardware, with software fallback)</option>
            <option value="software">Software overlay only</option>
          </select>
        </label>
      </section>

      <section>
        <h2>Nightly page reload</h2>
        <p class="hint">Guards against Chromium memory creep on the appliance.</p>
        <TimeField
          label="Reload at"
          value={night.nightly_reload_at ?? "04:00"}
          onChange={(v) => patch((c) => (c.night.nightly_reload_at = v))}
        />
      </section>
    </div>
  );
}

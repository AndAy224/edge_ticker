import { useState } from "preact/hooks";
import { config, health, patch } from "../state";

function ago(iso: string | undefined): string {
  if (!iso) return "";
  const minutes = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 60000));
  return minutes < 1 ? "just now" : `${minutes}m ago`;
}

function FetchStatus({ entry, what }: { entry: any; what: string }) {
  if (!entry) return <div class="fetch-status unknown">○ not fetched yet</div>;
  if (!entry.ok) {
    return <div class="fetch-status error">● {entry.error ?? "failed"}</div>;
  }
  return (
    <div class="fetch-status ok">
      ● {entry.items ?? entry.games ?? 0} {what} · {ago(entry.checked_at)}
      {entry.cached ? " · cached (304)" : ""}
    </div>
  );
}

function collectorStatus(name: string): any {
  return (health.value?.collectors ?? []).find((c: any) => c.name === name);
}

function ChipEditor({
  items,
  placeholder,
  onAdd,
  onRemove,
}: {
  items: string[];
  placeholder: string;
  onAdd: (value: string) => void;
  onRemove: (index: number) => void;
}) {
  const [draft, setDraft] = useState("");
  const add = () => {
    const value = draft.trim();
    if (value) {
      onAdd(value);
      setDraft("");
    }
  };
  return (
    <div class="chips">
      {items.map((item, i) => (
        <span class="chip" key={`${item}-${i}`}>
          {item}
          <button class="chip-x" onClick={() => onRemove(i)}>
            ✕
          </button>
        </span>
      ))}
      <input
        value={draft}
        placeholder={placeholder}
        onInput={(e) => setDraft(e.currentTarget.value)}
        onKeyDown={(e) => e.key === "Enter" && add()}
      />
      <button class="ghost" onClick={add}>
        Add
      </button>
    </div>
  );
}

function NumberField({
  label,
  value,
  onChange,
  step = 1,
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
  step?: number;
}) {
  return (
    <label class="field">
      {label}
      <input
        type="number"
        step={step}
        value={value}
        onInput={(e) => onChange(Number(e.currentTarget.value))}
      />
    </label>
  );
}

export function SourcesTab() {
  const cfg = config.value;
  const markets = cfg.modules?.markets ?? {};
  const sports = cfg.modules?.sports ?? {};
  const news = cfg.modules?.news ?? {};
  const weather = cfg.modules?.weather ?? {};
  const adsb = cfg.modules?.adsb ?? {};
  const newsHealth = collectorStatus("news");
  const sportsHealth = collectorStatus("sports");
  const feedStatus = (url: string) =>
    (newsHealth?.feeds ?? []).find((f: any) => f.url === url);
  const leagueStatus = (league: any) =>
    (sportsHealth?.leagues ?? []).find(
      (l: any) => l.sport === league.sport && l.league === league.league,
    );

  return (
    <div class="tab">
      <section>
        <h2>Markets — symbols</h2>
        <ChipEditor
          items={markets.symbols ?? []}
          placeholder="e.g. AMD or ETH-USD"
          onAdd={(value) =>
            patch((c) => c.modules.markets.symbols.push(value.toUpperCase()))
          }
          onRemove={(i) => patch((c) => c.modules.markets.symbols.splice(i, 1))}
        />
        <NumberField
          label="Poll interval (s)"
          value={markets.poll_seconds ?? 60}
          onChange={(v) => patch((c) => (c.modules.markets.poll_seconds = v))}
        />
      </section>

      <section>
        <h2>Sports — followed teams</h2>
        <p class="hint">Substring match against team names; followed games pin first.</p>
        <ChipEditor
          items={sports.followed_teams ?? []}
          placeholder="e.g. Rays"
          onAdd={(value) => patch((c) => c.modules.sports.followed_teams.push(value))}
          onRemove={(i) => patch((c) => c.modules.sports.followed_teams.splice(i, 1))}
        />
        <label class="toggle">
          <input
            type="checkbox"
            checked={sports.auto_feature === true}
            onChange={(e) =>
              patch((c) => (c.modules.sports.auto_feature = e.currentTarget.checked))
            }
          />
          Jump to sports and pin while a followed team's game is live
        </label>
        <h2>Sports — leagues</h2>
        <div class="rows">
          {(sports.leagues ?? []).map((league: any, i: number) => (
            <div class="row-group" key={i}>
              <div class="row">
                <input
                  value={league.sport}
                  placeholder="sport (e.g. baseball)"
                  onInput={(e) =>
                    patch((c) => (c.modules.sports.leagues[i].sport = e.currentTarget.value))
                  }
                />
                <input
                  value={league.league}
                  placeholder="league (e.g. mlb)"
                  onInput={(e) =>
                    patch((c) => (c.modules.sports.leagues[i].league = e.currentTarget.value))
                  }
                />
                <button
                  class="ghost danger"
                  onClick={() => patch((c) => c.modules.sports.leagues.splice(i, 1))}
                >
                  ✕
                </button>
              </div>
              <FetchStatus entry={leagueStatus(league)} what="games" />
            </div>
          ))}
          <button
            class="ghost"
            onClick={() =>
              patch((c) => c.modules.sports.leagues.push({ sport: "", league: "" }))
            }
          >
            + add league
          </button>
        </div>
      </section>

      <section>
        <h2>News — feeds</h2>
        <div class="rows">
          {(news.feeds ?? []).map((feed: any, i: number) => (
            <div class="row-group" key={i}>
              <div class="row">
                <input
                  value={feed.name}
                  placeholder="name"
                  onInput={(e) =>
                    patch((c) => (c.modules.news.feeds[i].name = e.currentTarget.value))
                  }
                />
                <input
                  class="wide"
                  value={feed.url}
                  placeholder="https://…/rss.xml"
                  onInput={(e) =>
                    patch((c) => (c.modules.news.feeds[i].url = e.currentTarget.value))
                  }
                />
                <button
                  class="ghost danger"
                  onClick={() => patch((c) => c.modules.news.feeds.splice(i, 1))}
                >
                  ✕
                </button>
              </div>
              <FetchStatus entry={feedStatus(feed.url)} what="items" />
            </div>
          ))}
          <button
            class="ghost"
            onClick={() => patch((c) => c.modules.news.feeds.push({ name: "", url: "" }))}
          >
            + add feed
          </button>
        </div>
        <NumberField
          label="Keep newest"
          value={news.keep ?? 30}
          onChange={(v) => patch((c) => (c.modules.news.keep = v))}
        />
      </section>

      <section>
        <h2>Weather / location</h2>
        <p class="hint">Also used as the receiver position for adsb and astro.</p>
        <label class="field">
          Location name
          <input
            value={weather.location_name ?? ""}
            onInput={(e) =>
              patch((c) => (c.modules.weather.location_name = e.currentTarget.value))
            }
          />
        </label>
        <NumberField
          label="Latitude"
          step={0.0001}
          value={weather.latitude ?? 0}
          onChange={(v) => patch((c) => (c.modules.weather.latitude = v))}
        />
        <NumberField
          label="Longitude"
          step={0.0001}
          value={weather.longitude ?? 0}
          onChange={(v) => patch((c) => (c.modules.weather.longitude = v))}
        />
        <NumberField
          label="ADS-B radius (km)"
          value={adsb.radius_km ?? 40}
          onChange={(v) => patch((c) => (c.modules.adsb.radius_km = v))}
        />
      </section>
    </div>
  );
}

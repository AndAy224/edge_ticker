// Inline SVG sport icons. The appliance has no emoji fonts (snap Chromium
// bundles none, fc-list is empty), so glyphs like ⚾ render as tofu — these
// stroke-based icons inherit currentColor and theme correctly instead.

const wrap = (body: string): string =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ` +
  `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;

export const SPORT_ICONS: Record<string, string> = {
  baseball: wrap(
    `<circle cx="12" cy="12" r="9"/>` +
      `<path d="M5.8 5.5c2.4 3.7 2.4 9.3 0 13"/>` +
      `<path d="M18.2 5.5c-2.4 3.7-2.4 9.3 0 13"/>`,
  ),
  football: wrap(
    `<ellipse cx="12" cy="12" rx="10" ry="6" transform="rotate(-35 12 12)"/>` +
      `<path d="M9.2 14.8l5.6-5.6"/>` +
      `<path d="M10 11.4l1.3 1.3M12.7 8.7L14 10"/>`,
  ),
  hockey: wrap(
    `<path d="M5 3.5l6.2 11.3"/>` +
      `<path d="M19 3.5L12.8 14.8"/>` +
      `<ellipse cx="12" cy="19" rx="4.5" ry="2"/>`,
  ),
  basketball: wrap(
    `<circle cx="12" cy="12" r="9"/>` +
      `<path d="M12 3v18M3 12h18"/>` +
      `<path d="M6 5.4c3.2 3.5 3.2 9.7 0 13.2M18 5.4c-3.2 3.5-3.2 9.7 0 13.2"/>`,
  ),
  soccer: wrap(
    `<circle cx="12" cy="12" r="9"/>` +
      `<path d="M12 8.2l3.6 2.6-1.4 4.2h-4.4l-1.4-4.2z"/>` +
      `<path d="M12 8.2V4.6M15.6 10.8l3.4-1.1M14.2 15l2.1 2.9M9.8 15l-2.1 2.9M8.4 10.8L5 9.7"/>`,
  ),
  generic: wrap(
    `<path d="M8 4h8v5a4 4 0 0 1-8 0z"/>` +
      `<path d="M8 5H5a3 3 0 0 0 3 4M16 5h3a3 3 0 0 1-3 4"/>` +
      `<path d="M12 13v3M9 19h6"/>`,
  ),
};

// ESPN league → sport, for payloads that predate the backend's per-game
// `sport` field (or leagues added in the admin without restarting).
const LEAGUE_SPORT: Record<string, string> = {
  MLB: "baseball",
  NFL: "football",
  NHL: "hockey",
  NBA: "basketball",
  WNBA: "basketball",
  MLS: "soccer",
  NCAAF: "football",
  NCAAM: "basketball",
  NCAAW: "basketball",
};

export function sportIcon(sport?: string | null, league?: string | null): string {
  const key = sport ?? (league ? LEAGUE_SPORT[league.toUpperCase()] : undefined);
  // Tape items may carry non-sport icon keys (e.g. "warning") — resolve those
  // too instead of silently falling back to the generic trophy.
  return SPORT_ICONS[key ?? ""] ?? WEATHER_ICONS[key ?? ""] ?? SPORT_ICONS.generic;
}

export const WEATHER_ICONS: Record<string, string> = {
  sun: wrap(
    `<circle cx="12" cy="12" r="4.5"/>` +
      `<path d="M12 3v2.5M12 18.5V21M3 12h2.5M18.5 12H21M5.6 5.6l1.8 1.8M16.6 16.6l1.8 1.8M18.4 5.6l-1.8 1.8M7.4 16.6l-1.8 1.8"/>`,
  ),
  cloud: wrap(
    `<path d="M7 18a4 4 0 0 1-.6-7.95A5.5 5.5 0 0 1 17.2 9.1 3.8 3.8 0 0 1 17 18z"/>`,
  ),
  fog: wrap(
    `<path d="M7 14a4 4 0 0 1-.6-7.95A5.5 5.5 0 0 1 17.2 5.1 3.8 3.8 0 0 1 17 14"/>` +
      `<path d="M5 17.5h14M7.5 21h9"/>`,
  ),
  rain: wrap(
    `<path d="M7 15a4 4 0 0 1-.6-7.95A5.5 5.5 0 0 1 17.2 6.1 3.8 3.8 0 0 1 17 15z"/>` +
      `<path d="M8.5 17.5v3M12 18.5v3M15.5 17.5v3"/>`,
  ),
  snow: wrap(
    `<path d="M7 15a4 4 0 0 1-.6-7.95A5.5 5.5 0 0 1 17.2 6.1 3.8 3.8 0 0 1 17 15z"/>` +
      `<path d="M8.5 18.2v.01M12 20v.01M15.5 18.2v.01M10.2 20.8v.01M13.8 17.4v.01"/>`,
  ),
  storm: wrap(
    `<path d="M7 14a4 4 0 0 1-.6-7.95A5.5 5.5 0 0 1 17.2 5.1 3.8 3.8 0 0 1 17 14z"/>` +
      `<path d="M12.5 14.5L10 18.5h4L11.5 22.5"/>`,
  ),
  // Severe-weather alert triangle (tape items + full-screen takeover).
  warning: wrap(
    `<path d="M10.4 4.3 2.9 17.6A1.8 1.8 0 0 0 4.5 20.3h15a1.8 1.8 0 0 0 1.6-2.7L13.6 4.3a1.8 1.8 0 0 0-3.2 0z"/>` +
      `<path d="M12 9.5v4.5"/>` +
      `<path d="M12 17.2v.01"/>`,
  ),
};

/** WMO weather-code groups → icon key (Open-Meteo `weather_code`). */
export function weatherIcon(code?: number | null): string {
  if (code == null) return WEATHER_ICONS.sun;
  if (code <= 1) return WEATHER_ICONS.sun;
  if (code <= 3) return WEATHER_ICONS.cloud;
  if (code <= 48) return WEATHER_ICONS.fog;
  if (code <= 67) return WEATHER_ICONS.rain;
  if (code <= 77) return WEATHER_ICONS.snow;
  if (code <= 82) return WEATHER_ICONS.rain;
  if (code <= 86) return WEATHER_ICONS.snow;
  return WEATHER_ICONS.storm;
}

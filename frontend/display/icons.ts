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
  return SPORT_ICONS[key ?? ""] ?? SPORT_ICONS.generic;
}

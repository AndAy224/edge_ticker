// Theme and layout catalog, shared by the display (applies the CSS variables)
// and the admin GUI (renders the selector with swatch previews). The display's
// styles.css :root block holds the midnight values as the no-config fallback;
// keep the two in sync if midnight changes.

export interface Theme {
  label: string;
  vars: Record<string, string>;
}

export const THEMES: Record<string, Theme> = {
  midnight: {
    label: "Midnight",
    vars: {
      "--bg": "#07090c",
      "--panel": "#10141a",
      "--panel-raised": "#161c24",
      "--text": "#e8ecf1",
      "--text-dim": "#8a94a3",
      "--up": "#2ecc71",
      "--down": "#ff4d5e",
      "--alert": "#ffb020",
      "--accent": "#4da3ff",
      "--line": "#1d242e",
      "--muted": "#2a313c",
      "--overlay-bg": "rgba(7, 9, 12, 0.97)",
    },
  },
  daylight: {
    label: "Daylight",
    vars: {
      "--bg": "#e9edf2",
      "--panel": "#f7f9fb",
      "--panel-raised": "#ffffff",
      "--text": "#15191f",
      "--text-dim": "#5b6573",
      "--up": "#1e9e54",
      "--down": "#d92638",
      "--alert": "#c77700",
      "--accent": "#1d6fd1",
      "--line": "#d4dae2",
      "--muted": "#c3cad4",
      "--overlay-bg": "rgba(233, 237, 242, 0.97)",
    },
  },
  amber: {
    label: "Amber CRT",
    vars: {
      "--bg": "#0a0700",
      "--panel": "#140e02",
      "--panel-raised": "#1d1503",
      "--text": "#ffb000",
      "--text-dim": "#8a6a1f",
      "--up": "#9ee37d",
      "--down": "#ff6b4d",
      "--alert": "#ffd24d",
      "--accent": "#ffb000",
      "--line": "#2a2006",
      "--muted": "#3a2c08",
      "--overlay-bg": "rgba(10, 7, 0, 0.97)",
    },
  },
  frost: {
    label: "Frost",
    vars: {
      "--bg": "#2e3440",
      "--panel": "#353c4a",
      "--panel-raised": "#3b4252",
      "--text": "#eceff4",
      "--text-dim": "#9aa5b8",
      "--up": "#a3be8c",
      "--down": "#bf616a",
      "--alert": "#ebcb8b",
      "--accent": "#88c0d0",
      "--line": "#434c5e",
      "--muted": "#4c566a",
      "--overlay-bg": "rgba(46, 52, 64, 0.97)",
    },
  },
};

export interface LayoutDef {
  label: string;
  description: string;
  /** How many rotation modules show side by side on the stage. */
  panes: 1 | 2 | 3;
}

export const LAYOUTS: Record<string, LayoutDef> = {
  "rail-left": {
    label: "Rail left",
    description: "Clock & weather rail on the left, stage on the right.",
    panes: 1,
  },
  "rail-right": {
    label: "Rail right",
    description: "Stage on the left, clock & weather rail on the right.",
    panes: 1,
  },
  "full-stage": {
    label: "Full stage",
    description: "No rail — one module uses the whole width. Weather stays on the tape.",
    panes: 1,
  },
  banner: {
    label: "Banner",
    description: "Slim clock & weather strip across the top, full-width stage below.",
    panes: 1,
  },
  focus: {
    label: "Focus 70/30",
    description: "Current module large, next module previews in a narrower right pane.",
    panes: 2,
  },
  split: {
    label: "Split",
    description: "Rail plus two modules side by side.",
    panes: 2,
  },
  mosaic: {
    label: "Mosaic",
    description: "Three modules side by side — no rail, compact clock chip.",
    panes: 3,
  },
  zen: {
    label: "Zen",
    description: "Giant clock & weather only; data lives on the tape.",
    panes: 1,
  },
};

export const DEFAULT_THEME = "midnight";
export const DEFAULT_LAYOUT = "rail-left";

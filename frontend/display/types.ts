export interface TapeItem {
  text: string;
  accent: "neutral" | "up" | "down" | "alert";
  priority: number;
  icon?: string | null;
}

export interface ModulePayload {
  module: string;
  updated_at: string;
  stale: boolean;
  stage: any;
  tape: TapeItem[];
}

export interface HAMapping {
  scenes: string[];
  lights: string[];
  climate: string | null;
  media: string | null;
  alerts?: { entity: string; state: string; text?: string }[];
}

export interface HAEntityState {
  state: string;
  attributes: Record<string, any>;
}

export interface Config {
  rotation?: { interval_seconds?: number; order?: string[] };
  modules?: Record<string, { enabled?: boolean; [key: string]: any }>;
  ha?: Partial<HAMapping>;
  night?: Record<string, any>;
  appearance?: { theme?: string; layout?: string };
}

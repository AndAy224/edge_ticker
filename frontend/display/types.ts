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
  fans: string[];
  climate: string | null;
  media: string | null;
  cameras?: string[];
  // The takeover fields are consumed by the backend (it builds the camera_alert
  // event); they live here because admin and display share the config shape.
  alerts?: {
    entity: string;
    state: string;
    text?: string;
    takeover?: boolean;
    cameras?: string[];
    duration_seconds?: number;
    severity?: "info" | "alert" | "critical";
    transport?: "stream" | "snapshot";
    cooldown_seconds?: number;
    takeover_title?: string;
  }[];
}

export interface CameraAlertEvent {
  id: string;
  key: string;
  source: string;
  title: string;
  subtitle?: string | null;
  severity: "info" | "alert" | "critical";
  transport?: "stream" | "snapshot";
  cameras: { id: string; label: string }[];
  duration_seconds: number;
  wake?: boolean;
  issued_at?: string;
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

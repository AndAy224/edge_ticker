export interface ModuleRenderer {
  id: string;
  renderStage(el: HTMLElement, data: any): void;
  // Tap-to-expand: main.ts resolves the tapped [data-detail] key to an item
  // via getDetailItem, then renders it with renderDetail.
  renderDetail?(el: HTMLElement, item: unknown): void;
  getDetailItem?(stage: any, key: string): unknown;
}

const renderers = new Map<string, ModuleRenderer>();

export function register(renderer: ModuleRenderer): void {
  renderers.set(renderer.id, renderer);
}

export function getRenderer(id: string): ModuleRenderer | undefined {
  return renderers.get(id);
}

export function hasRenderer(id: string): boolean {
  return renderers.has(id);
}

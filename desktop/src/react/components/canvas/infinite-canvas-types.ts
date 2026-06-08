export type InfiniteCanvasViewId = 'canvas' | 'smart-canvas' | 'api-settings' | 'comfyui-settings' | 'tools';

export interface InfiniteCanvasPageInfo {
  id: string;
  label: string;
  path: string;
  kind: 'primary' | 'tool';
}

export interface InfiniteCanvasRuntimeBridgeOptions {
  root: HTMLElement;
  onNavigate: (path: string) => void;
}

export interface InfiniteCanvasRuntimeBridgeCleanup {
  (): void;
}

declare global {
  interface Window {
    __openHanakoInfiniteCanvasNavigate?: (path: string) => void;
  }
}

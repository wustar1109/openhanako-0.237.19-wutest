// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest';

const hanaFetchMock = vi.fn();

vi.mock('../../hooks/use-hana-fetch', () => ({
  hanaFetch: (...args: unknown[]) => hanaFetchMock(...args),
  hanaUrl: (path: string) => `http://hana.local${path}`,
}));

vi.mock('../../stores', () => ({
  useStore: {
    getState: () => ({ serverPort: '1234', serverToken: 'token' }),
  },
}));

vi.mock('../../services/server-connection', () => ({
  requireServerConnection: () => ({ wsUrl: 'ws://hana.local', token: 'token' }),
  buildConnectionWsUrl: (_connection: unknown, path: string) => `ws://hana.local${path}?token=token`,
}));

import {
  infiniteCanvasAssetUrl,
  installInfiniteCanvasRuntimeBridge,
  toInfiniteCanvasProxyUrl,
} from '../../components/canvas/infinite-canvas-bridge';

class FakeWebSocket extends EventTarget {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  url: string;

  constructor(url: string | URL) {
    super();
    this.url = String(url);
  }

  close(): void {
    this.dispatchEvent(new Event('close'));
  }
}

describe('Infinite Canvas bridge', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    hanaFetchMock.mockReset();
  });

  it('rewrites legacy HTTP and asset paths to the scoped proxy', () => {
    expect(toInfiniteCanvasProxyUrl('/api/config')).toBe('/api/infinite-canvas/api/config');
    expect(toInfiniteCanvasProxyUrl('/static/canvas.html')).toBe('/api/infinite-canvas/static/canvas.html');
    expect(toInfiniteCanvasProxyUrl('/output/a.png')).toBe('/api/infinite-canvas/output/a.png');
    expect(infiniteCanvasAssetUrl('/assets/input/a.png')).toBe('http://hana.local/api/infinite-canvas/assets/input/a.png');
  });

  it('patches fetch and WebSocket only while mounted', async () => {
    const root = document.createElement('div');
    document.body.appendChild(root);
    const originalFetch = vi.fn();
    vi.stubGlobal('fetch', originalFetch);
    vi.stubGlobal('WebSocket', FakeWebSocket);
    hanaFetchMock.mockResolvedValue(new Response('{}'));

    const cleanup = installInfiniteCanvasRuntimeBridge({
      root,
      onNavigate: vi.fn(),
    });

    await fetch('/api/config');
    expect(hanaFetchMock).toHaveBeenCalledWith('/api/infinite-canvas/api/config', expect.any(Object));
    const socket = new WebSocket('/ws/stats') as unknown as FakeWebSocket;
    expect(socket.url).toBe('ws://hana.local/ws/infinite-canvas/stats?token=token');

    cleanup();

    await fetch('/api/config');
    expect(originalFetch).toHaveBeenCalledWith('/api/config');
  });

  it('does not rewrite authenticated Hana fetches while mounted', async () => {
    const root = document.createElement('div');
    document.body.appendChild(root);
    const originalFetch = vi.fn().mockResolvedValue(new Response('{}'));
    vi.stubGlobal('fetch', originalFetch);
    vi.stubGlobal('WebSocket', FakeWebSocket);

    const cleanup = installInfiniteCanvasRuntimeBridge({
      root,
      onNavigate: vi.fn(),
    });

    await fetch('http://hana.local/api/channels', {
      headers: { Authorization: 'Bearer token' },
    });

    expect(hanaFetchMock).not.toHaveBeenCalled();
    expect(originalFetch).toHaveBeenCalledWith('http://hana.local/api/channels', expect.objectContaining({
      headers: { Authorization: 'Bearer token' },
    }));

    cleanup();
  });

  it('intercepts static page navigation inside the host root', () => {
    const root = document.createElement('div');
    const link = document.createElement('a');
    link.href = '/static/smart-canvas.html?id=1';
    root.appendChild(link);
    document.body.appendChild(root);
    const onNavigate = vi.fn();
    const cleanup = installInfiniteCanvasRuntimeBridge({ root, onNavigate });

    link.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));

    expect(onNavigate).toHaveBeenCalledWith('/static/smart-canvas.html?id=1');
    cleanup();
  });
});

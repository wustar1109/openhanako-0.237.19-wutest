import { hanaFetch, hanaUrl } from '../../hooks/use-hana-fetch';
import { useStore } from '../../stores';
import {
  buildConnectionWsUrl,
  requireServerConnection,
} from '../../services/server-connection';
import type {
  InfiniteCanvasRuntimeBridgeCleanup,
  InfiniteCanvasRuntimeBridgeOptions,
} from './infinite-canvas-types';

const HTTP_PROXY_PREFIX = '/api/infinite-canvas';
const WS_PROXY_PREFIX = '/ws/infinite-canvas';
const LONG_REQUEST_TIMEOUT = 30 * 60 * 1000;
const REWRITABLE_ROOTS = ['/api/', '/static/', '/output/', '/assets/'];
const REWRITABLE_ATTRS = ['src', 'href', 'poster', 'data-url'];
let bridgeFetchBypassDepth = 0;

function headersHaveAuthorization(headers: HeadersInit | undefined | null): boolean {
  if (!headers) return false;
  if (headers instanceof Headers) return headers.has('authorization');
  if (Array.isArray(headers)) return headers.some(([key]) => key.toLowerCase() === 'authorization');
  return Object.keys(headers).some(key => key.toLowerCase() === 'authorization');
}

function shouldBypassPatchedFetch(input: RequestInfo | URL, init?: RequestInit): boolean {
  if (bridgeFetchBypassDepth > 0) return true;
  if (headersHaveAuthorization(init?.headers)) return true;
  if (typeof Request !== 'undefined' && input instanceof Request) {
    return headersHaveAuthorization(input.headers);
  }
  return false;
}

function isLoopbackHost(hostname: string): boolean {
  const host = hostname.toLowerCase();
  return host === '127.0.0.1' || host === 'localhost' || host === '::1' || host === '[::1]';
}

function isIgnorableUrl(raw: string): boolean {
  return /^(?:about:|blob:|data:|mailto:|tel:|javascript:|#)/i.test(raw.trim());
}

function absoluteUrlShouldRewrite(url: URL): boolean {
  if (url.protocol === 'file:') return true;
  if (typeof window !== 'undefined' && url.origin === window.location.origin) return true;
  return isLoopbackHost(url.hostname);
}

function pathWithSearchAndHash(url: URL): string {
  return `${url.pathname}${url.search}${url.hash}`;
}

function coerceLocalPath(raw: string): string | null {
  const trimmed = raw.trim();
  if (!trimmed || isIgnorableUrl(trimmed)) return null;
  if (trimmed.startsWith(HTTP_PROXY_PREFIX) || trimmed.startsWith(WS_PROXY_PREFIX)) return trimmed;
  if (trimmed.startsWith('/')) return trimmed;

  try {
    const url = new URL(trimmed, window.location.href);
    if (!absoluteUrlShouldRewrite(url)) return null;
    return pathWithSearchAndHash(url);
  } catch {
    return null;
  }
}

export function toInfiniteCanvasProxyUrl(path: string): string {
  const localPath = coerceLocalPath(path);
  if (!localPath) return path;
  if (localPath.startsWith(HTTP_PROXY_PREFIX)) return localPath;
  if (localPath.startsWith('/api/')) return `${HTTP_PROXY_PREFIX}${localPath}`;
  if (localPath.startsWith('/static/')) return `${HTTP_PROXY_PREFIX}${localPath}`;
  if (localPath.startsWith('/output/')) return `${HTTP_PROXY_PREFIX}${localPath}`;
  if (localPath.startsWith('/assets/')) return `${HTTP_PROXY_PREFIX}${localPath}`;
  return localPath;
}

export async function infiniteCanvasFetch(path: string, init?: RequestInit): Promise<Response> {
  bridgeFetchBypassDepth += 1;
  try {
    return await hanaFetch(toInfiniteCanvasProxyUrl(path), {
      ...(init || {}),
      timeout: LONG_REQUEST_TIMEOUT,
      throwOnHttpError: false,
    } as RequestInit & { timeout: number; throwOnHttpError: boolean });
  } finally {
    bridgeFetchBypassDepth -= 1;
  }
}

export function infiniteCanvasAssetUrl(path: string): string {
  return hanaUrl(toInfiniteCanvasProxyUrl(path));
}

function toInfiniteCanvasWsPath(path: string): string {
  const trimmed = String(path || '').trim();
  if (!trimmed) return `${WS_PROXY_PREFIX}/stats`;
  if (trimmed.startsWith(WS_PROXY_PREFIX)) return trimmed;
  try {
    const url = new URL(trimmed, window.location.href.replace(/^http/i, 'ws'));
    if (url.protocol === 'ws:' || url.protocol === 'wss:' || absoluteUrlShouldRewrite(url)) {
      if (url.pathname.startsWith('/ws/')) {
        return `${WS_PROXY_PREFIX}${url.pathname.slice('/ws'.length)}${url.search}`;
      }
    }
  } catch {
    // Fall through to path handling.
  }
  if (trimmed.startsWith('/ws/')) return `${WS_PROXY_PREFIX}${trimmed.slice('/ws'.length)}`;
  if (trimmed === '/ws') return WS_PROXY_PREFIX;
  return trimmed;
}

export function infiniteCanvasWsUrl(path: string): string {
  const connection = requireServerConnection(
    useStore.getState(),
    `infiniteCanvasWsUrl ${path}: server connection not ready`,
  );
  return buildConnectionWsUrl(connection, toInfiniteCanvasWsPath(path));
}

function shouldRewriteHttp(raw: string): boolean {
  const localPath = coerceLocalPath(raw);
  return !!localPath && (
    localPath.startsWith(HTTP_PROXY_PREFIX)
    || REWRITABLE_ROOTS.some(prefix => localPath.startsWith(prefix))
  );
}

function rewriteUrlForDom(raw: string, attrName = ''): string {
  const localPath = coerceLocalPath(raw);
  if (!localPath) return raw;
  if (attrName === 'href' && /^\/static\/[^?#]+\.html(?:[?#].*)?$/i.test(localPath)) return localPath;
  if (!shouldRewriteHttp(localPath)) return raw;
  return infiniteCanvasAssetUrl(localPath);
}

export function rewriteInfiniteCanvasCss(css: string, basePath = ''): string {
  return css.replace(
    /url\(\s*(['"]?)([^'")]+)\1\s*\)/g,
    (match, quote: string, url: string) => {
      const trimmed = url.trim();
      if (!trimmed || isIgnorableUrl(trimmed) || /^https?:\/\//i.test(trimmed)) return match;
      if (trimmed.startsWith('/static/') || trimmed.startsWith('/output/') || trimmed.startsWith('/assets/')) {
        return `url(${quote}${infiniteCanvasAssetUrl(trimmed)}${quote})`;
      }
      if (basePath) {
        try {
          const resolved = new URL(trimmed, `http://openhanako.local${basePath}`);
          if (resolved.pathname.startsWith('/static/') || resolved.pathname.startsWith('/output/') || resolved.pathname.startsWith('/assets/')) {
            return `url(${quote}${infiniteCanvasAssetUrl(pathWithSearchAndHash(resolved))}${quote})`;
          }
        } catch {
          return match;
        }
      }
      return match;
    },
  );
}

export function rewriteInfiniteCanvasScriptText(scriptText: string): string {
  let out = scriptText;
  out = out.replace(
    /\bwindow\.location\.href\s*=\s*([^;\n]+);?/g,
    'window.__openHanakoInfiniteCanvasNavigate?.($1);',
  );
  out = out.replace(
    /(^|[^\w.])location\.href\s*=\s*([^;\n]+);?/g,
    '$1window.__openHanakoInfiniteCanvasNavigate?.($2);',
  );
  out = out.replace(
    /\bwindow\.location\.assign\(([^)]*)\);?/g,
    'window.__openHanakoInfiniteCanvasNavigate?.($1);',
  );
  out = out.replace(
    /(^|[^\w.])location\.assign\(([^)]*)\);?/g,
    '$1window.__openHanakoInfiniteCanvasNavigate?.($2);',
  );
  out = out.replace(
    /from\s+(['"])three\1/g,
    `from "${infiniteCanvasAssetUrl('/static/vendor/js/three-0.160.0.module.js')}"`,
  );
  out = out.replace(
    /import\(\s*(['"])three\1\s*\)/g,
    `import("${infiniteCanvasAssetUrl('/static/vendor/js/three-0.160.0.module.js')}")`,
  );
  return out;
}

function rewriteElementUrls(element: Element): void {
  for (const attr of REWRITABLE_ATTRS) {
    const value = element.getAttribute(attr);
    if (!value) continue;
    const next = rewriteUrlForDom(value, attr);
    if (next !== value) element.setAttribute(attr, next);
  }

  const style = element.getAttribute('style');
  if (style && /url\(/i.test(style)) {
    const next = rewriteInfiniteCanvasCss(style);
    if (next !== style) element.setAttribute('style', next);
  }
}

function rewriteTreeUrls(root: ParentNode): void {
  if (root instanceof Element) rewriteElementUrls(root);
  root.querySelectorAll?.('*').forEach(rewriteElementUrls);
}

function staticHtmlNavigationTarget(raw: string): string | null {
  const localPath = coerceLocalPath(raw);
  if (!localPath) return null;
  return /^\/static\/[^?#]+\.html(?:[?#].*)?$/i.test(localPath) ? localPath : null;
}

export function installInfiniteCanvasRuntimeBridge(
  options: InfiniteCanvasRuntimeBridgeOptions,
): InfiniteCanvasRuntimeBridgeCleanup {
  const { root, onNavigate } = options;
  const originalFetch = window.fetch.bind(window);
  const originalXhrOpen = window.XMLHttpRequest.prototype.open;
  const OriginalWebSocket = window.WebSocket;
  const originalSetTimeout = window.setTimeout.bind(window);
  const originalClearTimeout = window.clearTimeout.bind(window);
  const originalSetInterval = window.setInterval.bind(window);
  const originalClearInterval = window.clearInterval.bind(window);
  const originalRequestAnimationFrame = window.requestAnimationFrame?.bind(window);
  const originalCancelAnimationFrame = window.cancelAnimationFrame?.bind(window);
  const originalWindowAdd = window.addEventListener.bind(window);
  const originalWindowRemove = window.removeEventListener.bind(window);
  const originalDocumentAdd = document.addEventListener.bind(document);
  const originalDocumentRemove = document.removeEventListener.bind(document);
  const OriginalResizeObserver = window.ResizeObserver;
  const originalCreateObjectUrl = URL.createObjectURL.bind(URL);
  const originalRevokeObjectUrl = URL.revokeObjectURL.bind(URL);
  const previousNavigate = window.__openHanakoInfiniteCanvasNavigate;
  const beforeWindowKeys = new Set(Object.getOwnPropertyNames(window));

  const sockets = new Set<WebSocket>();
  const timeouts = new Set<number>();
  const intervals = new Set<number>();
  const frames = new Set<number>();
  const objectUrls = new Set<string>();
  const observers = new Set<ResizeObserver>();
  const windowListeners: Array<[string, EventListenerOrEventListenerObject, boolean | AddEventListenerOptions | undefined]> = [];
  const documentListeners: Array<[string, EventListenerOrEventListenerObject, boolean | AddEventListenerOptions | undefined]> = [];

  window.__openHanakoInfiniteCanvasNavigate = (path: string) => {
    const target = staticHtmlNavigationTarget(String(path || ''));
    if (target) onNavigate(target);
  };

  window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
    if (shouldBypassPatchedFetch(input, init)) return originalFetch(input, init);
    const rawUrl = input instanceof Request ? input.url : String(input);
    if (!shouldRewriteHttp(rawUrl)) return originalFetch(input, init);
    const proxyPath = toInfiniteCanvasProxyUrl(rawUrl);
    const requestInit: RequestInit = input instanceof Request
      ? {
          method: input.method,
          headers: input.headers,
          body: input.method === 'GET' || input.method === 'HEAD' ? undefined : input.clone().body,
          credentials: input.credentials,
          cache: input.cache,
          mode: input.mode,
          redirect: input.redirect,
          referrer: input.referrer,
          referrerPolicy: input.referrerPolicy,
          integrity: input.integrity,
          keepalive: input.keepalive,
          signal: init?.signal || input.signal,
          ...init,
        }
      : { ...(init || {}) };
    return infiniteCanvasFetch(proxyPath, requestInit);
  }) as typeof window.fetch;

  window.XMLHttpRequest.prototype.open = function patchedOpen(
    method: string,
    url: string | URL,
    async?: boolean,
    username?: string | null,
    password?: string | null,
  ) {
    const raw = String(url);
    const next = shouldRewriteHttp(raw) ? infiniteCanvasAssetUrl(raw) : raw;
    return originalXhrOpen.call(this, method, next, async ?? true, username, password);
  };

  function PatchedWebSocket(url: string | URL, protocols?: string | string[]): WebSocket {
    const raw = String(url);
    const next = raw.includes('/ws/') || raw.startsWith('/ws')
      ? infiniteCanvasWsUrl(raw)
      : raw;
    const socket = protocols === undefined
      ? new OriginalWebSocket(next)
      : new OriginalWebSocket(next, protocols);
    sockets.add(socket);
    socket.addEventListener('close', () => sockets.delete(socket), { once: true });
    return socket;
  }
  PatchedWebSocket.prototype = OriginalWebSocket.prototype;
  Object.assign(PatchedWebSocket, {
    CONNECTING: OriginalWebSocket.CONNECTING,
    OPEN: OriginalWebSocket.OPEN,
    CLOSING: OriginalWebSocket.CLOSING,
    CLOSED: OriginalWebSocket.CLOSED,
  });
  window.WebSocket = PatchedWebSocket as unknown as typeof WebSocket;

  window.setTimeout = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => {
    const id = originalSetTimeout(handler, timeout, ...args);
    timeouts.add(id);
    return id;
  }) as typeof window.setTimeout;
  window.clearTimeout = ((id?: number) => {
    if (typeof id === 'number') timeouts.delete(id);
    return originalClearTimeout(id);
  }) as typeof window.clearTimeout;
  window.setInterval = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => {
    const id = originalSetInterval(handler, timeout, ...args);
    intervals.add(id);
    return id;
  }) as typeof window.setInterval;
  window.clearInterval = ((id?: number) => {
    if (typeof id === 'number') intervals.delete(id);
    return originalClearInterval(id);
  }) as typeof window.clearInterval;

  if (originalRequestAnimationFrame && originalCancelAnimationFrame) {
    window.requestAnimationFrame = ((callback: FrameRequestCallback) => {
      const id = originalRequestAnimationFrame((time) => {
        frames.delete(id);
        callback(time);
      });
      frames.add(id);
      return id;
    }) as typeof window.requestAnimationFrame;
    window.cancelAnimationFrame = ((id: number) => {
      frames.delete(id);
      return originalCancelAnimationFrame(id);
    }) as typeof window.cancelAnimationFrame;
  }

  window.addEventListener = ((type: string, listener: EventListenerOrEventListenerObject, opts?: boolean | AddEventListenerOptions) => {
    windowListeners.push([type, listener, opts]);
    return originalWindowAdd(type, listener, opts);
  }) as typeof window.addEventListener;
  window.removeEventListener = ((type: string, listener: EventListenerOrEventListenerObject, opts?: boolean | EventListenerOptions) => {
    for (let i = windowListeners.length - 1; i >= 0; i--) {
      const item = windowListeners[i];
      if (item[0] === type && item[1] === listener) windowListeners.splice(i, 1);
    }
    return originalWindowRemove(type, listener, opts);
  }) as typeof window.removeEventListener;
  document.addEventListener = ((type: string, listener: EventListenerOrEventListenerObject, opts?: boolean | AddEventListenerOptions) => {
    documentListeners.push([type, listener, opts]);
    return originalDocumentAdd(type, listener, opts);
  }) as typeof document.addEventListener;
  document.removeEventListener = ((type: string, listener: EventListenerOrEventListenerObject, opts?: boolean | EventListenerOptions) => {
    for (let i = documentListeners.length - 1; i >= 0; i--) {
      const item = documentListeners[i];
      if (item[0] === type && item[1] === listener) documentListeners.splice(i, 1);
    }
    return originalDocumentRemove(type, listener, opts);
  }) as typeof document.addEventListener;

  if (OriginalResizeObserver) {
    window.ResizeObserver = class TrackedResizeObserver extends OriginalResizeObserver {
      constructor(callback: ResizeObserverCallback) {
        super(callback);
        observers.add(this);
      }

      disconnect(): void {
        observers.delete(this);
        super.disconnect();
      }
    };
  }

  URL.createObjectURL = ((obj: Blob | MediaSource) => {
    const url = originalCreateObjectUrl(obj);
    objectUrls.add(url);
    return url;
  }) as typeof URL.createObjectURL;
  URL.revokeObjectURL = ((url: string) => {
    objectUrls.delete(url);
    return originalRevokeObjectUrl(url);
  }) as typeof URL.revokeObjectURL;

  const onRootClick = (event: MouseEvent) => {
    const target = event.target instanceof Element ? event.target.closest('a[href]') : null;
    const href = target?.getAttribute('href');
    if (!href) return;
    const navTarget = staticHtmlNavigationTarget(href);
    if (!navTarget) return;
    event.preventDefault();
    onNavigate(navTarget);
  };
  root.addEventListener('click', onRootClick, true);

  const mutationObserver = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      if (mutation.type === 'attributes' && mutation.target instanceof Element) {
        rewriteElementUrls(mutation.target);
      }
      mutation.addedNodes.forEach((node) => {
        if (node instanceof Element) rewriteTreeUrls(node);
      });
    }
  });
  mutationObserver.observe(root, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: REWRITABLE_ATTRS,
  });
  rewriteTreeUrls(root);

  return () => {
    mutationObserver.disconnect();
    root.removeEventListener('click', onRootClick, true);
    window.fetch = originalFetch;
    window.XMLHttpRequest.prototype.open = originalXhrOpen;
    window.WebSocket = OriginalWebSocket;
    window.setTimeout = originalSetTimeout;
    window.clearTimeout = originalClearTimeout;
    window.setInterval = originalSetInterval;
    window.clearInterval = originalClearInterval;
    if (originalRequestAnimationFrame) window.requestAnimationFrame = originalRequestAnimationFrame;
    if (originalCancelAnimationFrame) window.cancelAnimationFrame = originalCancelAnimationFrame;
    window.addEventListener = originalWindowAdd;
    window.removeEventListener = originalWindowRemove;
    document.addEventListener = originalDocumentAdd;
    document.removeEventListener = originalDocumentRemove;
    if (OriginalResizeObserver) window.ResizeObserver = OriginalResizeObserver;
    URL.createObjectURL = originalCreateObjectUrl;
    URL.revokeObjectURL = originalRevokeObjectUrl;
    if (previousNavigate) window.__openHanakoInfiniteCanvasNavigate = previousNavigate;
    else delete window.__openHanakoInfiniteCanvasNavigate;

    for (const socket of sockets) {
      try { socket.close(1001, 'Infinite Canvas page unloaded'); } catch {
        // Ignore teardown failures from already-closed sockets.
      }
    }
    for (const id of timeouts) originalClearTimeout(id);
    for (const id of intervals) originalClearInterval(id);
    if (originalCancelAnimationFrame) {
      for (const id of frames) originalCancelAnimationFrame(id);
    }
    for (const observer of observers) {
      try { observer.disconnect(); } catch {
        // Ignore teardown failures from disconnected observers.
      }
    }
    for (const [type, listener, opts] of windowListeners) {
      try { originalWindowRemove(type, listener, opts); } catch {
        // Ignore listener cleanup failures after the bridge has been restored.
      }
    }
    for (const [type, listener, opts] of documentListeners) {
      try { originalDocumentRemove(type, listener, opts); } catch {
        // Ignore listener cleanup failures after the bridge has been restored.
      }
    }
    for (const url of objectUrls) {
      try { originalRevokeObjectUrl(url); } catch {
        // Ignore cleanup failures for stale object URLs.
      }
    }

    for (const key of Object.getOwnPropertyNames(window)) {
      if (beforeWindowKeys.has(key)) continue;
      try {
        const descriptor = Object.getOwnPropertyDescriptor(window, key);
        if (!descriptor || descriptor.configurable) delete (window as unknown as Record<string, unknown>)[key];
      } catch {
        // Ignore non-configurable globals left behind by legacy canvas scripts.
      }
    }
  };
}

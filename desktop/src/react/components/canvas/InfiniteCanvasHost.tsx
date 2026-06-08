import { useEffect, useRef, useState } from 'react';
import {
  infiniteCanvasAssetUrl,
  infiniteCanvasFetch,
  installInfiniteCanvasRuntimeBridge,
  rewriteInfiniteCanvasCss,
  rewriteInfiniteCanvasScriptText,
} from './infinite-canvas-bridge';
import { useStore } from '../../stores';
import { hasServerConnection } from '../../services/server-connection';
import styles from './CanvasPage.module.css';

declare function t(key: string, vars?: Record<string, string | number>): string;

interface InfiniteCanvasHostProps {
  pagePath: string;
  onNavigate: (path: string) => void;
}

type LoadState =
  | { status: 'loading'; message: string }
  | { status: 'ready'; message: string }
  | { status: 'error'; message: string };

function cloneBodyWithoutScripts(source: Document, root: HTMLElement): HTMLScriptElement[] {
  const scripts: HTMLScriptElement[] = [];
  for (const child of Array.from(source.body.childNodes)) {
    if (child instanceof HTMLScriptElement) {
      scripts.push(child);
      continue;
    }
    const clone = child.cloneNode(true);
    if (clone instanceof Element) {
      clone.querySelectorAll('script').forEach(script => {
        scripts.push(script as HTMLScriptElement);
        script.remove();
      });
    }
    root.appendChild(clone);
  }
  return scripts;
}

function normalizeScriptType(script: HTMLScriptElement): string {
  return (script.getAttribute('type') || '').trim().toLowerCase();
}

function rewriteImportMap(scriptText: string): string {
  try {
    const data = JSON.parse(scriptText);
    if (data?.imports && typeof data.imports === 'object') {
      for (const [key, value] of Object.entries(data.imports)) {
        if (typeof value === 'string' && value.startsWith('/static/')) {
          data.imports[key] = infiniteCanvasAssetUrl(value);
        }
      }
    }
    return JSON.stringify(data);
  } catch {
    return scriptText;
  }
}

async function scriptText(script: HTMLScriptElement): Promise<string> {
  const src = script.getAttribute('src');
  if (!src) return script.textContent || '';
  const res = await infiniteCanvasFetch(src);
  if (!res.ok) throw new Error(`Failed to load script ${src}: ${res.status}`);
  return res.text();
}

function appendExecutableScript({
  host,
  text,
  type,
  injectedNodes,
  blobUrls,
}: {
  host: HTMLElement;
  text: string;
  type: string;
  injectedNodes: HTMLElement[];
  blobUrls: string[];
}): Promise<void> {
  return new Promise((resolve, reject) => {
    // eslint-disable-next-line no-restricted-syntax -- The DOM host must inject legacy script nodes from Infinite-Canvas HTML.
    const element = document.createElement('script');
    element.dataset.openhanakoInfiniteCanvas = 'script';

    if (type === 'module') {
      element.type = 'module';
      const blobUrl = URL.createObjectURL(new Blob([rewriteInfiniteCanvasScriptText(text)], { type: 'text/javascript' }));
      blobUrls.push(blobUrl);
      element.src = blobUrl;
      element.onload = () => resolve();
      element.onerror = () => reject(new Error('Infinite-Canvas module script failed'));
      injectedNodes.push(element);
      host.appendChild(element);
      return;
    }

    if (type === 'importmap') {
      element.type = 'importmap';
      element.textContent = rewriteImportMap(text);
      injectedNodes.push(element);
      document.head.appendChild(element);
      resolve();
      return;
    }

    if (type && type !== 'text/javascript' && type !== 'application/javascript') {
      resolve();
      return;
    }

    element.textContent = rewriteInfiniteCanvasScriptText(text);
    injectedNodes.push(element);
    host.appendChild(element);
    resolve();
  });
}

async function injectStyles(doc: Document, injectedNodes: HTMLElement[]): Promise<void> {
  for (const style of Array.from(doc.querySelectorAll('style'))) {
    // eslint-disable-next-line no-restricted-syntax -- The DOM host must inject legacy style nodes from Infinite-Canvas HTML.
    const element = document.createElement('style');
    element.dataset.openhanakoInfiniteCanvas = 'style';
    element.textContent = rewriteInfiniteCanvasCss(style.textContent || '');
    injectedNodes.push(element);
    document.head.appendChild(element);
  }

  for (const link of Array.from(doc.querySelectorAll('link[rel~="stylesheet"]'))) {
    const href = link.getAttribute('href');
    if (!href) continue;
    const res = await infiniteCanvasFetch(href);
    if (!res.ok) throw new Error(`Failed to load stylesheet ${href}: ${res.status}`);
    // eslint-disable-next-line no-restricted-syntax -- External Infinite-Canvas stylesheets are scoped and cleaned up by the DOM host.
    const element = document.createElement('style');
    element.dataset.openhanakoInfiniteCanvas = 'style';
    element.textContent = rewriteInfiniteCanvasCss(await res.text(), href);
    injectedNodes.push(element);
    document.head.appendChild(element);
  }
}

export function InfiniteCanvasHost({ pagePath, onNavigate }: InfiniteCanvasHostProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const serverReady = useStore(hasServerConnection);
  const [state, setState] = useState<LoadState>({
    status: 'loading',
    message: t('canvas.loading'),
  });

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const hostEl = host;

    let cancelled = false;
    const injectedNodes: HTMLElement[] = [];
    const blobUrls: string[] = [];

    if (!serverReady) {
      hostEl.innerHTML = '';
      setState({ status: 'loading', message: t('canvas.loading') });
      return;
    }

    const cleanupBridge = installInfiniteCanvasRuntimeBridge({
      root: hostEl,
      onNavigate,
    });

    async function load() {
      hostEl.innerHTML = '';
      setState({ status: 'loading', message: t('canvas.loading') });

      try {
        const res = await infiniteCanvasFetch(pagePath);
        if (cancelled) return;
        if (res.status === 503) {
          setState({ status: 'loading', message: t('canvas.serviceStarting') });
          return;
        }
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);

        const html = await res.text();
        if (cancelled) return;
        const doc = new DOMParser().parseFromString(html, 'text/html');

        await injectStyles(doc, injectedNodes);
        if (cancelled) return;

        const headScripts = Array.from(doc.head.querySelectorAll('script'));
        const bodyScripts = cloneBodyWithoutScripts(doc, hostEl);
        for (const script of [...headScripts, ...bodyScripts]) {
          const type = normalizeScriptType(script);
          const text = await scriptText(script);
          if (cancelled) return;
          await appendExecutableScript({
            host: hostEl,
            text,
            type,
            injectedNodes,
            blobUrls,
          });
        }

        if (!cancelled) setState({ status: 'ready', message: '' });
      } catch (err) {
        if (!cancelled) {
          const message = err instanceof Error ? err.message : String(err);
          setState({ status: 'error', message: `${t('canvas.serviceError')}: ${message}` });
        }
      }
    }

    void load();

    return () => {
      cancelled = true;
      cleanupBridge();
      hostEl.innerHTML = '';
      for (const node of injectedNodes) node.remove();
      for (const url of blobUrls) {
        try { URL.revokeObjectURL(url); } catch {
          // Ignore cleanup failures for stale blob URLs.
        }
      }
    };
  }, [pagePath, onNavigate, serverReady]);

  return (
    <div className={styles.hostShell}>
      {state.status !== 'ready' && (
        <div className={state.status === 'error' ? styles.hostError : styles.hostStatus}>
          {state.message}
        </div>
      )}
      <div ref={hostRef} className={styles.hostRoot} />
    </div>
  );
}

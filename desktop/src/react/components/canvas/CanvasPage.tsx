import { useCallback, useEffect, useState } from 'react';
import { hanaFetch } from '../../hooks/use-hana-fetch';
import { useStore } from '../../stores';
import { hasServerConnection } from '../../services/server-connection';
import { InfiniteCanvasHost } from './InfiniteCanvasHost';
import type { InfiniteCanvasPageInfo, InfiniteCanvasViewId } from './infinite-canvas-types';
import styles from './CanvasPage.module.css';

declare function t(key: string, vars?: Record<string, string | number>): string;

const PRIMARY_PAGES: InfiniteCanvasPageInfo[] = [
  { id: 'canvas', label: 'canvas.classic', path: '/static/canvas.html', kind: 'primary' },
  { id: 'smart-canvas', label: 'canvas.smart', path: '/static/smart-canvas.html', kind: 'primary' },
  { id: 'api-settings', label: 'canvas.apiSettings', path: '/static/api-settings.html', kind: 'primary' },
  { id: 'comfyui-settings', label: 'canvas.comfyuiSettings', path: '/static/comfyui-settings.html', kind: 'primary' },
];

const KNOWN_TOOL_LABELS: Record<string, string> = {
  'zimage.html': 'Z-Image',
  'enhance.html': 'Enhance',
  'klein.html': 'Klein',
  'angle.html': 'Angle',
  'online.html': 'Online',
  'gpt-chat.html': 'GPT Chat',
};

const FALLBACK_TOOL_PAGES = Object.entries(KNOWN_TOOL_LABELS).map(([file, label]) => ({
  id: file.replace(/\.html$/i, ''),
  label,
  path: `/static/${file}`,
  kind: 'tool' as const,
}));

function normalizePagePath(path: string): string {
  try {
    const url = new URL(path, 'http://openhanako.local');
    return `${url.pathname}${url.search}${url.hash}`;
  } catch {
    return path;
  }
}

function pageIdFromPath(path: string): string {
  const clean = normalizePagePath(path).split(/[?#]/)[0];
  return clean.replace(/^\/static\//, '').replace(/\.html$/i, '') || 'canvas';
}

function pageInfoForStaticFile(file: string): InfiniteCanvasPageInfo | null {
  if (!/\.html$/i.test(file) || file === 'index.html') return null;
  const path = `/static/${file}`;
  if (PRIMARY_PAGES.some(page => page.path === path)) return null;
  return {
    id: file.replace(/\.html$/i, ''),
    label: KNOWN_TOOL_LABELS[file] || file.replace(/\.html$/i, ''),
    path,
    kind: 'tool',
  };
}

export function CanvasPage() {
  const [pagePath, setPagePath] = useState('/static/canvas.html');
  const [toolPages, setToolPages] = useState<InfiniteCanvasPageInfo[]>(FALLBACK_TOOL_PAGES);
  const serverReady = useStore(hasServerConnection);

  useEffect(() => {
    if (!serverReady) return;
    let cancelled = false;
    hanaFetch('/api/infinite-canvas/openhanako/static-pages', {
      throwOnHttpError: false,
    } as RequestInit & { throwOnHttpError: boolean })
      .then(res => res.ok ? res.json() : null)
      .then(data => {
        if (cancelled || !Array.isArray(data?.pages)) return;
        const pages = data.pages
          .map((file: unknown) => typeof file === 'string' ? pageInfoForStaticFile(file) : null)
          .filter(Boolean) as InfiniteCanvasPageInfo[];
        if (pages.length > 0) setToolPages(pages);
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [serverReady]);

  const currentId = pageIdFromPath(pagePath);
  const view: InfiniteCanvasViewId = PRIMARY_PAGES.some(page => page.id === currentId)
    ? currentId as InfiniteCanvasViewId
    : 'tools';

  const navigate = useCallback((path: string) => {
    const normalized = normalizePagePath(path);
    if (!/^\/static\/[^?#]+\.html(?:[?#].*)?$/i.test(normalized)) return;
    setPagePath(normalized);
  }, []);

  const choosePrimary = useCallback((path: string) => {
    setPagePath(path);
  }, []);

  return (
    <section className={styles.page}>
      <div className={styles.toolbar}>
        <div className={styles.primaryTabs} role="tablist" aria-label={t('canvas.tab')}>
          {PRIMARY_PAGES.map(page => (
            <button
              key={page.id}
              type="button"
              role="tab"
              aria-selected={page.path === pagePath.split(/[?#]/)[0]}
              className={`${styles.navButton}${page.path === pagePath.split(/[?#]/)[0] ? ` ${styles.navButtonActive}` : ''}`}
              onClick={() => choosePrimary(page.path)}
            >
              {t(page.label)}
            </button>
          ))}
        </div>
        <label className={styles.toolSelectWrap}>
          <span>{t('canvas.tools')}</span>
          <select
            value={view === 'tools' ? pagePath.split(/[?#]/)[0] : ''}
            onChange={(event) => {
              if (event.target.value) choosePrimary(event.target.value);
            }}
          >
            <option value="">{t('canvas.tools')}</option>
            {toolPages.map(page => (
              <option key={page.path} value={page.path}>{page.label}</option>
            ))}
          </select>
        </label>
      </div>
      <InfiniteCanvasHost key={pagePath} pagePath={pagePath} onNavigate={navigate} />
    </section>
  );
}

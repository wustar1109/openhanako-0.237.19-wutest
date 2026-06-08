import type { PlatformApi } from '../types';

const noopUnsubscribe = () => {};

export function installMobilePlatform(): void {
  if (typeof window === 'undefined') return;
  document.documentElement.setAttribute('data-platform', 'web');
  if (window.platform) return;

  const api: Partial<PlatformApi> = {
    getServerPort: async () => window.location.port || '',
    getServerToken: async () => '',
    openSettings: () => {},
    openBrowserViewer: () => {},
    selectFolder: async () => null,
    selectFiles: async () => [],
    selectSkill: async () => null,
    readFile: async () => null,
    writeFile: async () => false,
    writeFileBinary: async () => false,
    copyFile: async () => false,
    readFileSnapshot: async () => null,
    writeFileIfUnchanged: async () => ({ ok: false }),
    watchFile: async () => false,
    unwatchFile: async () => false,
    onFileChanged: () => {},
    watchWorkspace: async () => false,
    unwatchWorkspace: async () => false,
    onWorkspaceChanged: () => noopUnsubscribe,
    readFileBase64: async () => null,
    getFileUrl: (value: string) => browserSafeUrl(value),
    readDocxHtml: async () => null,
    readXlsxHtml: async () => null,
    spawnViewer: async () => null,
    onViewerClosed: () => noopUnsubscribe,
    openFolder: () => {},
    openFile: (value: string) => {
      const url = browserSafeUrl(value);
      if (url) window.open(url, '_blank', 'noopener,noreferrer');
    },
    openExternal: (url: string) => window.open(url, '_blank', 'noopener,noreferrer'),
    showInFinder: () => {},
    trashItem: async () => false,
    settingsChanged: () => {},
    syncWindowTheme: () => {},
    onSettingsChanged: () => noopUnsubscribe,
    onOpenSettingsModal: () => noopUnsubscribe,
    onSwitchTab: () => noopUnsubscribe,
    onServerRestarted: () => noopUnsubscribe,
    getFilePath: () => null,
    appReady: () => {},
    getPlatform: async () => 'web',
    updateBrowserViewer: () => {},
    onBrowserUpdate: () => noopUnsubscribe,
    closeBrowserViewer: () => {},
    closeBrowser: () => {},
    browserGoBack: () => {},
    browserGoForward: () => {},
    browserReload: () => {},
    showNotification: () => {},
    getAppVersion: async () => '',
  };

  window.platform = api as PlatformApi;
}

function browserSafeUrl(value: string): string {
  if (!value) return '';
  if (/^(https?:|data:|blob:)/i.test(value)) return value;
  if (value.startsWith('/api/')) return value;
  return '';
}

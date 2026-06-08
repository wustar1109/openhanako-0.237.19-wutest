import React from 'react';
import { createRoot } from 'react-dom/client';
import { installMobilePlatform } from './react/mobile/mobile-platform';
import './react/mobile/mobile-entry.css';
import './react/mobile/MobileApp.css';

if (!window.t) {
  window.t = ((key: string) => key) as typeof window.t;
}

installMobilePlatform();

const root = document.getElementById('root');
if (!root) throw new Error('mobile root not found');

void import('./react/mobile/MobileApp').then(({ MobileApp }) => {
  createRoot(root).render(
    <React.StrictMode>
      <MobileApp />
    </React.StrictMode>,
  );
}).catch((err) => {
  console.error('[mobile] failed to boot renderer:', err);
  root.textContent = 'Hana Mobile 启动失败';
});

if ('serviceWorker' in navigator && window.isSecureContext) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('./sw.js').catch((err) => {
      console.warn('[mobile] service worker registration failed:', err);
    });
  });
}

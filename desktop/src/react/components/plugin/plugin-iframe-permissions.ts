export const PLUGIN_IFRAME_ALLOW = 'fullscreen; clipboard-read; clipboard-write';

export const PLUGIN_PAGE_IFRAME_SANDBOX = [
  'allow-scripts',
  'allow-forms',
  'allow-popups',
  'allow-popups-to-escape-sandbox',
  'allow-same-origin',
  'allow-modals',
  'allow-downloads',
].join(' ');

export const PLUGIN_CARD_IFRAME_SANDBOX = [
  'allow-scripts',
  'allow-same-origin',
  'allow-modals',
].join(' ');

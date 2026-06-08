// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render } from '@testing-library/react';
import { PluginPageView } from '../../components/plugin/PluginPageView';
import { PluginWidgetView } from '../../components/plugin/PluginWidgetView';
import { useStore } from '../../stores';

vi.mock('../../hooks/use-hana-fetch', () => ({
  hanaUrl: (path: string) => `http://127.0.0.1:3210${path}`,
}));

function expectInteractivePluginIframe(iframe: HTMLIFrameElement | null) {
  expect(iframe).toBeTruthy();
  expect(iframe).toHaveAttribute('allow', 'fullscreen; clipboard-read; clipboard-write');
  const sandbox = iframe?.getAttribute('sandbox') ?? '';
  expect(sandbox).toContain('allow-modals');
  expect(sandbox).toContain('allow-popups-to-escape-sandbox');
}

describe('plugin iframe permissions', () => {
  afterEach(() => {
    cleanup();
    useStore.setState({
      currentAgentId: null,
      pluginPages: [],
      pluginWidgets: [],
    } as never);
  });

  it('allows fullscreen, clipboard, and modal dialogs in full plugin pages', () => {
    useStore.setState({
      currentAgentId: 'butter',
      pluginPages: [{
        pluginId: 'infinite-canvas',
        title: 'Infinite Canvas',
        icon: null,
        routeUrl: '/api/plugins/infinite-canvas/page',
        hostCapabilities: [],
      }],
    } as never);

    const { container } = render(<PluginPageView pluginId="infinite-canvas" />);

    expectInteractivePluginIframe(container.querySelector('iframe'));
  });

  it('uses the same interactive permissions for plugin widgets', () => {
    useStore.setState({
      currentAgentId: 'butter',
      pluginWidgets: [{
        pluginId: 'infinite-canvas',
        title: 'Infinite Canvas',
        icon: null,
        routeUrl: '/api/plugins/infinite-canvas/widget',
        hostCapabilities: [],
      }],
    } as never);

    const { container } = render(<PluginWidgetView pluginId="infinite-canvas" />);

    expectInteractivePluginIframe(container.querySelector('iframe'));
  });
});

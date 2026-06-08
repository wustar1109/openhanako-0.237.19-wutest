/**
 * @vitest-environment jsdom
 */
import '@testing-library/jest-dom/vitest';
import { cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { PreviewRenderer } from '../../components/preview/PreviewRenderer';
import type { PreviewItem } from '../../types';

const mocks = vi.hoisted(() => ({
  hanaFetch: vi.fn(),
}));

vi.mock('../../hooks/use-hana-fetch', () => ({
  hanaFetch: mocks.hanaFetch,
}));

describe('PreviewRenderer HTML isolation', () => {
  const htmlContent = '<script src="https://cdn.tailwindcss.com"></script><div class="text-red-500">Hello</div>';
  const previewItem: PreviewItem = {
    id: 'html-demo',
    type: 'html',
    title: 'demo.html',
    content: htmlContent,
    filePath: '/tmp/demo.html',
    ext: 'html',
  };

  beforeEach(() => {
    mocks.hanaFetch.mockReset();
    mocks.hanaFetch.mockResolvedValue(new Response(JSON.stringify({
      previewUrl: 'http://127.0.0.1:14500/preview/html/pv_123?previewToken=preview_only_token',
    }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }));
  });

  afterEach(() => {
    cleanup();
  });

  it('registers HTML and loads it through a sandboxed isolated preview URL instead of srcDoc', async () => {
    const { container } = render(<PreviewRenderer previewItem={previewItem} />);

    expect(mocks.hanaFetch).toHaveBeenCalledWith('/api/preview/html', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title: 'demo.html',
        content: htmlContent,
      }),
    });

    const iframe = container.querySelector('iframe');
    expect(iframe).toBeTruthy();
    expect(iframe).toHaveAttribute('sandbox', 'allow-scripts');
    expect(iframe).toHaveAttribute('referrerpolicy', 'no-referrer');
    expect(iframe).not.toHaveAttribute('srcdoc');
    expect(iframe?.getAttribute('sandbox')).not.toContain('allow-same-origin');

    await waitFor(() => {
      expect(iframe).toHaveAttribute('src', 'http://127.0.0.1:14500/preview/html/pv_123?previewToken=preview_only_token');
    });
  });
});

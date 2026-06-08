/**
 * @vitest-environment jsdom
 */
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { PreviewPanel } from '../../components/PreviewPanel';
import { useStore, type StoreState } from '../../stores';
import type { PlatformApi } from '../../types';

describe('PreviewPanel markdown editor status', () => {
  beforeEach(() => {
    window.t = ((key: string) => key) as typeof window.t;
    Range.prototype.getClientRects = vi.fn(() => [] as unknown as DOMRectList);
    Range.prototype.getBoundingClientRect = vi.fn(() => ({
      x: 0,
      y: 0,
      width: 0,
      height: 0,
      top: 0,
      right: 0,
      bottom: 0,
      left: 0,
      toJSON: () => ({}),
    }));
    window.platform = {
      watchFile: vi.fn(async () => true),
      unwatchFile: vi.fn(async () => true),
      onFileChanged: vi.fn(),
      writeFile: vi.fn(async () => true),
      writeFileIfUnchanged: vi.fn(async () => ({
        ok: true,
        conflict: false,
        version: { mtimeMs: 2, size: 10, sha256: 'next' },
      })),
    } as unknown as PlatformApi;
    useStore.setState({
      previewOpen: true,
      previewItems: [{
        id: 'note',
        type: 'markdown',
        title: 'note.md',
        content: '你好ab',
        filePath: '/tmp/hana-note.md',
      }],
      openTabs: ['note'],
      activeTabId: 'note',
      markdownPreviewIds: [],
      quoteCandidate: null,
      quotedSelections: [],
      quotedSelection: null,
    } as Partial<StoreState>);
  });

  afterEach(() => {
    cleanup();
    useStore.setState({
      previewOpen: false,
      previewItems: [],
      openTabs: [],
      activeTabId: null,
      markdownPreviewIds: [],
      quoteCandidate: null,
      quotedSelections: [],
      quotedSelection: null,
    } as Partial<StoreState>);
  });

  it('shows selected and total character counts for editable markdown', () => {
    render(<PreviewPanel />);

    expect(screen.getByTestId('markdown-editor-status')).toHaveTextContent('选中 0 字 · 共 4 字');
  });
});

// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { SelectionQuoteActionSurface } from '../../components/selection/SelectionQuoteActionSurface';
import { useStore } from '../../stores';

describe('SelectionQuoteActionSurface', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    useStore.getState().clearQuoteCandidate();
    useStore.getState().clearQuotedSelections();
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it('adds the current selection candidate as an independent quote chip source', () => {
    useStore.getState().setQuoteCandidate({
      text: '第一段引用',
      sourceTitle: 'Assistant message',
      sourceKind: 'chat',
      sourceSessionPath: '/session/a.jsonl',
      sourceMessageId: 'assistant-1',
      sourceRole: 'assistant',
      charCount: 5,
      anchorRect: { left: 100, right: 180, top: 120, bottom: 140, width: 80, height: 20 },
    });
    render(<SelectionQuoteActionSurface />);

    fireEvent.click(screen.getByRole('button', { name: '引用到对话' }));

    expect(useStore.getState().quotedSelections).toHaveLength(1);
    expect(useStore.getState().quotedSelections[0]).toMatchObject({ text: '第一段引用' });
    expect(useStore.getState().quoteCandidate).toBeNull();
  });

  it('delays the tooltip for 500ms', () => {
    useStore.getState().setQuoteCandidate({
      text: '第一段引用',
      sourceTitle: 'Assistant message',
      sourceKind: 'chat',
      charCount: 5,
      anchorRect: { left: 100, right: 180, top: 120, bottom: 140, width: 80, height: 20 },
    });
    render(<SelectionQuoteActionSurface />);

    const button = screen.getByRole('button', { name: '引用到对话' });
    fireEvent.mouseEnter(button);
    act(() => { vi.advanceTimersByTime(499); });
    expect(screen.queryByRole('tooltip')).toBeNull();

    act(() => { vi.advanceTimersByTime(1); });
    expect(screen.getByRole('tooltip').textContent).toBe('引用到对话');
  });

  it('renders a compact SVG quote action shifted slightly to the right of the selection center', () => {
    useStore.getState().setQuoteCandidate({
      text: '第一段引用',
      sourceTitle: 'Assistant message',
      sourceKind: 'chat',
      charCount: 5,
      anchorRect: { left: 100, right: 180, top: 120, bottom: 140, width: 80, height: 20 },
    });
    render(<SelectionQuoteActionSurface />);

    const button = screen.getByRole('button', { name: '引用到对话' });
    const surface = button.closest('[data-selection-ignore="true"]') as HTMLElement;
    const icon = button.querySelector('svg');

    expect(icon).not.toBeNull();
    expect(icon?.getAttribute('fill')).toBe('currentColor');
    expect(icon?.hasAttribute('stroke')).toBe(false);
    expect(button.textContent).not.toContain('"');
    expect(surface.style.left).toBe('147px');
    expect(surface.style.top).toBe('86px');
  });

  it('follows the live native selection rect when the transcript scrolls', () => {
    let liveRect = { left: 100, right: 180, top: 120, bottom: 140, width: 80, height: 20 };
    const getSelection = vi.spyOn(window, 'getSelection').mockReturnValue({
      rangeCount: 1,
      toString: () => '第一段引用',
      getRangeAt: () => ({
        getBoundingClientRect: () => liveRect,
      }),
    } as unknown as Selection);
    const requestAnimationFrame = vi
      .spyOn(window, 'requestAnimationFrame')
      .mockImplementation((callback: FrameRequestCallback) => {
        callback(0);
        return 1;
      });
    const cancelAnimationFrame = vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => {});

    useStore.getState().setQuoteCandidate({
      text: '第一段引用',
      sourceTitle: 'Assistant message',
      sourceKind: 'chat',
      charCount: 5,
      anchorRect: { left: 100, right: 180, top: 120, bottom: 140, width: 80, height: 20 },
    });
    render(<SelectionQuoteActionSurface />);

    const surface = screen.getByRole('button', { name: '引用到对话' }).closest('[data-selection-ignore="true"]') as HTMLElement;
    expect(surface.style.top).toBe('86px');

    liveRect = { left: 100, right: 180, top: 70, bottom: 90, width: 80, height: 20 };
    act(() => {
      document.dispatchEvent(new Event('scroll'));
    });

    expect(surface.style.top).toBe('36px');

    getSelection.mockRestore();
    requestAnimationFrame.mockRestore();
    cancelAnimationFrame.mockRestore();
  });
});

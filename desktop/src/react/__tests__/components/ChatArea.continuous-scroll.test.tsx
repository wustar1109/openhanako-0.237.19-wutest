// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import React from 'react';
import { act, cleanup, fireEvent, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useStore } from '../../stores';
import type { ChatListItem } from '../../stores/chat-types';

vi.mock('../../components/chat/ChatTranscript', () => ({
  ChatTranscript: ({ items }: { items: ChatListItem[] }) => (
    <div data-testid="transcript">
      {items.map((item) => {
        if (item.type !== 'message') return <span key={item.id}>c</span>;
        const text = item.data.role === 'user'
          ? item.data.text
          : item.data.blocks?.map((block) => block.type === 'text' ? (block.source || block.html) : '').join('');
        return (
          <article key={item.data.id} data-message-id={item.data.id}>
            <span id={`message-${item.data.id}`}>{text}</span>
          </article>
        );
      })}
    </div>
  ),
}));

vi.mock('../../components/chat/ChatTimelineNavigator', () => ({
  ChatTimelineNavigator: () => null,
}));

import { ChatArea } from '../../components/chat/ChatArea';

class MockResizeObserver {
  observe() {}
  disconnect() {}
}

function setScrollMetrics(
  el: HTMLElement,
  metrics: { scrollHeight: number; clientHeight: number; scrollTop: number },
) {
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => metrics.scrollHeight });
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => metrics.clientHeight });
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get: () => metrics.scrollTop,
    set: (value) => { metrics.scrollTop = value; },
  });
}

function message(id: string, role: 'user' | 'assistant'): ChatListItem {
  return {
    type: 'message',
    data: role === 'user'
      ? { id, role, text: id, textHtml: `<p>${id}</p>` }
      : { id, role, blocks: [{ type: 'text', html: `<p>${id}</p>` }] },
  };
}

describe('ChatArea continuous bottom scroll', () => {
  beforeEach(() => {
    window.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;
    window.requestAnimationFrame = ((cb: FrameRequestCallback) => {
      cb(16);
      return 1;
    }) as typeof window.requestAnimationFrame;
    window.cancelAnimationFrame = (() => {}) as typeof window.cancelAnimationFrame;
    useStore.setState({
      currentSessionPath: '/chat/scroll.jsonl',
      welcomeVisible: false,
      quoteCandidate: null,
      quotedSelections: [],
      quotedSelection: null,
      chatSessions: {
        '/chat/scroll.jsonl': {
          items: [message('u-1', 'user')],
          hasMore: false,
          loadingMore: false,
        },
      },
      sessions: [{ path: '/chat/scroll.jsonl', agentId: 'hana', title: null, firstMessage: '', modified: '', messageCount: 1 }],
      streamingSessions: ['/chat/scroll.jsonl'],
      agents: [{ id: 'hana', name: 'Hana', yuan: 'hanako' }],
    } as never);
    window.getSelection()?.removeAllRanges();
  });

  afterEach(() => {
    cleanup();
    window.getSelection()?.removeAllRanges();
    vi.restoreAllMocks();
  });

  it('does not force sticky when an assistant/tool message is appended after the user scrolled up', async () => {
    const metrics = { scrollHeight: 1000, clientHeight: 300, scrollTop: 500 };
    const { container } = render(<ChatArea />);

    await waitFor(() => {
      expect(container.querySelector('[class*="sessionPanel"]')).toBeTruthy();
    });

    const panel = container.querySelector('[class*="sessionPanel"]') as HTMLElement;
    setScrollMetrics(panel, metrics);

    act(() => {
      metrics.scrollTop = 500;
      fireEvent.scroll(panel);
    });

    act(() => {
      useStore.setState((state) => ({
        chatSessions: {
          ...state.chatSessions,
          '/chat/scroll.jsonl': {
            ...state.chatSessions['/chat/scroll.jsonl'],
            items: [
              ...state.chatSessions['/chat/scroll.jsonl'].items,
              {
                type: 'message',
                data: {
                  id: 'a-tool',
                  role: 'assistant',
                  blocks: [{ type: 'tool_group', tools: [{ name: 'test.tool', done: false, success: false }], collapsed: false }],
                },
              },
            ],
          },
        },
      } as never));
    });

    expect(metrics.scrollTop).toBe(500);
  });

  it('captures chat text selection from the active panel on mouseup', async () => {
    useStore.setState({
      currentSessionPath: '/chat/scroll.jsonl',
      chatSessions: {
        '/chat/scroll.jsonl': {
          items: [{
            type: 'message',
            data: {
              id: 'a-quote',
              role: 'assistant',
              blocks: [{ type: 'text', html: '<p>可以引用的话</p>', source: '可以引用的话' }],
            },
          }],
          hasMore: false,
          loadingMore: false,
        },
      },
    } as never);
    const { container } = render(<ChatArea />);

    await waitFor(() => {
      expect(container.querySelector('[class*="sessionPanel"]')).toBeTruthy();
    });

    selectElementText(document.getElementById('message-a-quote')!);
    fireEvent.mouseUp(container.querySelector('[class*="sessionPanel"]') as HTMLElement);

    expect(useStore.getState().quoteCandidate).toMatchObject({
      text: '可以引用的话',
      sourceKind: 'chat',
      sourceMessageId: 'a-quote',
      sourceSessionPath: '/chat/scroll.jsonl',
    });
  });
});

function selectElementText(element: HTMLElement): void {
  const range = document.createRange();
  range.selectNodeContents(element);
  const selection = window.getSelection();
  selection?.removeAllRanges();
  selection?.addRange(range);
}

// @vitest-environment jsdom

import { EditorState } from '@codemirror/state';
import type { EditorView } from '@codemirror/view';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { captureChatSelection, captureSelection, clearSelection, initQuotedSelectionLifecycle } from '../../stores/selection-actions';
import { useStore } from '../../stores';
import type { PreviewItem } from '../../types';

const previewItem: PreviewItem = {
  id: 'preview-1',
  title: 'note.md',
  type: 'markdown',
  content: '',
  filePath: '/notes/note.md',
};

describe('captureSelection', () => {
  beforeEach(() => {
    document.body.innerHTML = '';
    window.getSelection()?.removeAllRanges();
    useStore.getState().clearQuoteCandidate();
    useStore.getState().clearQuotedSelections();
    useStore.setState({ selectedIdsBySession: {}, chatSessions: {} } as never);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('uses the trimmed quoted text range for lineEnd when selection includes a trailing newline', () => {
    const doc = 'alpha\nbeta\ngamma';
    const state = EditorState.create({
      doc,
      selection: { anchor: 6, head: 11 },
    });

    captureSelection(previewItem, { state } as EditorView);

    expect(useStore.getState().quoteCandidate).toMatchObject({
      text: 'beta',
      sourceTitle: 'note.md',
      sourceKind: 'preview',
      sourceFilePath: '/notes/note.md',
      lineStart: 2,
      lineEnd: 2,
      charCount: 4,
    });
  });

  it('sets explicit message selection per session and removes empty session entries', () => {
    const state = useStore.getState();

    state.setMessageSelection('/session/a.jsonl', ['m2', 'm1', 'm2']);

    expect(useStore.getState().selectedIdsBySession['/session/a.jsonl']).toEqual(['m2', 'm1']);

    useStore.getState().setMessageSelection('/session/a.jsonl', []);

    expect(useStore.getState().selectedIdsBySession['/session/a.jsonl']).toBeUndefined();
  });

  it('captures selected assistant chat text as a quote candidate with explicit message ownership', () => {
    useStore.setState({
      chatSessions: {
        '/session/a.jsonl': {
          items: [
            {
              type: 'message',
              data: {
                id: 'assistant-1',
                role: 'assistant',
                blocks: [{ type: 'text', html: '<p>这段文字值得引用</p>', source: '这段文字值得引用' }],
              },
            },
          ],
          hasMore: false,
          loadingMore: false,
        },
      },
    } as never);
    document.body.innerHTML = `
      <article data-message-id="assistant-1">
        <p><span id="selected-text">这段文字值得引用</span></p>
      </article>
    `;
    selectElementText(document.getElementById('selected-text')!);

    captureChatSelection('/session/a.jsonl');

    expect(useStore.getState().quoteCandidate).toMatchObject({
      text: '这段文字值得引用',
      sourceKind: 'chat',
      sourceSessionPath: '/session/a.jsonl',
      sourceMessageId: 'assistant-1',
      sourceRole: 'assistant',
      charCount: 8,
    });
    expect(useStore.getState().quotedSelections).toEqual([]);
  });

  it('captures chat text from the document-level mouseup lifecycle when release happens outside the chat panel', () => {
    const dispose = initQuotedSelectionLifecycle(document);
    try {
      seedChatFixture();
      document.body.innerHTML = `
        <section data-chat-selection-root="" data-session-path="/session/a.jsonl">
          <article data-message-id="assistant-1">
            <p><span id="selected-text">document mouseup quote</span></p>
          </article>
        </section>
        <aside id="outside-chat">outside</aside>
      `;
      selectElementText(document.getElementById('selected-text')!);

      window.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));

      expect(useStore.getState().quoteCandidate).toMatchObject({
        text: 'document mouseup quote',
        sourceKind: 'chat',
        sourceSessionPath: '/session/a.jsonl',
        sourceMessageId: 'assistant-1',
      });
    } finally {
      dispose();
    }
  });

  it('falls back to the message element bounds when the native selection range has no usable rects', () => {
    seedChatFixture();
    document.body.innerHTML = `
      <section data-chat-selection-root="" data-session-path="/session/a.jsonl">
        <article id="message" data-message-id="assistant-1">
          <p><span id="selected-text">fallback bounds quote</span></p>
        </article>
      </section>
    `;
    const message = document.getElementById('message')!;
    Object.defineProperty(message, 'getBoundingClientRect', {
      value: () => domRect({ left: 24, right: 224, top: 80, bottom: 128, width: 200, height: 48 }),
    });
    const range = selectElementText(document.getElementById('selected-text')!);
    Object.defineProperty(range, 'getClientRects', {
      configurable: true,
      value: () => [] as unknown as DOMRectList,
    });
    Object.defineProperty(range, 'getBoundingClientRect', {
      configurable: true,
      value: () => domRect({
      left: 0,
      right: 0,
      top: 0,
      bottom: 0,
      width: 0,
      height: 0,
      }),
    });

    captureChatSelection('/session/a.jsonl');

    expect(useStore.getState().quoteCandidate?.anchorRect).toEqual({
      left: 24,
      right: 224,
      top: 80,
      bottom: 128,
      width: 200,
      height: 48,
    });
  });

  it('keeps added quotes when composer focus cancels the native selection candidate', () => {
    const dispose = initQuotedSelectionLifecycle(document);
    try {
      useStore.getState().addQuotedSelection({
        text: 'old quote',
        sourceTitle: 'Assistant message',
        sourceKind: 'chat',
        sourceSessionPath: '/session/a.jsonl',
        sourceMessageId: 'assistant-1',
        sourceRole: 'assistant',
        charCount: 9,
      });
      document.body.innerHTML = '<textarea id="composer"></textarea>';
      document.getElementById('composer')?.focus();

      window.getSelection()?.removeAllRanges();
      document.dispatchEvent(new Event('selectionchange'));

      expect(useStore.getState().quotedSelections[0]).toMatchObject({
        text: 'old quote',
        sourceKind: 'chat',
      });
    } finally {
      dispose();
    }
  });

  it('clears an existing chat candidate when the collapsed native selection remains in the same chat session', () => {
    const dispose = initQuotedSelectionLifecycle(document);
    try {
      useStore.setState({
        chatSessions: {
          '/session/a.jsonl': {
            items: [
              {
                type: 'message',
                data: {
                  id: 'assistant-1',
                  role: 'assistant',
                  blocks: [{ type: 'text', html: '<p>inside chat</p>', source: 'inside chat' }],
                },
              },
            ],
            hasMore: false,
            loadingMore: false,
          },
        },
      } as never);
      document.body.innerHTML = `
        <section data-chat-selection-root="" data-session-path="/session/a.jsonl">
          <article data-message-id="assistant-1">
            <span id="selected-text">inside chat</span>
            <span id="caret-host">cancel here</span>
          </article>
        </section>
      `;
      selectElementText(document.getElementById('selected-text')!);
      captureChatSelection('/session/a.jsonl');
      expect(useStore.getState().quoteCandidate).toMatchObject({ text: 'inside chat' });

      placeCollapsedSelection(document.getElementById('caret-host')!);

      document.dispatchEvent(new Event('selectionchange'));

      expect(useStore.getState().quoteCandidate).toBeNull();
    } finally {
      dispose();
    }
  });

  it('keeps added preview quotes when chat capture sees an empty selection', () => {
    useStore.getState().addQuotedSelection({
      text: 'preview quote',
      sourceTitle: 'note.md',
      sourceKind: 'preview',
      sourceFilePath: '/notes/note.md',
      charCount: 13,
    });

    window.getSelection()?.removeAllRanges();
    captureChatSelection('/session/a.jsonl');

    expect(useStore.getState().quotedSelections[0]).toMatchObject({
      text: 'preview quote',
      sourceKind: 'preview',
    });
  });

  it('clears only quotes matching the requested source scope', () => {
    useStore.getState().setQuoteCandidate({
      text: 'chat quote',
      sourceTitle: 'Assistant message',
      sourceKind: 'chat',
      sourceSessionPath: '/session/a.jsonl',
      sourceMessageId: 'assistant-1',
      sourceRole: 'assistant',
      charCount: 10,
    });

    clearSelection({ sourceKind: 'preview' });

    expect(useStore.getState().quoteCandidate).toMatchObject({
      text: 'chat quote',
      sourceKind: 'chat',
    });

    clearSelection({ sourceKind: 'chat', sourceSessionPath: '/session/a.jsonl' });

    expect(useStore.getState().quoteCandidate).toBeNull();
  });

  it('clears an existing candidate when chat capture sees an empty selection', () => {
    useStore.getState().setQuoteCandidate({
      text: 'old quote',
      sourceTitle: 'Assistant message',
      sourceKind: 'chat',
      sourceSessionPath: '/session/a.jsonl',
      sourceMessageId: 'assistant-1',
      sourceRole: 'assistant',
      charCount: 9,
    });

    window.getSelection()?.removeAllRanges();
    captureChatSelection('/session/a.jsonl');

    expect(useStore.getState().quoteCandidate).toBeNull();
  });

  it('ignores cross-message chat selections instead of stealing ambiguous ownership', () => {
    useStore.setState({
      chatSessions: {
        '/session/a.jsonl': {
          items: [
            { type: 'message', data: { id: 'user-1', role: 'user', text: '第一条' } },
            { type: 'message', data: { id: 'assistant-1', role: 'assistant', blocks: [] } },
          ],
          hasMore: false,
          loadingMore: false,
        },
      },
    } as never);
    document.body.innerHTML = `
      <article data-message-id="user-1"><span id="start-text">第一条</span></article>
      <article data-message-id="assistant-1"><span id="end-text">第二条</span></article>
    `;
    selectAcrossElements(
      document.getElementById('start-text')!,
      document.getElementById('end-text')!,
    );

    captureChatSelection('/session/a.jsonl');

    expect(useStore.getState().quoteCandidate).toBeNull();
  });

  it('ignores selections inside chat action buttons', () => {
    useStore.setState({
      chatSessions: {
        '/session/a.jsonl': {
          items: [
            { type: 'message', data: { id: 'assistant-1', role: 'assistant', blocks: [] } },
          ],
          hasMore: false,
          loadingMore: false,
        },
      },
    } as never);
    document.body.innerHTML = `
      <article data-message-id="assistant-1">
        <button type="button"><span id="button-text">复制</span></button>
      </article>
    `;
    selectElementText(document.getElementById('button-text')!);

    captureChatSelection('/session/a.jsonl');

    expect(useStore.getState().quoteCandidate).toBeNull();
  });
});

function seedChatFixture(): void {
  useStore.setState({
    chatSessions: {
      '/session/a.jsonl': {
        items: [
          {
            type: 'message',
            data: {
              id: 'assistant-1',
              role: 'assistant',
              blocks: [{ type: 'text', html: '<p>document mouseup quote</p>', source: 'document mouseup quote' }],
            },
          },
        ],
        hasMore: false,
        loadingMore: false,
      },
    },
  } as never);
}

function domRect(rect: { left: number; right: number; top: number; bottom: number; width: number; height: number }): DOMRect {
  return {
    ...rect,
    x: rect.left,
    y: rect.top,
    toJSON: () => rect,
  } as DOMRect;
}

function selectElementText(element: HTMLElement): Range {
  const range = document.createRange();
  range.selectNodeContents(element);
  const selection = window.getSelection();
  selection?.removeAllRanges();
  selection?.addRange(range);
  return range;
}

function selectAcrossElements(startElement: HTMLElement, endElement: HTMLElement): void {
  const startNode = startElement.firstChild;
  const endNode = endElement.firstChild;
  if (!startNode || !endNode) throw new Error('test fixture must contain text nodes');
  const range = document.createRange();
  range.setStart(startNode, 0);
  range.setEnd(endNode, endNode.textContent?.length || 0);
  const selection = window.getSelection();
  selection?.removeAllRanges();
  selection?.addRange(range);
}

function placeCollapsedSelection(element: HTMLElement): void {
  const textNode = element.firstChild;
  if (!textNode) throw new Error('test fixture must contain a text node');
  const range = document.createRange();
  range.setStart(textNode, 0);
  range.collapse(true);
  const selection = window.getSelection();
  selection?.removeAllRanges();
  selection?.addRange(range);
}

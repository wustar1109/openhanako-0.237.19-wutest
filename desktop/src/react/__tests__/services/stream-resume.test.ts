// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  loadMessages: vi.fn(async () => {}),
  clearChat: vi.fn(),
  streamBufferManager: {
    clear: vi.fn(),
    finishTurn: vi.fn(),
  },
  ws: {
    readyState: 1,
    send: vi.fn(),
  },
}));

vi.mock('../../hooks/use-stream-buffer', () => ({
  streamBufferManager: mocks.streamBufferManager,
}));

vi.mock('../../stores/session-actions', () => ({
  loadMessages: mocks.loadMessages,
}));

vi.mock('../../stores/agent-actions', () => ({
  clearChat: mocks.clearChat,
}));

vi.mock('../../services/websocket', () => ({
  getWebSocket: () => mocks.ws,
}));

import { useStore } from '../../stores';
import { injectHandlers, replayStreamResume } from '../../services/stream-resume';

describe('stream-resume', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useStore.setState({
      currentSessionPath: '/focused.jsonl',
      streamingSessions: ['/background.jsonl'],
      sessions: [
        { path: '/focused.jsonl', title: 'focused' },
        { path: '/background.jsonl', title: 'background' },
      ],
      chatSessions: {
        '/focused.jsonl': { items: [], hasMore: false, loadingMore: false },
        '/background.jsonl': { items: [], hasMore: false, loadingMore: false },
      },
    } as never);
  });

  it('hydrates a completed empty resume for background sessions instead of leaving them stuck streaming', async () => {
    const statuses: Array<{ isStreaming: boolean; sessionPath: string | null }> = [];
    injectHandlers(vi.fn(), (isStreaming, sessionPath) => {
      statuses.push({ isStreaming, sessionPath });
      useStore.setState((state) => ({
        streamingSessions: isStreaming
          ? Array.from(new Set([...state.streamingSessions, sessionPath].filter(Boolean))) as string[]
          : state.streamingSessions.filter((path) => path !== sessionPath),
      }));
    });

    replayStreamResume({
      type: 'stream_resume',
      sessionPath: '/background.jsonl',
      streamId: 'stream_1',
      sinceSeq: 3,
      nextSeq: 4,
      isStreaming: false,
      reset: false,
      truncated: false,
      events: [],
    });

    await vi.waitFor(() => {
      expect(mocks.loadMessages).toHaveBeenCalledWith('/background.jsonl');
    });

    expect(mocks.streamBufferManager.finishTurn).toHaveBeenCalledWith('/background.jsonl');
    expect(mocks.streamBufferManager.clear).not.toHaveBeenCalledWith('/background.jsonl');
    expect(statuses).toContainEqual({ isStreaming: false, sessionPath: '/background.jsonl' });
    expect(useStore.getState().streamingSessions).toEqual([]);
  });

  it('replays background session events to the normal websocket handler', () => {
    const handled: unknown[] = [];
    const statuses: Array<{ isStreaming: boolean; sessionPath: string | null }> = [];
    injectHandlers((msg) => handled.push(msg), (isStreaming, sessionPath) => {
      statuses.push({ isStreaming, sessionPath });
    });

    replayStreamResume({
      type: 'stream_resume',
      sessionPath: '/background.jsonl',
      streamId: 'stream_2',
      sinceSeq: 1,
      nextSeq: 3,
      isStreaming: true,
      reset: false,
      truncated: false,
      events: [
        { seq: 2, event: { type: 'text_delta', delta: 'late text' } },
      ],
    });

    expect(handled).toEqual([
      expect.objectContaining({
        type: 'text_delta',
        delta: 'late text',
        sessionPath: '/background.jsonl',
        streamId: 'stream_2',
        seq: 2,
        __fromReplay: true,
      }),
    ]);
    expect(statuses).toEqual([{ isStreaming: true, sessionPath: '/background.jsonl' }]);
  });
});

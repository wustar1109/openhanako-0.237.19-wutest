// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { InputArea } from '../../components/InputArea';
import { useStore } from '../../stores';

vi.mock('@tiptap/react', () => ({
  useEditor: () => ({
    commands: {
      focus: vi.fn(),
      clearContent: vi.fn(),
      scrollIntoView: vi.fn(),
      setContent: vi.fn(),
      insertContent: vi.fn(),
    },
    chain: () => ({
      clearContent: () => ({
        insertContent: () => ({
          insertContent: () => ({
            focus: () => ({ run: vi.fn() }),
          }),
        }),
      }),
    }),
    getText: () => '',
    getJSON: () => ({ type: 'doc', content: [] }),
    state: { tr: { setMeta: vi.fn(() => ({})) } },
    view: { dispatch: vi.fn() },
    on: vi.fn(),
    off: vi.fn(),
  }),
  EditorContent: () => React.createElement('div', { 'data-testid': 'editor' }),
}));

vi.mock('@tiptap/starter-kit', () => ({
  default: { configure: () => ({}) },
}));

vi.mock('@tiptap/extension-bold', () => ({
  Bold: { extend: () => ({}) },
}));

vi.mock('@tiptap/extension-placeholder', () => ({
  default: { configure: () => ({}) },
}));

vi.mock('../../components/input/extensions/skill-badge', () => ({
  SkillBadge: {},
}));

vi.mock('../../components/input/extensions/file-badge', () => ({
  FileBadge: {},
}));

vi.mock('../../hooks/use-i18n', () => ({
  useI18n: () => ({ t: (key: string) => key, locale: 'zh-CN' }),
}));

vi.mock('../../hooks/use-config', () => ({
  fetchConfig: vi.fn(async () => ({})),
}));

vi.mock('../../hooks/use-hana-fetch', () => ({
  hanaFetch: vi.fn(async () => new Response('{}', { status: 200 })),
  hanaUrl: (path: string) => `http://127.0.0.1:3210${path}`,
}));

vi.mock('../../stores/session-actions', () => ({
  ensureSession: vi.fn(async () => true),
  loadSessions: vi.fn(),
}));

vi.mock('../../stores/desk-actions', () => ({
  loadDeskFiles: vi.fn(),
  searchDeskFiles: vi.fn(async () => []),
  toggleJianSidebar: vi.fn(),
}));

vi.mock('../../services/websocket', () => ({
  getWebSocket: vi.fn(() => null),
}));

vi.mock('../../MainContent', () => ({
  attachFilesFromPaths: vi.fn(),
}));

vi.mock('../../components/input/SlashCommandMenu', () => ({
  SlashCommandMenu: () => null,
}));

vi.mock('../../components/input/FileMentionMenu', () => ({
  FileMentionMenu: () => null,
}));

vi.mock('../../components/input/InputStatusBars', () => ({
  InputStatusBars: ({
    slashBusy,
    compacting,
    screenshotBusy,
    inlineError,
    slashResult,
  }: {
    slashBusy?: string | null;
    compacting?: boolean;
    screenshotBusy?: boolean;
    inlineError?: string | null;
    slashResult?: unknown;
  }) => (
    slashBusy || compacting || screenshotBusy || inlineError || slashResult
      ? React.createElement('div', { 'data-testid': 'input-status-bars' })
      : null
  ),
}));

vi.mock('../../components/input/InputContextRow', () => ({
  InputContextRow: ({
    attachedFiles,
    hasQuotedSelection,
    sessionTodos,
  }: {
    attachedFiles?: unknown[];
    hasQuotedSelection?: boolean;
    sessionTodos?: unknown[];
  }) => (
    (attachedFiles?.length || hasQuotedSelection || sessionTodos?.length)
      ? React.createElement('div', { 'data-testid': 'input-context-row' })
      : null
  ),
}));

vi.mock('../../components/input/InputControlBar', () => ({
  InputControlBar: () => React.createElement('button', { type: 'button' }, 'send'),
}));

vi.mock('../../components/input/SessionConfirmationPrompt', () => ({
  SessionConfirmationPrompt: () => React.createElement('div', { 'data-testid': 'approval-prompt' }),
}));

vi.mock('../../hooks/use-slash-items', () => ({
  useSkillSlashItems: () => [],
}));

vi.mock('../../utils/paste-upload-feedback', () => ({
  notifyPasteUploadFailure: vi.fn(),
}));

vi.mock('../../services/stream-resume', () => ({
  replayStreamResume: vi.fn(),
  isStreamResumeRebuilding: () => null,
  isStreamScopedMessage: () => false,
  updateSessionStreamMeta: vi.fn(),
}));

function expectBefore(first: HTMLElement, second: HTMLElement) {
  expect(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
}

function seedLayeredInputState() {
  const sessionPath = '/session/status-stack.jsonl';
  useStore.setState({
    currentSessionPath: sessionPath,
    connected: true,
    pendingNewSession: false,
    pendingSessionSwitchPath: null,
    streamingSessions: [],
    compactingSessions: [sessionPath],
    inlineErrors: {},
    screenshotTaskCount: 0,
    screenshotProgress: null,
    attachedFiles: [{
      path: '/tmp/example.md',
      name: 'example.md',
      isDirectory: false,
    }],
    attachedFilesBySession: {
      [sessionPath]: [{
        path: '/tmp/example.md',
        name: 'example.md',
        isDirectory: false,
      }],
    },
    docContextAttached: false,
    quoteCandidate: null,
    quotedSelections: [{
      text: 'quoted',
      sourceTitle: 'note.md',
      sourceKind: 'preview',
      charCount: 6,
    }],
    quotedSelection: null,
    todosBySession: {
      [sessionPath]: [{
        content: '读上下文',
        activeForm: '正在读上下文',
        status: 'in_progress',
      }],
    },
    models: [{
      id: 'deepseek-chat',
      provider: 'deepseek',
      name: 'DeepSeek Chat',
      input: ['text'],
      isCurrent: true,
    }],
    sessionModelsByPath: {},
    previewItems: [],
    previewOpen: false,
    activeTabId: null,
    chatSessions: {},
    serverPort: 3210,
    serverToken: null,
    modelSwitching: false,
    welcomeVisible: false,
    agentYuan: 'hanako',
  } as never);
  useStore.getState().initSession(sessionPath, [{
    type: 'message',
    data: {
      id: 'assistant-confirmation',
      role: 'assistant',
      blocks: [{
        type: 'session_confirmation',
        confirmId: 'confirm-1',
        kind: 'tool_action_approval',
        surface: 'input',
        status: 'pending',
        title: '允许 Hana 执行这次操作',
      }],
    },
  }], false);
}

describe('InputArea status stack', () => {
  beforeEach(() => {
    seedLayeredInputState();
    window.platform = {} as typeof window.platform;
    delete (window as unknown as { hana?: unknown }).hana;
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('places transient notices between context chips and the approval prompt', () => {
    render(React.createElement(InputArea));

    const contextRow = screen.getByTestId('input-context-row');
    const statusBars = screen.getByTestId('input-status-bars');
    const approvalPrompt = screen.getByTestId('approval-prompt');
    const editor = screen.getByTestId('editor');

    expectBefore(contextRow, statusBars);
    expectBefore(statusBars, approvalPrompt);
    expectBefore(approvalPrompt, editor);
  });
});

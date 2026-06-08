// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AssistantMessage } from '../../components/chat/AssistantMessage';
import { useStore } from '../../stores';

vi.mock('../../hooks/use-hana-fetch', () => ({
  hanaFetch: vi.fn(async () => new Response('{}', { status: 200 })),
  hanaUrl: (path: string) => `http://127.0.0.1:3210${path}`,
}));

vi.mock('../../utils/screenshot', () => ({
  takeScreenshot: vi.fn(),
}));

describe('AssistantMessage media generation placeholder', () => {
  beforeEach(() => {
    window.t = ((key: string) => key) as typeof window.t;
    useStore.setState({
      agents: [],
      agentName: 'Hanako',
      agentYuan: 'hanako',
      streamingSessions: [],
      selectedMessageIdsBySession: {},
    } as never);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('renders a grey image placeholder with inline status text and cycling dot slot', () => {
    const { container } = render(
      <AssistantMessage
        showAvatar={false}
        sessionPath="/sessions/main.jsonl"
        readOnly
        message={{
          id: 'a1',
          role: 'assistant',
          blocks: [{
            type: 'media_generation',
            taskId: 'task-img',
            kind: 'image',
            status: 'pending',
            prompt: 'Low-poly 3D illustration of a Chinese college student character sitting at the front row of a classroom',
          }],
        }}
      />,
    );

    expect(screen.getByLabelText('图片生成中...')).toBeInTheDocument();
    expect(container.querySelector('[class*="mediaGenerationDots"]')).toBeInTheDocument();
    expect(screen.getByText(/^Low-poly 3D illustration/)).toBeInTheDocument();
  });

  it('isolates a malformed rich block without hiding sibling message blocks', () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => undefined);

    expect(() => render(
      <AssistantMessage
        showAvatar={false}
        sessionPath="/sessions/main.jsonl"
        readOnly
        message={{
          id: 'a1',
          role: 'assistant',
          blocks: [
            { type: 'text', html: '<p>before bad block</p>' },
            { type: 'plugin_card' } as never,
            { type: 'text', html: '<p>after bad block</p>' },
          ],
        }}
      />,
    )).not.toThrow();

    expect(screen.getByText('before bad block')).toBeInTheDocument();
    expect(screen.getByText('after bad block')).toBeInTheDocument();
    expect(errorSpy).toHaveBeenCalled();
  });
});

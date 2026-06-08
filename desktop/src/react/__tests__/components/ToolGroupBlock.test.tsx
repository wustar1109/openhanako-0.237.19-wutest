// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ToolGroupBlock } from '../../components/chat/ToolGroupBlock';

describe('ToolGroupBlock', () => {
  beforeEach(() => {
    window.t = ((key: string) => key) as typeof window.t;
  });

  afterEach(() => {
    vi.useRealTimers();
    cleanup();
  });

  it('shows the full bash command in the hover title when the visible detail is truncated', () => {
    const command = 'rm -rf /Users/jason/.claude/plugins/marketplaces/temp_*';

    render(
      <ToolGroupBlock
        collapsed={false}
        tools={[{
          name: 'bash',
          args: { command },
          done: true,
          success: true,
        }]}
      />,
    );

    const detail = screen.getByTitle(command);

    expect(detail.textContent).toBe('rm -rf /Users/jason/.claude/plugins/mar…');
  });

  it('syncs a multi-tool group to collapsed when the completed block updates', () => {
    const { rerender } = render(
      <ToolGroupBlock
        collapsed={false}
        tools={[
          { name: 'bash', args: { command: 'npm test' }, done: true, success: true },
          { name: 'read', args: { file_path: '/tmp/report.md' }, done: false, success: false },
        ]}
      />,
    );

    const content = screen.getByText('npm test').closest('div')?.parentElement;
    expect(content).toBeTruthy();
    expect(content?.className).not.toContain('toolGroupContentCollapsed');

    rerender(
      <ToolGroupBlock
        collapsed={true}
        tools={[
          { name: 'bash', args: { command: 'npm test' }, done: true, success: true },
          { name: 'read', args: { file_path: '/tmp/report.md' }, done: true, success: true },
        ]}
      />,
    );

    expect(content?.className).toContain('toolGroupContentCollapsed');
  });

  it('keeps a single tool as a plain indicator without a fold summary', () => {
    render(
      <ToolGroupBlock
        collapsed={true}
        tools={[{
          name: 'bash',
          args: { command: 'npm test' },
          done: true,
          success: true,
        }]}
      />,
    );

    expect(screen.queryByText('toolGroup.count')).toBeNull();
    expect(screen.getByText('npm test')).toBeTruthy();
  });

  it('shows a live remaining countdown for running wait tools', () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_700_000_000_000);

    render(
      <ToolGroupBlock
        collapsed={false}
        tools={[{
          name: 'wait',
          args: {
            seconds: 30,
            startedAt: 1_700_000_000_000,
            durationMs: 30_000,
          },
          done: false,
          success: false,
        }]}
      />,
    );

    expect(screen.getByText('30s')).toBeTruthy();

    act(() => {
      vi.advanceTimersByTime(11_000);
    });

    expect(screen.getByText('19s')).toBeTruthy();
  });
});

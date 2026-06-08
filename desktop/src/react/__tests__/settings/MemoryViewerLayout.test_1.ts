import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const css = readFileSync(new URL('../../settings/Settings.module.css', import.meta.url), 'utf8');

function cssRule(selector: string) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = css.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`));
  return match?.[1] ?? '';
}

describe('memory viewer layout contract', () => {
  it('keeps long memory content inside a scrollable body', () => {
    expect(cssRule('.memory-viewer')).toMatch(/max-height:\s*min\(70vh,\s*calc\(100dvh - var\(--space-xl\) - var\(--space-xl\)\)\);/);
    expect(cssRule('.memory-viewer')).toMatch(/overflow:\s*hidden;/);

    expect(cssRule('.memory-viewer-body')).toMatch(/flex:\s*1 1 auto;/);
    expect(cssRule('.memory-viewer-body')).toMatch(/min-height:\s*0;/);
    expect(cssRule('.memory-viewer-body')).toMatch(/overflow-y:\s*auto;/);
  });
});

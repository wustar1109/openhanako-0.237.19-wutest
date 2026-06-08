import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

function readCss(relativePath: string): string {
  return readFileSync(path.join(process.cwd(), relativePath), 'utf8');
}

function cssRule(css: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return css.match(new RegExp(`${escaped}\\s*\\{(?<body>[^}]*)\\}`))?.groups?.body || '';
}

function declarationValue(rule: string, property: string): string | null {
  const escaped = property.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return rule.match(new RegExp(`${escaped}\\s*:\\s*(?<value>[^;]+);`))?.groups?.value.trim() || null;
}

describe('selection quote action surface layout', () => {
  it('copies the compact floating action chrome from editor preview actions while keeping icon buttons square', () => {
    const selectionCss = readCss('desktop/src/react/components/selection/SelectionQuoteActionSurface.module.css');
    const previewCss = readCss('desktop/src/react/components/preview/FloatingActions.module.css');
    const selectionSurface = cssRule(selectionCss, '.surface');
    const previewSurface = cssRule(previewCss, '.floatingActions');
    const selectionButton = cssRule(selectionCss, '.button');

    for (const property of ['display', 'gap', 'background', 'border-radius', 'padding', 'box-shadow']) {
      expect(declarationValue(selectionSurface, property)).toBe(declarationValue(previewSurface, property));
    }

    expect(declarationValue(selectionButton, 'width')).toBe('22px');
    expect(declarationValue(selectionButton, 'height')).toBe('22px');
    expect(declarationValue(selectionButton, 'padding')).toBe('0');
  });
});

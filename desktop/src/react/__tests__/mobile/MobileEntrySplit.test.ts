import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

function staticImportSpecifiers(source: string): string[] {
  const specifiers: string[] = [];
  const importRe = /^\s*import(?:\s+type)?(?:[\s\S]*?)\s+from\s+['"]([^'"]+)['"];?/gm;
  const sideEffectImportRe = /^\s*import\s+['"]([^'"]+)['"];?/gm;
  for (const match of source.matchAll(importRe)) {
    specifiers.push(match[1]);
  }
  for (const match of source.matchAll(sideEffectImportRe)) {
    specifiers.push(match[1]);
  }
  return specifiers;
}

describe('Mobile PWA entry split', () => {
  it('uses a mobile-specific CSS entry instead of the desktop global stylesheet', () => {
    const source = readFileSync(path.join(process.cwd(), 'desktop/src/mobile-main.tsx'), 'utf8');
    const imports = staticImportSpecifiers(source);

    expect(imports).toContain('./react/mobile/mobile-entry.css');
    expect(imports).not.toContain('./styles.css');
  });

  it('keeps the bundled serif font contract in the mobile-specific CSS entry', () => {
    const css = readFileSync(path.join(process.cwd(), 'desktop/src/react/mobile/mobile-entry.css'), 'utf8');

    expect(css).toMatch(/@import\s+url\(['"]?\.\.\/\.\.\/themes\/new-warm-paper-fonts\.css['"]?\)/);
    expect(css).toMatch(/--font-serif:\s*'EB Garamond',\s*'Noto Serif SC',\s*'Source Han Serif SC',\s*'Songti SC',\s*'STSong',\s*serif/);
    expect(css).toMatch(/body\.font-sans\s*\{[\s\S]*--font-serif:\s*var\(--font-ui\)/);
  });

  it('does not statically pull desktop-only app pages or heavy preview overlays into the mobile shell', () => {
    const source = readFileSync(path.join(process.cwd(), 'desktop/src/react/mobile/MobileApp.tsx'), 'utf8');
    const imports = staticImportSpecifiers(source);

    expect(imports).not.toContain('../components/app/AppPages');
    expect(imports).not.toContain('../components/app/WorkspaceCompanionRail');
    expect(imports).not.toContain('../components/PreviewPanel');
    expect(imports).not.toContain('../components/shared/MediaViewer/MediaViewer');
  });
});

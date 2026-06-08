import { createRequire } from 'node:module';
import { describe, expect, it } from 'vitest';

const require = createRequire(import.meta.url);

describe('screenshot markdown renderer helpers', () => {
  it('resolves relative markdown image paths from the source markdown file', () => {
    const { resolveScreenshotMarkdownImageSrc } = require('../desktop/src/shared/screenshot-markdown.cjs');

    expect(resolveScreenshotMarkdownImageSrc('文本附件/Cover Image.png', {
      sourceFilePath: '/vault/notes/day.md',
    })).toBe('file:///vault/notes/%E6%96%87%E6%9C%AC%E9%99%84%E4%BB%B6/Cover%20Image.png');
  });

  it('renders code articles as escaped preformatted code instead of markdown paragraphs', () => {
    const { renderScreenshotCodeArticle } = require('../desktop/src/shared/screenshot-markdown.cjs');

    expect(renderScreenshotCodeArticle('<div>x</div>', 'html')).toBe(
      '<pre><code class="language-html">&lt;div&gt;x&lt;/div&gt;</code></pre>',
    );
  });

  it('drops unsupported explicit image protocols in screenshot markdown', () => {
    const { resolveScreenshotMarkdownImageSrc } = require('../desktop/src/shared/screenshot-markdown.cjs');

    expect(resolveScreenshotMarkdownImageSrc('javascript:alert(1)', {
      sourceFilePath: '/vault/notes/day.md',
    })).toBe('');
  });
});

import { describe, expect, it } from 'vitest';
import { buildItemsFromHistory } from '../../utils/history-builder';

describe('buildItemsFromHistory user image restoration', () => {
  it('把服务端 ISO timestamp 归一成前端毫秒时间', () => {
    const items = buildItemsFromHistory({
      messages: [{
        id: 'u1',
        role: 'user',
        content: 'hello',
        timestamp: '2026-05-07T05:42:00.000Z',
      }],
    });

    const first = items[0];
    expect(first.type).toBe('message');
    if (first.type !== 'message') throw new Error('expected message');
    expect(first.data.timestamp).toBe(Date.parse('2026-05-07T05:42:00.000Z'));
  });

  it('保留后端 session entry id 作为分支操作来源', () => {
    const items = buildItemsFromHistory({
      messages: [{
        id: '0',
        entryId: 'entry-user-1',
        role: 'user',
        content: 'hello',
      }],
    });

    const first = items[0];
    expect(first.type).toBe('message');
    if (first.type !== 'message') throw new Error('expected message');
    expect(first.data.id).toBe('0');
    expect(first.data.sourceEntryId).toBe('entry-user-1');
  });

  it('隐藏 bridge 写入用户消息里的内部时间标签', () => {
    const items = buildItemsFromHistory({
      messages: [{
        id: 'u1',
        role: 'user',
        content: '<t>05-13 05:03</t> hello from phone',
      }],
    });

    const first = items[0];
    expect(first.type).toBe('message');
    if (first.type !== 'message') throw new Error('expected message');
    expect(first.data.text).toBe('hello from phone');
  });

  it('隐藏旧插话消息里的中英文内部前缀', () => {
    const items = buildItemsFromHistory({
      messages: [
        {
          id: 'u1',
          role: 'user',
          content: '（插话，无需 MOOD）\n先别展开',
        },
        {
          id: 'u2',
          role: 'user',
          content: '(Interjection, no MOOD needed)\njust answer directly',
        },
      ],
    });

    const first = items[0];
    const second = items[1];
    expect(first.type).toBe('message');
    expect(second.type).toBe('message');
    if (first.type !== 'message' || second.type !== 'message') throw new Error('expected messages');
    expect(first.data.text).toBe('先别展开');
    expect(second.data.text).toBe('just answer directly');
  });

  it('把 attached_image 标记恢复成图片附件，并从正文隐藏', () => {
    const items = buildItemsFromHistory({
      messages: [{
        id: 'u1',
        role: 'user',
        content: '[attached_image: /Users/test/.hanako/attachments/upload-abc.png]\n(看图)',
      }],
    });

    expect(items).toHaveLength(1);
    const first = items[0];
    expect(first.type).toBe('message');
    if (first.type !== 'message') throw new Error('expected message');
    expect(first.data.text).toBe('(看图)');
    expect(first.data.textHtml).not.toContain('attached_image');
    expect(first.data.attachments).toEqual([{
      path: '/Users/test/.hanako/attachments/upload-abc.png',
      name: 'upload-abc.png',
      isDir: false,
    }]);
  });

  it('原生 image block 与 attached_image 路径合并为一个图片附件', () => {
    const items = buildItemsFromHistory({
      messages: [{
        id: 'u1',
        role: 'user',
        content: '[attached_image: /Users/test/.hanako/attachments/upload-native.png]\n看看这个',
        images: [{ data: 'BASE64', mimeType: 'image/png' }],
      }],
    });

    const first = items[0];
    expect(first.type).toBe('message');
    if (first.type !== 'message') throw new Error('expected message');
    expect(first.data.text).toBe('看看这个');
    expect(first.data.attachments).toEqual([{
      path: '/Users/test/.hanako/attachments/upload-native.png',
      name: 'upload-native.png',
      isDir: false,
      mimeType: 'image/png',
    }]);
  });

  it('丢弃字段残缺的历史 sideband block，保留同消息的可渲染内容', () => {
    const items = buildItemsFromHistory({
      messages: [{
        id: 'a1',
        role: 'assistant',
        content: '可见正文',
      }],
      blocks: [
        { type: 'file', afterIndex: 0, label: 'missing-path.png', ext: 'png' },
        { type: 'plugin_card', afterIndex: 0 },
        { type: 'cron_confirm', afterIndex: 0, status: 'approved' },
        { type: 'file', afterIndex: 0, filePath: '/tmp/report.pdf', label: 'report.pdf', ext: 'pdf' },
      ],
    });

    const first = items[0];
    expect(first.type).toBe('message');
    if (first.type !== 'message') throw new Error('expected message');
    expect(first.data.blocks?.map(block => block.type)).toEqual(['text', 'file']);
    expect(first.data.blocks?.[1]).toMatchObject({
      type: 'file',
      filePath: '/tmp/report.pdf',
      label: 'report.pdf',
      ext: 'pdf',
    });
  });
});

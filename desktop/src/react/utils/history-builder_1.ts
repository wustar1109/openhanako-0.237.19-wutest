/**
 * history-builder.ts — 将 /api/sessions/messages 的 API 响应转换为 ChatListItem[]
 *
 * 替代 app-messages-shim.ts loadMessages() 中的 DOM 构建循环。
 */

import type { ChatMessage, ChatListItem, ContentBlock } from '../stores/chat-types';
import type { TodoItem } from '../types';
import { parseMoodFromContent, parseCardFromContent, parseUserAttachments } from './message-parser';
import { renderMarkdown } from './markdown';
import { extOfName } from './file-kind';

/* eslint-disable @typescript-eslint/no-explicit-any -- API 历史消息 JSON 结构动态，难以静态收窄 */

const LEGACY_STEER_PREFIX_RE = /^(?:（插话，无需 MOOD）|\(Interjection, no MOOD needed\))\n?/;

// ── API 响应类型 ──

export interface HistoryApiResponse {
  messages: Array<{
    id?: string;
    entryId?: string;
    role: string;
    content: string;
    thinking?: string;
    toolCalls?: Array<{ name: string; args?: Record<string, unknown> }>;
    images?: Array<{ data: string; mimeType: string }>;
    timestamp?: number | string | null;
  }>;
  blocks?: Array<any>;
  // COMPAT(v0.98/v0.127, remove no earlier than v0.133):
  // 以下三个老字段在新服务端不再返回；其中 artifacts 仅保留为旧 session 恢复协议。
  fileOutputs?: Array<{
    afterIndex: number;
    files: Array<{ fileId?: string; filePath: string; label: string; ext: string; mime?: string; kind?: string; storageKind?: string; status?: string; missingAt?: number | null }>;
  }>;
  artifacts?: Array<{
    afterIndex: number;
    artifactId: string;
    artifactType: string;
    title: string;
    content: string;
    language?: string;
    fileId?: string;
    filePath?: string;
    label?: string;
    ext?: string;
    mime?: string;
    kind?: string;
    storageKind?: string;
    status?: string;
    missingAt?: number | null;
  }>;
  cards?: Array<{
    afterIndex: number;
    card: { type: string; pluginId: string; route: string; title?: string; description?: string };
  }>;
  todos?: TodoItem[];
  hasMore?: boolean;
}

// ── 兼容层 ──

/**
 * COMPAT(v0.98/v0.127, remove no earlier than v0.133):
 * 旧历史消息兼容层，可在确认老 session 已完成迁移后整个删除。
 *
 * 将老格式（fileOutputs/artifacts/cards）转为新 blocks[] 格式。
 * 新服务端返回 blocks[]，此函数只在升级过渡期（老服务端 → 新前端）命中。
 * 如果没有 data.blocks，还需从 toolCalls 重建 cron/settings 确认卡片，
 * 因为老 session 的 toolResult.details 没有 jobData/settingKey 字段。
 */
function normalizeBlocks(data: HistoryApiResponse): Array<any> {
  if (data.blocks) return data.blocks.map(normalizeHistoryBlock).filter((block): block is Record<string, any> => !!block);
  const blocks: Array<any> = [];
  for (const fo of (data.fileOutputs || [])) {
    for (const f of fo.files) {
      blocks.push({ type: 'file', afterIndex: fo.afterIndex, ...f });
    }
  }
  for (const ar of (data.artifacts || [])) {
    const { afterIndex, ...artifact } = ar;
    blocks.push({ type: 'artifact', afterIndex, ...artifact });
  }
  for (const cd of (data.cards || [])) {
    blocks.push({ type: 'plugin_card', afterIndex: cd.afterIndex, card: { ...cd.card, type: cd.card.type || 'iframe' } });
  }

  // COMPAT: 从 toolCalls 重建 cron/settings 确认卡片（仅老 session 无 blocks[] 时）
  for (let i = 0; i < (data.messages || []).length; i++) {
    const m = data.messages[i];
    if (m.role !== 'assistant' || !m.toolCalls) continue;
    for (const tc of m.toolCalls) {
      if (tc.name === 'update_settings' && tc.args) {
        const a = tc.args as Record<string, string>;
        if (a.action === 'apply' || (!a.action && a.key && a.value)) {
          blocks.push({
            type: 'settings_confirm',
            afterIndex: i,
            confirmId: '',
            settingKey: a.key || '',
            cardType: (a.key === 'sandbox' || a.key === 'memory.enabled') ? 'toggle' : 'list',
            currentValue: '',
            proposedValue: a.value || '',
            label: a.key || '',
            status: 'confirmed',
          });
        }
      }
      if (tc.name === 'cron' && tc.args) {
        const a = tc.args as Record<string, any>;
        if (a.action === 'add') {
          blocks.push({
            type: 'cron_confirm',
            afterIndex: i,
            confirmId: '',
            jobData: { type: a.type, schedule: a.schedule, prompt: a.prompt, label: a.label },
            status: 'approved',
          });
        }
      }
    }
  }

  return blocks;
}

function isRecord(value: unknown): value is Record<string, any> {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function nonEmptyString(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  return trimmed ? value : null;
}

function normalizeBlockAfterIndex(value: unknown): number | null {
  return Number.isInteger(value) && (value as number) >= 0 ? value as number : null;
}

function basenamePortable(value: string): string {
  const normalized = value.replace(/\\/g, '/');
  const slash = normalized.lastIndexOf('/');
  return slash >= 0 ? normalized.slice(slash + 1) : normalized;
}

function normalizeHistoryBlock(raw: unknown): Record<string, any> | null {
  if (!isRecord(raw)) return null;
  const type = nonEmptyString(raw.type);
  const afterIndex = normalizeBlockAfterIndex(raw.afterIndex);
  if (!type || afterIndex === null) return null;

  if (type === 'file') {
    const filePath = nonEmptyString(raw.filePath);
    if (!filePath) return null;
    const label = nonEmptyString(raw.label) || basenamePortable(filePath);
    const ext = nonEmptyString(raw.ext) || extOfName(label) || extOfName(filePath) || '';
    return { ...raw, type, afterIndex, filePath, label, ext };
  }

  if (type === 'plugin_card') {
    if (!isRecord(raw.card)) return null;
    const pluginId = nonEmptyString(raw.card.pluginId);
    const route = nonEmptyString(raw.card.route);
    if (!pluginId || !route) return null;
    return { ...raw, type, afterIndex, card: { ...raw.card, pluginId, route } };
  }

  if (type === 'cron_confirm') {
    if (!isRecord(raw.jobData)) return null;
    return {
      ...raw,
      type,
      afterIndex,
      jobData: raw.jobData,
      status: nonEmptyString(raw.status) || 'approved',
    };
  }

  if (type === 'screenshot') {
    if (!nonEmptyString(raw.base64) || !nonEmptyString(raw.mimeType)) return null;
  } else if (type === 'settings_update') {
    if (!isRecord(raw.update)) return null;
  } else if (type === 'skill') {
    if (!nonEmptyString(raw.skillName)) return null;
  }

  return { ...raw, type, afterIndex };
}

// ── 构建 ──

function normalizeHistoryTimestamp(value: number | string | null | undefined): number | undefined {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string') {
    const parsed = Date.parse(value);
    return Number.isNaN(parsed) ? undefined : parsed;
  }
  return undefined;
}

export function buildItemsFromHistory(data: HistoryApiResponse): ChatListItem[] {
  const items: ChatListItem[] = [];

  // 按 afterIndex 分组统一 blocks
  const allBlocks = normalizeBlocks(data);
  const blockMap: Record<number, Array<any>> = {};
  for (const b of allBlocks) {
    (blockMap[b.afterIndex] ??= []).push(b);
  }

  for (let i = 0; i < data.messages.length; i++) {
    const m = data.messages[i];
    const id = m.id || `hist-${i}`;
    const timestamp = normalizeHistoryTimestamp(m.timestamp);

    if (m.role === 'user') {
      // strip steer 前缀（内部标记，不应展示给用户）
      const rawContent = (m.content || '')
        .replace(LEGACY_STEER_PREFIX_RE, '')
        .replace(/^<t>[^<]*<\/t>\s*/, '');

      // 过滤系统注入的后台任务通知（steer 消息），不展示给用户
      if (/<hana-background-result\s/.test(rawContent) || /<hana-deferred-tasks>/.test(rawContent)) {
        continue;
      }

      const { text, files, attachedImages, attachedVideos, deskContext, quotedText } = parseUserAttachments(rawContent);
      const fileAtts = files.map(f => ({
        path: f.path,
        name: f.name,
        isDir: f.isDirectory,
      }));
      const imageBlocks = m.images || [];
      const markerImageAtts = attachedImages.map((ref, idx) => {
        const img = imageBlocks[idx];
        return {
          path: ref.path,
          name: ref.name,
          isDir: false,
          mimeType: img?.mimeType,
        };
      });
      const imageAtts = imageBlocks.slice(attachedImages.length).map((img, idx) => ({
        path: `image-${idx}`,
        name: `image-${idx}.${(img.mimeType || 'image/png').split('/')[1] || 'png'}`,
        isDir: false,
        base64Data: img.data,
        mimeType: img.mimeType,
      }));
      const markerVideoAtts = attachedVideos.map((ref) => ({
        path: ref.path,
        name: ref.name,
        isDir: false,
      }));
      const allAtts = [...fileAtts, ...markerImageAtts, ...markerVideoAtts, ...imageAtts];
      const msg: ChatMessage = {
        id,
        sourceEntryId: m.entryId,
        role: 'user',
        text,
        textHtml: text ? renderMarkdown(text) : undefined,
        attachments: allAtts.length ? allAtts : undefined,
        deskContext: deskContext || undefined,
        quotedText: quotedText || undefined,
        timestamp,
      };
      items.push({ type: 'message', data: msg });
    } else if (m.role === 'assistant') {
      const blocks: ContentBlock[] = [];

      // 1. Thinking
      if (m.thinking) {
        blocks.push({ type: 'thinking', content: m.thinking, sealed: true });
      }

      // 2. Mood + 主文本
      const { mood, yuan, text: afterMood } = parseMoodFromContent(m.content);
      if (mood && yuan) {
        blocks.push({ type: 'mood', yuan, text: mood });
      }

      // 3. Tool calls
      if (m.toolCalls?.length) {
        blocks.push({
          type: 'tool_group',
          tools: m.toolCalls.map(tc => ({
            name: tc.name,
            args: tc.args,
            done: true,
            success: true,
          })),
          collapsed: m.toolCalls.length > 1,
        });
      }

      // 4. 主文本（去掉 mood 和 card 后的内容）
      const { cards, text: mainText } = parseCardFromContent(afterMood);
      if (mainText) {
        blocks.push({ type: 'text', html: renderMarkdown(mainText) });
      }

      // 5. Cards (before file outputs)
      for (const card of cards) {
        blocks.push({ type: 'plugin_card', card });
      }

      // 6. Content Blocks from unified sideband
      const msgBlocks = blockMap[i];
      if (msgBlocks) {
        for (const b of msgBlocks) blocks.push(b);
      }

      const msg: ChatMessage = { id, sourceEntryId: m.entryId, role: 'assistant', blocks };
      if (timestamp !== undefined) msg.timestamp = timestamp;
      items.push({ type: 'message', data: msg });
    }
  }

  return items;
}

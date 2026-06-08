/**
 * deep-memory.js — 深度记忆处理器
 *
 * 每日执行一次。遍历所有"脏" session（summary !== snapshot），
 * 通过 snapshot diff 发现新增内容，调 LLM 拆成元事实 + 打标签，
 * 写入 FactStore。
 *
 * 这条链路替代 v1 的 extractMemoryEvents → findNewEvents → 三区间 → score/decay。
 */

import { callText } from "../../core/llm-client.js";
import { getLocale } from "../../server/i18n.js";
import {
  buildFactTimeContext,
  normalizeFactTime,
  resolveMemoryTimeZone,
} from "./time-context.js";
import { createModuleLogger } from "../debug-log.js";

const log = createModuleLogger("deep-memory");

const MAX_RETRIES = 3;
const MAX_CONCURRENT = 3;
const _failCounts = new Map(); // session → { count, lastUpdated }
const FAIL_COUNT_TTL_MS = 60 * 60 * 1000;

function cleanExpiredFailCounts() {
  const cutoff = Date.now() - FAIL_COUNT_TTL_MS;
  for (const [k, v] of _failCounts) {
    if (v.lastUpdated < cutoff) _failCounts.delete(k);
  }
}

/**
 * 处理所有脏 session，提取新增元事实写入 fact-store
 *
 * @param {import('./session-summary.js').SessionSummaryManager} summaryManager
 * @param {import('./fact-store.js').FactStore} factStore
 * @param {{ model: string, api: string, api_key: string, base_url: string }} resolvedModel
 * @returns {Promise<{ processed: number, factsAdded: number }>}
 */
export async function processDirtySessions(summaryManager, factStore, resolvedModel, opts = {}) {
  const dirty = summaryManager.getDirtySessions({ since: opts.since || null });
  if (dirty.length === 0) {
    return { processed: 0, factsAdded: 0 };
  }

  log.log(`${dirty.length} 个脏 session 待处理`);

  let totalFacts = 0;

  const processOne = async (session) => {
    try {
      const sourceTimeRange = !session?.source_time_range && typeof opts.getSourceTimeRange === "function"
        ? await opts.getSourceTimeRange(session.session_id)
        : null;
      const sessionForTime = sourceTimeRange
        ? { ...session, source_time_range: sourceTimeRange }
        : session;
      const timeContext = buildFactTimeContext(sessionForTime, { timeZone: opts.timeZone });
      const facts = await extractFactsFromDiff(
        session.summary,
        session.snapshot || "",
        resolvedModel,
        timeContext,
      );

      if (facts.length > 0) {
        factStore.addBatch(
          facts.map((f) => ({
            fact: f.fact,
            tags: f.tags || [],
            time: f.time || null,
            session_id: session.session_id,
          })),
        );
        totalFacts += facts.length;
        log.log(
          `${session.session_id.slice(0, 8)}...: ${facts.length} 条元事实`,
        );
      }

      summaryManager.markProcessed(session.session_id);
      _failCounts.delete(session.session_id);
    } catch (err) {
      cleanExpiredFailCounts();
      const prev = _failCounts.get(session.session_id);
      const count = (prev?.count || 0) + 1;
      _failCounts.set(session.session_id, { count, lastUpdated: Date.now() });

      if (count >= MAX_RETRIES) {
        log.error(
          `${session.session_id.slice(0, 8)}... 连续失败 ${count} 次，标记跳过: ${err.message}`,
        );
        summaryManager.markProcessed(session.session_id);
        _failCounts.delete(session.session_id);
      } else {
        log.error(
          `处理失败 (${session.session_id.slice(0, 8)}... ${count}/${MAX_RETRIES}): ${err.message}`,
        );
      }
    }
  };

  // 分批并行处理，每批最多 MAX_CONCURRENT 个 LLM 调用
  for (let i = 0; i < dirty.length; i += MAX_CONCURRENT) {
    const batch = dirty.slice(i, i + MAX_CONCURRENT);
    await Promise.allSettled(batch.map(processOne));
  }

  log.log(
    `完成：${dirty.length} 个 session，${totalFacts} 条新元事实`,
  );
  return { processed: dirty.length, factsAdded: totalFacts };
}

/**
 * 从摘要 diff 中提取元事实
 *
 * @param {string} currentSummary - 当前摘要全文
 * @param {string} previousSnapshot - 上次处理时的摘要快照
 * @param {{ model: string, api: string, api_key: string, base_url: string }} resolvedModel
 * @returns {Promise<Array<{ fact: string, tags: string[], time: string }>>}
 */
async function extractFactsFromDiff(currentSummary, previousSnapshot, resolvedModel, timeContext = null) {
  const { model: utilityModel, api, api_key, base_url } = resolvedModel;

  const hasPrevious = !!previousSnapshot;

  const isZh = getLocale().startsWith("zh");

  let userContent;
  const timeContextBlock = buildTimeContextBlock(timeContext, isZh);
  if (hasPrevious) {
    const prevLabel = isZh ? "## 上次快照" : "## Previous Snapshot";
    const currLabel = isZh ? "## 当前摘要" : "## Current Summary";
    userContent = `${timeContextBlock}\n\n${prevLabel}\n\n${previousSnapshot}\n\n${currLabel}\n\n${currentSummary}`;
  } else {
    const label = isZh ? "## 摘要内容" : "## Summary Content";
    userContent = `${timeContextBlock}\n\n${label}\n\n${currentSummary}`;
  }

  const raw = await callText({
    api, model: utilityModel,
    apiKey: api_key,
    baseUrl: base_url,
    systemPrompt: buildFactExtractionPrompt(hasPrevious),
    messages: [{ role: "user", content: userContent }],
    temperature: 0.3,
    maxTokens: 4096,
    timeoutMs: 60_000,
  });

  // 兼容 markdown 代码块包裹（提取最外层 fence 之间的内容）
  const fenceMatch = raw.match(/^```(?:json)?\s*\n([\s\S]*?)\n\s*```\s*$/);
  const jsonStr = (fenceMatch ? fenceMatch[1] : raw).trim();

  try {
    const facts = JSON.parse(jsonStr);
    if (!Array.isArray(facts)) return [];
    return facts
      .filter((f) => f && typeof f.fact === "string" && f.fact.length > 0)
      .map((f) => ({
        ...f,
        time: normalizeFactTime(f.time, timeContext || {}),
      }));
  } catch {
    log.error(`JSON 解析失败: ${jsonStr.slice(0, 200)}`);
    return [];
  }
}

function buildTimeContextBlock(timeContext, isZh) {
  const context = timeContext || {};
  const sourceRange = context.sourceRange || {};
  const timezone = resolveMemoryTimeZone(context.timezone);
  const localDates = Array.isArray(context.localDates) && context.localDates.length > 0
    ? context.localDates.join(", ")
    : isZh ? "未知" : "unknown";
  const range = sourceRange.start || sourceRange.end
    ? `${sourceRange.start || "?"} → ${sourceRange.end || "?"}`
    : isZh ? "未知" : "unknown";
  const summaryDateTimes = Array.isArray(context.summaryDateTimes) && context.summaryDateTimes.length > 0
    ? context.summaryDateTimes.join(", ")
    : isZh ? "无" : "none";

  if (isZh) {
    return `## 时间上下文

- 时区：${timezone}
- 会话来源时间范围：${range}
- 会话来源本地日期：${localDates}
- 摘要中明确出现的完整时间：${summaryDateTimes}

时间规则：只允许使用本时间上下文或摘要正文中明确出现的日期。摘要只有 HH:MM 且会话来源只有一个本地日期时，才能把该日期和 HH:MM 合成 time；摘要跨多日本地日期且只有 HH:MM 时，time 填 null。不要从输出格式示例或说明文字中推断日期。`;
  }

  return `## Time Context

- Timezone: ${timezone}
- Source time range: ${range}
- Source local dates: ${localDates}
- Explicit full timestamps in summary: ${summaryDateTimes}

Time rule: use only dates from this time context or dates explicitly present in the summary text. If the summary has HH:MM only and the source has exactly one local date, combine that date with HH:MM; if the source spans multiple local dates and the summary has HH:MM only, use null. Do not infer dates from output-format examples or explanatory text.`;
}

/**
 * 构建元事实提取 prompt
 */
function buildFactExtractionPrompt(hasPrevious) {
  const isZh = getLocale().startsWith("zh");

  if (isZh) {
    const diffInstruction = hasPrevious
      ? `你会收到两部分输入：
1. **上次快照**：上次已处理的摘要内容
2. **当前摘要**：最新的完整摘要

请找出"当前摘要"相对于"上次快照"新增或变化的内容，将其拆分成独立的元事实。
已经在上次快照中存在的内容不要重复提取。`
      : `将以下摘要内容拆分成独立的元事实。`;

    return `你是一个记忆拆分器。${diffInstruction}

## 规则

1. 只提取用户画像和粗颗粒近况相关的客观事实。
   用户画像包括：身份、人格特质、审美、兴趣、喜欢或讨厌的事物、长期关系、长期关注方向。
   粗颗粒近况包括：用户最近关注的领域/项目/主题，例如"记忆系统""Project Hana""AI Agent"。

2. 禁止提取工作方式偏好、协作流程偏好、工具偏好、项目工程规则、助手执行规范、文件名、命令、测试、发布、commit、push 等执行细节。
   如果一条事实描述的是“以后遇到类似任务应该怎么做”，它应进入经验库或技能，不进入记忆事实。
   如果一条事实描述的是某个主题里的具体子问题、具体方案、具体改法，也不要提取。

3. 每条事实必须是原子的（一条只记一件事）。
   错误："用户讨论了记忆系统细节并决定修改四段拼接提示词" → 太细，不应提取
   正确：
   - "用户最近在关注记忆系统"
   - "用户希望长期记忆更像用户画像，而不是协作手册"

4. 标签用于后续检索，选择有辨识度的关键词，2~5 个。
   标签选择原则：人名、项目名、技术名词、主题类别等

5. time 字段从摘要中的时间标注和“时间上下文”提取，格式 YYYY-MM-DDTHH:MM。
   只使用摘要正文明确出现的日期，或“时间上下文”提供的会话来源本地日期。
   如果摘要只有 HH:MM，且时间上下文只有一个会话来源本地日期，结合该日期和时间标注推算完整时间。
   如果摘要只有 HH:MM，但时间上下文显示会话跨多个本地日期，填 null。
   如果无法确定具体时间，填 null。

6. 不要提取助手的内心活动，只提取客观事实和事件。

7. 如果没有新增内容值得提取，返回空数组 []。

## 输出格式

严格 JSON 数组，不要 markdown 代码块：
[
  {"fact": "用户最近在关注记忆系统", "tags": ["记忆系统", "近况"], "time": null},
  {"fact": "用户希望长期记忆更像用户画像，而不是协作手册", "tags": ["用户画像", "长期记忆", "边界"], "time": null}
]`;
  }

  // English prompt
  const diffInstruction = hasPrevious
    ? `You will receive two inputs:
1. **Previous Snapshot**: the summary content from last processing
2. **Current Summary**: the latest full summary

Find content that is new or changed in "Current Summary" compared to "Previous Snapshot", and split it into independent atomic facts.
Do not re-extract content that already exists in the previous snapshot.`
    : `Split the following summary content into independent atomic facts.`;

  return `You are a memory splitter. ${diffInstruction}

## Rules

1. Extract only objective facts about the user profile and coarse current state.
   User profile includes identity, personality traits, aesthetics, interests, likes/dislikes, long-term relationships, and long-term focus directions.
   Coarse current state includes the broad domain/project/theme the user is recently focused on, such as "memory systems", "Project Hana", or "AI Agent".

2. Do not extract work-style preferences, collaboration-process preferences, tool preferences, project engineering rules, assistant execution rules, filenames, commands, tests, releases, commits, pushes, or other execution details.
   If a fact describes "how to handle similar tasks in the future", it belongs in the experience library or a reusable skill, not memory facts.
   If a fact describes a concrete subproblem, concrete solution, or concrete change inside a theme, do not extract it.

3. Each fact must be atomic (one fact per entry).
   Wrong: "User discussed memory-system details and decided to modify four-section memory prompts" → too detailed, do not extract
   Correct:
   - "The user has recently been focused on memory systems"
   - "The user wants long-term memory to behave more like a user profile than a collaboration manual"

4. Tags are for later retrieval; choose distinctive keywords, 2-5 per fact.
   Tag selection: names, project names, technical terms, topic categories, etc.

5. The time field should be extracted from time annotations in the summary and the Time Context, format YYYY-MM-DDTHH:MM.
   Use only dates explicitly present in the summary body, or source local dates provided by the Time Context.
   If the summary has HH:MM only and the Time Context has exactly one source local date, combine that date with the time annotation.
   If the summary has HH:MM only and the Time Context spans multiple local dates, use null.
   If the exact time cannot be determined, use null.

6. Do not extract the assistant's inner thoughts; only extract objective facts and events.

7. If there is no new content worth extracting, return an empty array [].

## Output Format

Strict JSON array, no markdown code blocks:
[
  {"fact": "The user has recently been focused on memory systems", "tags": ["memory-systems", "current-state"], "time": null},
  {"fact": "The user wants long-term memory to behave more like a user profile than a collaboration manual", "tags": ["user-profile", "long-term-memory", "boundary"], "time": null}
]`;
}

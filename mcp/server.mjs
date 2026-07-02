#!/usr/bin/env node

import fs from "node:fs";
import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import readline from "node:readline";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import * as z from "zod/v4";

const HOME = os.homedir();
const CODEX_HOME = path.resolve(process.env.CODEX_HOME || path.join(HOME, ".codex"));
const INDEX_DIR = path.join(CODEX_HOME, "session-recall");
const INDEX_PATH = path.join(INDEX_DIR, "index.jsonl");
const SESSION_ROOT = path.join(CODEX_HOME, "sessions");
const ARCHIVED_ROOT = path.join(CODEX_HOME, "archived_sessions");

const MAX_RECORD_TEXT = 6000;
const MAX_SESSION_TEXT = 600000;
const SERVER_NAME = "session-recall";
const SERVER_VERSION = "0.3.0";
const INDEX_REFRESH_TTL_MS = 60000;
let lastIndexRefreshCheckMs = 0;

const CJK_SEQUENCE_RE = /[\u3040-\u30ff\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]+/gu;
const CJK_HAS_RE = /[\u3040-\u30ff\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]/u;

const SYNONYM_GROUPS = {
  recall: [
    "找回",
    "召回",
    "回到",
    "回去",
    "找不到",
    "找不回",
    "找不回来",
    "搜不到",
    "检索",
    "搜索",
    "查找",
    "search",
    "recall",
    "retrieve",
    "reopen",
  ],
  session: ["session", "sessions", "thread", "threads", "会话", "线程", "历史", "任务"],
  archive: ["归档", "archived", "archive"],
  semantic: ["语义", "意思", "含义", "大概", "模糊", "近义词", "semantic", "meaning"],
  plugin: ["插件", "plugin", "mcp", "skill"],
  knowledge: ["知识库", "资料库", "记忆库", "knowledge", "memory"],
};

const QUERY_EXPANSION_GROUPS = {
  aside: [
    "Aside browser",
    "Aside AI Browser",
    "aside 浏览器",
    "Aside Discord",
    "Aside research",
    "Aside CLI MCP",
  ],
  "ego lite": [
    "ego-lite",
    "ego lite",
    "Eagle Night",
    "ego lite dashboard",
    "ego-lite Growth",
  ],
  "session recall": [
    "session recall",
    "codex-session-recall",
    "Session Recall",
    "local session search",
    "历史 session 搜索",
  ],
  chatgpt: ["ChatGPT", "GPT", "ChatGPT Codex", "ChatGPT 对话"],
  lark: ["Lark", "Feishu", "飞书", "lark-cli"],
  posthog: ["PostHog", "posthog insight", "dashboard", "analytics"],
};

const NOISE_PATTERNS = {
  aside: [/<\/?aside\b/gi, /\baside[\s.#:[{]/gi, /class=['"][^'"]*\baside\b/gi],
};

const FIELD_LABELS = {
  title: "title",
  first_user_message: "first user message",
  preview: "preview",
  cwd: "workspace path",
  content: "session body",
};

function utcNowMs() {
  return Date.now();
}

function parseIsoMs(value) {
  if (!value) return null;
  const parsed = Date.parse(String(value).replace("Z", "+00:00"));
  return Number.isNaN(parsed) ? null : parsed;
}

function compactText(value, maxChars = MAX_RECORD_TEXT) {
  if (value === null || value === undefined) return "";
  let text = "";
  if (typeof value === "string") {
    text = value;
  } else {
    try {
      text = JSON.stringify(value);
    } catch {
      text = String(value);
    }
  }
  text = text.replace(/\s+/g, " ").trim();
  if (text.length > maxChars) return `${text.slice(0, maxChars)} ...`;
  return text;
}

function cleanTitle(text, maxChars = 90) {
  const compact = compactText(text, maxChars + 30);
  if (compact.length <= maxChars) return compact;
  return `${compact.slice(0, maxChars - 3).trimEnd()}...`;
}

function threadIdFromPath(filePath) {
  const match = path.basename(filePath).match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i);
  return match ? match[0].toLowerCase() : null;
}

function isInside(parent, child) {
  const relative = path.relative(parent, child);
  return Boolean(relative) && !relative.startsWith("..") && !path.isAbsolute(relative);
}

function contentItemText(item) {
  if (!item || typeof item !== "object" || Array.isArray(item)) return compactText(item);
  const itemType = item.type || "";
  if (["input_text", "output_text", "text"].includes(itemType)) return compactText(item.text || "");
  if (["tool_result", "function_call_output"].includes(itemType)) {
    return compactText(item.output || item.content || item);
  }
  return compactText(item.text || item.content || "");
}

function extractResponseMessage(payload) {
  const role = payload?.role;
  if (!["user", "assistant"].includes(role)) return null;
  const content = payload.content;
  const parts = [];
  if (Array.isArray(content)) {
    for (const item of content) {
      const text = contentItemText(item);
      if (text) parts.push(text);
    }
  } else if (typeof content === "string") {
    parts.push(compactText(content));
  }
  const text = parts.join("\n").trim();
  if (!text || text.startsWith("<environment_context>")) return null;
  return [role, text];
}

function appendUnique(messages, seen, role, text, lineNo) {
  const compact = compactText(text);
  if (!compact) return;
  const key = `${role}:${compact.slice(0, 500)}`;
  if (seen.has(key)) return;
  seen.add(key);
  messages.push({ role, text: compact, line: lineNo });
}

async function parseSessionFile(filePath) {
  let stat;
  try {
    stat = await fsp.stat(filePath);
  } catch {
    return null;
  }

  let threadId = threadIdFromPath(filePath);
  let cwd = "";
  let createdAtMs = null;
  const updatedAtMs = Math.trunc(stat.mtimeMs);
  const messages = [];
  const seenMessages = new Set();
  const indexedFragments = [];
  let source = "";

  try {
    const input = fs.createReadStream(filePath, { encoding: "utf8" });
    const lines = readline.createInterface({ input, crlfDelay: Infinity });
    let lineNo = 0;

    for await (const line of lines) {
      lineNo += 1;
      let obj;
      try {
        obj = JSON.parse(line);
      } catch {
        continue;
      }

      const payload = obj.payload || {};
      const recordType = obj.type;
      const payloadType = payload.type;

      if (recordType === "session_meta") {
        threadId = payload.session_id || payload.id || threadId;
        cwd = payload.cwd || cwd;
        source = payload.source || source;
        createdAtMs = parseIsoMs(payload.timestamp) || createdAtMs;
        continue;
      }

      if (recordType === "turn_context") {
        cwd = payload.cwd || cwd;
        continue;
      }

      if (recordType === "event_msg") {
        if (payloadType === "user_message") {
          const text = payload.message || "";
          appendUnique(messages, seenMessages, "user", text, lineNo);
          indexedFragments.push(`user: ${compactText(text)}`);
        } else if (payloadType === "agent_message") {
          const text = payload.message || "";
          appendUnique(messages, seenMessages, "assistant", text, lineNo);
          indexedFragments.push(`assistant: ${compactText(text)}`);
        } else if (["exec_command", "command_started"].includes(payloadType)) {
          const text = payload.cmd || payload.command || payload;
          indexedFragments.push(`command: ${compactText(text)}`);
        } else if (["exec_command_output", "command_output"].includes(payloadType)) {
          const text = payload.output || payload.message || payload;
          indexedFragments.push(`command output: ${compactText(text, 2000)}`);
        } else if (["web_search_end", "browser_action"].includes(payloadType)) {
          indexedFragments.push(compactText(payload, 2000));
        }
        continue;
      }

      if (recordType === "response_item") {
        if (payloadType === "message") {
          const msg = extractResponseMessage(payload);
          if (msg) {
            const [role, text] = msg;
            appendUnique(messages, seenMessages, role, text, lineNo);
            indexedFragments.push(`${role}: ${compactText(text)}`);
          }
        } else if (["function_call", "tool_call"].includes(payloadType)) {
          const name = payload.name || payload.tool_name || "tool";
          const args = payload.arguments || payload.input || "";
          indexedFragments.push(`tool call ${name}: ${compactText(args, 2000)}`);
        } else if (["function_call_output", "tool_result"].includes(payloadType)) {
          const text = payload.output || payload.content || payload;
          indexedFragments.push(`tool output: ${compactText(text, 2000)}`);
        } else if (payloadType === "web_search_call") {
          indexedFragments.push(compactText(payload, 2000));
        }
      }
    }

    if (!threadId) return null;

    const firstUser = messages.find((message) => message.role === "user")?.text || "";
    const lastAssistant = [...messages].reverse().find((message) => message.role === "assistant")?.text || "";
    const title = cleanTitle(firstUser) || threadId;
    const preview = cleanTitle(firstUser || lastAssistant, 220);
    let content = indexedFragments.join("\n");
    if (content.length > MAX_SESSION_TEXT) content = `${content.slice(0, MAX_SESSION_TEXT)}\n...`;

    return {
      thread_id: threadId,
      title,
      cwd,
      created_at_ms: Math.trunc(createdAtMs || stat.ctimeMs || 0),
      updated_at_ms: updatedAtMs,
      archived: isInside(ARCHIVED_ROOT, filePath) ? 1 : 0,
      rollout_path: filePath,
      first_user_message: firstUser,
      preview,
      message_count: messages.length,
      source: compactText(source, 200),
      content,
      messages,
      file_mtime_ms: updatedAtMs,
    };
  } catch {
    return null;
  }
}

async function pathExists(target) {
  try {
    await fsp.access(target);
    return true;
  } catch {
    return false;
  }
}

async function walkJsonl(root) {
  const output = [];
  if (!(await pathExists(root))) return output;
  async function visit(dir) {
    let entries = [];
    try {
      entries = await fsp.readdir(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const fullPath = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        await visit(fullPath);
      } else if (entry.isFile() && entry.name.endsWith(".jsonl")) {
        output.push(fullPath);
      }
    }
  }
  await visit(root);
  return output;
}

async function sessionPaths() {
  const all = [...(await walkJsonl(SESSION_ROOT)), ...(await walkJsonl(ARCHIVED_ROOT))];
  const unique = [...new Set(all.map((item) => path.resolve(item)))];
  const withTimes = [];
  for (const item of unique) {
    try {
      const stat = await fsp.stat(item);
      withTimes.push([item, stat.mtimeMs]);
    } catch {
      continue;
    }
  }
  withTimes.sort((a, b) => b[1] - a[1]);
  return withTimes.map(([item]) => item);
}

async function loadIndexRows() {
  try {
    const text = await fsp.readFile(INDEX_PATH, "utf8");
    const rows = [];
    for (const line of text.split(/\r?\n/)) {
      if (!line.trim()) continue;
      try {
        rows.push(JSON.parse(line));
      } catch {
        continue;
      }
    }
    return rows;
  } catch {
    return [];
  }
}

async function loadIndexMap() {
  const rows = await loadIndexRows();
  return new Map(rows.filter((row) => row.thread_id).map((row) => [row.thread_id, row]));
}

async function writeIndexRows(rows) {
  await fsp.mkdir(INDEX_DIR, { recursive: true });
  rows.sort((a, b) => Number(b.updated_at_ms || 0) - Number(a.updated_at_ms || 0));
  const body = rows.map((row) => JSON.stringify(row)).join("\n");
  const tmp = `${INDEX_PATH}.tmp`;
  await fsp.writeFile(tmp, body ? `${body}\n` : "", "utf8");
  await fsp.rename(tmp, INDEX_PATH);
}

async function refreshIndex(force = false) {
  const known = await loadIndexMap();
  const paths = await sessionPaths();
  const now = utcNowMs();
  let indexed = 0;
  let skipped = 0;
  let errors = 0;
  const rows = [];

  for (const filePath of paths) {
    const pathThreadId = threadIdFromPath(filePath);
    let statUpdated = 0;
    try {
      statUpdated = Math.trunc((await fsp.stat(filePath)).mtimeMs);
    } catch {
      errors += 1;
      continue;
    }

    const old = pathThreadId ? known.get(pathThreadId) : null;
    if (!force && old && Number(old.file_mtime_ms || old.indexed_at_ms || 0) >= statUpdated && old.rollout_path === filePath) {
      rows.push(old);
      skipped += 1;
      continue;
    }

    const parsed = await parseSessionFile(filePath);
    if (!parsed) {
      errors += 1;
      continue;
    }
    rows.push({ ...parsed, indexed_at_ms: now, file_mtime_ms: statUpdated });
    indexed += 1;
  }

  await writeIndexRows(rows);
  return {
    index_path: INDEX_PATH,
    scanned_files: paths.length,
    indexed_or_updated: indexed,
    skipped_unchanged: skipped,
    errors,
    total_sessions: rows.length,
    index_format: "jsonl",
  };
}

async function ensureIndex() {
  if (!(await pathExists(INDEX_PATH))) {
    await refreshIndex(true);
    lastIndexRefreshCheckMs = utcNowMs();
    return;
  }
  const now = utcNowMs();
  if (now - lastIndexRefreshCheckMs >= INDEX_REFRESH_TTL_MS) {
    await refreshIndex(false);
    lastIndexRefreshCheckMs = now;
  }
}

function splitQuery(query) {
  const clean = String(query || "").trim().toLowerCase();
  const terms = clean.match(/[a-z0-9_./:-]+|[\u3040-\u30ff\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]+/giu) || [];
  const expanded = [];
  for (const term of terms) {
    expanded.push(term);
    if (CJK_HAS_RE.test(term) && term.length >= 5) {
      for (let idx = 0; idx < term.length - 1; idx += 1) {
        expanded.push(term.slice(idx, idx + 2));
      }
    }
  }
  return expanded.filter((term) => term.length >= 2);
}

function normalizeSpace(text) {
  return String(text || "").replace(/\s+/g, " ").trim();
}

function expandQueryVariants(query, maxVariants = 5) {
  const clean = normalizeSpace(query);
  if (!clean) return [];
  const variants = [clean];
  const queryLower = clean.toLowerCase();

  for (const [key, expansions] of Object.entries(QUERY_EXPANSION_GROUPS)) {
    if (queryLower.includes(key) || expansions.some((item) => queryLower.includes(item.toLowerCase()))) {
      variants.push(...expansions);
    }
  }

  if (clean.includes("-")) variants.push(clean.replace(/-/g, " "));
  if (clean.includes(" ")) variants.push(clean.replace(/\s+/g, "-"));

  const tokens = splitQuery(clean);
  if (tokens.length > 1 && tokens.length <= 5) variants.push(tokens.join(" "));
  for (const token of tokens) {
    if (token.length >= 4) variants.push(token);
  }

  return [...new Set(variants.map((item) => normalizeSpace(item)).filter(Boolean))].slice(0, maxVariants);
}

function countMatches(regex, text) {
  const matches = text.match(regex);
  return matches ? matches.length : 0;
}

function noisePenalty(row, query, terms) {
  const text = [
    row.title || "",
    row.first_user_message || "",
    row.preview || "",
    row.cwd || "",
    String(row.content || "").slice(0, 120000),
  ]
    .join("\n")
    .toLowerCase();
  let penalty = 0;
  const termSet = new Set([...terms, ...splitQuery(query)]);
  for (const [term, patterns] of Object.entries(NOISE_PATTERNS)) {
    if (!termSet.has(term)) continue;
    const noisyHits = patterns.reduce((count, pattern) => count + countMatches(pattern, text), 0);
    if (noisyHits <= 0) continue;
    const productContext = [
      `${term} browser`,
      `${term} ai browser`,
      `${term} discord`,
      `${term} research`,
      `${term} 浏览器`,
    ].some((phrase) => text.includes(phrase));
    if (!productContext) penalty += Math.min(noisyHits, 10) * 12;
  }
  return penalty;
}

function increment(counter, key, amount = 1) {
  counter.set(key, (counter.get(key) || 0) + amount);
}

function conceptTokens(text, maxChars = 80000) {
  const textLower = String(text || "").slice(0, maxChars).toLowerCase();
  const tokens = new Map();

  for (const word of textLower.match(/[a-z0-9_./:-]{2,}/giu) || []) {
    increment(tokens, word);
  }

  for (const match of textLower.matchAll(CJK_SEQUENCE_RE)) {
    const seq = match[0];
    if (seq.length >= 2) increment(tokens, seq);
    for (const size of [2, 3, 4]) {
      if (seq.length >= size) {
        for (let idx = 0; idx <= seq.length - size; idx += 1) {
          increment(tokens, seq.slice(idx, idx + size));
        }
      }
    }
  }

  for (const [canonical, variants] of Object.entries(SYNONYM_GROUPS)) {
    for (const variant of variants) {
      if (textLower.includes(variant.toLowerCase())) {
        increment(tokens, `concept:${canonical}`, 4);
        increment(tokens, canonical, 2);
      }
    }
  }

  return tokens;
}

function weightedConceptVector(row) {
  const vector = new Map();
  const sections = [
    [row.title || "", 5],
    [row.first_user_message || "", 4],
    [row.preview || "", 3],
    [row.cwd || "", 2],
    [row.content || "", 1],
  ];
  for (const [text, weight] of sections) {
    for (const [token, count] of conceptTokens(text)) {
      increment(vector, token, count * weight);
    }
  }
  return vector;
}

function conceptCosine(queryVector, docVector) {
  if (!queryVector.size || !docVector.size) return 0;
  let dot = 0;
  for (const [token, value] of queryVector) {
    dot += value * (docVector.get(token) || 0);
  }
  const normQuery = Math.sqrt([...queryVector.values()].reduce((sum, value) => sum + value * value, 0));
  const normDoc = Math.sqrt([...docVector.values()].reduce((sum, value) => sum + value * value, 0));
  if (normQuery <= 0 || normDoc <= 0) return 0;
  return dot / (normQuery * normDoc);
}

function scoreLocalConceptRow(row, query, terms) {
  const queryVector = conceptTokens(query, 4000);
  if (!queryVector.size) return [0, ""];
  const docVector = weightedConceptVector(row);
  const similarity = conceptCosine(queryVector, docVector);

  let directBonus = 0;
  const rowText = [
    row.title || "",
    row.first_user_message || "",
    row.preview || "",
    row.cwd || "",
    String(row.content || "").slice(0, 80000),
  ]
    .join("\n")
    .toLowerCase();
  const queryLower = String(query || "").toLowerCase();
  for (const variants of Object.values(SYNONYM_GROUPS)) {
    if (
      variants.some((variant) => queryLower.includes(variant.toLowerCase())) &&
      variants.some((variant) => rowText.includes(variant.toLowerCase()))
    ) {
      directBonus += 8;
    }
  }
  if (terms.length) {
    const matched = terms.filter((term) => rowText.includes(term)).length;
    directBonus += Math.min(matched, 6) * 2;
  }

  const score = similarity * 240 + directBonus - noisePenalty(row, query, terms);
  if (score < 10) return [0, ""];
  return [score, makeSnippet(row, [String(query || "").toLowerCase(), ...terms])];
}

function scoreRow(row, query, terms) {
  const queryLower = String(query || "").toLowerCase().trim();
  const title = String(row.title || "").toLowerCase();
  const first = String(row.first_user_message || "").toLowerCase();
  const preview = String(row.preview || "").toLowerCase();
  const cwd = String(row.cwd || "").toLowerCase();
  const content = String(row.content || "").toLowerCase();

  let score = 0;
  if (queryLower && title.includes(queryLower)) score += 120;
  if (queryLower && first.includes(queryLower)) score += 100;
  if (queryLower && preview.includes(queryLower)) score += 80;
  if (queryLower && cwd.includes(queryLower)) score += 50;
  if (queryLower && content.includes(queryLower)) score += 60;

  let matchedTerms = 0;
  for (const term of terms) {
    if (title.includes(term)) {
      score += 18;
      matchedTerms += 1;
    } else if (first.includes(term) || preview.includes(term)) {
      score += 12;
      matchedTerms += 1;
    } else if (cwd.includes(term)) {
      score += 8;
      matchedTerms += 1;
    } else if (content.includes(term)) {
      score += 5;
      matchedTerms += 1;
    }
  }

  if (terms.length && matchedTerms === 0) return [0, ""];
  if (terms.length) score *= 1 + Math.min(matchedTerms / Math.max(terms.length, 1), 1);

  const ageDays = Math.max((utcNowMs() - Number(row.updated_at_ms || 0)) / 86400000, 0);
  score += Math.max(0, 8 - Math.min(ageDays, 8)) / 2;
  score -= noisePenalty(row, query, terms);
  if (score <= 0) return [0, ""];
  return [score, makeSnippet(row, [queryLower, ...terms])];
}

function projectFromCwd(cwd) {
  if (!cwd) return "";
  return path.basename(cwd) || cwd;
}

function matchedFieldDetails(row, query, terms, maxTerms = 8) {
  const needles = [...new Set([String(query || "").toLowerCase().trim(), ...terms].filter(Boolean))];
  const fields = {
    title: row.title || "",
    first_user_message: row.first_user_message || "",
    preview: row.preview || "",
    cwd: row.cwd || "",
    content: row.content || "",
  };
  const matchedFields = [];
  const matchedTerms = [];
  for (const [field, value] of Object.entries(fields)) {
    const valueLower = String(value).toLowerCase();
    let fieldHit = false;
    for (const needle of needles) {
      if (needle && valueLower.includes(needle)) {
        fieldHit = true;
        if (matchedTerms.length < maxTerms) matchedTerms.push(needle);
      }
    }
    if (fieldHit) matchedFields.push(field);
  }
  return [[...new Set(matchedFields)], [...new Set(matchedTerms)]];
}

function confidenceLabel(score, matchType, matchedFields) {
  const strongFields = new Set(["title", "first_user_message", "preview"]);
  if (score >= 160 || (score >= 90 && matchedFields.some((field) => strongFields.has(field)))) return "high";
  if (score >= 45 || matchedFields.length) return "medium";
  return "low";
}

function matchSummary(row, query, terms, score, matchType) {
  const [matchedFields, matchedTerms] = matchedFieldDetails(row, query, terms);
  const confidence = confidenceLabel(score, matchType, matchedFields);
  let summary = "";
  if (matchedFields.length) {
    const fields = matchedFields.slice(0, 3).map((field) => FIELD_LABELS[field] || field).join(", ");
    const termText = matchedTerms.slice(0, 3).join(", ");
    summary = `Matched ${fields}`;
    if (termText) summary += ` for: ${termText}`;
    if (matchType === "smart") summary += "; concept match";
  } else {
    summary = matchType === "smart" ? "Concept match from session content" : "Weak local match";
  }
  return [summary, matchedFields, matchedTerms, confidence];
}

function makeSnippet(row, needles, width = 130) {
  const sources = [row.title || "", row.first_user_message || "", row.preview || "", row.content || "", row.cwd || ""];
  for (const needle of needles) {
    if (!needle) continue;
    for (const source of sources) {
      const sourceText = String(source);
      const pos = sourceText.toLowerCase().indexOf(String(needle).toLowerCase());
      if (pos >= 0) {
        const start = Math.max(0, pos - Math.trunc(width / 2));
        const end = Math.min(sourceText.length, pos + String(needle).length + Math.trunc(width / 2));
        const prefix = start > 0 ? "..." : "";
        const suffix = end < sourceText.length ? "..." : "";
        return `${prefix}${compactText(sourceText.slice(start, end), width * 2)}${suffix}`;
      }
    }
  }
  return compactText(row.preview || row.first_user_message || row.title, width * 2);
}

function fmtTime(ms) {
  if (!ms) return "";
  const date = new Date(ms);
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

async function searchSessions({
  query,
  limit = 10,
  include_archived = true,
  cwd_contains = null,
  mode = "keyword",
  refresh = true,
}) {
  if (refresh) await ensureIndex();
  const finalLimit = Math.max(1, Math.min(Number.parseInt(limit || 10, 10), 50));
  const finalMode = String(mode || "keyword").toLowerCase();
  if (!["keyword", "smart", "hybrid"].includes(finalMode)) {
    throw new Error("mode must be one of: keyword, smart, hybrid");
  }

  const terms = splitQuery(query);
  let rows = await loadIndexRows();
  if (!include_archived) rows = rows.filter((row) => !Boolean(row.archived));
  if (cwd_contains) {
    const cwdNeedle = String(cwd_contains).toLowerCase();
    rows = rows.filter((row) => String(row.cwd || "").toLowerCase().includes(cwdNeedle));
  }
  rows.sort((a, b) => Number(b.updated_at_ms || 0) - Number(a.updated_at_ms || 0));

  const keywordResults = new Map();
  const smartResults = new Map();
  for (const row of rows) {
    if (["keyword", "hybrid"].includes(finalMode)) {
      const [score, snippet] = scoreRow(row, query, terms);
      if (score > 0) keywordResults.set(row.thread_id, { score, row, snippet });
    }
    if (["smart", "hybrid"].includes(finalMode)) {
      const [score, snippet] = scoreLocalConceptRow(row, query, terms);
      if (score > 0) smartResults.set(row.thread_id, { score, row, snippet });
    }
  }

  const combined = new Map();
  for (const [threadId, item] of keywordResults) {
    combined.set(threadId, { score: item.score, row: item.row, snippet: item.snippet, matchType: "keyword", keywordScore: item.score, smartScore: 0 });
  }
  for (const [threadId, item] of smartResults) {
    if (combined.has(threadId)) {
      const old = combined.get(threadId);
      combined.set(threadId, {
        score: old.score + item.score,
        row: old.row,
        snippet: old.keywordScore >= item.score ? old.snippet : item.snippet,
        matchType: old.matchType !== "smart" ? "hybrid" : "smart",
        keywordScore: old.keywordScore,
        smartScore: item.score,
      });
    } else {
      combined.set(threadId, { score: item.score, row: item.row, snippet: item.snippet, matchType: "smart", keywordScore: 0, smartScore: item.score });
    }
  }

  const sorted = [...combined.values()].sort(
    (a, b) => b.score - a.score || Number(b.row.updated_at_ms || 0) - Number(a.row.updated_at_ms || 0),
  );
  const results = [];
  for (const item of sorted.slice(0, finalLimit)) {
    const row = item.row;
    const [summary, matchedFields, matchedTerms, confidence] = matchSummary(row, query, terms, item.score, item.matchType);
    results.push({
      thread_id: row.thread_id,
      title: row.title,
      result_label: cleanTitle(row.title, 80),
      cwd: row.cwd,
      project: projectFromCwd(row.cwd),
      archived: Boolean(row.archived),
      status: Boolean(row.archived) ? "archived" : "current",
      updated_at: fmtTime(Number(row.updated_at_ms || 0)),
      created_at: fmtTime(Number(row.created_at_ms || 0)),
      message_count: row.message_count,
      score: Math.round(item.score * 100) / 100,
      keyword_score: Math.round(item.keywordScore * 100) / 100,
      smart_score: Math.round(item.smartScore * 100) / 100,
      match_type: item.matchType,
      confidence,
      match_summary: summary,
      matched_fields: matchedFields,
      matched_terms: matchedTerms,
      snippet: item.snippet,
      open_url: `codex://threads/${row.thread_id}`,
      open_url_reliability:
        "unreliable_from_assistant_markdown; prefer Codex internal thread navigation when available",
      open_instruction: `Ask the assistant to open thread ${row.thread_id}.`,
      rollout_path: row.rollout_path,
    });
  }

  return {
    query,
    mode: finalMode,
    count: results.length,
    results,
    note:
      "Show thread_id and ask the user to reply with the result number or thread_id to open it. Avoid presenting codex://threads/<thread_id> as a Markdown link because it can route to a blank loading page in the desktop app. For vague memory, generate several query variants and use search_many_sessions.",
  };
}

async function searchManySessions({
  queries,
  limit_per_query = 12,
  final_limit = 30,
  include_archived = true,
  cwd_contains = null,
  mode = "keyword",
  auto_expand = true,
}) {
  await ensureIndex();
  let cleanQueries = (Array.isArray(queries) ? queries : []).map((query) => compactText(query, 300)).filter(Boolean);
  if (auto_expand) {
    const expanded = [];
    for (const query of cleanQueries) expanded.push(...expandQueryVariants(query));
    cleanQueries = expanded;
  }
  cleanQueries = [...new Set(cleanQueries)].slice(0, 18);
  const perQueryLimit = Math.max(1, Math.min(Number.parseInt(limit_per_query || 12, 10), 50));
  const mergedLimit = Math.max(1, Math.min(Number.parseInt(final_limit || 30, 10), 100));

  const merged = new Map();
  const primaryQuery = cleanQueries[0] || "";
  for (const query of cleanQueries) {
    const queryMode = auto_expand && mode === "hybrid" && query !== primaryQuery ? "keyword" : mode;
    const response = await searchSessions({
      query,
      limit: perQueryLimit,
      include_archived,
      cwd_contains,
      mode: queryMode,
      refresh: false,
    });
    for (const result of response.results) {
      const existing = merged.get(result.thread_id);
      if (!existing) {
        merged.set(result.thread_id, {
          ...result,
          matched_queries: [query],
          aggregate_score: result.score,
          _best_variant_score: result.score,
        });
      } else {
        existing.matched_queries.push(query);
        existing.aggregate_score += result.score * 0.75;
        existing.score = Math.max(existing.score, result.score);
        if (result.score > (existing._best_variant_score || 0)) {
          existing._best_variant_score = result.score;
          existing.snippet = result.snippet;
          existing.match_summary = result.match_summary || existing.match_summary || "";
          existing.confidence = result.confidence || existing.confidence || "medium";
          existing.matched_fields = result.matched_fields || existing.matched_fields || [];
          existing.matched_terms = result.matched_terms || existing.matched_terms || [];
        }
      }
    }
  }

  const results = [...merged.values()];
  for (const item of results) {
    item.aggregate_score += Math.min(item.matched_queries.length, 5) * 10;
    delete item._best_variant_score;
  }
  results.sort((a, b) => b.aggregate_score - a.aggregate_score || String(b.updated_at).localeCompare(String(a.updated_at)));
  return {
    queries: cleanQueries,
    auto_expand,
    mode,
    count: Math.min(results.length, mergedLimit),
    results: results.slice(0, mergedLimit),
    note: "These are high-recall candidates. Codex should semantically rerank them against the user's original intent before answering.",
  };
}

async function getSession({ thread_id, query = null, max_messages = 24 }) {
  await ensureIndex();
  const limit = Math.max(1, Math.min(Number.parseInt(max_messages || 24, 10), 80));
  const rows = await loadIndexRows();
  const row = rows.find((item) => item.thread_id === thread_id);
  if (!row) throw new Error(`No indexed session found for thread_id: ${thread_id}`);

  const parsed = await parseSessionFile(row.rollout_path);
  const messages = parsed?.messages || [];
  let selected = [];
  if (query) {
    const terms = [String(query).toLowerCase(), ...splitQuery(query)];
    const matches = [];
    messages.forEach((message, idx) => {
      const textLower = String(message.text || "").toLowerCase();
      if (terms.some((term) => term && textLower.includes(term))) matches.push(idx);
    });
    const picked = [];
    for (const idx of matches) {
      for (let around = Math.max(0, idx - 1); around < Math.min(messages.length, idx + 2); around += 1) {
        picked.push(around);
      }
    }
    const seen = new Set();
    for (const idx of picked) {
      if (seen.has(idx)) continue;
      seen.add(idx);
      selected.push(messages[idx]);
      if (selected.length >= limit) break;
    }
  } else {
    const head = messages.slice(0, Math.min(8, Math.trunc(limit / 2)));
    const tail = messages.length > head.length ? messages.slice(Math.max(head.length, messages.length - limit + head.length)) : [];
    selected = [...head, ...tail];
  }

  return {
    thread_id: row.thread_id,
    title: row.title,
    cwd: row.cwd,
    archived: Boolean(row.archived),
    created_at: fmtTime(Number(row.created_at_ms || 0)),
    updated_at: fmtTime(Number(row.updated_at_ms || 0)),
    message_count: row.message_count,
    open_url: `codex://threads/${row.thread_id}`,
    open_url_reliability:
      "unreliable_from_assistant_markdown; prefer Codex internal thread navigation when available",
    open_instruction: `Ask the assistant to open thread ${row.thread_id}.`,
    rollout_path: row.rollout_path,
    messages: selected,
  };
}

async function callTool(name, args) {
  const argumentsObject = args || {};
  if (name === "refresh_index") return refreshIndex(Boolean(argumentsObject.force));
  if (name === "search_sessions") {
    return searchSessions({
      query: String(argumentsObject.query || ""),
      limit: argumentsObject.limit ?? 10,
      include_archived: argumentsObject.include_archived ?? true,
      cwd_contains: argumentsObject.cwd_contains || null,
      mode: String(argumentsObject.mode || "keyword"),
    });
  }
  if (name === "search_many_sessions") {
    return searchManySessions({
      queries: argumentsObject.queries || [],
      limit_per_query: argumentsObject.limit_per_query ?? 12,
      final_limit: argumentsObject.final_limit ?? 30,
      include_archived: argumentsObject.include_archived ?? true,
      cwd_contains: argumentsObject.cwd_contains || null,
      mode: String(argumentsObject.mode || "keyword"),
      auto_expand: argumentsObject.auto_expand ?? true,
    });
  }
  if (name === "get_session") {
    return getSession({
      thread_id: String(argumentsObject.thread_id || ""),
      query: argumentsObject.query || null,
      max_messages: argumentsObject.max_messages ?? 24,
    });
  }
  throw new Error(`Unknown tool: ${name}`);
}

function toolResult(payload) {
  return {
    content: [{ type: "text", text: JSON.stringify(payload, null, 2) }],
  };
}

function registerTools(server) {
  server.registerTool(
    "refresh_index",
    {
      description: "Scan local session files and refresh the searchable index.",
      inputSchema: {
        force: z.boolean().optional().default(false).describe("Reindex every session even if it appears unchanged."),
      },
    },
    async ({ force }) => toolResult(await refreshIndex(Boolean(force))),
  );

  server.registerTool(
    "search_sessions",
    {
      description:
        "Search local sessions by remembered content and return links for going back to the original session.",
      inputSchema: {
        query: z.string().describe("Natural language, keyword, filename, project name, or phrase to search for."),
        limit: z.number().int().min(1).max(50).optional().default(10).describe("Maximum results to return."),
        include_archived: z.boolean().optional().default(true).describe("Include archived sessions."),
        cwd_contains: z.string().optional().describe("Optional path substring filter for the session workspace."),
        mode: z
          .enum(["keyword", "smart", "hybrid"])
          .optional()
          .default("keyword")
          .describe("keyword is exact local search. smart is no-key local concept search. hybrid combines both."),
      },
    },
    async (args) => toolResult(await callTool("search_sessions", args)),
  );

  server.registerTool(
    "search_many_sessions",
    {
      description: "Run multiple local query variants and merge high-recall session candidates for model reranking.",
      inputSchema: {
        queries: z.array(z.string()).describe("Several query variants generated from the user's fuzzy memory."),
        limit_per_query: z.number().int().min(1).max(50).optional().default(12).describe("Maximum results per query variant."),
        final_limit: z.number().int().min(1).max(100).optional().default(30).describe("Maximum merged candidates."),
        include_archived: z.boolean().optional().default(true).describe("Include archived sessions."),
        cwd_contains: z.string().optional().describe("Optional path substring filter for the session workspace."),
        mode: z
          .enum(["keyword", "smart", "hybrid"])
          .optional()
          .default("keyword")
          .describe("keyword with auto_expand is fastest. Use hybrid or smart for a deeper fallback."),
        auto_expand: z
          .boolean()
          .optional()
          .default(true)
          .describe("Automatically expand vague queries with aliases, language variants, and punctuation variants."),
      },
    },
    async (args) => toolResult(await callTool("search_many_sessions", args)),
  );

  server.registerTool(
    "get_session",
    {
      description: "Read a matched session summary and selected message excerpts.",
      inputSchema: {
        thread_id: z.string().describe("Session UUID returned by search_sessions."),
        query: z.string().optional().describe("Optional query for selecting relevant excerpts from the session."),
        max_messages: z.number().int().min(1).max(80).optional().default(24).describe("Maximum message excerpts to return."),
      },
    },
    async (args) => toolResult(await callTool("get_session", args)),
  );
}

async function main() {
  const server = new McpServer({
    name: SERVER_NAME,
    version: SERVER_VERSION,
  });
  registerTools(server);
  await server.connect(new StdioServerTransport());
}

main().catch((error) => {
  console.error(error?.stack || error);
  process.exit(1);
});

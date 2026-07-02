---
name: session-recall
description: Search local session history by remembered content, then help the user return to the matching session.
---

# Session Recall

Use this skill when the user wants to find, recall, search, reopen, or inspect an older session and they remember the content better than the title.

## Workflow

1. Use the `session_recall` MCP tools.
2. If this is the first use in the thread, or results look stale, call `refresh_index`.
3. For exact searches, call `search_sessions` with the user's remembered phrase or description.
   - Use `mode: "keyword"` for exact phrase, filename, command, project, or title searches.
   - Use `mode: "smart"` for no-key local concept search.
   - Use `mode: "hybrid"` when the user says they only remember the meaning, concept, or vague context.
4. For vague-memory searches, do agentic recall:
   - Generate 5-10 query variants from the user's intent. Include likely synonyms in the user's language and any languages visible in the remembered content.
   - Add product names, actions, files, project names, and alternative phrasings. Do not assume the content is Chinese or English.
   - First call `search_many_sessions` with those variants, `mode: "keyword"`, `auto_expand: true`, and a broad `final_limit` such as 30-50.
   - If results are weak, call `search_many_sessions` again with `mode: "hybrid"` or `mode: "smart"` as a deeper fallback.
   - Use the assistant's semantic judgment to rerank the returned candidates against the user's original intent.
   - If the top candidates are close or ambiguous, call `get_session` for 2-4 candidates before answering.
5. Show the top matches with title, updated time, workspace path when useful, archived status, why it matched, the returned snippet, and the thread id.
6. If the user asks for more detail before opening, call `get_session` on the best matching `thread_id`.
7. If the user asks to open one of the returned sessions, use the Codex thread navigation tool when it is available. Search results should still include a clickable Markdown link built from `open_url` so the user can open the session directly.

## Semantic Recall Without Embeddings

Do not use embeddings or external APIs.

This plugin uses a two-stage no-embedding approach:

1. The MCP tool does high-recall local retrieval over titles, user messages, assistant messages, paths, snippets, command text, concept tokens, phrase expansion, CJK character n-grams, and a small multilingual synonym map.
2. The assistant uses its model capability to generate query variants and semantically rerank the returned candidates.

This is not vector embedding search. Its quality depends on whether the local retrieval step surfaces the right candidate in the pool. Use broader query variants and larger candidate pools when the user's memory is fuzzy.

## Result Style

Keep results compact. Prefer 3-5 matches unless the user asks for more. Do not dump full sessions into the answer. The main goal is fast recall and returning to the original session.

For Chinese conversations, use this format:

```md
我找到了 <N> 个可能相关的 session。最像的是第 <K> 个。

1. [<short title>](codex://threads/<thread_id>)
   时间：<updated time>
   状态/项目：<当前/已归档> · <workspace path or project name>
   为什么像：<one short reason, based on the original user intent and matched snippet>
   命中片段："<short snippet>"
   打开：点击标题，或回复“打开第 <K> 个”。
```

Rules:

- Localize labels and action text to the user's current language.
- In Chinese, use `已归档` only when the result says `archived: true`; otherwise use `当前`.
- Use `display_link` when available. Otherwise use `match_summary`, `matched_fields`, `matched_terms`, `confidence`, `project`, `status`, and `open_url` to explain and link results.
- If the title is very long, shorten it to a readable title, but keep the link target unchanged.
- The "why it matched" line should be the assistant's semantic judgment, not just a raw score.
- Do not show raw `score`, `keyword_score`, `smart_score`, `rollout_path`, `thread_id`, or internal query variants unless the user asks or you need it for troubleshooting.
- Make the result title or an explicit "打开这个 session" label a Markdown link using `display_link` or `open_url`. Do not make the user copy a thread ID.
- If results are weak or noisy, say that clearly and show the best 2-3 candidates.

## Privacy Boundary

This plugin reads local session files and stores a local JSONL index under `$CODEX_HOME/session-recall/index.jsonl`. Do not upload session contents or use web search for the user's private history.

## Limitations

Without embeddings, results depend on local recall plus assistant reranking. For very abstract memories with no overlapping words or concepts, ask one short follow-up question or broaden query variants before concluding nothing was found.

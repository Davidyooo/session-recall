#!/usr/bin/env python3
"""Local MCP server for searching session history.

The server indexes local JSONL rollouts and exposes MCP tools:
refresh_index, search_sessions, search_many_sessions, and get_session.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import sqlite3
import sys
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import Any


HOME = Path.home()
CODEX_HOME = Path(os.environ.get("CODEX_HOME", HOME / ".codex")).expanduser()
INDEX_DIR = CODEX_HOME / "session-recall"
INDEX_PATH = INDEX_DIR / "index.sqlite"
STATE_DB = CODEX_HOME / "sqlite" / "state_5.sqlite"
SESSION_ROOT = CODEX_HOME / "sessions"
ARCHIVED_ROOT = CODEX_HOME / "archived_sessions"

MAX_RECORD_TEXT = 6000
MAX_SESSION_TEXT = 600_000
SERVER_NAME = "session-recall"
SERVER_VERSION = "0.1.2"
INDEX_REFRESH_TTL_MS = 60_000
LAST_INDEX_REFRESH_CHECK_MS = 0
CJK_SEQUENCE_RE = (
    r"[\u3040-\u30ff\u31f0-\u31ff\u3400-\u4dbf"
    r"\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]+"
)

SYNONYM_GROUPS = {
    "recall": [
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
    "session": [
        "session",
        "sessions",
        "thread",
        "threads",
        "会话",
        "线程",
        "历史",
        "任务",
    ],
    "archive": ["归档", "archived", "archive"],
    "semantic": ["语义", "意思", "含义", "大概", "模糊", "近义词", "semantic", "meaning"],
    "plugin": ["插件", "plugin", "mcp", "skill"],
    "knowledge": ["知识库", "资料库", "记忆库", "knowledge", "memory"],
}


QUERY_EXPANSION_GROUPS = {
    "aside": [
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
    "chatgpt": [
        "ChatGPT",
        "GPT",
        "ChatGPT Codex",
        "ChatGPT 对话",
    ],
    "lark": [
        "Lark",
        "Feishu",
        "飞书",
        "lark-cli",
    ],
    "posthog": [
        "PostHog",
        "posthog insight",
        "dashboard",
        "analytics",
    ],
}

NOISE_PATTERNS = {
    "aside": [
        r"</?aside\b",
        r"\baside[\s.#:[{]",
        r"class=['\"][^'\"]*\baside\b",
    ],
}

FIELD_LABELS = {
    "title": "title",
    "first_user_message": "first user message",
    "preview": "preview",
    "cwd": "workspace path",
    "content": "session body",
}

def utc_now_ms() -> int:
    return int(time.time() * 1000)


def parse_iso_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        clean = value.replace("Z", "+00:00")
        return int(dt.datetime.fromisoformat(clean).timestamp() * 1000)
    except Exception:
        return None


def compact_text(value: Any, max_chars: int = MAX_RECORD_TEXT) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        return text[:max_chars] + " ..."
    return text


def clean_title(text: str, max_chars: int = 90) -> str:
    text = compact_text(text, max_chars=max_chars + 30)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def thread_id_from_path(path: Path) -> str | None:
    match = re.search(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        path.name,
        re.I,
    )
    return match.group(1).lower() if match else None


def load_state_metadata() -> dict[str, dict[str, Any]]:
    if not STATE_DB.exists():
        return {}
    rows: dict[str, dict[str, Any]] = {}
    uri = f"file:{STATE_DB}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=1)
        con.row_factory = sqlite3.Row
        for row in con.execute(
            """
            select id, title, cwd, created_at_ms, updated_at_ms, archived,
                   rollout_path, first_user_message, preview
            from threads
            """
        ):
            rows[row["id"]] = dict(row)
        con.close()
    except Exception:
        return {}
    return rows


def content_item_text(item: Any) -> str:
    if not isinstance(item, dict):
        return compact_text(item)
    item_type = item.get("type", "")
    if item_type in {"input_text", "output_text", "text"}:
        return compact_text(item.get("text", ""))
    if item_type in {"tool_result", "function_call_output"}:
        return compact_text(item.get("output") or item.get("content") or item)
    return compact_text(item.get("text") or item.get("content") or "")


def extract_response_message(payload: dict[str, Any]) -> tuple[str, str] | None:
    role = payload.get("role")
    if role not in {"user", "assistant"}:
        return None
    parts = []
    for item in payload.get("content") or []:
        text = content_item_text(item)
        if text:
            parts.append(text)
    text = "\n".join(parts).strip()
    if not text or text.startswith("<environment_context>"):
        return None
    return role, text


def append_unique(messages: list[dict[str, Any]], seen: set[str], role: str, text: str, line_no: int) -> None:
    text = compact_text(text)
    if not text:
        return
    key = f"{role}:{text[:500]}"
    if key in seen:
        return
    seen.add(key)
    messages.append({"role": role, "text": text, "line": line_no})


def parse_session_file(path: Path, state_row: dict[str, Any] | None = None) -> dict[str, Any] | None:
    thread_id = thread_id_from_path(path)
    cwd = ""
    created_at_ms: int | None = None
    updated_at_ms = int(path.stat().st_mtime * 1000)
    messages: list[dict[str, Any]] = []
    seen_messages: set[str] = set()
    indexed_fragments: list[str] = []
    source = ""

    try:
        with path.open("r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                payload = obj.get("payload") or {}
                record_type = obj.get("type")
                payload_type = payload.get("type")

                if record_type == "session_meta":
                    thread_id = payload.get("session_id") or payload.get("id") or thread_id
                    cwd = payload.get("cwd") or cwd
                    source = payload.get("source") or source
                    created_at_ms = parse_iso_ms(payload.get("timestamp")) or created_at_ms
                    continue

                if record_type == "turn_context":
                    cwd = payload.get("cwd") or cwd
                    continue

                if record_type == "event_msg":
                    if payload_type == "user_message":
                        text = payload.get("message", "")
                        append_unique(messages, seen_messages, "user", text, line_no)
                        indexed_fragments.append(f"user: {compact_text(text)}")
                    elif payload_type == "agent_message":
                        text = payload.get("message", "")
                        append_unique(messages, seen_messages, "assistant", text, line_no)
                        indexed_fragments.append(f"assistant: {compact_text(text)}")
                    elif payload_type in {"exec_command", "command_started"}:
                        text = payload.get("cmd") or payload.get("command") or payload
                        indexed_fragments.append(f"command: {compact_text(text)}")
                    elif payload_type in {"exec_command_output", "command_output"}:
                        text = payload.get("output") or payload.get("message") or payload
                        indexed_fragments.append(f"command output: {compact_text(text, 2000)}")
                    elif payload_type in {"web_search_end", "browser_action"}:
                        indexed_fragments.append(compact_text(payload, 2000))
                    continue

                if record_type == "response_item":
                    if payload_type == "message":
                        msg = extract_response_message(payload)
                        if msg:
                            role, text = msg
                            append_unique(messages, seen_messages, role, text, line_no)
                            indexed_fragments.append(f"{role}: {compact_text(text)}")
                    elif payload_type in {"function_call", "tool_call"}:
                        name = payload.get("name") or payload.get("tool_name") or "tool"
                        args = payload.get("arguments") or payload.get("input") or ""
                        indexed_fragments.append(f"tool call {name}: {compact_text(args, 2000)}")
                    elif payload_type in {"function_call_output", "tool_result"}:
                        text = payload.get("output") or payload.get("content") or payload
                        indexed_fragments.append(f"tool output: {compact_text(text, 2000)}")
                    elif payload_type in {"web_search_call"}:
                        indexed_fragments.append(compact_text(payload, 2000))

        if not thread_id:
            return None

        state_row = state_row or {}
        first_user = next((m["text"] for m in messages if m["role"] == "user"), "")
        last_assistant = next((m["text"] for m in reversed(messages) if m["role"] == "assistant"), "")
        title = state_row.get("title") or clean_title(first_user) or thread_id
        preview = state_row.get("preview") or clean_title(first_user or last_assistant, 220)
        content = "\n".join(indexed_fragments)
        if len(content) > MAX_SESSION_TEXT:
            content = content[:MAX_SESSION_TEXT] + "\n..."

        created_at_ms = (
            state_row.get("created_at_ms")
            or created_at_ms
            or int(path.stat().st_ctime * 1000)
        )
        updated_at_ms = state_row.get("updated_at_ms") or updated_at_ms
        cwd = state_row.get("cwd") or cwd
        archived = bool(state_row.get("archived", False)) or ARCHIVED_ROOT in path.parents

        return {
            "thread_id": thread_id,
            "title": title,
            "cwd": cwd,
            "created_at_ms": int(created_at_ms or 0),
            "updated_at_ms": int(updated_at_ms or 0),
            "archived": 1 if archived else 0,
            "rollout_path": str(path),
            "first_user_message": first_user,
            "preview": preview,
            "message_count": len(messages),
            "source": compact_text(source, 200),
            "content": content,
            "messages": messages,
        }
    except OSError:
        return None


def session_paths() -> list[Path]:
    paths: list[Path] = []
    if SESSION_ROOT.exists():
        paths.extend(SESSION_ROOT.glob("**/*.jsonl"))
    if ARCHIVED_ROOT.exists():
        paths.extend(ARCHIVED_ROOT.glob("*.jsonl"))
    return sorted(set(paths), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)


def connect_index() -> sqlite3.Connection:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(INDEX_PATH)
    con.row_factory = sqlite3.Row
    con.execute("pragma journal_mode = wal")
    con.execute(
        """
        create table if not exists sessions (
            thread_id text primary key,
            title text not null,
            cwd text not null,
            created_at_ms integer not null,
            updated_at_ms integer not null,
            archived integer not null,
            rollout_path text not null,
            first_user_message text not null,
            preview text not null,
            message_count integer not null,
            source text not null,
            content text not null,
            indexed_at_ms integer not null
        )
        """
    )
    con.execute(
        """
        create virtual table if not exists sessions_fts using fts5(
            thread_id unindexed,
            title,
            cwd,
            first_user_message,
            preview,
            content,
            tokenize='unicode61'
        )
        """
    )
    return con


def refresh_index(force: bool = False) -> dict[str, Any]:
    con = connect_index()
    state = load_state_metadata()
    paths = session_paths()
    now = utc_now_ms()
    indexed = 0
    skipped = 0
    errors = 0

    known = {
        row["thread_id"]: row
        for row in con.execute("select thread_id, rollout_path, updated_at_ms, indexed_at_ms from sessions")
    }

    for path in paths:
        path_thread_id = thread_id_from_path(path)
        stat_updated = int(path.stat().st_mtime * 1000)
        old = known.get(path_thread_id or "")
        if not force and old and old["indexed_at_ms"] >= stat_updated:
            skipped += 1
            continue

        parsed = parse_session_file(path, state.get(path_thread_id or ""))
        if not parsed:
            errors += 1
            continue

        con.execute(
            """
            insert into sessions (
                thread_id, title, cwd, created_at_ms, updated_at_ms, archived,
                rollout_path, first_user_message, preview, message_count,
                source, content, indexed_at_ms
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(thread_id) do update set
                title=excluded.title,
                cwd=excluded.cwd,
                created_at_ms=excluded.created_at_ms,
                updated_at_ms=excluded.updated_at_ms,
                archived=excluded.archived,
                rollout_path=excluded.rollout_path,
                first_user_message=excluded.first_user_message,
                preview=excluded.preview,
                message_count=excluded.message_count,
                source=excluded.source,
                content=excluded.content,
                indexed_at_ms=excluded.indexed_at_ms
            """,
            (
                parsed["thread_id"],
                parsed["title"],
                parsed["cwd"],
                parsed["created_at_ms"],
                parsed["updated_at_ms"],
                parsed["archived"],
                parsed["rollout_path"],
                parsed["first_user_message"],
                parsed["preview"],
                parsed["message_count"],
                parsed["source"],
                parsed["content"],
                now,
            ),
        )
        con.execute("delete from sessions_fts where thread_id = ?", (parsed["thread_id"],))
        con.execute(
            """
            insert into sessions_fts (
                thread_id, title, cwd, first_user_message, preview, content
            ) values (?, ?, ?, ?, ?, ?)
            """,
            (
                parsed["thread_id"],
                parsed["title"],
                parsed["cwd"],
                parsed["first_user_message"],
                parsed["preview"],
                parsed["content"],
            ),
        )
        indexed += 1

    con.commit()
    total = con.execute("select count(*) from sessions").fetchone()[0]
    con.close()
    return {
        "index_path": str(INDEX_PATH),
        "scanned_files": len(paths),
        "indexed_or_updated": indexed,
        "skipped_unchanged": skipped,
        "errors": errors,
        "total_sessions": total,
    }


def ensure_index() -> None:
    global LAST_INDEX_REFRESH_CHECK_MS
    if not INDEX_PATH.exists():
        refresh_index(force=True)
        LAST_INDEX_REFRESH_CHECK_MS = utc_now_ms()
        return
    now = utc_now_ms()
    if now - LAST_INDEX_REFRESH_CHECK_MS >= INDEX_REFRESH_TTL_MS:
        refresh_index(force=False)
        LAST_INDEX_REFRESH_CHECK_MS = now


def split_query(query: str) -> list[str]:
    query = query.strip().lower()
    terms = re.findall(rf"[a-z0-9_./:-]+|{CJK_SEQUENCE_RE}", query, flags=re.I)
    expanded: list[str] = []
    for term in terms:
        expanded.append(term)
        if re.search(CJK_SEQUENCE_RE, term) and len(term) >= 5:
            expanded.extend(term[i : i + 2] for i in range(0, len(term) - 1))
    return [t for t in expanded if len(t) >= 2]


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def expand_query_variants(query: str, max_variants: int = 5) -> list[str]:
    clean = normalize_space(query)
    if not clean:
        return []
    variants: list[str] = [clean]
    query_l = clean.lower()

    for key, expansions in QUERY_EXPANSION_GROUPS.items():
        if key in query_l or any(item.lower() in query_l for item in expansions):
            variants.extend(expansions)

    if "-" in clean:
        variants.append(clean.replace("-", " "))
    if " " in clean:
        variants.append(clean.replace(" ", "-"))

    tokens = split_query(clean)
    if 1 < len(tokens) <= 5:
        variants.append(" ".join(tokens))
    for token in tokens:
        if len(token) >= 4 and token not in variants:
            variants.append(token)

    return list(dict.fromkeys(normalize_space(item) for item in variants if normalize_space(item)))[:max_variants]


def noise_penalty(row: sqlite3.Row, query: str, terms: list[str]) -> float:
    text = "\n".join(
        [
            row["title"] or "",
            row["first_user_message"] or "",
            row["preview"] or "",
            row["cwd"] or "",
            (row["content"] or "")[:120_000],
        ]
    ).lower()
    penalty = 0.0
    term_set = set(terms + split_query(query))
    for term, patterns in NOISE_PATTERNS.items():
        if term not in term_set:
            continue
        noisy_hits = sum(len(re.findall(pattern, text, flags=re.I)) for pattern in patterns)
        if noisy_hits <= 0:
            continue
        product_context = any(
            phrase in text
            for phrase in [
                f"{term} browser",
                f"{term} ai browser",
                f"{term} discord",
                f"{term} research",
                f"{term} 浏览器",
            ]
        )
        if not product_context:
            penalty += min(noisy_hits, 10) * 12
    return penalty


def concept_tokens(text: str, max_chars: int = 80_000) -> Counter[str]:
    text_l = (text or "")[:max_chars].lower()
    tokens: Counter[str] = Counter()

    for word in re.findall(r"[a-z0-9_./:-]{2,}", text_l, flags=re.I):
        tokens[word] += 1

    for seq in re.findall(CJK_SEQUENCE_RE, text_l):
        if len(seq) >= 2:
            tokens[seq] += 1
        for size in (2, 3, 4):
            if len(seq) >= size:
                for idx in range(0, len(seq) - size + 1):
                    tokens[seq[idx : idx + size]] += 1

    for canonical, variants in SYNONYM_GROUPS.items():
        for variant in variants:
            if variant.lower() in text_l:
                tokens[f"concept:{canonical}"] += 4
                tokens[canonical] += 2

    return tokens


def weighted_concept_vector(row: sqlite3.Row | dict[str, Any]) -> Counter[str]:
    vector: Counter[str] = Counter()
    sections = [
        (row["title"] or "", 5),
        (row["first_user_message"] or "", 4),
        (row["preview"] or "", 3),
        (row["cwd"] or "", 2),
        (row["content"] or "", 1),
    ]
    for text, weight in sections:
        for token, count in concept_tokens(text).items():
            vector[token] += count * weight
    return vector


def concept_cosine(query_vector: Counter[str], doc_vector: Counter[str]) -> float:
    if not query_vector or not doc_vector:
        return 0.0
    common = set(query_vector) & set(doc_vector)
    dot = sum(query_vector[token] * doc_vector[token] for token in common)
    norm_query = math.sqrt(sum(value * value for value in query_vector.values()))
    norm_doc = math.sqrt(sum(value * value for value in doc_vector.values()))
    if norm_query <= 0 or norm_doc <= 0:
        return 0.0
    return dot / (norm_query * norm_doc)


def score_local_concept_row(row: sqlite3.Row, query: str, terms: list[str]) -> tuple[float, str]:
    query_vector = concept_tokens(query, max_chars=4000)
    if not query_vector:
        return 0.0, ""
    doc_vector = weighted_concept_vector(row)
    similarity = concept_cosine(query_vector, doc_vector)

    direct_bonus = 0.0
    row_text = "\n".join(
        [
            row["title"] or "",
            row["first_user_message"] or "",
            row["preview"] or "",
            row["cwd"] or "",
            (row["content"] or "")[:80_000],
        ]
    ).lower()
    for canonical, variants in SYNONYM_GROUPS.items():
        if any(variant.lower() in query.lower() for variant in variants) and any(
            variant.lower() in row_text for variant in variants
        ):
            direct_bonus += 8
    if terms:
        matched = sum(1 for term in terms if term in row_text)
        direct_bonus += min(matched, 6) * 2

    score = similarity * 240 + direct_bonus - noise_penalty(row, query, terms)
    if score < 10:
        return 0.0, ""
    snippet = make_snippet(row, [query.lower()] + terms)
    return score, snippet


def score_row(row: sqlite3.Row, query: str, terms: list[str]) -> tuple[float, str]:
    query_l = query.lower().strip()
    title = (row["title"] or "").lower()
    first = (row["first_user_message"] or "").lower()
    preview = (row["preview"] or "").lower()
    cwd = (row["cwd"] or "").lower()
    content = (row["content"] or "").lower()
    haystack = "\n".join([title, first, preview, cwd, content])

    score = 0.0
    if query_l and query_l in title:
        score += 120
    if query_l and query_l in first:
        score += 100
    if query_l and query_l in preview:
        score += 80
    if query_l and query_l in cwd:
        score += 50
    if query_l and query_l in content:
        score += 60

    matched_terms = 0
    for term in terms:
        if term in title:
            score += 18
            matched_terms += 1
        elif term in first or term in preview:
            score += 12
            matched_terms += 1
        elif term in cwd:
            score += 8
            matched_terms += 1
        elif term in content:
            score += 5
            matched_terms += 1

    if terms and matched_terms == 0:
        return 0.0, ""
    if terms:
        score *= 1 + min(matched_terms / max(len(terms), 1), 1.0)

    age_days = max((utc_now_ms() - int(row["updated_at_ms"])) / 86_400_000, 0)
    score += max(0, 8 - min(age_days, 8)) / 2
    score -= noise_penalty(row, query, terms)
    if score <= 0:
        return 0.0, ""

    snippet = make_snippet(row, [query_l] + terms)
    return score, snippet


def project_from_cwd(cwd: str) -> str:
    if not cwd:
        return ""
    path = Path(cwd)
    return path.name or str(path)


def matched_field_details(row: sqlite3.Row, query: str, terms: list[str], max_terms: int = 8) -> tuple[list[str], list[str]]:
    needles = [query.lower().strip()] + terms
    needles = [needle for needle in dict.fromkeys(needles) if needle]
    fields = {
        "title": row["title"] or "",
        "first_user_message": row["first_user_message"] or "",
        "preview": row["preview"] or "",
        "cwd": row["cwd"] or "",
        "content": row["content"] or "",
    }
    matched_fields: list[str] = []
    matched_terms: list[str] = []
    for field, value in fields.items():
        value_l = value.lower()
        field_hit = False
        for needle in needles:
            if needle and needle in value_l:
                field_hit = True
                if len(matched_terms) < max_terms:
                    matched_terms.append(needle)
        if field_hit:
            matched_fields.append(field)
    return list(dict.fromkeys(matched_fields)), list(dict.fromkeys(matched_terms))


def confidence_label(score: float, match_type: str, matched_fields: list[str]) -> str:
    strong_fields = {"title", "first_user_message", "preview"}
    if score >= 160 or (score >= 90 and strong_fields & set(matched_fields)):
        return "high"
    if score >= 45 or matched_fields:
        return "medium"
    return "low"


def match_summary(
    row: sqlite3.Row,
    query: str,
    terms: list[str],
    score: float,
    match_type: str,
) -> tuple[str, list[str], list[str], str]:
    matched_fields, matched_terms = matched_field_details(row, query, terms)
    confidence = confidence_label(score, match_type, matched_fields)
    if matched_fields:
        fields = ", ".join(FIELD_LABELS.get(field, field) for field in matched_fields[:3])
        term_text = ", ".join(matched_terms[:3])
        summary = f"Matched {fields}"
        if term_text:
            summary += f" for: {term_text}"
        if match_type == "smart":
            summary += "; concept match"
    else:
        summary = "Concept match from session content" if match_type == "smart" else "Weak local match"
    return summary, matched_fields, matched_terms, confidence


def make_snippet(row: sqlite3.Row, needles: list[str], width: int = 130) -> str:
    sources = [
        row["title"] or "",
        row["first_user_message"] or "",
        row["preview"] or "",
        row["content"] or "",
        row["cwd"] or "",
    ]
    for needle in needles:
        for source in sources:
            if not needle:
                continue
            source_l = source.lower()
            pos = source_l.find(needle.lower())
            if pos >= 0:
                start = max(0, pos - width // 2)
                end = min(len(source), pos + len(needle) + width // 2)
                prefix = "..." if start > 0 else ""
                suffix = "..." if end < len(source) else ""
                return prefix + compact_text(source[start:end], width * 2) + suffix
    return compact_text(row["preview"] or row["first_user_message"] or row["title"], width * 2)


def fmt_time(ms: int) -> str:
    if not ms:
        return ""
    return dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def search_sessions(
    query: str,
    limit: int = 10,
    include_archived: bool = True,
    cwd_contains: str | None = None,
    mode: str = "keyword",
    refresh: bool = True,
) -> dict[str, Any]:
    if refresh:
        ensure_index()
    limit = max(1, min(int(limit or 10), 50))
    mode = (mode or "keyword").lower()
    if mode not in {"keyword", "smart", "hybrid"}:
        raise ValueError("mode must be one of: keyword, smart, hybrid")
    terms = split_query(query)
    con = connect_index()
    sql = "select * from sessions"
    filters = []
    params: list[Any] = []
    if not include_archived:
        filters.append("archived = 0")
    if cwd_contains:
        filters.append("cwd like ?")
        params.append(f"%{cwd_contains}%")
    if filters:
        sql += " where " + " and ".join(filters)
    sql += " order by updated_at_ms desc"

    keyword_results: dict[str, tuple[float, sqlite3.Row, str]] = {}
    smart_results: dict[str, tuple[float, sqlite3.Row, str]] = {}
    for row in con.execute(sql, params):
        if mode in {"keyword", "hybrid"}:
            score, snippet = score_row(row, query, terms)
            if score > 0:
                keyword_results[row["thread_id"]] = (score, row, snippet)
        if mode in {"smart", "hybrid"}:
            score, snippet = score_local_concept_row(row, query, terms)
            if score > 0:
                smart_results[row["thread_id"]] = (score, row, snippet)
    con.close()

    combined: dict[str, tuple[float, sqlite3.Row, str, str, float, float]] = {}
    for thread_id, (score, row, snippet) in keyword_results.items():
        combined[thread_id] = (score, row, snippet, "keyword", score, 0.0)
    for thread_id, (score, row, snippet) in smart_results.items():
        if thread_id in combined:
            old_score, old_row, old_snippet, old_kind, keyword_score, _old_smart_score = combined[thread_id]
            combined[thread_id] = (
                old_score + score,
                old_row,
                old_snippet if keyword_score >= score else snippet,
                "hybrid" if old_kind != "smart" else "smart",
                keyword_score,
                score,
            )
        else:
            combined[thread_id] = (score, row, snippet, "smart", 0.0, score)

    results = list(combined.values())
    results.sort(key=lambda item: (item[0], item[1]["updated_at_ms"]), reverse=True)
    output = []
    for score, row, snippet, match_type, keyword_score, smart_score in results[:limit]:
        thread_id = row["thread_id"]
        summary, matched_fields, matched_terms, confidence = match_summary(
            row=row,
            query=query,
            terms=terms,
            score=score,
            match_type=match_type,
        )
        output.append(
            {
                "thread_id": thread_id,
                "title": row["title"],
                "result_label": clean_title(row["title"], 80),
                "cwd": row["cwd"],
                "project": project_from_cwd(row["cwd"]),
                "archived": bool(row["archived"]),
                "status": "archived" if bool(row["archived"]) else "current",
                "updated_at": fmt_time(int(row["updated_at_ms"])),
                "created_at": fmt_time(int(row["created_at_ms"])),
                "message_count": row["message_count"],
                "score": round(score, 2),
                "keyword_score": round(keyword_score, 2),
                "smart_score": round(smart_score, 2),
                "match_type": match_type,
                "confidence": confidence,
                "match_summary": summary,
                "matched_fields": matched_fields,
                "matched_terms": matched_terms,
                "snippet": snippet,
                "open_url": f"codex://threads/{thread_id}",
                "open_url_reliability": "unreliable_from_assistant_markdown; prefer Codex internal thread navigation when available",
                "open_instruction": f"Ask the assistant to open thread {thread_id}.",
                "rollout_path": row["rollout_path"],
            }
        )
    return {
        "query": query,
        "mode": mode,
        "count": len(output),
        "results": output,
        "note": "Show thread_id and ask the user to reply with the result number or thread_id to open it. Avoid presenting codex://threads/<thread_id> as a Markdown link because it can route to a blank loading page in the desktop app. For vague memory, generate several query variants and use search_many_sessions.",
    }


def search_many_sessions(
    queries: list[str],
    limit_per_query: int = 12,
    final_limit: int = 30,
    include_archived: bool = True,
    cwd_contains: str | None = None,
    mode: str = "keyword",
    auto_expand: bool = True,
) -> dict[str, Any]:
    ensure_index()
    clean_queries = [compact_text(query, 300) for query in queries if compact_text(query, 300)]
    if auto_expand:
        expanded_queries: list[str] = []
        for query in clean_queries:
            expanded_queries.extend(expand_query_variants(query))
        clean_queries = expanded_queries
    clean_queries = list(dict.fromkeys(clean_queries))[:18]
    limit_per_query = max(1, min(int(limit_per_query or 12), 50))
    final_limit = max(1, min(int(final_limit or 30), 100))

    merged: dict[str, dict[str, Any]] = {}
    primary_query = clean_queries[0] if clean_queries else ""
    for query in clean_queries:
        query_mode = mode
        if auto_expand and mode == "hybrid" and query != primary_query:
            query_mode = "keyword"
        response = search_sessions(
            query=query,
            limit=limit_per_query,
            include_archived=include_archived,
            cwd_contains=cwd_contains,
            mode=query_mode,
            refresh=False,
        )
        for result in response["results"]:
            thread_id = result["thread_id"]
            existing = merged.get(thread_id)
            if existing is None:
                item = dict(result)
                item["matched_queries"] = [query]
                item["aggregate_score"] = result["score"]
                merged[thread_id] = item
            else:
                existing["matched_queries"].append(query)
                existing["aggregate_score"] += result["score"] * 0.75
                existing["score"] = max(existing["score"], result["score"])
                if result["score"] > existing.get("_best_variant_score", 0):
                    existing["_best_variant_score"] = result["score"]
                    existing["snippet"] = result["snippet"]
                    existing["match_summary"] = result.get("match_summary", existing.get("match_summary", ""))
                    existing["confidence"] = result.get("confidence", existing.get("confidence", "medium"))
                    existing["matched_fields"] = result.get("matched_fields", existing.get("matched_fields", []))
                    existing["matched_terms"] = result.get("matched_terms", existing.get("matched_terms", []))

    results = list(merged.values())
    for item in results:
        item["aggregate_score"] += min(len(item["matched_queries"]), 5) * 10
        item.pop("_best_variant_score", None)
    results.sort(key=lambda item: (item["aggregate_score"], item["updated_at"]), reverse=True)
    return {
        "queries": clean_queries,
        "auto_expand": auto_expand,
        "mode": mode,
        "count": min(len(results), final_limit),
        "results": results[:final_limit],
        "note": "These are high-recall candidates. Codex should semantically rerank them against the user's original intent before answering.",
    }


def get_session(thread_id: str, query: str | None = None, max_messages: int = 24) -> dict[str, Any]:
    ensure_index()
    max_messages = max(1, min(int(max_messages or 24), 80))
    con = connect_index()
    row = con.execute("select * from sessions where thread_id = ?", (thread_id,)).fetchone()
    con.close()
    if not row:
        raise ValueError(f"No indexed session found for thread_id: {thread_id}")

    parsed = parse_session_file(Path(row["rollout_path"]))
    messages = parsed["messages"] if parsed else []
    selected: list[dict[str, Any]]
    if query:
        terms = [query.lower()] + split_query(query)
        matches = []
        for idx, msg in enumerate(messages):
            text_l = msg["text"].lower()
            hit = any(term and term in text_l for term in terms)
            if hit:
                matches.append(idx)
        picked = []
        for idx in matches:
            picked.extend(range(max(0, idx - 1), min(len(messages), idx + 2)))
        seen = set()
        selected = []
        for idx in picked:
            if idx in seen:
                continue
            seen.add(idx)
            selected.append(messages[idx])
            if len(selected) >= max_messages:
                break
    else:
        head = messages[: min(8, max_messages // 2)]
        tail = messages[-max_messages + len(head) :] if len(messages) > len(head) else []
        selected = head + tail

    return {
        "thread_id": row["thread_id"],
        "title": row["title"],
        "cwd": row["cwd"],
        "archived": bool(row["archived"]),
        "created_at": fmt_time(int(row["created_at_ms"])),
        "updated_at": fmt_time(int(row["updated_at_ms"])),
        "message_count": row["message_count"],
        "open_url": f"codex://threads/{row['thread_id']}",
        "open_url_reliability": "unreliable_from_assistant_markdown; prefer Codex internal thread navigation when available",
        "open_instruction": f"Ask the assistant to open thread {row['thread_id']}.",
        "rollout_path": row["rollout_path"],
        "messages": selected,
    }


TOOLS = [
    {
        "name": "refresh_index",
        "description": "Scan local session files and refresh the searchable index.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "force": {
                    "type": "boolean",
                    "description": "Reindex every session even if it appears unchanged.",
                    "default": False,
                }
            },
        },
    },
    {
        "name": "search_sessions",
        "description": "Search local sessions by remembered content and return links for going back to the original session.",
        "inputSchema": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language, keyword, filename, project name, or phrase to search for.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results to return, from 1 to 50.",
                    "default": 10,
                },
                "include_archived": {
                    "type": "boolean",
                    "description": "Include archived sessions.",
                    "default": True,
                },
                "cwd_contains": {
                    "type": "string",
                    "description": "Optional path substring filter for the session workspace.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["keyword", "smart", "hybrid"],
                    "description": "keyword is exact local search. smart is no-key local concept search. hybrid combines both.",
                    "default": "keyword",
                },
            },
        },
    },
    {
        "name": "search_many_sessions",
        "description": "Run multiple local query variants and merge high-recall session candidates for model reranking.",
        "inputSchema": {
            "type": "object",
            "required": ["queries"],
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Several query variants generated from the user's fuzzy memory.",
                },
                "limit_per_query": {
                    "type": "integer",
                    "description": "Maximum results per query variant.",
                    "default": 12,
                },
                "final_limit": {
                    "type": "integer",
                    "description": "Maximum merged candidates to return for Codex to rerank.",
                    "default": 30,
                },
                "include_archived": {
                    "type": "boolean",
                    "description": "Include archived sessions.",
                    "default": True,
                },
                "cwd_contains": {
                    "type": "string",
                    "description": "Optional path substring filter for the session workspace.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["keyword", "smart", "hybrid"],
                    "description": "keyword with auto_expand is fastest. Use hybrid or smart for a deeper fallback when keyword expansion is not enough.",
                    "default": "keyword",
                },
                "auto_expand": {
                    "type": "boolean",
                    "description": "Automatically expand vague queries with aliases, language variants, and punctuation variants before merging results.",
                    "default": True,
                },
            },
        },
    },
    {
        "name": "get_session",
        "description": "Read a matched session summary and selected message excerpts.",
        "inputSchema": {
            "type": "object",
            "required": ["thread_id"],
            "properties": {
                "thread_id": {
                    "type": "string",
                    "description": "Session UUID returned by search_sessions.",
                },
                "query": {
                    "type": "string",
                    "description": "Optional query for selecting relevant excerpts from the session.",
                },
                "max_messages": {
                    "type": "integer",
                    "description": "Maximum message excerpts to return.",
                    "default": 24,
                },
            },
        },
    },
]


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "refresh_index":
        return refresh_index(force=bool(arguments.get("force", False)))
    if name == "search_sessions":
        return search_sessions(
            query=str(arguments.get("query", "")),
            limit=int(arguments.get("limit", 10)),
            include_archived=bool(arguments.get("include_archived", True)),
            cwd_contains=arguments.get("cwd_contains"),
            mode=str(arguments.get("mode", "keyword")),
        )
    if name == "search_many_sessions":
        return search_many_sessions(
            queries=arguments.get("queries") or [],
            limit_per_query=int(arguments.get("limit_per_query", 12)),
            final_limit=int(arguments.get("final_limit", 30)),
            include_archived=bool(arguments.get("include_archived", True)),
            cwd_contains=arguments.get("cwd_contains"),
            mode=str(arguments.get("mode", "keyword")),
            auto_expand=bool(arguments.get("auto_expand", True)),
        )
    if name == "get_session":
        return get_session(
            thread_id=str(arguments.get("thread_id", "")),
            query=arguments.get("query"),
            max_messages=int(arguments.get("max_messages", 24)),
        )
    raise ValueError(f"Unknown tool: {name}")


def read_message() -> dict[str, Any] | None:
    first = sys.stdin.buffer.readline()
    if not first:
        return None
    while first in {b"\r\n", b"\n"}:
        first = sys.stdin.buffer.readline()
        if not first:
            return None

    if first.lstrip().startswith(b"{"):
        return json.loads(first)

    headers = {}
    line = first
    while line not in {b"\r\n", b"\n", b""}:
        try:
            key, value = line.decode("ascii").split(":", 1)
            headers[key.strip().lower()] = value.strip()
        except ValueError:
            pass
        line = sys.stdin.buffer.readline()
    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    body = sys.stdin.buffer.read(length)
    return json.loads(body)


def write_message(message: dict[str, Any]) -> None:
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def result_response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle_request(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}

    if request_id is None:
        return None

    if method == "initialize":
        protocol = params.get("protocolVersion") or "2024-11-05"
        return result_response(
            request_id,
            {
                "protocolVersion": protocol,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )

    if method == "tools/list":
        return result_response(request_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            payload = call_tool(name, arguments)
            return result_response(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(payload, ensure_ascii=False, indent=2),
                        }
                    ],
                    "isError": False,
                },
            )
        except Exception as exc:
            return result_response(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
                        }
                    ],
                    "isError": True,
                },
            )

    return error_response(request_id, -32601, f"Method not found: {method}")


def main() -> int:
    while True:
        request = read_message()
        if request is None:
            return 0
        response = handle_request(request)
        if response is not None:
            write_message(response)


if __name__ == "__main__":
    raise SystemExit(main())

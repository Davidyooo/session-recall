# Session Recall

<p align="center">
  <img src="./plugins/session-recall/assets/logo.png" alt="Session Recall logo" width="112" />
</p>

Find old Codex sessions from fuzzy memory.

Session Recall indexes your local Codex session history and helps you find the conversation you only half remember. It is local-first: it reads session files from your own `~/.codex` directory and stores a local SQLite index under `~/.codex/session-recall/`.

## What It Does

- Searches local Codex session history by remembered content.
- Expands vague searches with aliases, punctuation variants, and language variants.
- Returns readable results with title, project, time, archived status, snippet, confidence, and why it matched.
- Supports exact keyword search, fast expanded search, and deeper smart/hybrid fallback.

## Example

Ask Codex:

```text
Use Session Recall to find my old Aside browser conversation.
```

The plugin can expand that into searches such as:

- `Aside browser`
- `Aside AI Browser`
- `aside 浏览器`
- `Aside Discord`

Then it returns likely sessions and explains why each one matched.

## Install From This Repo

Clone the repo:

```bash
git clone https://github.com/<your-org>/session-recall-plugin.git
cd session-recall-plugin
```

Add this repo as a Codex plugin marketplace:

```bash
codex plugin marketplace add "$PWD"
codex plugin add session-recall@session-recall
```

Start a new Codex thread after installing so the skill and MCP tools are picked up.

## Tools

- `refresh_index`: refresh the local session index.
- `search_sessions`: search one query exactly, smartly, or with hybrid mode.
- `search_many_sessions`: merge several query variants; defaults to fast expanded keyword search.
- `get_session`: read selected excerpts from a matched session.

## Privacy

This plugin does not upload your sessions. It reads local Codex session files and writes a local index to:

```text
~/.codex/session-recall/index.sqlite
```

Do not share that SQLite index or your local session files.

## Limits

- This is not embedding/vector search.
- Very fuzzy memories may still need a follow-up keyword.
- It searches saved local sessions; it does not recover unsaved ephemeral chats.

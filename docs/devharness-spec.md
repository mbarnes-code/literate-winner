# `devharness` — Autonomous & Deterministic Software-Developer Harness

**Specification v1.0**
**Date:** 2026-08-23
**Status:** Design spec, pre-implementation
**Primary language:** Python 3.12 (with optional Rust helper crate for Linux sandbox)
**Working name:** `devharness` (final name TBD)

> Companion documents:
> - [agentic-harness-report.md](agentic-harness-report.md) — cross-repo architectural study
> - [software developer task and workflows](software%20developer%20task%20and%20workflows) — target task inventory

---

## Table of contents

1. [Goals & non-goals](#1-goals--non-goals)
2. [Determinism strategy](#2-determinism-strategy)
3. [High-level architecture](#3-high-level-architecture)
4. [Component specifications](#4-component-specifications)
5. [Complete tool catalog](#5-complete-tool-catalog)
6. [Lift-and-reuse plan (with license compliance)](#6-lift-and-reuse-plan-with-license-compliance)
7. [Dependencies](#7-dependencies)
8. [File directory sketch](#8-file-directory-sketch)
9. [Configuration format](#9-configuration-format)
10. [Sandbox & approval model](#10-sandbox--approval-model)
11. [Data flow — end-to-end](#11-data-flow--end-to-end)
12. [MVP roadmap](#12-mvp-roadmap)
13. [Testing & evaluation](#13-testing--evaluation)
14. [License & attribution](#14-license--attribution)

---

## 1. Goals & non-goals

### 1.1 Goals

- **Autonomously perform every developer task** listed in [`docs/software developer task and workflows`](software%20developer%20task%20and%20workflows): formatting, linting, type-checking, secret scanning, testing, coverage inspection, debugging, git operations, refactoring, dependency management, and documentation maintenance.
- **Deterministic where the underlying tool is deterministic.** The harness must not add nondeterminism on top of `ruff format`, `pytest -p no:randomly`, `cargo test`, `gitleaks`, etc.
- **Bounded LLM nondeterminism.** Model output is the only stochastic component. The harness controls temperature (default `0`), enforces structured tool output, checkpoints every turn, and provides rollback primitives.
- **Language-polyglot from day one.** Cover Python, TypeScript/JavaScript, Rust, and Go — the four ecosystems named as most-requested in the workflow doc.
- **Local-first.** Runs on the user's machine or CI worker with no cloud dependency for the core loop. Cloud sandboxes are pluggable, not required.
- **Reference-implementation-quality.** Ship with observability, cost tracking, resumable sessions, and prompt caching enabled out of the box.

### 1.2 Non-goals

- **Not a general-purpose chat assistant.** Only ships tools relevant to software development. Web browsing, image generation, TTS, computer-use are explicitly out of scope for v1.
- **Not a research platform.** Not building a substrate for LLM training experiments (that's what `autoresearch` is).
- **Not an IDE.** No editor UI, no LSP language server. Ships a CLI + HTTP API + MCP-server mode.
- **Not a replacement for Codex/Goose/Aider.** These are excellent; `devharness` is a purpose-built harness that lifts the best pieces from each and specializes them for autonomous, deterministic CI-style workflows.

### 1.3 Success criteria

An install of `devharness` on a fresh Ubuntu container with the target repo cloned must, given a natural-language goal like *"add tests for the `parse_config` function and open a PR"*, be able to:

1. Read the target function, understand its interface.
2. Write the test file using the repo's existing test conventions.
3. Run the test suite; observe failures.
4. Iterate on the test until it passes.
5. Run linter + formatter + type check; fix any issues.
6. Run secret scan; abort if new secrets introduced.
7. Create a semantic commit + push to a new branch.
8. Open a PR via `gh` with a summary of changes.
9. Do all of the above with a **complete rollout log** in `~/.devharness/rollouts/<session_id>/` such that the entire session can be replayed byte-for-byte.

---

## 2. Determinism strategy

The single hardest problem this harness solves is **making an LLM-driven system reproducibly do the same thing twice given the same input**. The strategy has ten layers:

| # | Layer | Mechanism |
|---|---|---|
| 1 | **Model config** | Default `temperature=0`, `top_p=1`, fixed `seed` on providers that support it (OpenAI). Per-role overrides in config, not by the model. |
| 2 | **Structured tool output** | All tool results returned as JSON with a versioned schema; no free-text-only tools. Model sees the same shape every time. |
| 3 | **Deterministic tool ordering** | Tool schemas sorted by name before serializing to the model at session start. Never reordered mid-session. |
| 4 | **Frozen prompt cache** | Tier-1 system prompt is byte-stable per session (SHA-256 tracked). Any drift aborts the session with a clear error. |
| 5 | **Lockfile-first installs** | `install_deps` always uses `--frozen` / `--immutable` (`uv sync --frozen`, `pnpm install --frozen-lockfile`, `cargo install --locked`). Non-frozen installs require `--allow-non-frozen`. |
| 6 | **Env snapshot** | `record_env_snapshot` captures interpreter versions, tool binaries + hashes, lockfile hashes into `env-manifest.json`. `verify_env_snapshot` fails-loud on drift. |
| 7 | **Idempotent mutations** | Every file-mutating tool call is preceded by an automatic WIP git checkpoint (`commit_checkpoint`). `rollback_to_checkpoint` reverses arbitrary damage. |
| 8 | **Verification gates** | After every mutation batch, the loop runs `assert_no_diff` on unintended paths + re-runs `run_tests` on the affected scope. |
| 9 | **Rollout replay** | Every session writes an append-only rollout: `(turn_id, model_call_hash, tool_calls[], tool_results[], approval_decisions[])`. Replaying the rollout against a mocked provider reproduces the exact sequence. |
| 10 | **No mid-session reconfiguration** | Model, provider, sandbox type, and tool set are frozen at session start. Changes require a new session (resumable from the last checkpoint). |

Each layer is enforced by a specific component (§4) and validated in the eval suite (§13).

---

## 3. High-level architecture

```text
                            ┌──────────────────────────────┐
                            │           SURFACES           │
                            │  CLI TUI    devharness run   │
                            │  Headless   devharness -x    │
                            │  HTTP       POST /run        │
                            │  MCP server devharness mcp   │
                            └──────────────┬───────────────┘
                                           │  session_id (deterministic)
                                           ▼
      ┌────────────────────────────────────────────────────────────────┐
      │                       CONTROL PLANE                            │
      │                                                                │
      │   ┌───────────────────────────────────────────────────────┐   │
      │   │  SessionStore  (SQLite WAL, append-only rollout)      │   │
      │   │  ThreadState   (model, tools, sandbox, cwd, budget)   │   │
      │   └───────────────────────────────────────────────────────┘   │
      │                            │                                   │
      │                            ▼                                   │
      │   ┌───────────────────────────────────────────────────────┐   │
      │   │   Loop engine  (explicit Step[] — Goose-style)        │   │
      │   │                                                       │   │
      │   │   EntryHooks → LoadRepoHints (AGENTS.md walker)       │   │
      │   │              → PromptBuilder (3-tier + shared blocks) │   │
      │   │              → PromptInjectionScan                    │   │
      │   │              → InferenceRunner (provider dispatch)    │   │
      │   │              → StreamHandler (SSE parsing)            │   │
      │   │              → ToolDispatch (parallel + path-overlap) │   │
      │   │              → ApprovalGate (with cache)              │   │
      │   │              → SandboxRunner (jail + exec)            │   │
      │   │              → ResultRedactor (Luhn/SSN/keys)         │   │
      │   │              → ResultSpillover (>100KB → disk ref)    │   │
      │   │              → Compaction (token threshold)           │   │
      │   │              → BudgetCheck (turns/tokens/dollars)     │   │
      │   │              → VerificationGate (assert_no_diff)      │   │
      │   │              → StopHooks                              │   │
      │   │                                                       │   │
      │   │   Effects → SessionStore.append()                     │   │
      │   └───────────────────────────────────────────────────────┘   │
      └──┬─────────────────┬──────────────────┬──────────────────┬───┘
         │                 │                  │                  │
         ▼                 ▼                  ▼                  ▼
   ┌──────────┐    ┌─────────────┐    ┌──────────────┐   ┌──────────────┐
   │Provider  │    │Tool         │    │Sandbox       │   │Memory        │
   │Layer     │    │Registry     │    │Backends      │   │Providers     │
   │          │    │             │    │              │   │              │
   │Anthropic │    │Self-        │    │local (jail)  │   │AGENTS.md     │
   │OpenAI    │    │registering  │    │docker        │   │devharness/   │
   │Bedrock   │    │modules      │    │landlock+bwrap│   │  memory.md   │
   │Google    │    │             │    │modal (opt)   │   │(pluggable)   │
   │Ollama    │    │AST scan     │    │daytona (opt) │   │              │
   │          │    │cache        │    │              │   │              │
   │Fallback  │    │             │    │              │   │              │
   │chain     │    │             │    │              │   │              │
   │Prompt    │    │             │    │              │   │              │
   │caching   │    │             │    │              │   │              │
   └──────────┘    └─────────────┘    └──────────────┘   └──────────────┘
         │                 │                  │                  │
         └─────────────────┴────────┬─────────┴──────────────────┘
                                    │
                                    ▼
      ┌────────────────────────────────────────────────────────────────┐
      │                    CROSS-CUTTING LAYERS                        │
      │                                                                │
      │  Observability   OpenTelemetry (gen_ai.*) + local JSONL logs   │
      │  Cost tracking   per-turn / per-session / cumulative           │
      │  Config          TOML w/ ${VAR} expansion; profiles overlay    │
      │  Redaction       Luhn + IIN + SSN + generic secret keywords    │
      │  Prompt scanner  threat_patterns for repo-loaded context files │
      └────────────────────────────────────────────────────────────────┘
```

**Key architectural choices vs. the reference repos:**

1. **Explicit `Step[]` engine (Goose-style), not LangGraph.** This gives us deterministic step ordering, per-step observability, and freedom from LangGraph's version churn. We port DeepAgents' summarization/subagent logic but rehome it into our step engine.
2. **Provider abstraction owned by us, not LangChain.** We import provider SDKs directly (`anthropic`, `openai`, `boto3`, `google-genai`, `ollama`). Fallback chain and prompt caching are middleware layers we own.
3. **Tool registry with AST discovery (Hermes-style).** Zero hand-maintained tool lists. Adding a tool = dropping a file into `devharness/tools/impl/`.
4. **Sandbox is a `Protocol`, not a class hierarchy.** Backends: `local` (default), `docker`, `landlock` (Linux w/ helper Rust crate), `modal` (optional), `daytona` (optional).
5. **Every mutation is checkpointed.** The harness runs `commit_checkpoint` before any `mutating-*` tool call unless the caller passed `--no-checkpoint`. Rollback is a first-class primitive.

---

## 4. Component specifications

Each component below has: **purpose**, **inputs/outputs**, **lift source** (if any), and **determinism contract**.

### 4.1 `SessionStore` — SQLite append-only rollout

- **Purpose:** persist every turn's messages, tool calls, tool results, approval decisions to a per-session SQLite file. Enable crash-safe resume and byte-for-byte replay.
- **Lift source:** [visa-vulnerability-agentic-harness/vvaharness/orchestrator/store.py](visa-vulnerability-agentic-harness/vvaharness/orchestrator/store.py) — checkpoint save/load pattern (Apache 2.0; **requires NOTICE**).
- **Storage layout:** `~/.devharness/sessions/{session_id}/rollout.db`.
- **Schema:**
  ```sql
  CREATE TABLE turns (turn_id INTEGER PRIMARY KEY, ts INTEGER, kind TEXT, payload JSONB);
  CREATE TABLE approvals (id INTEGER PRIMARY KEY, turn_id INTEGER, tool TEXT, args_hash TEXT, decision TEXT);
  CREATE TABLE snapshots (id INTEGER PRIMARY KEY, ts INTEGER, kind TEXT, sha TEXT, path TEXT);
  CREATE TABLE env (key TEXT PRIMARY KEY, value TEXT);
  ```
- **Determinism contract:** append-only; PRAGMAs `journal_mode=WAL`, `synchronous=NORMAL`. A session is fully identified by `(rollout.db, env-manifest.json)`.

### 4.2 `LoopEngine` — the `Step[]` engine

- **Purpose:** drive one turn = one iteration through an ordered list of `Step`s. Each `Step` returns `NotApplicable | Applied(Effect[])`.
- **Lift source:** design pattern from Goose (`goose-agent/machine.rs`) — no code lifted (Rust), only the pattern.
- **Steps in v1** (in order):
  1. `LoadRepoHints` — read `AGENTS.md`, `.cursorrules`, `.goosehints`, `CLAUDE.md` from cwd up to git root.
  2. `PromptBuilder` — assemble tier-1/2/3 prompt; freeze tier-1 hash.
  3. `PromptInjectionScan` — scan repo-loaded context for `[SYSTEM]`-style markers.
  4. `InferenceRunner` — send request via `ProviderLayer`; stream response.
  5. `ToolDispatch` — parse tool_calls, split into parallel-safe batches, run.
  6. `ApprovalGate` — check cache, prompt if needed, cache decision.
  7. `SandboxRunner` — execute the tool inside its declared sandbox class.
  8. `ResultRedactor` — mask secrets/PII in results before they re-enter model context.
  9. `ResultSpillover` — spill oversized results to disk with pointer marker.
  10. `Compaction` — if `sum(tokens) > threshold`, summarize older messages.
  11. `BudgetCheck` — enforce max turns / tokens / dollars.
  12. `VerificationGate` — post-mutation checks (`assert_no_diff`, re-run affected tests).
  13. `StopHooks` — final validators; can veto turn end.
- **Determinism contract:** step order is fixed. No mid-session insertion of steps. A session's step-list hash goes into the env manifest.

### 4.3 `ProviderLayer`

- **Purpose:** talk to LLM providers with a uniform interface + fallback chain + prompt caching.
- **Interface (Python `Protocol`):**
  ```python
  class Provider(Protocol):
      name: str
      def stream(self, req: Request) -> Iterator[StreamEvent]: ...
      def supports_prompt_cache(self) -> bool: ...
      def supports_reasoning(self) -> bool: ...
      def price_per_1k(self) -> Tuple[float, float, float, float]:
          """(input, output, cache_read, cache_write)"""
  ```
- **Built-in providers:** `anthropic` (default), `openai`, `openai_responses`, `bedrock`, `google_genai`, `ollama`.
- **Lift sources:**
  - Metadata profile pattern from [hermes-agent/providers/base.py](hermes-agent/providers/base.py) (MIT).
  - Prompt caching design from [deepagents/libs/deepagents/deepagents/middleware/_prompt_caching.py](deepagents/libs/deepagents/deepagents/middleware/_prompt_caching.py) (MIT). Reimplemented for our loop (no LangChain dependency).
  - Anthropic ephemeral-cache-block pattern from `vvaharness/backends/sdk.py` (Apache 2.0).
- **Fallback chain:** `[primary, fallback1, fallback2]`. On `429`, wait `Retry-After` then try next; on `5xx`, immediate rotate. Cross-provider fallback supported (Anthropic → OpenAI on quota).
- **Determinism contract:** default `temperature=0`, `top_p=1`, `seed=42`. Overriding requires explicit per-role config entry. `Provider.stream()` must be deterministic given identical `Request` (subject to provider guarantees).

### 4.4 `ToolRegistry`

- **Purpose:** discover, deduplicate, serialize tools. Zero hand-maintained lists.
- **Lift source:** [hermes-agent/tools/registry.py](hermes-agent/tools/registry.py) — AST discovery pattern (MIT; port verbatim then rename symbols).
- **Registration API:**
  ```python
  from devharness.tools.registry import register

  @register(
      name="run_pytest",
      category="test",
      sandbox_class="mutating-file",  # pytest may write cache dirs
      approval_class="auto",
      timeout=600,
      parallel_safe=True,
  )
  def run_pytest(paths: list[str] = ["."], pattern: str | None = None, ...) -> ToolResult:
      ...
  ```
- **Discovery:** AST-scan `devharness/tools/impl/*.py` at import time; cache results in `~/.devharness/cache/tool_discovery.json` (invalidated on mtime change). Adapted from Hermes' `_is_registry_register_call()`.
- **Determinism contract:** at session start, the tool list is sorted by `name` and frozen. `tools_hash = sha256(json.dumps(tool_specs, sort_keys=True))` is written to the env manifest.

### 4.5 `ToolDispatcher`

- **Purpose:** given a batch of `tool_calls` from the model, decide serial vs. parallel and execute.
- **Lift source:** [hermes-agent/agent/tool_dispatch_helpers.py](hermes-agent/agent/tool_dispatch_helpers.py) — `_should_parallelize_tool_batch`, `_paths_overlap`, `_is_destructive_command` (MIT; port line-for-line).
- **Rules:**
  1. Two calls touching same path → serial.
  2. Any destructive call (`rm`, `git push`, `apply_patch` on same file) → serial with the entire batch.
  3. Approval-required calls → serial (one modal at a time).
  4. Otherwise → concurrent up to `MAX_PARALLEL=8` workers.
- **Determinism contract:** batches sorted by `(destructive_flag desc, call_id asc)` before dispatch. Given the same batch, execution order is fixed.

### 4.6 `Sandbox`

- **Purpose:** execute a tool inside its declared isolation class.
- **Backends (implement `SandboxBackend` protocol):**
  - `local` — path-jail only; runs on host with cwd confinement. Lift `_jail()` from [visa-vulnerability-agentic-harness/vvaharness/backends/localtools.py](visa-vulnerability-agentic-harness/vvaharness/backends/localtools.py) (Apache 2.0; port verbatim).
  - `docker` — spawn container per session with project mounted rw at `/workspace`, egress off unless `network` sandbox class.
  - `landlock` (Linux only) — Landlock + bwrap via optional small Rust helper crate. Study pattern from `codex/codex-rs/linux-sandbox/` (Apache 2.0; design only, we write our own).
  - `modal` — cloud backend (optional; `pip install devharness[modal]`).
  - `daytona` — cloud backend (optional).
- **Sandbox class → capabilities matrix:**
  | Class | FS read | FS write | Shell | Network |
  |---|---|---|---|---|
  | `read-only` | cwd | ❌ | ❌ | ❌ |
  | `mutating-file` | cwd | cwd | ❌ | ❌ |
  | `mutating-git` | cwd + `.git` | cwd + `.git` | git subprocess only | ❌ |
  | `mutating-env` | cwd + package cache | cwd + package cache | pkg manager only | package registry |
  | `network` | cwd | cwd | ❌ | on |
  | `mixed` | cwd | cwd | on | on (requires approval) |
- **Determinism contract:** sandbox choice is per-tool declaration, not per-call. Given a session, sandbox behavior is stable.

### 4.7 `ApprovalGate`

- **Purpose:** enforce the three approval modes (`suggest` / `auto-edit` / `auto`) with session-cached decisions.
- **Lift source:** [hermes-agent/tools/approval.py](hermes-agent/tools/approval.py) — `ContextVar`-based session state + hash-keyed cache (MIT; adapt).
- **Cache key:** `sha256(json.dumps({"tool": name, "args": canonicalized_args}, sort_keys=True))`.
- **Approval modes:**
  - `suggest` (default): all `mutating-*` and `network` require approval; `read-only` and `mutating-file` in the current git dirty-set auto-approve.
  - `auto-edit`: `read-only` + `mutating-file` auto; shell + git-push + network prompt.
  - `auto`: everything auto. **Requires explicit `--yolo` CLI flag or config `approval.mode = auto`.**
- **Cache scope:** session. Cleared on session end. Cannot persist across sessions.
- **Determinism contract:** approval decisions are recorded in `approvals` table. Replay mode reads decisions from the rollout instead of prompting.

### 4.8 `Compaction`

- **Purpose:** when the message list exceeds a token threshold, summarize older messages and evict them.
- **Lift source:** [deepagents/libs/deepagents/deepagents/middleware/summarization.py](deepagents/libs/deepagents/deepagents/middleware/summarization.py) — trigger + summary + keep-N pattern (MIT; port structure, remove LangChain dependency).
- **Config:**
  ```toml
  [compaction]
  trigger_tokens = 100_000
  keep_recent_messages = 20
  summary_model = "anthropic:claude-haiku-4-5"  # cheap
  anti_thrash_cooldown_s = 600
  ```
- **Determinism contract:** the summary itself is nondeterministic (model output), but the *trigger* and *retention* are deterministic. Summary text is written to `~/.devharness/sessions/{id}/summaries/{turn_id}.md` for audit.

### 4.9 `Redactor`

- **Purpose:** strip credit cards (Luhn+IIN), SSNs (area/group gated), bearer tokens, generic API keys from tool results before they enter model context.
- **Lift source:** [visa-vulnerability-agentic-harness/vvaharness/report/redact.py](visa-vulnerability-agentic-harness/vvaharness/report/redact.py) — `_luhn()`, `_cc_network()`, `_ssn_valid()`, `_bearer_credential()` (Apache 2.0; **port line-for-line**, **requires NOTICE**).
- **Custom rules:** operator can add regex patterns in `config.toml`:
  ```toml
  [redaction.custom]
  patterns = [
    { name = "acme_internal_key", regex = "acme_[A-Za-z0-9]{32}" },
  ]
  ```
- **Determinism contract:** given the same input string, output is deterministic (regex is state-free). Non-negotiable — every tool result passes through redaction.

### 4.10 `PromptBuilder`

- **Purpose:** assemble the three-tier system prompt with shared blocks; freeze tier-1 for the session.
- **Lift sources:**
  - Shared-blocks pattern from [visa-vulnerability-agentic-harness/vvaharness/util/prompts.py](visa-vulnerability-agentic-harness/vvaharness/util/prompts.py) (Apache 2.0).
  - Tier structure from three-tier design (report §5.7).
  - Jinja2 for template rendering (BSD; PyPI).
- **Tiers:**
  1. **Stable:** identity + tool-use rules + env (OS, shell, cwd, tool availability) + determinism contract. SHA-256'd at session start.
  2. **Context:** user goal + `AGENTS.md` + repo hints + `USER.md` (optional).
  3. **Volatile:** todo state + memory snapshot + timestamp.
- **Templates:** shipped in `devharness/prompts/*.jinja2`; overridable at `~/.devharness/prompts/{name}`.
- **Determinism contract:** tier-1 hash written to env manifest. Loop fails-loud if tier-1 hash changes mid-session.

### 4.11 `ResultSpillover`

- **Purpose:** oversized tool results (>100KB by default) get written to disk; model sees a pointer + preview.
- **Lift source:** [hermes-agent/tools/tool_result_storage.py](hermes-agent/tools/tool_result_storage.py) — three-layer budget pattern (MIT).
- **Format:** result stored at `~/.devharness/sessions/{id}/spillover/{turn_id}_{tool_name}_{call_id}.txt`; injected marker: `<persisted-output>/path/to/file (12.4 KB, showing first 2000 chars)</persisted-output>\n<preview>...preview...</preview>`.
- **Determinism contract:** spillover paths derived from `(turn_id, tool_name, call_id)`. Given identical run, paths are identical.

### 4.12 `PromptInjectionScanner`

- **Purpose:** scan repo-loaded context files (`AGENTS.md`, `.cursorrules`, etc.) for hidden instructions before injecting into system prompt.
- **Lift source:** [hermes-agent/tools/threat_patterns.py](hermes-agent/tools/threat_patterns.py) (MIT).
- **Behavior:** detected markers replaced with `[REDACTED: prompt-injection candidate]` and logged. Session continues but with a warning.
- **Determinism contract:** regex-based; same input → same output.

### 4.13 `CostTracker`

- **Purpose:** per-turn / per-session / cumulative cost tracking; enforce budget limits.
- **Lift sources:**
  - Token counting from [visa-vulnerability-agentic-harness/vvaharness/util/tokens.py](visa-vulnerability-agentic-harness/vvaharness/util/tokens.py) (Apache 2.0).
  - `UsageBar` pattern from `hermes-agent/agent/billing_usage.py` (MIT).
- **Pricing table:** built-in table at `devharness/providers/pricing.toml`; user-overrideable.
- **Metric emission:** OpenTelemetry `gen_ai.usage.{input,output,cache_read,cache_write}_tokens`, `devharness.session.cost_usd`.
- **Determinism contract:** cost is a function of `(tokens, model, pricing_table_version)` — all three are recorded in the rollout.

### 4.14 `Observer`

- **Purpose:** emit OTel `gen_ai.*` spans + local JSONL logs for every LLM call and tool call.
- **Design:** DIY on top of `opentelemetry-api` + `opentelemetry-exporter-otlp` (no Python reference implementation exists; Codex/Goose OTel emitters are Rust and study-only).
- **Standard fields:** `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.finish_reasons`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `session.id`, `turn.id`, `tool.{name}.duration_ms`, `tool.{name}.status`, `approval.decision`.
- **Local JSONL:** `~/.devharness/sessions/{id}/events.jsonl` (structured, one event per line).

### 4.15 `Config`

- **Purpose:** load layered config from defaults + user file + profile overlay + env expansion + CLI flags.
- **Lift source:** [visa-vulnerability-agentic-harness/vvaharness/config/__init__.py](visa-vulnerability-agentic-harness/vvaharness/config/__init__.py) — `_expand()`, `_expand_env()`, `_deep_merge()` (Apache 2.0; port verbatim).
- **Load order:**
  1. Hardcoded defaults (`devharness/config/defaults.toml`)
  2. `~/.devharness/config.toml`
  3. `${PROFILE_DIR}/{profile_name}.toml` (if `--profile` set)
  4. Env-var expansion in all string values
  5. CLI flag overrides
- **Determinism contract:** the merged config is dumped to `~/.devharness/sessions/{id}/resolved-config.toml` at session start. Sessions are portable.

### 4.16 `SubagentRunner`

- **Purpose:** implement the `task` tool — spawn an isolated child agent for a subtask.
- **Lift source:** design from [deepagents/libs/deepagents/deepagents/middleware/subagents.py](deepagents/libs/deepagents/deepagents/middleware/subagents.py) — `SubAgent` TypedDict shape (MIT; design lift, we implement the runner ourselves without LangChain).
- **Behavior:**
  - Child gets: fresh conversation, parent's tools *minus* `[task, request_approval, finish]`, same sandbox, same provider.
  - Child does NOT see parent's message history — only the task description + optional summary passed by parent.
  - Parent sees child's final string result (or structured `response_format`) as a `tool` message.
  - **Depth cap 2 by default.**
- **Determinism contract:** child session_id derived from `parent_session_id + call_id + turn_id`. Full rollout stored under parent's session directory in `subagents/{child_session_id}/`.

---

## 5. Complete tool catalog

Every tool below is defined as a `@register`-decorated function in `devharness/tools/impl/`. Sandbox class, approval class, and timeout are declared at registration; parameters are auto-derived to JSON schema from type hints (via `pydantic` or `msgspec`).

**Legend:**
- **SC** = Sandbox Class: `RO` (read-only) / `MF` (mutating-file) / `MG` (mutating-git) / `ME` (mutating-env) / `N` (network) / `MX` (mixed)
- **AC** = Approval Class: `A` (auto) / `S` (suggest — asks in `suggest` mode) / `!` (always asks)
- **Par** = Parallel-safe with disjoint paths

### 5.1 Category: Code Quality (6 tools)

| Tool | SC | AC | Par | Timeout | Backing CLI | Notes |
|---|---|---|---|---|---|---|
| `format_code` | MF | A | ✅ | 60s | dispatch: `ruff format` / `prettier --write` / `rustfmt` / `gofmt -w` | Ext-based dispatch. `--check` mode for read-only. Deterministic (formatters are). |
| `lint_code` | RO | A | ✅ | 120s | `ruff check` / `eslint` / `cargo clippy -- -D warnings` / `go vet` | Output: JSON diagnostics + severity summary. |
| `typecheck` | RO | A | ✅ | 180s | `mypy` / `pyright --outputjson` / `tsc --noEmit` / `cargo check` | Model gets structured diagnostics list. |
| `scan_secrets` | RO | A | ✅ | 60s | `gitleaks detect --no-git --report-format=json` | JSON report parsed & summarized. |
| `scan_dependencies` | RO | A | ✅ | 120s | `pip-audit` / `npm audit --json` / `cargo audit --json` / `govulncheck` | Aggregated vulnerability list. |
| `scan_semgrep` | RO | A | ✅ | 180s | `semgrep --config=auto --json` | Configurable rulesets. |

**Deterministic output guarantees:** all six formatters/linters return byte-stable output given identical input; the harness pins tool versions in the env manifest.

### 5.2 Category: Testing (5 tools)

| Tool | SC | AC | Par | Timeout | Backing CLI | Notes |
|---|---|---|---|---|---|---|
| `run_tests` | MF | S | ❌ | 900s | Auto-dispatch: `pytest -p no:randomly` / `vitest --run` / `cargo test --locked` / `go test ./... -count=1` | `no:randomly` + `-count=1` = deterministic ordering. |
| `run_single_test` | MF | S | ❌ | 300s | `pytest {path}::{name}` / `vitest run -t "{name}"` / `cargo test {name} -- --exact` / `go test -run "^{name}$"` | Targeted iteration. |
| `run_test_pattern` | MF | S | ❌ | 600s | `pytest -k "{pattern}"` / `vitest -t "{pattern}"` / `cargo test {pattern}` | Regex/glob filter. |
| `coverage_report` | MF | A | ❌ | 900s | `coverage.py` / `c8` / `cargo tarpaulin --out Json` | Returns per-file line-coverage table. |
| `list_failing_tests` | RO | A | ✅ | 10s | Parses `~/.devharness/sessions/{id}/last-test-output.json` | No new run — just parse. |

### 5.3 Category: Debugging & Exploration (8 tools)

| Tool | SC | AC | Par | Timeout | Backing CLI | Notes |
|---|---|---|---|---|---|---|
| `grep` | RO | A | ✅ | 30s | `rg --json` | Wraps ripgrep; falls back to `grep -R` if `rg` missing (warn once). |
| `find_files` | RO | A | ✅ | 30s | `fd --json` | Fallback: `find` with limited options. |
| `read_file` | RO | A | ✅ | 10s | pure Python read w/ `_jail()` | Line range + byte cap (200KB default). |
| `jq_query` | RO | A | ✅ | 15s | `jq` | Input from file or previous tool result reference. |
| `yq_query` | RO | A | ✅ | 15s | `yq` | Same as `jq_query` for YAML. |
| `git_log_search` | RO | A | ✅ | 60s | `git log -S "{needle}" --oneline` | Regression hunting via pickaxe. |
| `git_bisect_start` / `git_bisect_step` | MG | ! | ❌ | 60s | `git bisect start/good/bad/reset` | Requires approval — modifies HEAD. |
| `git_blame` | RO | A | ✅ | 30s | `git blame --porcelain` | Returns structured annotations. |

### 5.4 Category: Git & Version Control (10 tools)

| Tool | SC | AC | Par | Timeout | Backing CLI | Notes |
|---|---|---|---|---|---|---|
| `git_status` | RO | A | ✅ | 10s | `git status --porcelain=v2` | Structured. |
| `git_diff` | RO | A | ✅ | 30s | `git diff [--cached] [ref1..ref2]` | Includes stats + patch. |
| `git_add` | MG | A | ❌ | 30s | `git add` | Adds specific paths only; no `git add .`. |
| `git_commit` | MG | S | ❌ | 30s | `git commit -m` | Enforces Conventional Commits regex `^(feat\|fix\|docs\|style\|refactor\|perf\|test\|build\|ci\|chore)(\(.+\))?: .+`. Rejects otherwise. |
| `git_checkout_branch` | MG | S | ❌ | 30s | `git checkout -b` / `git checkout` | New branches auto; switching existing → suggest. |
| `git_rebase` | MG | ! | ❌ | 180s | `git rebase` | Always requires approval. |
| `git_merge` | MG | ! | ❌ | 60s | `git merge` | Always requires approval. |
| `git_apply_patch` | MG | S | ❌ | 30s | `git apply` | Standard unified diff, not V4A. |
| `apply_patch` | MF | S | ❌ | 30s | Python V4A parser | Lift from Hermes `patch_parser.py`. Model's primary edit tool. |
| `gh_pr_create` / `gh_pr_comment` / `gh_pr_list` / `gh_issue_create` | N | ! | ❌ | 60s | `gh` CLI | Requires `gh auth status` at startup. All PR ops always ask. |

### 5.5 Category: AST Refactoring (5 tools)

| Tool | SC | AC | Par | Timeout | Backing CLI | Notes |
|---|---|---|---|---|---|---|
| `ast_grep_search` | RO | A | ✅ | 60s | `ast-grep --json` | Structural pattern search. |
| `ast_grep_rewrite` | MF | S | ❌ | 120s | `ast-grep -u` (update) | Dry-run mode returns preview; apply requires flag. |
| `rename_symbol` | MF | S | ❌ | 180s | `ast-grep` rewrite w/ language-specific patterns | Ships pre-built patterns for py/ts/rs/go. |
| `remove_dead_code` | MF | S | ❌ | 120s | `knip` (js/ts) / `autoflake --remove-all-unused-imports` (py) / `cargo machete` (rust) | Auto-dispatch by repo detection. |
| `tree_sitter_query` | RO | A | ✅ | 60s | Python `tree_sitter` bindings | Raw query language; power tool. |

### 5.6 Category: Environment & Dependencies (7 tools)

| Tool | SC | AC | Par | Timeout | Backing CLI | Notes |
|---|---|---|---|---|---|---|
| `install_deps` | ME | S | ❌ | 600s | `uv sync --frozen` / `pnpm install --frozen-lockfile` / `cargo build --locked` / `go mod download` | Frozen by default. Non-frozen requires `allow_unlocked=True`. |
| `add_dep` | ME | ! | ❌ | 120s | `uv add` / `pnpm add` / `cargo add` / `go get` | Requires approval — modifies lockfile. |
| `remove_dep` | ME | ! | ❌ | 60s | `uv remove` / `pnpm remove` / `cargo rm` (via cargo-edit) | Always asks. |
| `sync_lockfile` | ME | S | ❌ | 300s | `uv lock` / `pnpm install --lockfile-only` / `cargo update` | Verify lock is up-to-date. |
| `docker_build` | ME | ! | ❌ | 900s | `docker build` | Always asks. |
| `docker_run` | MX | ! | ❌ | 600s | `docker run` w/ explicit volume + network policy | Always asks; sandbox spec required. |
| `docker_compose_up` / `docker_compose_down` | ME | ! | ❌ | 300s | `docker compose` | Always asks. |

### 5.7 Category: Documentation (5 tools)

| Tool | SC | AC | Par | Timeout | Backing CLI | Notes |
|---|---|---|---|---|---|---|
| `openapi_generate` | RO | A | ✅ | 60s | Custom: introspect FastAPI/Flask; emit YAML | Python-only in v1. |
| `openapi_validate` | RO | A | ✅ | 30s | `redocly lint` / `swagger-cli validate` | Structural check. |
| `generate_changelog` | RO | A | ✅ | 60s | `git-cliff --output -` | Requires `cliff.toml` in repo or falls back to Conventional Commits default. |
| `check_markdown_links` | RO | A | ✅ | 120s | `lychee` | Reports dead links. |
| `check_code_snippets_in_md` | RO | A | ✅ | 60s | Custom: extract fenced blocks, run `python -c` / `node -e` / `cargo script` (mise fallback) | Only executes safe subset (no I/O). |

### 5.8 Category: Core harness tools (8 tools)

| Tool | SC | AC | Par | Timeout | Notes |
|---|---|---|---|---|---|
| `plan` | RO | A | ❌ | 30s | Emit a structured plan (list of steps); model self-review. |
| `save_plan` / `approve_plan` | RO | ! | ❌ | 30s | Persists plan; requires user approval to proceed to mutation phase. |
| `todo_add` / `todo_mark_done` / `todo_list` | RO | A | ✅ | 5s | Persisted to `~/.devharness/sessions/{id}/todo.json`. |
| `task` | inherited | A | ❌ | 1800s | Delegate to subagent (§4.16). |
| `memory_add` / `memory_recall` | RO/MF | A | ✅ | 10s | Long-term memory in `~/.devharness/memory.md` or via pluggable provider. |
| `web_search` | N | S | ✅ | 30s | Configurable: Tavily / Exa / DuckDuckGo (default: none — must be configured). |
| `fetch_url` | N | S | ✅ | 30s | Content limit 500KB; respects `robots.txt` unless `--no-robots`. |
| `request_approval` | RO | ! | ❌ | 300s | Explicit HITL — asks user for freeform input. |
| `finish` | RO | A | ❌ | 5s | End turn; structured result with summary + changed_files. |

### 5.9 Category: Determinism helpers (5 tools — novel)

| Tool | SC | AC | Par | Timeout | Notes |
|---|---|---|---|---|---|
| `assert_no_diff` | RO | A | ✅ | 30s | Given a list of glob paths, assert `git diff --quiet` on those paths. Fails-loud if unexpected mutation. |
| `commit_checkpoint` | MG | A | ❌ | 30s | Auto-creates a WIP commit before a batch of mutations. Silent unless commit succeeds. |
| `rollback_to_checkpoint` | MG | ! | ❌ | 30s | `git reset --hard {commit_sha}`. Always asks. |
| `record_env_snapshot` | RO | A | ✅ | 30s | Emits `env-manifest.json` — Python version, `uv --version`, `pytest --version`, `ruff --version`, `git --version`, lockfile SHAs. |
| `verify_env_snapshot` | RO | A | ✅ | 30s | Compares current env to a stored manifest. Fails if any tool version or lockfile SHA drifts. |

### 5.10 Tool count summary

| Category | Count | Sandbox mix |
|---|---|---|
| Code Quality | 6 | 5 RO, 1 MF |
| Testing | 5 | 4 MF, 1 RO |
| Debugging & Exploration | 8 | 7 RO, 1 MG |
| Git & VCS | 10 | 2 RO, 7 MG, 1 N |
| AST Refactoring | 5 | 2 RO, 3 MF |
| Env & Deps | 7 | 6 ME, 1 MX |
| Documentation | 5 | 5 RO |
| Core harness | 9 | 6 RO, 1 N, 1 MF, 1 inherited |
| Determinism | 5 | 2 RO, 3 MG |
| **TOTAL** | **60** | 27 RO, 8 MF, 12 MG, 7 ME, 3 N, 3 misc |

---

## 6. Lift-and-reuse plan (with license compliance)

Every file in the table below was verified to exist in its cited path (§checked 2026-08-23). Numbers in parens are approximate LOC.

### 6.1 Priority-A lifts (port for MVP; ~1,300 LOC total)

| # | Component | Source file (verified) | License | Strategy | Attribution |
|---|---|---|---|---|---|
| 1 | Path jail | [visa-vulnerability-agentic-harness/vvaharness/backends/localtools.py](visa-vulnerability-agentic-harness/vvaharness/backends/localtools.py) (~250) | Apache 2.0 | Port verbatim | NOTICE + docstring |
| 2 | V4A patch parser | [hermes-agent/tools/patch_parser.py](hermes-agent/tools/patch_parser.py) (~400) | MIT | Port verbatim | docstring |
| 3 | Tool dispatch + path overlap | [hermes-agent/agent/tool_dispatch_helpers.py](hermes-agent/agent/tool_dispatch_helpers.py) (~400) | MIT | Port verbatim | docstring |
| 4 | SQLite session store | [visa-vulnerability-agentic-harness/vvaharness/orchestrator/store.py](visa-vulnerability-agentic-harness/vvaharness/orchestrator/store.py) (~200) | Apache 2.0 | Adapt | NOTICE + docstring |
| 5 | Tool registry + AST discovery | [hermes-agent/tools/registry.py](hermes-agent/tools/registry.py) (~400) | MIT | Port + adapt | docstring |

### 6.2 Priority-B lifts (port during weeks 3–6; ~1,500 LOC)

| # | Component | Source | License | Strategy |
|---|---|---|---|---|
| 6 | Redaction (secrets/PII) | [visa-vvah/vvaharness/report/redact.py](visa-vulnerability-agentic-harness/vvaharness/report/redact.py) (~300) | Apache 2.0 | Port verbatim + custom rules |
| 7 | Config expansion + overlay | [visa-vvah/vvaharness/config/__init__.py](visa-vulnerability-agentic-harness/vvaharness/config/__init__.py) (~130) | Apache 2.0 | Port verbatim |
| 8 | Tool result spillover | [hermes-agent/tools/tool_result_storage.py](hermes-agent/tools/tool_result_storage.py) (~300) | MIT | Adapt (change spillover dir) |
| 9 | Approval cache | [hermes-agent/tools/approval.py](hermes-agent/tools/approval.py) (~200) | MIT | Adapt |
| 10 | Prompt-injection scanner | [hermes-agent/tools/threat_patterns.py](hermes-agent/tools/threat_patterns.py) (~80) | MIT | Port verbatim |
| 11 | Shared prompt blocks pattern | [visa-vvah/vvaharness/util/prompts.py](visa-vulnerability-agentic-harness/vvaharness/util/prompts.py) (~100) | Apache 2.0 | Port pattern; author own blocks |
| 12 | Provider metadata profile | [hermes-agent/providers/base.py](hermes-agent/providers/base.py) (~150) | MIT | Adapt (drop LangChain deps) |

### 6.3 Priority-C lifts (study for design, reimplement)

| # | Component | Source | Strategy |
|---|---|---|---|
| 13 | Summarization middleware | [deepagents/libs/deepagents/deepagents/middleware/summarization.py](deepagents/libs/deepagents/deepagents/middleware/summarization.py) | Design lift — reimplement without LangChain dependency |
| 14 | SubAgent TypedDict pattern | [deepagents/libs/deepagents/deepagents/middleware/subagents.py](deepagents/libs/deepagents/deepagents/middleware/subagents.py) | Design lift — reimplement runner |
| 15 | DeltaChannel reducer | [deepagents/libs/deepagents/deepagents/_messages_reducer.py](deepagents/libs/deepagents/deepagents/_messages_reducer.py) | Study for pattern; our append-only rollout serves same purpose |
| 16 | Apply-patch canonical Rust | [codex/codex-rs/apply-patch/src/parser.rs](codex/codex-rs/apply-patch/src/parser.rs) | Reference for edge-case behavior of the Python V4A parser |
| 17 | Linux sandbox pattern | codex/codex-rs/linux-sandbox/ (Rust) | Design lift — we may ship a similar helper crate in `crates/sandbox-linux/` |
| 18 | State machine pattern | goose (Rust) | Design lift — our `Step[]` engine follows this |

### 6.4 Explicitly NOT lifted

| Component | Reason |
|---|---|
| Anything from `autoresearch/` | **No LICENSE file — not lift-friendly.** Referenced in docs only. |
| Bedrock Engineer UI components | TypeScript/Electron; wrong ecosystem. |
| LangGraph checkpointer | Requires LangGraph runtime commitment. |
| Codex approval/Guardian framework | Deep Rust integration; we implement our own approval cache instead. |
| Goose recipe engine | Rust; we defer declarative workflows to post-v1. |
| Hermes messaging gateways | Out of scope (Slack/Telegram/etc.). |

### 6.5 License compliance mechanics

The project's own license is **Apache 2.0** (compatible with all lifted code; MIT is subsumable).

Required artifacts:
1. **`LICENSE`** — Apache 2.0 text.
2. **`NOTICE`** — top-level notice file. Format:
   ```
   devharness
   Copyright 2026 <maintainer>

   This product includes software developed at:
   - Visa, Inc. (Apache 2.0) — https://github.com/visa/visa-vulnerability-agentic-harness
     Files derived from: vvaharness/backends/localtools.py, vvaharness/orchestrator/store.py,
                        vvaharness/report/redact.py, vvaharness/config/__init__.py,
                        vvaharness/util/prompts.py
   - OpenAI (Apache 2.0) — https://github.com/openai/codex
     Design reference: codex-rs/apply-patch/, codex-rs/linux-sandbox/
   - Block, Inc. (Apache 2.0) — https://github.com/block/goose
     Design reference: goose-agent/machine.rs
   ```
3. **`THIRD_PARTY_LICENSES.md`** — full text of each incorporated Apache 2.0 and MIT license.
4. **Docstring at top of every ported file** citing origin repo + commit SHA + source path.

Example docstring header:
```python
# devharness/sandbox/_jail.py
#
# Adapted from visa-vulnerability-agentic-harness/vvaharness/backends/localtools.py
# (Apache License 2.0, Copyright 2026 Visa, Inc.)
# Source: https://github.com/visa/visa-vulnerability-agentic-harness
# Original commit: <SHA>
# See NOTICE and THIRD_PARTY_LICENSES.md at project root.
```

---

## 7. Dependencies

### 7.1 Python runtime

- **Python 3.12+** (`.python-version` pinned; `mise` compatible)
- **Package manager:** `uv` (recommended) — declarative, fast, reproducible via `uv.lock`

### 7.2 Core Python dependencies (`pyproject.toml`)

```toml
[project]
name = "devharness"
requires-python = ">=3.12,<3.14"

dependencies = [
  # LLM providers
  "anthropic>=0.40.0",
  "openai>=1.60.0",
  "google-genai>=0.7.0",
  "boto3>=1.36.0",          # Bedrock
  "ollama>=0.4.0",          # local models

  # Data / validation
  "pydantic>=2.10.0",
  "pydantic-settings>=2.7.0",
  "msgspec>=0.19.0",        # faster than pydantic for tool-schema paths

  # Config
  "tomli-w>=1.1.0",         # write; tomllib is stdlib for read

  # Templating
  "jinja2>=3.1.5",

  # HTTP
  "httpx[http2,socks]>=0.28.0",

  # SQLite helpers
  "aiosqlite>=0.20.0",       # async access
  # (stdlib `sqlite3` for sync)

  # CLI
  "typer>=0.15.0",           # or `click` — Typer chosen for pydantic integration
  "rich>=13.9.0",            # TUI rendering
  "prompt_toolkit>=3.0.50",  # interactive TUI (Hermes-style)

  # Observability
  "opentelemetry-api>=1.30.0",
  "opentelemetry-sdk>=1.30.0",
  "opentelemetry-exporter-otlp>=1.30.0",

  # Parsing / structural
  "tree-sitter>=0.24.0",
  "tree-sitter-language-pack>=0.6.0",  # bundles py/ts/rs/go

  # Async / concurrency
  "anyio>=4.7.0",

  # Utility
  "wcmatch>=10.0",           # glob patterns (DeepAgents-style)
  "python-dotenv>=1.0.1",
  "tenacity>=9.0.0",         # retries
  "cryptography>=44.0.0",    # secrets handling
]

[project.optional-dependencies]
mcp     = ["mcp>=1.2.0"]                                        # official Python MCP SDK
modal   = ["modal>=0.68.0"]                                     # cloud sandbox
daytona = ["daytona-sdk>=0.10.0"]                               # cloud sandbox
dev     = ["pytest>=8.3.0", "pytest-xdist>=3.6.0", "pytest-randomly>=3.16.0",
           "ruff>=0.9.0", "mypy>=1.14.0", "pyright>=1.1.390",
           "coverage[toml]>=7.6.0", "hypothesis>=6.123.0"]
```

### 7.3 System / CLI tool dependencies

Discovered at runtime via `verify_env_snapshot`; the harness ships a `devharness doctor` command that reports missing tools. Every tool has a fallback strategy (warn + degrade, or fail-loud).

| Tool | Required? | Purpose | Detected via | Fallback |
|---|---|---|---|---|
| `git` | required | version control | `git --version` | none — hard fail |
| `gh` | soft-required | GitHub CLI | `gh --version` | disable `gh_*` tools with warning |
| `rg` (ripgrep) | recommended | fast grep | `rg --version` | `grep -R` (warn perf hit) |
| `fd` | recommended | fast find | `fd --version` | `find` (warn) |
| `jq` | required | JSON tool | `jq --version` | none — hard fail |
| `yq` | recommended | YAML tool | `yq --version` | Python `PyYAML` fallback |
| `ast-grep` (`sg`) | recommended | AST search/rewrite | `sg --version` | disable AST tools |
| `ruff` | recommended | Python format+lint | `ruff --version` | disable Python tools |
| `prettier` | recommended | JS/TS format | `prettier --version` | disable JS tools |
| `eslint` | recommended | JS/TS lint | `eslint --version` | disable |
| `tsc` | recommended | TS typecheck | `tsc --version` | disable |
| `mypy` / `pyright` | recommended | Python typecheck | `mypy --version` / `pyright --version` | disable |
| `rustfmt`, `clippy` | recommended | Rust format+lint | via `cargo` | disable Rust tools |
| `gofmt`, `go vet` | recommended | Go tools | via `go` | disable Go tools |
| `pytest` | recommended | Python tests | `pytest --version` | disable |
| `vitest` / `jest` | recommended | JS/TS tests | via package.json | disable |
| `cargo test` | recommended | Rust tests | via `cargo` | disable |
| `gitleaks` | recommended | secret scan | `gitleaks version` | disable `scan_secrets` |
| `semgrep` | optional | code scan | `semgrep --version` | disable |
| `git-cliff` | optional | changelog | `git-cliff --version` | disable `generate_changelog` |
| `lychee` | optional | link check | `lychee --version` | disable |
| `docker` | soft-required | containers | `docker version` | disable `docker_*` tools |
| `docker compose` | optional | multi-container | `docker compose version` | disable |
| `uv` | soft-required | Python pkgmgr | `uv --version` | fallback to `pip` (warn) |
| `pnpm` | soft-required | JS pkgmgr | `pnpm --version` | fallback to `npm` |
| `pip-audit`, `npm audit`, `cargo audit`, `govulncheck` | optional | dep audit | per-lang | disable that lang's audit |
| `knip`, `autoflake`, `cargo-machete` | optional | dead-code | per-lang | disable that lang's dead-code |
| `bwrap` (bubblewrap) | Linux sandbox only | bwrap sandbox | `bwrap --version` | fall back to `docker` sandbox |

### 7.4 Optional Rust helper crate

For Linux `landlock`-level sandbox (Codex-style depth), an optional companion crate:

```
crates/devharness-sandbox-linux/
├── Cargo.toml           # cdylib for Python `cffi` binding, or PyO3
├── src/
│   ├── lib.rs           # Landlock+bwrap wrapper
│   └── seccomp.rs
└── README.md
```

Dependencies: `landlock`, `nix`, `pyo3`. Ships as `pip install devharness[landlock]` on Linux only.

---

## 8. File directory sketch

```text
devharness/                                    # PROJECT ROOT
├── LICENSE                                    # Apache 2.0
├── NOTICE                                     # third-party attribution
├── THIRD_PARTY_LICENSES.md                    # full license texts
├── README.md
├── AGENTS.md                                  # instructions for agents working ON devharness
├── CHANGELOG.md                               # generated by git-cliff
├── pyproject.toml
├── uv.lock
├── .python-version                            # 3.12
├── mise.toml                                  # optional runtime pin (also declares rust for the helper crate)
├── docker/
│   ├── Dockerfile.runtime                     # bundled runtime for `docker` sandbox
│   └── Dockerfile.dev
│
├── devharness/                                # PYTHON PACKAGE
│   ├── __init__.py                            # public API: run(), Session, Config
│   │
│   ├── loop/                                  # THE CORE
│   │   ├── __init__.py
│   │   ├── engine.py                          # Step[] driver
│   │   ├── steps/
│   │   │   ├── __init__.py
│   │   │   ├── load_repo_hints.py             # AGENTS.md walker
│   │   │   ├── prompt_builder.py              # 3-tier assembly
│   │   │   ├── prompt_injection_scan.py       # LIFT: hermes/tools/threat_patterns.py
│   │   │   ├── inference_runner.py
│   │   │   ├── stream_handler.py
│   │   │   ├── tool_dispatch.py               # LIFT: hermes/agent/tool_dispatch_helpers.py
│   │   │   ├── approval_gate.py               # LIFT: hermes/tools/approval.py
│   │   │   ├── sandbox_runner.py
│   │   │   ├── result_redactor.py             # LIFT: vvah/report/redact.py
│   │   │   ├── result_spillover.py            # LIFT: hermes/tools/tool_result_storage.py
│   │   │   ├── compaction.py                  # DESIGN LIFT: deepagents/middleware/summarization.py
│   │   │   ├── budget_check.py
│   │   │   ├── verification_gate.py
│   │   │   └── stop_hooks.py
│   │   ├── state.py                           # ThreadState, TurnContext
│   │   └── effects.py                         # Effect ADT
│   │
│   ├── providers/                             # LLM PROVIDER LAYER
│   │   ├── __init__.py
│   │   ├── base.py                            # Provider protocol; ADAPT hermes/providers/base.py
│   │   ├── anthropic.py                       # + prompt caching
│   │   ├── openai.py                          # Chat Completions
│   │   ├── openai_responses.py                # Responses API
│   │   ├── bedrock.py                         # + Bedrock prompt caching
│   │   ├── google_genai.py
│   │   ├── ollama.py                          # local
│   │   ├── fallback.py                        # cross-provider retry
│   │   ├── prompt_cache.py                    # cache_control block helpers
│   │   └── pricing.toml                       # token pricing table
│   │
│   ├── tools/                                 # TOOL SYSTEM
│   │   ├── __init__.py
│   │   ├── registry.py                        # LIFT: hermes/tools/registry.py
│   │   ├── schema.py                          # JSON-schema from type hints
│   │   ├── result.py                          # ToolResult dataclass
│   │   └── impl/                              # ONE FILE PER TOOL
│   │       ├── __init__.py                    # (empty; discovery via AST)
│   │       ├── quality/
│   │       │   ├── format_code.py
│   │       │   ├── lint_code.py
│   │       │   ├── typecheck.py
│   │       │   ├── scan_secrets.py
│   │       │   ├── scan_dependencies.py
│   │       │   └── scan_semgrep.py
│   │       ├── test/
│   │       │   ├── run_tests.py
│   │       │   ├── run_single_test.py
│   │       │   ├── run_test_pattern.py
│   │       │   ├── coverage_report.py
│   │       │   └── list_failing_tests.py
│   │       ├── debug/
│   │       │   ├── grep.py
│   │       │   ├── find_files.py
│   │       │   ├── read_file.py
│   │       │   ├── jq_query.py
│   │       │   ├── yq_query.py
│   │       │   ├── git_log_search.py
│   │       │   ├── git_bisect.py
│   │       │   └── git_blame.py
│   │       ├── git/
│   │       │   ├── git_status.py
│   │       │   ├── git_diff.py
│   │       │   ├── git_add.py
│   │       │   ├── git_commit.py              # conventional-commits validator
│   │       │   ├── git_checkout_branch.py
│   │       │   ├── git_rebase.py
│   │       │   ├── git_merge.py
│   │       │   ├── git_apply_patch.py
│   │       │   ├── apply_patch.py             # LIFT: hermes/tools/patch_parser.py
│   │       │   └── gh.py                      # gh_pr_*, gh_issue_*
│   │       ├── refactor/
│   │       │   ├── ast_grep_search.py
│   │       │   ├── ast_grep_rewrite.py
│   │       │   ├── rename_symbol.py
│   │       │   ├── remove_dead_code.py
│   │       │   └── tree_sitter_query.py
│   │       ├── env/
│   │       │   ├── install_deps.py
│   │       │   ├── add_dep.py
│   │       │   ├── remove_dep.py
│   │       │   ├── sync_lockfile.py
│   │       │   ├── docker_build.py
│   │       │   ├── docker_run.py
│   │       │   └── docker_compose.py
│   │       ├── docs/
│   │       │   ├── openapi_generate.py
│   │       │   ├── openapi_validate.py
│   │       │   ├── generate_changelog.py
│   │       │   ├── check_markdown_links.py
│   │       │   └── check_code_snippets_in_md.py
│   │       ├── core/
│   │       │   ├── plan.py
│   │       │   ├── todo.py
│   │       │   ├── task.py                    # subagent
│   │       │   ├── memory.py
│   │       │   ├── web_search.py
│   │       │   ├── fetch_url.py
│   │       │   ├── request_approval.py
│   │       │   └── finish.py
│   │       └── determinism/
│   │           ├── assert_no_diff.py
│   │           ├── commit_checkpoint.py
│   │           ├── rollback_to_checkpoint.py
│   │           ├── record_env_snapshot.py
│   │           └── verify_env_snapshot.py
│   │
│   ├── sandbox/                               # SANDBOX BACKENDS
│   │   ├── __init__.py
│   │   ├── protocol.py                        # SandboxBackend Protocol
│   │   ├── _jail.py                           # LIFT: vvah/backends/localtools.py `_jail()`
│   │   ├── local.py                           # in-process w/ path jail
│   │   ├── docker.py                          # container-per-session
│   │   ├── landlock.py                        # Linux (calls devharness_sandbox_linux Rust crate)
│   │   ├── modal.py                           # optional
│   │   └── daytona.py                         # optional
│   │
│   ├── memory/                                # MEMORY PROVIDERS
│   │   ├── __init__.py
│   │   ├── protocol.py                        # MemoryProvider Protocol
│   │   ├── local.py                           # ~/.devharness/memory.md
│   │   └── plugins/                           # optional (vector DB, honcho, mem0)
│   │       └── __init__.py
│   │
│   ├── session/                               # PERSISTENCE
│   │   ├── __init__.py
│   │   ├── store.py                           # LIFT: vvah/orchestrator/store.py
│   │   ├── rollout.py                         # append-only writer
│   │   └── replay.py                          # replay-from-rollout
│   │
│   ├── config/                                # CONFIGURATION
│   │   ├── __init__.py                        # LIFT: vvah/config/__init__.py
│   │   ├── defaults.toml
│   │   ├── schema.py                          # pydantic Settings
│   │   └── profiles/
│   │       ├── default.toml
│   │       ├── dev.toml
│   │       └── ci.toml
│   │
│   ├── prompts/                               # PROMPT TEMPLATES
│   │   ├── system_stable.jinja2               # tier 1
│   │   ├── system_context.jinja2              # tier 2
│   │   ├── system_volatile.jinja2             # tier 3
│   │   ├── summarization.jinja2               # for compaction
│   │   ├── subagent.jinja2                    # for children
│   │   ├── plan.jinja2
│   │   └── blocks/                            # shared blocks; LIFT PATTERN: vvah/util/prompts.py
│   │       ├── tool_use_rules.md
│   │       ├── determinism_contract.md
│   │       ├── conventional_commits.md
│   │       ├── verification_rules.md
│   │       └── safety_rules.md
│   │
│   ├── observability/
│   │   ├── __init__.py
│   │   ├── otel.py                            # gen_ai.* emitter
│   │   ├── logs.py                            # JSONL local logging
│   │   └── cost.py                            # LIFT: vvah/util/tokens.py + hermes/agent/billing_usage.py
│   │
│   ├── redaction/
│   │   ├── __init__.py
│   │   └── rules.py                           # LIFT: vvah/report/redact.py
│   │
│   ├── cli/                                   # ENTRY POINTS
│   │   ├── __init__.py
│   │   ├── main.py                            # typer app
│   │   ├── commands/
│   │   │   ├── run.py                         # `devharness run "..."`
│   │   │   ├── doctor.py                      # `devharness doctor`
│   │   │   ├── sessions.py                    # list/show/resume
│   │   │   ├── replay.py                      # `devharness replay <session_id>`
│   │   │   ├── mcp.py                         # `devharness mcp` — MCP-server mode
│   │   │   └── serve.py                       # `devharness serve` — HTTP API
│   │   └── tui.py                             # interactive REPL
│   │
│   ├── api/                                   # HTTP API
│   │   ├── __init__.py
│   │   ├── app.py                             # FastAPI
│   │   ├── routes/
│   │   │   ├── sessions.py
│   │   │   ├── runs.py
│   │   │   └── health.py
│   │   └── auth.py
│   │
│   └── mcp_server/                            # MCP-server mode
│       ├── __init__.py
│       └── server.py                          # exposes devharness as MCP server
│
├── crates/                                    # OPTIONAL RUST HELPERS
│   └── devharness-sandbox-linux/
│       ├── Cargo.toml
│       └── src/
│           ├── lib.rs
│           └── seccomp.rs
│
├── tests/                                     # TEST SUITE
│   ├── unit/
│   │   ├── test_jail.py                       # verifies against vvah tests
│   │   ├── test_patch_parser.py               # V4A edge cases
│   │   ├── test_tool_dispatch.py              # path-overlap detection
│   │   ├── test_registry.py
│   │   ├── test_store.py
│   │   ├── test_redaction.py
│   │   ├── test_config.py
│   │   ├── test_approval_cache.py
│   │   ├── test_prompt_builder.py
│   │   └── test_provider_fallback.py
│   ├── integration/
│   │   ├── test_end_to_end_python_repo.py     # writes tests in a sample repo
│   │   ├── test_end_to_end_rust_repo.py
│   │   ├── test_end_to_end_ts_repo.py
│   │   ├── test_end_to_end_go_repo.py
│   │   ├── test_replay_deterministic.py       # same input → same rollout
│   │   └── test_sandbox_backends.py
│   ├── fixtures/
│   │   ├── sample-python-repo/
│   │   ├── sample-rust-repo/
│   │   ├── sample-ts-repo/
│   │   └── sample-go-repo/
│   └── conftest.py
│
├── evals/                                     # EVAL SUITE
│   ├── swe_bench_lite/                        # SWE-bench-Lite scenarios
│   ├── multi_language/                        # cross-lang benchmarks
│   ├── determinism/                           # 3× same-input → same-rollout
│   └── budget/                                # cost-under-target
│
├── docs/                                      # (this folder)
│   ├── agentic-harness-report.md
│   ├── devharness-spec.md                     # THIS FILE
│   ├── software developer task and workflows
│   └── ...
│
└── scripts/                                   # DEV HELPERS
    ├── bootstrap.sh
    ├── generate_notice.py                     # emit NOTICE from LIFT_MANIFEST.toml
    └── LIFT_MANIFEST.toml                     # canonical list of every ported file
```

---

## 9. Configuration format

### 9.1 Layout

Config files are TOML with `${VAR}` and `${VAR:-default}` expansion (§4.15).

**Default location:** `~/.devharness/config.toml`.
**Override:** `devharness run --config path/to/config.toml`.
**Profile:** `devharness run --profile ci`.

### 9.2 Example `config.toml`

```toml
[session]
# Session ID derivation: for interactive TUI use random UUID; for CI use
# deterministic hash of (goal + repo_head_sha) so the same task on the same
# code produces the same session_id.
id_strategy = "deterministic"  # "uuid" | "deterministic"

[budget]
max_turns = 100
max_input_tokens = 2_000_000
max_output_tokens = 500_000
max_cost_usd = 10.0

[model.default]
provider = "anthropic"
model    = "claude-opus-4-6"
temperature = 0.0
top_p = 1.0
seed = 42
max_tokens = 8192

# Per-role overrides
[model.planning]
provider = "anthropic"
model    = "claude-opus-4-6"

[model.execution]
provider = "anthropic"
model    = "claude-sonnet-4-6"

[model.summarization]
provider = "anthropic"
model    = "claude-haiku-4-5"
temperature = 0.0

[fallback_chain]
default = [
  { provider = "anthropic", model = "claude-sonnet-4-6" },
  { provider = "openai",    model = "gpt-4.1"        },
]

[approval]
mode = "suggest"                       # "suggest" | "auto-edit" | "auto"
require_approval_for_git_push = true
cache_within_session = true

[sandbox]
default = "local"                      # "local" | "docker" | "landlock" | "modal" | "daytona"
docker.image = "ghcr.io/mbarnes-code/devharness/runtime:latest"
docker.mount = "rw,cwd:/workspace"
docker.network = "none"

[compaction]
trigger_tokens = 100_000
keep_recent_messages = 20
summary_model = "anthropic:claude-haiku-4-5"
anti_thrash_cooldown_s = 600

[redaction]
enabled = true
[[redaction.custom]]
name = "acme_key"
regex = "acme_[A-Za-z0-9]{32}"

[observability]
otel.endpoint = "${OTEL_ENDPOINT:-http://localhost:4317}"
otel.enabled = true
jsonl.enabled = true

[secrets]
anthropic_api_key = "${ANTHROPIC_API_KEY}"
openai_api_key    = "${OPENAI_API_KEY:-}"
aws_region        = "${AWS_REGION:-us-east-1}"
gh_token          = "${GH_TOKEN}"

[tools.web_search]
enabled = false                        # explicit opt-in
provider = "tavily"
api_key = "${TAVILY_API_KEY:-}"

[tools.timeouts]
# override any tool's default timeout
run_tests = 1200
install_deps = 900
```

### 9.3 Profile overlay example

```toml
# ~/.devharness/profiles/ci.toml — inherits from ~/.devharness/config.toml, overlays only:
[approval]
mode = "auto"                          # CI is trusted

[budget]
max_cost_usd = 2.0                     # tight budget in CI

[sandbox]
default = "docker"                     # always containerize in CI

[observability]
otel.endpoint = "${CI_OTEL_ENDPOINT}"
```

---

## 10. Sandbox & approval model

### 10.1 Layered defense

1. **Sandbox class per tool** (declared at registration, immutable at runtime).
2. **Sandbox backend** (per session, selectable from `local` / `docker` / `landlock` / `modal` / `daytona`).
3. **Path jail** (universal — even in `local`, filesystem tools go through `_jail()`).
4. **Approval mode** (per session — `suggest` / `auto-edit` / `auto`).
5. **Approval cache** (per session, keyed by `(tool_name, canonical_args_hash)`).
6. **Redaction** (universal — every tool result before re-injection into context).
7. **Prompt-injection scan** (on repo-loaded context files at session start).

### 10.2 Approval decision tree

```text
tool_call arrives
    │
    ▼
Is approval_class == "!" ?  ── yes ──► ALWAYS ask user
    │
    no
    │
    ▼
Check approval cache for (tool, hash(args))
    │
    ├─ HIT (approved) ─► execute
    │
    ▼
Read approval.mode:
    ├── "auto"      ─► execute
    ├── "suggest"   ─► if sandbox_class in {read-only, mutating-file} in git-dirty-set
    │                    → execute (auto)
    │                  else
    │                    → ask user
    └── "auto-edit" ─► if sandbox_class in {read-only, mutating-file}
                         → execute (auto)
                       else
                         → ask user
```

Cache decisions are persisted in `approvals` table for replay.

### 10.3 Session start-of-life checks

1. **`devharness doctor`** style preflight:
   - Verify Python version, `git`, `gh` (if using PR tools), all recommended CLIs.
   - Report missing tools + which tool categories will be disabled.
2. **Env snapshot** (`record_env_snapshot`).
3. **Provider auth check** (call each configured provider's `/models` endpoint; abort if unauthorized).
4. **Sandbox backend health check** (spawn a `whoami` in the sandbox; abort if fails).
5. **Freeze tier-1 prompt** and record its SHA-256 in env manifest.
6. **Serialize + freeze tool list**, record `tools_hash`.
7. **Announce session_id, resolved config path, budgets** to the user.

---

## 11. Data flow — end-to-end

Example: user says *"add unit tests for `parse_config()` in `src/config.py` and open a PR"*.

```text
1. CLI: devharness run "add unit tests..."
        │
        ▼
2. Config resolution:
   defaults.toml → ~/.devharness/config.toml → --profile → env expansion → CLI overrides
        │
        ▼
3. Session start:
   - Derive session_id (deterministic hash if configured)
   - Open ~/.devharness/sessions/{session_id}/rollout.db
   - Emit `record_env_snapshot` → env-manifest.json
   - Preflight (doctor + provider auth + sandbox spawn)
   - Freeze tier-1 prompt (SHA in manifest)
   - Freeze tool list (tools_hash in manifest)
        │
        ▼
4. Turn 1 begins:
   - LoadRepoHints reads AGENTS.md, .cursorrules, CLAUDE.md
   - PromptInjectionScan on those files
   - PromptBuilder assembles:
       tier1 (frozen) + tier2 (goal + AGENTS.md) + tier3 (empty todo)
   - InferenceRunner sends request to Anthropic (model.execution)
        │
        ▼
5. Model responds with tool_calls:
   [read_file(src/config.py), grep(pattern="parse_config", path="tests/")]
        │
        ▼
6. ToolDispatch:
   - Both read_file and grep are RO, parallel-safe, disjoint paths → parallel batch
   - No approval needed (RO in suggest mode)
   - Executed in devharness/sandbox/local.py via _jail()
   - Results redacted, checked for spillover, injected as `role="tool"` messages
        │
        ▼
7. Turn 2 begins (loop back with tool results):
   Model responds:
     - todo_add("write parse_config unit tests")
     - commit_checkpoint()  # auto-inserted by harness before next mutation
     - apply_patch(...V4A patch adding tests/test_config.py...)
        │
        ▼
8. ToolDispatch:
   - todo_add is RO auto
   - commit_checkpoint is MG auto (harness invariant)
   - apply_patch is MF; in "suggest" mode with the file NOT in git-dirty-set → asks user
        │
        ▼
9. ApprovalGate prompts:
   "Apply the following patch? [y/N/always]"
   User: "always"
   → Cached for (apply_patch, hash(this args)) for the session
   → Executed via sandbox_runner → local (path-jailed)
        │
        ▼
10. Turn 3:
    Model: run_tests(paths=["tests/test_config.py"])
    → Sandbox runs `pytest -p no:randomly tests/test_config.py`
    → Failed (expected — model may have made a small mistake)
    → Result (JSON summary) injected
        │
        ▼
11. Turn 4:
    Model: apply_patch (fix), run_tests (retry)
    → Approved from cache
    → Tests pass
        │
        ▼
12. Turn 5:
    - format_code (auto)
    - lint_code (auto, RO)
    - typecheck (auto, RO)
    - scan_secrets (auto, RO)
    → All pass
        │
        ▼
13. Turn 6:
    - git_add ["tests/test_config.py"]
    - git_commit -m "feat(config): add unit tests for parse_config"
      → Conventional Commits regex ✅
    - gh_pr_create(title="...", body="...")
      → Always requires approval (network + org side-effect)
    → User approves
    → PR opened
        │
        ▼
14. Model calls finish(summary="Added tests, opened PR #123", changed_files=[...])
        │
        ▼
15. Session end:
    - Compute cost from tokens
    - Emit final OTel span
    - Close rollout.db
    - Print session summary + PR link
```

**Replay:** `devharness replay {session_id}` reads rollout.db, mocks the provider to return recorded responses, and reproduces the exact tool sequence. Used in tests + debugging.

---

## 12. MVP roadmap

### Milestone 0 — Foundation (week 1)
- [ ] Project scaffolding, `pyproject.toml`, `NOTICE`, `THIRD_PARTY_LICENSES.md`, `LIFT_MANIFEST.toml`
- [ ] `devharness/config/` — port from VVAH
- [ ] `devharness/sandbox/_jail.py` — port from VVAH `localtools.py`
- [ ] `devharness/session/store.py` — port from VVAH `orchestrator/store.py`
- [ ] `devharness doctor` command working

### Milestone 1 — Loop skeleton (week 2)
- [ ] `devharness/loop/engine.py` — Step[] driver
- [ ] `devharness/providers/base.py` + `anthropic.py` (only)
- [ ] `devharness/tools/registry.py` — port from Hermes, AST-discovery cache
- [ ] Three tools: `read_file`, `grep`, `run_tests` (pytest only)
- [ ] `devharness run "hello"` executes one turn end-to-end

### Milestone 2 — Editing capability (week 3)
- [ ] `devharness/tools/impl/git/apply_patch.py` — port Hermes V4A parser
- [ ] `devharness/tools/impl/determinism/commit_checkpoint.py`
- [ ] `devharness/loop/steps/tool_dispatch.py` — port Hermes dispatch + path overlap
- [ ] `devharness/loop/steps/approval_gate.py` — port Hermes approval cache
- [ ] `devharness/loop/steps/result_redactor.py` — port VVAH redact
- [ ] Can now: read, edit, test, commit

### Milestone 3 — Prompt & context (week 4)
- [ ] `devharness/loop/steps/prompt_builder.py` — 3-tier
- [ ] `devharness/prompts/*.jinja2` — shipped templates
- [ ] `devharness/loop/steps/load_repo_hints.py` — AGENTS.md walker
- [ ] `devharness/loop/steps/prompt_injection_scan.py`
- [ ] `devharness/providers/prompt_cache.py` — Anthropic ephemeral blocks
- [ ] `devharness/loop/steps/result_spillover.py`

### Milestone 4 — Multi-provider + observability (week 5)
- [ ] `devharness/providers/openai.py`, `openai_responses.py`, `bedrock.py`, `google_genai.py`, `ollama.py`
- [ ] `devharness/providers/fallback.py`
- [ ] `devharness/observability/otel.py` + `logs.py` + `cost.py`
- [ ] `devharness/loop/steps/compaction.py` (design lift from DeepAgents)

### Milestone 5 — Full tool catalog (week 6)
- [ ] All 60 tools in `devharness/tools/impl/`
- [ ] Integration tests for 4 sample repos (py, ts, rs, go)
- [ ] `devharness/loop/steps/verification_gate.py` (`assert_no_diff`, re-run affected tests)

### Milestone 6 — Sandboxes & surfaces (week 7)
- [ ] `devharness/sandbox/docker.py`
- [ ] `devharness/sandbox/landlock.py` + `crates/devharness-sandbox-linux/`
- [ ] `devharness/cli/tui.py` — interactive TUI
- [ ] `devharness/api/app.py` — HTTP API
- [ ] `devharness/mcp_server/server.py` — MCP-server mode

### Milestone 7 — Subagents & memory (week 8)
- [ ] `devharness/tools/impl/core/task.py` — SubagentRunner (design lift from DeepAgents)
- [ ] `devharness/memory/local.py` — local memory provider

### Milestone 8 — Determinism polish + eval (week 9)
- [ ] All 5 `determinism/` tools implemented
- [ ] `devharness replay` command
- [ ] `evals/determinism/` — 3× same-input → same-rollout tests
- [ ] `evals/swe_bench_lite/` — subset eval
- [ ] Cloud sandbox providers (`modal`, `daytona`) as optional extras

### Milestone 9 — Docs & 1.0 (week 10)
- [ ] Doc site
- [ ] Migration guide from Codex/Aider
- [ ] Public examples
- [ ] `pip install devharness` on PyPI

---

## 13. Testing & evaluation

### 13.1 Test pyramid

1. **Unit tests** (~500 tests target): each subsystem in isolation. Uses `pytest -p no:randomly` for determinism. Provider tests use `httpx` `MockTransport`.
2. **Integration tests** (~50): 4 sample repos in `tests/fixtures/`, each language. Full loop against a mocked provider.
3. **Determinism tests** (~10): given identical config + goal + mocked provider responses, run 3 times; assert `rollout.db` bytes identical, `changed_files` identical.
4. **Cost tests**: assert per-scenario cost stays under a budget threshold (regression detection).
5. **Snapshot tests**: TUI rendering via `rich`'s recording feature.
6. **Eval suite** (`evals/`): SWE-bench-Lite subset, multi-language benchmarks, budget-under-target.

### 13.2 CI pipeline

- **Lint + format + typecheck** on every push (`ruff check`, `ruff format --check`, `mypy`).
- **Unit tests** on push (~30s).
- **Integration tests** on PR (~5min).
- **Determinism tests** nightly.
- **Eval suite** weekly (uses real provider quota — gated).
- **Dependency audit** nightly (`pip-audit`, `gitleaks`).

### 13.3 Behavior contract tests (Hermes-style)

Assert invariants, not values:
- `role alternation never breaks` — after any turn, no two consecutive `user` or `assistant` messages.
- `tier-1 hash stable` — SHA-256 of tier-1 unchanged through the session.
- `every mutation preceded by a checkpoint` — for every `MG`/`MF` tool call, an earlier `commit_checkpoint` exists.
- `every tool result passed through redaction` — no `role="tool"` message contains an unredacted secret.
- `no unbounded loops` — recursion limit trips before turn 100 in test scenarios.

---

## 14. License & attribution

### 14.1 Project license

**Apache License 2.0**. Files: `LICENSE`, `NOTICE`, `THIRD_PARTY_LICENSES.md`.

### 14.2 Attribution summary

| Repo | Lifts | License | Attribution mechanism |
|---|---|---|---|
| **visa-vulnerability-agentic-harness** | `_jail()`, `store.py`, `redact.py`, `config/__init__.py`, `util/prompts.py`, `util/tokens.py` | Apache 2.0 | NOTICE entry + per-file docstring |
| **hermes-agent** | `patch_parser.py`, `tool_dispatch_helpers.py`, `registry.py`, `approval.py`, `tool_result_storage.py`, `threat_patterns.py`, `providers/base.py`, `agent/billing_usage.py` | MIT | Per-file docstring + THIRD_PARTY_LICENSES.md |
| **deepagents** | Design lifts: `summarization.py`, `subagents.py`, `_messages_reducer.py`, `_prompt_caching.py` | MIT | Per-file docstring |
| **codex** | Design references only: `apply-patch/`, `linux-sandbox/` | Apache 2.0 | NOTICE mention |
| **goose** | Design references only: `machine.rs` (Step[] engine) | Apache 2.0 | NOTICE mention |
| **open-swe** | Design references only (webhook thread-id pattern) | MIT | Docstring mention where applied |
| **bedrock-engineer** | Design references only (TUI patterns) | MIT-0 | No attribution required (voluntary NOTICE mention) |
| **autoresearch** | **NONE — no LICENSE file** | Unlicensed | Referenced in prose only |

### 14.3 `LIFT_MANIFEST.toml` (canonical source-of-truth for `NOTICE`)

Every ported file's origin is recorded in `scripts/LIFT_MANIFEST.toml`:

```toml
[[lifts]]
dest = "devharness/sandbox/_jail.py"
source_repo = "visa/visa-vulnerability-agentic-harness"
source_path = "vvaharness/backends/localtools.py"
source_commit = "<sha>"
license = "Apache-2.0"
strategy = "verbatim"
lines = "61-70,71-94,96-140"

[[lifts]]
dest = "devharness/tools/impl/git/apply_patch.py"
source_repo = "NousResearch/hermes-agent"
source_path = "tools/patch_parser.py"
source_commit = "<sha>"
license = "MIT"
strategy = "verbatim"

# ... one entry per port
```

`scripts/generate_notice.py` reads this manifest and emits `NOTICE` + entries in `THIRD_PARTY_LICENSES.md` — keeping attribution automatic and CI-verifiable.

---

## Appendix A — Cheat sheet for reviewers

**"Where do I look if I want to add a new tool?"**
→ Create `devharness/tools/impl/{category}/{name}.py`, decorate with `@register(...)`, done. AST discovery picks it up.

**"How do I add a new LLM provider?"**
→ Create `devharness/providers/{provider}.py` implementing `Provider` protocol; register in `providers/__init__.py`; add pricing to `providers/pricing.toml`.

**"How do I add a new sandbox backend?"**
→ Create `devharness/sandbox/{backend}.py` implementing `SandboxBackend` protocol; add config schema in `config/schema.py`; write integration test.

**"How do I make my run deterministic?"**
→ Set `temperature = 0`, `seed = 42`; use `session.id_strategy = "deterministic"`; freeze provider version in `env-manifest.json`; verify with `devharness replay`.

**"How do I contribute a lift from another repo?"**
→ Add entry to `scripts/LIFT_MANIFEST.toml`, port the code with a docstring header referencing origin, run `scripts/generate_notice.py`, submit PR.

---

## Appendix B — Open questions (to resolve during MVP)

1. **`msgspec` vs `pydantic` for tool schemas.** `msgspec` is ~20× faster but less ergonomic. Bench during Milestone 1; may go dual (`msgspec` for hot path, `pydantic` for config).
2. **Whether to ship a first-party MCP client** or rely on `pip install devharness[mcp]` with the upstream Python SDK. Leaning upstream to avoid protocol drift.
3. **How aggressive should `commit_checkpoint` be?** Every batch of MF/MG calls creates a commit — this could produce 100+ commits per session. Squash-on-finish? Configurable retention?
4. **Provider auto-fallback semantics for structured tool schemas.** Different providers have different `tool_choice` semantics; the fallback layer must normalize. Detailed spec TBD.
5. **Whether the AGENTS.md walker should traverse Git submodules.** Default: no. But some monorepos may want yes.

These will be closed during implementation and reflected in v1.1 of this spec.

---

**End of spec.**

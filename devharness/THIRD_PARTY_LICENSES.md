# Third-party licenses

This document identifies the third-party open-source projects whose code has
been adapted into `devharness`. Every ported file also carries an in-source
attribution header; the canonical machine-readable manifest of all lifts is
[`scripts/LIFT_MANIFEST.toml`](scripts/LIFT_MANIFEST.toml).

The `devharness` project itself is distributed under the [Apache License 2.0](LICENSE).
The Apache 2.0 license text at `LICENSE` satisfies the redistribution
obligations of both the upstream projects listed below, since:

- Apache 2.0 sources: our project license *is* Apache 2.0, and the required
  attribution has been forwarded into [`NOTICE`](NOTICE) per Apache 2.0 § 4(d).
- MIT sources: Apache 2.0 is compatible with MIT; the required MIT copyright
  notice and permission notice are reproduced verbatim below (per MIT terms).

## Provenance summary

| Upstream project | License | Files ported into devharness | Attribution |
|---|---|---|---|
| [Visa Vulnerability Agentic Harness](https://github.com/visa/visa-vulnerability-agentic-harness) | Apache 2.0 | 6 files (see [§1](#1-visa-vulnerability-agentic-harness-vvaharness)) | NOTICE forwarded per § 4(d) |
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | MIT | 8 files (see [§2](#2-hermes-agent)) | Copyright + permission notice below || [DeepAgents](https://github.com/langchain-ai/deepagents) | MIT | 1 file (see [§3](#3-deepagents)) | Copyright + permission notice below |
---

## 1. Visa Vulnerability Agentic Harness (vvaharness)

- **Upstream:** <https://github.com/visa/visa-vulnerability-agentic-harness>
- **Upstream commit at time of port:** `3d972f679d8f5e3838b394edee0b5ea9c626b0fb`
- **License:** Apache License 2.0 — copyright 2026 Visa, Inc.
- **License text:** available in the upstream repository at `LICENSE`; identical
  to the standardized Apache 2.0 text at
  <https://www.apache.org/licenses/LICENSE-2.0>. The same text is shipped with
  this project as [LICENSE](LICENSE).

### Upstream `NOTICE` (forwarded per Apache 2.0 § 4(d))

The upstream `NOTICE` file is reproduced verbatim in the top-level
[`NOTICE`](NOTICE) file of this project.

### Ported files

| Adapted file in devharness | Upstream source |
|---|---|
| `devharness/sandbox/_jail.py` | `vvaharness/backends/localtools.py` |
| `devharness/providers/anthropic.py` (partial) | `vvaharness/backends/sdk.py` |
| `devharness/session/store.py` | `vvaharness/orchestrator/store.py` |
| `devharness/config/__init__.py` | `vvaharness/config/__init__.py` |
| `devharness/redaction/rules.py` | `vvaharness/report/redact.py` |
| `devharness/observability/cost.py` (partial) | `vvaharness/util/tokens.py` |

Each ported file was selectively adapted (not copied verbatim); adaptations
are documented inline in each file's module docstring and in
[`scripts/LIFT_MANIFEST.toml`](scripts/LIFT_MANIFEST.toml).

---

## 2. Hermes Agent

- **Upstream:** <https://github.com/NousResearch/hermes-agent>
- **Upstream commit at time of port:** `f293e7206b4ddd66042329442c6afebc19a8808d`
- **License:** MIT License — Copyright (c) 2025 Nous Research

### Required MIT notice

MIT requires that "the above copyright notice and this permission notice shall
be included in all copies or substantial portions of the Software." The full
notice, reproduced verbatim from the upstream `LICENSE`, is:

> MIT License
>
> Copyright (c) 2025 Nous Research
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to
> deal in the Software without restriction, including without limitation the
> rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
> sell copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in
> all copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
> FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
> DEALINGS IN THE SOFTWARE.

### Ported files

| Adapted file in devharness | Upstream source |
|---|---|
| `devharness/tools/impl/git/apply_patch.py` | `tools/patch_parser.py` |
| `devharness/tools/registry.py` | `tools/registry.py` |
| `devharness/loop/steps/tool_dispatch.py` | `agent/tool_dispatch_helpers.py` |
| `devharness/loop/steps/approval_gate.py` | `tools/approval.py` (selective) |
| `devharness/loop/steps/result_spillover.py` | `tools/tool_result_storage.py` |
| `devharness/loop/steps/prompt_injection_scan.py` | `tools/threat_patterns.py` |
| `devharness/providers/base.py` | `providers/base.py` |
| `devharness/observability/cost.py` (partial) | `agent/billing_usage.py` |

Each ported file was selectively adapted (not copied verbatim); adaptations
are documented inline in each file's module docstring and in
[`scripts/LIFT_MANIFEST.toml`](scripts/LIFT_MANIFEST.toml).

---

## 3. DeepAgents

- **Upstream:** <https://github.com/langchain-ai/deepagents>
- **Upstream commit at time of port:** `23b83ad50f63d241d0069a3dc426d43b211adf2e`
- **License:** MIT License — Copyright (c) LangChain, Inc.

### Required MIT notice

MIT requires that "the above copyright notice and this permission notice shall
be included in all copies or substantial portions of the Software." The full
notice, reproduced verbatim from the upstream `LICENSE`, is:

> MIT License
>
> Copyright (c) LangChain, Inc.
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to
> deal in the Software without restriction, including without limitation the
> rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
> sell copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in
> all copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
> FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
> DEALINGS IN THE SOFTWARE.

### Ported files

| Adapted file in devharness | Upstream source |
|---|---|
| `devharness/loop/steps/compaction.py` | `libs/deepagents/deepagents/middleware/summarization.py` (extract-pattern) |

Each ported file was selectively adapted (not copied verbatim); adaptations
are documented inline in each file's module docstring and in
[`scripts/LIFT_MANIFEST.toml`](scripts/LIFT_MANIFEST.toml).

---

## Design references (no code lifted)

The following upstream projects informed the design of devharness but no code
was copied from them. They are acknowledged here in gratitude; no legal
obligations arise from a pure design lift.

| Project | License | Upstream | Design reference |
|---|---|---|---|
| OpenAI Codex | Apache 2.0 | <https://github.com/openai/codex> | V4A patch grammar; per-OS sandbox layout (Landlock+bwrap / Seatbelt / RestrictedToken) |
| Block Goose | Apache 2.0 | <https://github.com/block/goose> | Explicit `Step[]` state-machine loop engine |
| LangChain DeepAgents | MIT | <https://github.com/langchain-ai/deepagents> | Middleware composition; SubAgent TypedDict shape (the summarization pattern is a code lift; see [§3](#3-deepagents)) |
| LangChain Open SWE | MIT | <https://github.com/langchain-ai/open-swe> | Deterministic thread-id-from-invocation-surface pattern |
| Bedrock Engineer | MIT-0 | <https://github.com/aws-samples/bedrock-engineer> | PLAN/ACT dual-mode prompt UX; TUI patterns |

---

## Reporting license issues

If you believe a file in this repository has an incorrect attribution or is
missing required notices, please open an issue on the devharness repository
identifying (a) the file, (b) the upstream source, and (c) the specific
license clause you believe is not satisfied.

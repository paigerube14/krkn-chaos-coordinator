<p align="center">
  <h1 align="center">krkn-chaos-coordinator</h1>
  <p align="center">
    <b>AI-driven multi-agent system for autonomous chaos test coverage expansion</b>
    <br/>
    Monitors JIRA bugs · Identifies coverage gaps · Creates PRs & issues
    <br/><br/>
    <a href="config/agents/README.md"><img src="https://img.shields.io/badge/agents-pluggable-blue?style=flat-square" alt="Pluggable Agents"></a>
    <a href="config/filters/README.md"><img src="https://img.shields.io/badge/filters-configurable-green?style=flat-square" alt="Configurable Filters"></a>
    <img src="https://img.shields.io/badge/tests-200%20passing-brightgreen?style=flat-square" alt="Tests">
    <img src="https://img.shields.io/badge/python-3.11%2B-yellow?style=flat-square" alt="Python">
    <img src="https://img.shields.io/badge/license-Apache--2.0-lightgrey?style=flat-square" alt="License">
  </p>
</p>

---

## Pipeline

```
DISCOVER → FILTER → MAP → ANALYZE → ACT → REMEMBER
```

| Phase | What it does |
|:------|:------------|
| **DISCOVER** | Query JIRA bugs (4-tier version matching) + z-stream changelogs |
| **FILTER** | 3-tier: keyword pre-filter → semantic cache → LLM classification |
| **MAP** | ChromaDB RAG + LLM reasoning over existing krkn scenarios |
| **ANALYZE** | Score confidence (0-100), generate specific krkn modifications |
| **ACT** | Create GitHub issues or draft PRs based on confidence |
| **REMEMBER** | Store in Neo4j — never re-analyze the same bug |

## Agents

```
Orchestrator (dedup, approval queue)
├── control_plane       Etcd, kube-apiserver, HyperShift
├── networking          OVN-K, DNS, router, SR-IOV, MetalLB
├── node_machine        Kubelet, CRI-O, Machine API, Bare Metal
├── storage             CSI, Image Registry, LVMS
├── operators_platform  OLM, Console, Auth, Monitoring
├── upgrade_lifecycle   CVO, MCO, Installer variants
└── your_agent          drop a YAML in config/agents/
```

113 OCPBUGS components across 6 built-in agents. Pluggable — [add your own →](config/agents/README.md)

---

## Quick Start

```bash
git clone https://github.com/shahsahil264/krkn-chaos-coordinator.git
cd krkn-chaos-coordinator
./setup.sh
```

> The interactive setup script handles Python, venv, krkn clone, API tokens (JIRA + GitHub), Neo4j, and verification. Prerequisites: Python 3.11+, Podman/Docker, Git.

After setup:
```bash
source venv/bin/activate
PYTHONPATH=. python -m src.knowledge.ingest ./chroma_data    # one-time, ~6 min
```

## Usage

#### Claude Code (recommended)
```bash
claude
/krkn-chaos-scan           # interactive — version, agents, lookback, scan type
```

#### CLI
```bash
python src/main.py --release 4.21 --use-llm                         # all agents
python src/main.py --release 4.21 --agent control_plane --use-llm   # single
python src/main.py --release 4.20,4.21 --agent networking,storage   # multi
python src/main.py --release 4.21                                    # keyword only (no LLM)
```

#### Status output
```
[networking] ●●○○○○ FILTER   ████░░░░ 3/8  LLM 3/8 — OCPBUGS-86810
[networking] ●●●●●● REMEMBER ████████ done — 2 gaps, 10 LLM calls, $0.27
```
Colored progress bars in terminal. Clean plain text in Claude Code. Logs → `krkn-chaos-coordinator.log`.

---

## Adding a New Agent

One YAML file. No code changes.

```yaml
# config/agents/virtualization.yaml
name: virtualization
description: "OpenShift Virtualization / CNV / KubeVirt"
components:                          # JIRA components to monitor
  - "OpenShift Virtualization"
  - "Virtualization / virt-controller"
filter:                              # domain-specific keywords (merged with common)
  chaos_keywords:
    - "vm migration failed"
    - "virt-launcher crash"
docs:                                # domain docs for ChromaDB (github / local / url)
  - type: github
    owner: kubevirt
    repo: kubevirt
    path: docs
```

```bash
python src/main.py --release 4.21 --agent virtualization --use-llm
```

Guides: [Agent config](config/agents/README.md) · [Filter keywords](config/filters/README.md)

---

<details>
<summary><b>4-Tier JIRA Version Query</b></summary>
<br/>

| Tier | What it catches |
|:-----|:---------------|
| 1 | Bugs tagged with target release (4.21.*) |
| 2 | Open bugs from older versions (unfixed — likely still present) |
| 3 | Open bugs from newer versions (if it exists on 5.0, it exists on 4.21) |
| 4 | Bugs with no `affectedVersion` set |

Closed/Verified bugs on other versions are excluded — they're already fixed.
</details>

<details>
<summary><b>Token Optimization (91% cost reduction)</b></summary>
<br/>

| Layer | Technique | Savings |
|:------|:----------|:--------|
| 1 | Keyword pre-filter | ~55% bugs at zero tokens |
| 2 | Semantic cache (ChromaDB) | Reuses past LLM decisions |
| 3 | Model routing | Sonnet for FILTER/MAP, Opus for ANALYZE |
| 4 | Confidence escalation | Sonnet → Opus only when uncertain |
| 5 | Prompt caching | 90% off repeated system prompts |
| 6 | Batch API | 50% off, stacks with caching |

`claude_code` provider uses `--bare` to strip 62K system prompt overhead → ~2,700 tokens/call.
</details>

<details>
<summary><b>LLM Providers</b></summary>
<br/>

| Provider | API Key | Notes |
|:---------|:--------|:------|
| `claude_code` | No | Auto-detected when `claude` is on PATH |
| `anthropic` | Yes | Production — prompt caching + batch API |
| `ollama` | No | Local models, free |
| `openai` | Yes | GPT-4o compatible |
| `google` | Yes | Gemini compatible |

Per-phase routing: `LLM_FILTER_MODEL=claude-sonnet-4-6` · `LLM_ANALYZE_MODEL=claude-opus-4-6`
</details>

<details>
<summary><b>Environment Variables</b></summary>
<br/>

| Variable | Required | Default | Description |
|:---------|:---------|:--------|:------------|
| `JIRA_USERNAME` | Yes | — | Your JIRA email |
| `JIRA_API_TOKEN` | Yes | — | [Generate here](https://id.atlassian.com/manage-profile/security/api-tokens) |
| `GITHUB_TOKEN` | Yes | — | [Generate here](https://github.com/settings/tokens) |
| `NEO4J_PASSWORD` | Yes | `password` | Neo4j password |
| `LLM_PROVIDER` | No | auto-detected | `claude_code` · `anthropic` · `ollama` · `openai` · `google` · `none` |
| `KRKN_REPO_PATH` | No | `~/krkn` | Local krkn clone path |
| `OCP_RELEASE` | No | `4.21` | Target OpenShift release |
</details>

<details>
<summary><b>Project Structure</b></summary>
<br/>

```
config/
├── agents/                    # Drop a YAML to add a new agent
│   ├── control_plane.yaml
│   └── ...
└── filters/
    └── common.yaml            # Shared filter keywords

src/
├── main.py                    # CLI entry point
├── status.py                  # Pipeline status line
├── models.py                  # Domain models
├── reasoning.py               # LLM reasoning (MAP + ANALYZE)
├── agents/
│   ├── base_agent.py          # Pipeline: DISCOVER → REMEMBER
│   └── registry.py            # Auto-discovers agents from YAML
├── apis/
│   ├── jira_client.py         # JIRA REST API (4-tier query)
│   ├── github_client.py       # GitHub API
│   └── release_client.py      # Z-stream changelogs
├── knowledge/
│   ├── chromadb_store.py      # Vector search
│   ├── neo4j_store.py         # Graph memory
│   ├── ingest.py              # Doc ingestion (GitHub/local/URL)
│   └── filter_cache.py        # Semantic cache
├── filter/
│   ├── chaos_filter.py        # Keyword filter (YAML config)
│   ├── llm_filter.py          # LLM filter (5 providers)
│   ├── llm_config.py          # Per-phase model routing
│   └── llm_tools.py           # Typed tool functions
└── evals/
    └── filter_eval.py         # Model comparison eval
```
</details>

---

**Tests:** `PYTHONPATH=. pytest tests/ -v` — 200 tests (187 unit + 13 integration)

**License:** Apache-2.0

# krkn-chaos-coordinator

AI-driven multi-agent system that autonomously expands [krkn](https://github.com/krkn-chaos/krkn) chaos test coverage for OpenShift clusters by monitoring JIRA bugs, identifying coverage gaps, and creating PRs/issues.

## How It Works

```
DISCOVER → FILTER → MAP → ANALYZE → ACT → REMEMBER
```

| Phase | What it does |
|-------|-------------|
| **DISCOVER** | Query JIRA bugs (4-tier version matching) + z-stream changelogs |
| **FILTER** | 3-tier: keyword pre-filter → semantic cache → LLM classification |
| **MAP** | ChromaDB RAG + LLM reasoning over existing krkn scenarios |
| **ANALYZE** | Score confidence (0-100), generate specific krkn modifications |
| **ACT** | Create GitHub issues or draft PRs based on confidence |
| **REMEMBER** | Store in Neo4j — never re-analyze the same bug |

6 pluggable domain agents (control plane, networking, node/machine, storage, operators, upgrade), auto-discovered from `config/agents/*.yaml`. [Add your own →](config/agents/README.md)

## Quick Start

```bash
git clone https://github.com/shahsahil264/krkn-chaos-coordinator.git
cd krkn-chaos-coordinator
./setup.sh                    # interactive — walks you through everything
```

Then ingest the knowledge base (one-time, ~6 min):
```bash
source venv/bin/activate
PYTHONPATH=. python -m src.knowledge.ingest ./chroma_data
```

> **Prerequisites:** Python 3.11+, Podman or Docker, Git. The setup script checks for these and tells you how to install anything missing. It prompts for JIRA and GitHub tokens with links to generate them.

## Running

**Claude Code (recommended):**
```bash
claude
/krkn-chaos-scan              # interactive — asks version, agents, scan type
```

**CLI:**
```bash
PYTHONPATH=. python src/main.py --release 4.21 --use-llm                          # all agents
PYTHONPATH=. python src/main.py --release 4.21 --agent control_plane --use-llm    # single agent
PYTHONPATH=. python src/main.py --release 4.20,4.21 --agent networking,storage    # multi
```

**Status output:**
```
[networking] ●●○○○○ FILTER   ████░░░░ 3/8 LLM 3/8 — OCPBUGS-86810
[networking] ●●●●●● REMEMBER ████████ done — 2 gaps, 10 LLM calls, $0.27
```

## Adding a New Agent

Create one YAML file in `config/agents/`. No code changes needed.

```yaml
# config/agents/virtualization.yaml
name: virtualization
description: "OpenShift Virtualization / CNV / KubeVirt"
components:
  - "OpenShift Virtualization"
  - "Virtualization / virt-controller"
filter:
  chaos_keywords:
    - "vm migration failed"
    - "virt-launcher crash"
docs:
  - type: github
    owner: kubevirt
    repo: kubevirt
    path: docs
```

Then: `PYTHONPATH=. python src/main.py --release 4.21 --agent virtualization --use-llm`

Full reference: [config/agents/README.md](config/agents/README.md) | Filter keywords: [config/filters/README.md](config/filters/README.md)

## Key Design Decisions

<details>
<summary><b>4-tier JIRA version query</b></summary>

| Tier | Catches |
|------|---------|
| 1 | Bugs tagged with target release (4.21.*) |
| 2 | Open bugs from older versions (unfixed) |
| 3 | Open bugs from newer versions (if it exists on 5.0, it exists on 4.21) |
| 4 | Bugs with no version set |

Closed/Verified bugs on other versions are excluded.
</details>

<details>
<summary><b>Token optimization (91% cost reduction)</b></summary>

1. **Keyword pre-filter** — catches ~55% at zero tokens
2. **Semantic cache** — ChromaDB reuses past LLM decisions
3. **Model routing** — Sonnet for FILTER/MAP, Opus for ANALYZE
4. **Confidence escalation** — Sonnet → Opus only when uncertain
5. **Prompt caching** — 90% off repeated system prompts
6. **Batch API** — 50% off, stacks with caching

`claude_code` provider: `--bare` strips 62K system prompt overhead → ~2,700 tokens/call.
</details>

<details>
<summary><b>5 LLM providers</b></summary>

| Provider | API Key | Notes |
|----------|---------|-------|
| `claude_code` | No | Auto-detected when `claude` CLI is on PATH |
| `anthropic` | Yes | Production — prompt caching + batch API |
| `ollama` | No | Local models, free |
| `openai` | Yes | GPT-4o compatible |
| `google` | Yes | Gemini compatible |

Per-phase routing: `LLM_FILTER_MODEL=claude-sonnet-4-6`, `LLM_ANALYZE_MODEL=claude-opus-4-6`
</details>

<details>
<summary><b>Environment variables</b></summary>

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `JIRA_USERNAME` | Yes | — | Your JIRA email |
| `JIRA_API_TOKEN` | Yes | — | [Generate here](https://id.atlassian.com/manage-profile/security/api-tokens) |
| `GITHUB_TOKEN` | Yes | — | [Generate here](https://github.com/settings/tokens) |
| `NEO4J_PASSWORD` | Yes | `password` | Neo4j password |
| `LLM_PROVIDER` | No | auto-detected | `claude_code`, `anthropic`, `ollama`, `openai`, `google`, `none` |
| `KRKN_REPO_PATH` | No | `~/krkn` | Path to local krkn clone |
| `OCP_RELEASE` | No | `4.21` | Target OpenShift release |
</details>

<details>
<summary><b>Project structure</b></summary>

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
├── reasoning.py               # LLM reasoning for MAP + ANALYZE
├── agents/
│   ├── base_agent.py          # Pipeline: DISCOVER→REMEMBER
│   └── registry.py            # Auto-discovers agents from YAML
├── apis/
│   ├── jira_client.py         # JIRA REST API (4-tier version query)
│   ├── github_client.py       # GitHub API
│   └── release_client.py      # Z-stream changelog enrichment
├── knowledge/
│   ├── chromadb_store.py      # Vector search (4 collections)
│   ├── neo4j_store.py         # Graph memory
│   ├── ingest.py              # Doc ingestion (GitHub, local, URL)
│   └── filter_cache.py        # Semantic cache
├── filter/
│   ├── chaos_filter.py        # Keyword filter (from YAML config)
│   ├── llm_filter.py          # LLM filter (5 providers, token tracking)
│   ├── llm_config.py          # Per-phase model routing
│   └── llm_tools.py           # Typed tool functions
└── evals/
    └── filter_eval.py         # Model comparison eval
```
</details>

## Tests

```bash
PYTHONPATH=. pytest tests/ -v       # 200 tests (187 unit + 13 integration)
```

## License

Apache-2.0

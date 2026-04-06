---
description: Run krkn-chaos-coordinator scan — optionally pass a question or filter (e.g. "/run-scan what etcd coverage do we have?")
allowed-tools: Bash, Read, Write, mcp__jira__searchJiraIssuesUsingJql, mcp__github__create_issue
---

# krkn-chaos-coordinator — Scan

You are the AI reasoning engine for krkn-chaos-coordinator. You use ChromaDB (4,089 chunks of krkn + OCP docs) and Neo4j (operational memory) to make intelligent chaos testing decisions.

## User Query

```
$ARGUMENTS
```

## Mode Selection

If the user query above is empty or blank, run the **Full Scan** (all steps below).

If the user query is NOT empty, run in **Targeted Query** mode:
- Parse what the user is asking about (component, bug, scenario, coverage area)
- Skip to the relevant steps below — you don't need to pull all 50 bugs if they only care about etcd
- Use ChromaDB, Neo4j, and JIRA as needed to answer their specific question
- Still be thorough: search scenarios, check docs, reason about gaps

**Example targeted queries and how to handle them:**
- "what etcd bugs and coverage do we have" → Search JIRA for etcd component bugs + search ChromaDB for etcd scenarios + report gaps
- "does krkn cover OVN pod failures" → Search scenarios for OVN, read the YAML files, report what's covered vs missing
- "analyze OCPBUGS-12345" → Pull that specific bug, run FILTER/MAP/ANALYZE on just that bug
- "what gaps exist for networking" → Query Neo4j for networking gap counts + search ChromaDB for networking scenarios
- "show me all hog scenarios" → Search ChromaDB/krkn docs for hog scenario plugins and list them
- "what components have the most open gaps" → Query Neo4j gap counts

## Step 1: DISCOVER

Pull recent bugs from JIRA (skip or narrow if in Targeted Query mode):

```
mcp__jira__searchJiraIssuesUsingJql with:
  cloudId: https://redhat.atlassian.net
  jql: project = OCPBUGS AND issuetype = Bug AND created >= -14d ORDER BY created DESC
  maxResults: 50
  fields: ["summary", "description", "status", "priority", "components", "created"]
  responseContentFormat: markdown
```

## Step 2: FILTER (Claude reasoning + ChromaDB)

For EACH bug, do these steps — don't batch, actually reason per bug:

**2a. Read the bug** — understand the summary and description.

**2b. Search OCP docs** for component context:
```bash
cd /Users/sahil/krkn-chaos-coordinator && PYTHONPATH=. ./venv/bin/python3 -c "
from src.knowledge.chromadb_store import ChromaStore
c = ChromaStore(persist_dir='./chroma_data')
for r in c.search_all('PUT_COMPONENT_AND_SUMMARY_HERE', n_results=3):
    print(r['text'][:300])
    print('---')
"
```

**2c. Decide** using this rule:
> If the bug involves a component behaving incorrectly during, after, or because of any disruption — it's chaos-relevant. Even if the symptom is in a different component.

**Chaos-relevant:** performance degradation, crash/restart, operator degraded, node failure, network disruption, resource exhaustion, service down, upgrade/rollback failure, recovery failure, scaling issues, intermittent failures, data corruption, certificate issues.

**NOT chaos-relevant:** CVEs, test infra, docs, backports, dependency bumps, stubs/clones.

Output:
```
PASS: OCPBUGS-XXXXX — [failure mode] (injection: [method])
SKIP: OCPBUGS-XXXXX — [reason]
```

## Step 3: MAP (Claude reads actual scenarios)

For each PASS bug, find existing krkn scenarios:

```bash
PYTHONPATH=. ./venv/bin/python3 -c "
from src.knowledge.chromadb_store import ChromaStore
c = ChromaStore(persist_dir='./chroma_data')
print('=== Matching scenarios ===')
for r in c.search_scenarios('PUT_COMPONENT_AND_SUMMARY_HERE', n_results=5):
    print(f'[dist={r[\"distance\"]:.3f}] {r[\"text\"][:200]}')
    print()
"
```

Then **READ the actual matched scenario YAML** if one exists:
```bash
cat /Users/sahil/krkn/scenarios/openshift/SCENARIO_FILE.yaml
```

Now reason:
- What does this scenario actually inject? (pod kill? node drain? network latency?)
- Does it cover the EXACT failure mode in the bug?
- Or does it test the same component but a different failure?

Decision:
- **FULL MATCH**: Scenario tests this exact failure → no action needed
- **PARTIAL MATCH**: Same component, different failure → extend it
- **NO MATCH**: Nothing covers this → new scenario needed

## Step 4: ANALYZE (Claude reasons about each gap)

For each gap (PARTIAL or NO MATCH), reason deeply:

**4a. What krkn plugins are available?**
```bash
PYTHONPATH=. ./venv/bin/python3 -c "
from src.knowledge.chromadb_store import ChromaStore
c = ChromaStore(persist_dir='./chroma_data')
for r in c.search_krkn_docs('PUT_FAILURE_MODE_HERE', n_results=3):
    print(r['text'][:300])
    print('---')
"
```

**4b. Check Neo4j for similar resolved bugs:**
```bash
PYTHONPATH=. ./venv/bin/python3 -c "
from src.knowledge.neo4j_store import Neo4jStore
n = Neo4jStore(); n.connect()
for s in n.get_similar_resolved_bugs('PUT_COMPONENT_NAME'):
    print(f'{s[\"bug_key\"]}: {s[\"summary\"][:60]} → {s[\"issue_url\"]}')
for g in n.get_component_gap_counts()[:5]:
    print(f'{g[\"component\"]}: {g[\"gaps\"]} gaps ({g[\"open_gaps\"]} open)')
n.close()
"
```

**4c. Score confidence** by actually reasoning:

| Question | If YES | If NO |
|----------|--------|-------|
| Can I explain the exact reproduction steps? | +20 | +0 |
| Is there an existing scenario to extend? | +25 | +0 |
| Do I understand HOW this fails from the OCP docs? | +20 | +0 |
| Is there a krkn plugin that injects this exact failure? | +15 | +0 |
| Does this match the agent's domain? | +10 | +0 |
| Have we solved a similar bug before? (Neo4j) | +10 | +0 |

**4d. For HIGH confidence gaps, generate SPECIFIC modifications:**

Don't say "extend pod_etcd.yml". Instead say:
```
Extend scenarios/openshift/etcd.yml:
- Add a new test case that deploys CPU hog pods on master nodes
  (use hog_scenarios plugin with cpu target 80%, duration 300s)
- While hog is running, check etcd operator status:
  oc get co/etcd -o jsonpath='{.status.conditions}'
- Assert: etcd should NOT report Degraded=True while members
  are actually healthy (etcdctl endpoint health shows true)
- This reproduces OCPBUGS-81323 where the 30s health check
  timeout is exceeded under API server load
```

## Step 5: ACT

Present each gap to the user:

```
Gap #1: [HIGH 85/100]
Bug: OCPBUGS-XXXXX — summary
Component: Etcd

What I found:
- OCP docs say: [relevant architecture context]
- Closest krkn scenario: [what it tests]
- This bug is different because: [what's NOT covered]

Recommendation:
- [specific changes needed]
- krkn plugin: [exact plugin name]
- Repos to update: krkn, krkn-hub, website

→ [Approve] [Reject]
```

When approved, create GitHub issue on `shahsahil264/krkn` with the full analysis.

## Step 6: REMEMBER

Store results in Neo4j:
```bash
PYTHONPATH=. ./venv/bin/python3 -c "
from src.knowledge.neo4j_store import Neo4jStore
from src.models import *
n = Neo4jStore(); n.connect()
# Results get stored via the pipeline
n.close()
"
```

## Key Principles

1. **READ before deciding** — don't pattern match, actually understand the bug
2. **SEARCH before recommending** — check what krkn already has, what OCP docs say
3. **BE SPECIFIC** — don't say "extend this scenario", say exactly what to change
4. **BE HONEST** — if you don't understand the component, say LOW confidence
5. **CHECK HISTORY** — Neo4j tells you what was solved before

"""Streamlit web dashboard for krkn-chaos-coordinator.

Aesthetic: Mission Control / Chaos Operations Center
Dark industrial theme with amber/red danger accents.
"""

import json
import sys
import datetime
from collections import Counter
from pathlib import Path

import streamlit as st
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.filter.chaos_filter import filter_bugs
from src.knowledge.chromadb_store import ChromaStore, DocChunk
from src.knowledge.scenario_index import index_scenarios_from_repo
from src.knowledge.component_map import AGENT_COMPONENTS, get_all_agents
from src.coordinator.orchestrator import deduplicate_gaps
from src.agents.act import build_issue_title, build_issue_body
from src.models import (
    ActionType, Bug, Confidence, FilterResult,
    GapAnalysis, MatchResult, ScenarioMatch,
)

HISTORY_FILE = Path("/tmp/krkn_coordinator_history.json")

# === CUSTOM CSS ===
CUSTOM_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&family=Outfit:wght@300;400;500;600;700;800;900&display=swap');

/* Root theme */
:root {
    --bg-primary: #0a0a0f;
    --bg-secondary: #12121a;
    --bg-card: #16161f;
    --bg-card-hover: #1c1c28;
    --border: #2a2a3a;
    --border-glow: #ff4a1c33;
    --text-primary: #e8e6e3;
    --text-secondary: #8a8a9a;
    --text-dim: #555566;
    --accent-red: #ff4a1c;
    --accent-amber: #ffaa00;
    --accent-green: #00ff88;
    --accent-cyan: #00d4ff;
    --accent-purple: #aa66ff;
    --danger: #ff2244;
    --glow-red: 0 0 20px rgba(255, 74, 28, 0.3);
    --glow-green: 0 0 20px rgba(0, 255, 136, 0.3);
    --glow-amber: 0 0 20px rgba(255, 170, 0, 0.3);
}

/* Global overrides */
.stApp {
    background-color: var(--bg-primary) !important;
    font-family: 'Outfit', sans-serif !important;
}

.stApp header { background-color: transparent !important; }

h1, h2, h3, h4, h5, h6 {
    font-family: 'JetBrains Mono', monospace !important;
    letter-spacing: -0.02em;
}

/* Sidebar */
section[data-testid="stSidebar"] {
    background-color: var(--bg-secondary) !important;
    border-right: 1px solid var(--border) !important;
}

section[data-testid="stSidebar"] .stMarkdown p,
section[data-testid="stSidebar"] .stMarkdown li {
    font-family: 'JetBrains Mono', monospace !important;
    font-size: 0.8rem;
}

/* Custom metric cards */
.metric-card {
    background: linear-gradient(135deg, var(--bg-card) 0%, var(--bg-secondary) 100%);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 20px;
    text-align: center;
    transition: all 0.3s ease;
    position: relative;
    overflow: hidden;
}

.metric-card::before {
    content: '';
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    height: 2px;
    background: linear-gradient(90deg, transparent, var(--accent-red), transparent);
    opacity: 0.6;
}

.metric-card:hover {
    border-color: var(--accent-red);
    box-shadow: var(--glow-red);
    transform: translateY(-2px);
}

.metric-value {
    font-family: 'JetBrains Mono', monospace;
    font-size: 2.4rem;
    font-weight: 700;
    line-height: 1;
    margin-bottom: 4px;
}

.metric-label {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.15em;
    color: var(--text-secondary);
}

.metric-red .metric-value { color: var(--accent-red); }
.metric-green .metric-value { color: var(--accent-green); }
.metric-amber .metric-value { color: var(--accent-amber); }
.metric-cyan .metric-value { color: var(--accent-cyan); }
.metric-purple .metric-value { color: var(--accent-purple); }
.metric-white .metric-value { color: var(--text-primary); }

/* Header */
.header-container {
    background: linear-gradient(135deg, #0f0f18 0%, #1a0a0a 50%, #0f0f18 100%);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 28px 36px;
    margin-bottom: 24px;
    position: relative;
    overflow: hidden;
}

.header-container::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 3px;
    background: linear-gradient(90deg, var(--accent-red), var(--accent-amber), var(--accent-red));
    animation: scan 3s ease-in-out infinite;
}

@keyframes scan {
    0%, 100% { opacity: 0.4; }
    50% { opacity: 1; }
}

.header-title {
    font-family: 'JetBrains Mono', monospace;
    font-size: 1.8rem;
    font-weight: 700;
    color: var(--text-primary);
    letter-spacing: -0.03em;
    margin: 0;
}

.header-title span {
    color: var(--accent-red);
}

.header-subtitle {
    font-family: 'Outfit', sans-serif;
    font-size: 0.9rem;
    color: var(--text-secondary);
    margin-top: 4px;
}

.header-badge {
    display: inline-block;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.2em;
    padding: 3px 10px;
    border: 1px solid var(--accent-red);
    border-radius: 3px;
    color: var(--accent-red);
    margin-left: 12px;
    vertical-align: middle;
}

/* Gap cards */
.gap-card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-left: 3px solid var(--accent-amber);
    border-radius: 6px;
    padding: 18px 22px;
    margin-bottom: 12px;
    transition: all 0.2s ease;
}

.gap-card:hover {
    border-color: var(--accent-amber);
    background: var(--bg-card-hover);
}

.gap-card.high { border-left-color: var(--accent-red); }
.gap-card.medium { border-left-color: var(--accent-amber); }
.gap-card.low { border-left-color: var(--text-dim); }

.gap-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;
}

.gap-bug-key {
    font-family: 'JetBrains Mono', monospace;
    font-weight: 600;
    font-size: 0.95rem;
    color: var(--accent-cyan);
}

.gap-score {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.75rem;
    padding: 2px 8px;
    border-radius: 3px;
}

.gap-score.high { background: rgba(255, 74, 28, 0.15); color: var(--accent-red); border: 1px solid var(--accent-red); }
.gap-score.medium { background: rgba(255, 170, 0, 0.15); color: var(--accent-amber); border: 1px solid var(--accent-amber); }
.gap-score.low { background: rgba(85, 85, 102, 0.15); color: var(--text-dim); border: 1px solid var(--text-dim); }

.gap-summary {
    font-family: 'Outfit', sans-serif;
    font-size: 0.85rem;
    color: var(--text-primary);
    margin-bottom: 6px;
}

.gap-meta {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.7rem;
    color: var(--text-secondary);
}

/* Status indicators */
.status-dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    margin-right: 6px;
    animation: pulse 2s ease-in-out infinite;
}

.status-dot.active { background: var(--accent-green); box-shadow: var(--glow-green); }
.status-dot.warning { background: var(--accent-amber); box-shadow: var(--glow-amber); }
.status-dot.danger { background: var(--accent-red); box-shadow: var(--glow-red); }
.status-dot.idle { background: var(--text-dim); }

@keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.5; }
}

/* Coverage heatmap cells */
.coverage-gap { background: rgba(255, 34, 68, 0.2) !important; color: var(--danger) !important; }
.coverage-covered { background: rgba(0, 255, 136, 0.15) !important; color: var(--accent-green) !important; }
.coverage-none { background: rgba(255, 170, 0, 0.15) !important; color: var(--accent-amber) !important; }
.coverage-idle { background: rgba(85, 85, 102, 0.1) !important; color: var(--text-dim) !important; }

/* Pipeline status bar */
.pipeline-bar {
    display: flex;
    gap: 2px;
    margin: 16px 0;
}

.pipeline-step {
    flex: 1;
    text-align: center;
    padding: 8px 4px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    border-radius: 3px;
    transition: all 0.3s ease;
}

.pipeline-step.done {
    background: rgba(0, 255, 136, 0.15);
    color: var(--accent-green);
    border: 1px solid rgba(0, 255, 136, 0.3);
}

.pipeline-step.active {
    background: rgba(255, 170, 0, 0.15);
    color: var(--accent-amber);
    border: 1px solid rgba(255, 170, 0, 0.3);
    animation: pulse 1.5s ease-in-out infinite;
}

.pipeline-step.pending {
    background: rgba(85, 85, 102, 0.1);
    color: var(--text-dim);
    border: 1px solid var(--border);
}

/* Section dividers */
.section-divider {
    border: none;
    height: 1px;
    background: linear-gradient(90deg, transparent, var(--border), transparent);
    margin: 24px 0;
}

/* Tab styling */
.stTabs [data-baseweb="tab-list"] {
    gap: 0;
    background: var(--bg-secondary);
    border-radius: 6px;
    padding: 2px;
}

.stTabs [data-baseweb="tab"] {
    font-family: 'JetBrains Mono', monospace !important;
    font-size: 0.75rem !important;
    text-transform: uppercase;
    letter-spacing: 0.1em;
}

/* Buttons */
.stButton > button {
    font-family: 'JetBrains Mono', monospace !important;
    font-size: 0.75rem !important;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    border-radius: 4px !important;
    transition: all 0.2s ease !important;
}

/* Injection method bars */
.injection-bar {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 12px 16px;
    margin-bottom: 8px;
}

.injection-bar-fill {
    height: 4px;
    border-radius: 2px;
    margin-top: 8px;
    transition: width 0.5s ease;
}
</style>
"""


def load_bugs_from_json(path: str) -> list[Bug]:
    with open(path) as f:
        data = json.load(f)
    bugs = []
    for issue in data.get("issues", {}).get("nodes", []):
        fields = issue["fields"]
        components = fields.get("components", [])
        comp_name = components[0]["name"] if components else "Unknown"
        desc = fields.get("description", "") or ""
        bugs.append(Bug(
            key=issue["key"], summary=fields.get("summary", ""),
            description=desc, component=comp_name,
            priority=fields.get("priority", {}).get("name", "Unknown"),
            status=fields.get("status", {}).get("name", "Unknown"),
            created=fields.get("created", ""),
            url=f"https://redhat.atlassian.net/browse/{issue['key']}",
        ))
    return bugs


def run_pipeline(bugs, krkn_path):
    relevant, skipped = filter_bugs(bugs)
    scenarios = index_scenarios_from_repo(Path(krkn_path))
    chroma = ChromaStore(persist_dir="/tmp/krkn_chroma_streamlit")
    chunks = [
        DocChunk(
            text=f"{s.scenario_type}: {s.file_path} ({s.description})",
            component=s.plugin_name, doc_type="scenario", source="krkn",
        )
        for s in scenarios
    ]
    chroma.add_scenario_docs(chunks)
    matched, unmatched = [], []
    for fr in relevant:
        bug = fr.bug
        query = f"{bug.component} {bug.summary}"
        chroma_results = chroma.search_scenarios(query, n_results=5)
        comp_lower = bug.component.lower()
        matching = [
            s for s in scenarios
            if comp_lower in s.name.lower()
            or comp_lower in s.scenario_type.lower()
            or any(kw in s.file_path.lower() for kw in comp_lower.split())
        ]
        if matching and chroma_results and chroma_results[0]["distance"] < 0.3:
            matched.append(ScenarioMatch(bug=bug, match_result=MatchResult.FULL_MATCH,
                matched_scenario=matching[0].file_path, matched_repo="krkn-chaos/krkn"))
        elif matching:
            unmatched.append(ScenarioMatch(bug=bug, match_result=MatchResult.PARTIAL_MATCH,
                matched_scenario=matching[0].file_path, matched_repo="krkn-chaos/krkn"))
        else:
            unmatched.append(ScenarioMatch(bug=bug, match_result=MatchResult.NO_MATCH))
    gaps = []
    for match in unmatched:
        bug = match.bug
        score, reasons = 0, []
        if bug.description and len(bug.description) > 200:
            score += 20; reasons.append("Clear repro (+20)")
        if match.match_result == MatchResult.PARTIAL_MATCH:
            score += 25; reasons.append(f"Partial: {match.matched_scenario} (+25)")
        if any(kw in bug.summary.lower() for kw in [
            "timeout", "crash", "unavailable", "degraded", "unhealthy", "not cleared", "failure", "failed",
        ]):
            score += 20; reasons.append("Known failure mode (+20)")
        score += 10; reasons.append("Domain match (+10)")
        confidence = Confidence.HIGH if score >= 70 else Confidence.MEDIUM if score >= 40 else Confidence.LOW
        action = ActionType.DRAFT_PR if score >= 70 else ActionType.GITHUB_ISSUE
        modifications = [f"Extend {match.matched_scenario}"] if match.matched_scenario else []
        gaps.append(GapAnalysis(bug=bug, confidence_score=score, confidence_level=confidence,
            action_type=action, reasoning="; ".join(reasons),
            base_scenario=match.matched_scenario, modifications=modifications))
    return relevant, skipped, matched, unmatched, gaps, scenarios


def save_run_history(bugs, relevant, skipped, gaps):
    history = []
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE) as f:
            history = json.load(f)
    history.append({
        "timestamp": datetime.datetime.now().isoformat(),
        "total_bugs": len(bugs),
        "relevant": len(relevant),
        "skipped": len(skipped),
        "gaps": len(gaps),
        "high": sum(1 for g in gaps if g.confidence_level == Confidence.HIGH),
        "medium": sum(1 for g in gaps if g.confidence_level == Confidence.MEDIUM),
        "low": sum(1 for g in gaps if g.confidence_level == Confidence.LOW),
    })
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def load_run_history():
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return []


def render_metric(value, label, color="white"):
    return f"""
    <div class="metric-card metric-{color}">
        <div class="metric-value">{value}</div>
        <div class="metric-label">{label}</div>
    </div>
    """


def render_gap_card(gap, index):
    level = gap.confidence_level.value
    action = "DRAFT PR" if gap.action_type == ActionType.DRAFT_PR else "ISSUE"
    return f"""
    <div class="gap-card {level}">
        <div class="gap-header">
            <span class="gap-bug-key">{gap.bug.key}</span>
            <span class="gap-score {level}">{level.upper()} {gap.confidence_score}/100 &rarr; {action}</span>
        </div>
        <div class="gap-summary">{gap.bug.summary[:90]}</div>
        <div class="gap-meta">
            Component: {gap.bug.component} &bull; Priority: {gap.bug.priority}
            {f' &bull; Base: {gap.base_scenario}' if gap.base_scenario else ''}
        </div>
    </div>
    """


def render_pipeline_bar(step="complete"):
    steps = ["discover", "filter", "map", "analyze", "act"]
    active_idx = steps.index(step) if step in steps else len(steps)
    html = '<div class="pipeline-bar">'
    for i, s in enumerate(steps):
        if step == "complete" or i < active_idx:
            cls = "done"
        elif i == active_idx:
            cls = "active"
        else:
            cls = "pending"
        html += f'<div class="pipeline-step {cls}">{s}</div>'
    html += '</div>'
    return html


# === APP ===

st.set_page_config(page_title="krkn-chaos-coordinator", page_icon="🔥", layout="wide")
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

# Header
st.markdown("""
<div class="header-container">
    <div class="header-title">krkn<span>-chaos-</span>coordinator<span class="header-badge">ops center</span></div>
    <div class="header-subtitle">Autonomous chaos test coverage expansion for OpenShift clusters</div>
</div>
""", unsafe_allow_html=True)

# Sidebar
with st.sidebar:
    st.markdown("### ⚙ Configuration")
    release = st.text_input("OCP Release", value="4.21")
    krkn_path = st.text_input("krkn repo", value="/Users/sahil/krkn")
    jira_path = st.text_input("JIRA data", value="tests/fixtures/jira_etcd_bugs.json")

    st.markdown('<hr class="section-divider">', unsafe_allow_html=True)

    st.markdown("### Domain Agents")
    for agent in get_all_agents():
        display = agent.replace("_", " ").title()
        components = AGENT_COMPONENTS.get(agent, [])
        status = "active" if agent == "control_plane" else "idle"
        dot = f'<span class="status-dot {status}"></span>'
        with st.expander(f"{display} ({len(components)})"):
            for c in components:
                st.markdown(f"`{c}`")

    st.markdown('<hr class="section-divider">', unsafe_allow_html=True)
    run_button = st.button("LAUNCH SCAN", type="primary", use_container_width=True)

# Main
if run_button or st.session_state.get("has_run"):
    st.session_state["has_run"] = True

    if not Path(jira_path).exists():
        st.error(f"Data file not found: {jira_path}")
        st.stop()

    bugs = load_bugs_from_json(jira_path)

    with st.status("Executing pipeline...", expanded=True) as status:
        st.write(f"`DISCOVER` Loading {len(bugs)} bugs...")
        relevant, skipped, matched, unmatched, gaps, scenarios = run_pipeline(bugs, krkn_path)
        st.write(f"`FILTER` {len(relevant)} relevant, {len(skipped)} filtered")
        st.write(f"`MAP` {len(scenarios)} scenarios indexed")
        st.write(f"`ANALYZE` {len(gaps)} gaps identified")
        status.update(label="Scan complete", state="complete")

    save_run_history(bugs, relevant, skipped, gaps)

    # Pipeline bar
    st.markdown(render_pipeline_bar("complete"), unsafe_allow_html=True)

    # Metrics
    prs = sum(1 for g in gaps if g.action_type == ActionType.DRAFT_PR)
    issues_count = sum(1 for g in gaps if g.action_type == ActionType.GITHUB_ISSUE)

    cols = st.columns(6)
    metrics = [
        (len(bugs), "Bugs Scanned", "white"),
        (len(relevant), "Chaos Relevant", "green"),
        (len(skipped), "Filtered Out", "amber"),
        (len(gaps), "Gaps Found", "red"),
        (prs, "Draft PRs", "purple"),
        (issues_count, "Issues", "cyan"),
    ]
    for col, (val, label, color) in zip(cols, metrics):
        col.markdown(render_metric(val, label, color), unsafe_allow_html=True)

    st.markdown('<hr class="section-divider">', unsafe_allow_html=True)

    # Tabs
    tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs([
        "APPROVAL QUEUE", "BUG INTEL", "FILTER ANALYSIS",
        "COVERAGE MAP", "INJECTION METHODS",
        "SCENARIO INDEX", "RUN HISTORY",
    ])

    # === TAB 1: APPROVAL QUEUE ===
    with tab1:
        if not gaps:
            st.markdown("""
            <div style="text-align: center; padding: 60px 0;">
                <div style="font-family: 'JetBrains Mono'; font-size: 3rem; color: var(--accent-green);">✓</div>
                <div style="font-family: 'JetBrains Mono'; font-size: 1.1rem; color: var(--accent-green); margin-top: 8px;">
                    ALL CLEAR — No coverage gaps detected
                </div>
            </div>
            """, unsafe_allow_html=True)
        else:
            for i, gap in enumerate(gaps):
                st.markdown(render_gap_card(gap, i), unsafe_allow_html=True)
                col_a, col_b, col_c = st.columns([1, 1, 6])
                col_a.button("APPROVE", key=f"approve_{i}", type="primary")
                col_b.button("REJECT", key=f"reject_{i}")

    # === TAB 2: BUG INTEL ===
    with tab2:
        all_results = list(relevant) + list(skipped)
        for fr in all_results:
            bug = fr.bug
            if fr.chaos_relevant:
                icon = "🔴"
                badge = f'<span style="color: var(--accent-red); font-family: JetBrains Mono; font-size: 0.7rem;">CHAOS RELEVANT</span>'
            else:
                icon = "⚫"
                badge = f'<span style="color: var(--text-dim); font-family: JetBrains Mono; font-size: 0.7rem;">FILTERED</span>'

            with st.expander(f"{icon} {bug.key}: {bug.summary[:65]}"):
                c1, c2, c3 = st.columns(3)
                c1.markdown(f"**Component:** `{bug.component}`")
                c2.markdown(f"**Priority:** `{bug.priority}`")
                c3.markdown(f"**Status:** `{bug.status}`")

                if fr.chaos_relevant:
                    st.markdown(f"""
                    <div style="background: rgba(255, 74, 28, 0.1); border: 1px solid rgba(255, 74, 28, 0.3);
                                border-radius: 6px; padding: 12px; margin: 8px 0; font-family: JetBrains Mono; font-size: 0.8rem;">
                        <strong style="color: var(--accent-red);">FAILURE MODE:</strong> {fr.failure_mode}<br>
                        <strong style="color: var(--accent-amber);">INJECTION:</strong> {fr.injection_method}
                    </div>
                    """, unsafe_allow_html=True)
                else:
                    st.warning(f"Skip: {fr.skip_reason}")

                desc = bug.description or "*No description*"
                st.markdown(desc[:2000] if len(desc) > 2000 else desc)
                st.markdown(f"[Open in JIRA →]({bug.url})")

    # === TAB 3: FILTER ANALYSIS ===
    with tab3:
        col_l, col_r = st.columns([1, 1])

        with col_l:
            st.markdown("#### Skip Reason Breakdown")
            skip_reasons = []
            for s in skipped:
                reason = s.skip_reason or "Unknown"
                if "CVE" in reason:
                    skip_reasons.append("CVE / Security Patch")
                elif "No chaos-relevant" in reason:
                    skip_reasons.append("No Failure Keywords")
                elif "injection" in reason.lower():
                    skip_reasons.append("No krkn Injection")
                else:
                    skip_reasons.append("Other")

            if skip_reasons:
                reason_counts = Counter(skip_reasons)
                df = pd.DataFrame({"Reason": list(reason_counts.keys()), "Count": list(reason_counts.values())})
                st.bar_chart(df.set_index("Reason"), color="#ff4a1c")

            st.markdown("#### Chaos Relevance Ratio")
            df_ratio = pd.DataFrame({"Status": ["Relevant", "Skipped"], "Count": [len(relevant), len(skipped)]})
            st.bar_chart(df_ratio.set_index("Status"), color="#00ff88")

        with col_r:
            st.markdown("#### Priority Distribution")
            priorities = [b.priority for b in bugs]
            prio_counts = Counter(priorities)
            df_prio = pd.DataFrame({"Priority": list(prio_counts.keys()), "Count": list(prio_counts.values())})
            st.bar_chart(df_prio.set_index("Priority"), color="#ffaa00")

            st.markdown("#### Component Distribution")
            components = [b.component for b in bugs]
            comp_counts = Counter(components)
            df_comp = pd.DataFrame({"Component": list(comp_counts.keys()), "Count": list(comp_counts.values())})
            st.bar_chart(df_comp.set_index("Component"), color="#00d4ff")

    # === TAB 4: COVERAGE MAP ===
    with tab4:
        st.markdown("#### Component Coverage Status")

        scenario_components = Counter()
        for s in scenarios:
            scenario_components[s.plugin_name] += 1
            for part in s.file_path.lower().split("/"):
                if part not in ("scenarios", "openshift", "kube", "kind", "kubevirt"):
                    scenario_components[part] += 1

        heatmap_data = []
        for agent_name in get_all_agents():
            components = AGENT_COMPONENTS.get(agent_name, [])
            display_name = agent_name.replace("_", " ").title()
            agent_bugs = [b for b in bugs if b.component in components]
            agent_gaps = [g for g in gaps if g.bug.component in components]

            agent_scenarios = 0
            for comp in components:
                for key, count in scenario_components.items():
                    if key in comp.lower() or comp.lower() in key:
                        agent_scenarios += count

            if agent_gaps:
                status = "🔴 GAP"
            elif agent_scenarios > 0:
                status = "🟢 COVERED"
            elif agent_bugs:
                status = "🟡 NO TESTS"
            else:
                status = "⚫ QUIET"

            heatmap_data.append({
                "Agent": display_name,
                "Components": len(components),
                "Bugs (14d)": len(agent_bugs),
                "Scenarios": agent_scenarios,
                "Gaps": len(agent_gaps),
                "Status": status,
            })

        st.dataframe(pd.DataFrame(heatmap_data), use_container_width=True, hide_index=True)

        st.markdown("""
        | Symbol | Meaning |
        |--------|---------|
        | 🔴 GAP | Has bugs that need new chaos tests |
        | 🟢 COVERED | Has chaos scenarios, no new gaps |
        | 🟡 NO TESTS | Has bugs but no matching scenarios |
        | ⚫ QUIET | No recent bugs in this area |
        """)

    # === TAB 5: INJECTION METHODS ===
    with tab5:
        if relevant:
            methods = [r.injection_method or "unknown" for r in relevant]
            method_counts = Counter(methods)

            plugin_map = {
                "node": ("Node failures — drain, reboot, shutdown, delete", ["node_actions", "shut_down"], "#ff4a1c"),
                "pod": ("Pod failures — kill, restart, eviction, OOM", ["pod_disruption", "application_outage"], "#aa66ff"),
                "network": ("Network chaos — partition, latency, DNS, packet loss", ["network_chaos", "network_chaos_ng"], "#00d4ff"),
                "resource_stress": ("Resource stress — CPU, memory, disk, I/O pressure", ["hogs"], "#ffaa00"),
                "cluster_state": ("Cluster state — operators, upgrades, CRDs, etcd", ["pod_disruption", "pvc"], "#00ff88"),
                "time_skew": ("Time skew — NTP drift, clock jumps, cert expiry", ["time_actions"], "#ff66aa"),
                "cloud_provider": ("Cloud provider — VM stop, volume detach, AZ outage", ["node_actions", "zone_outage"], "#ff8800"),
            }

            for method, count in sorted(method_counts.items(), key=lambda x: -x[1]):
                desc, plugins, color = plugin_map.get(method, (method, ["unknown"], "#888"))
                pct = int(count / len(relevant) * 100)

                st.markdown(f"""
                <div class="injection-bar">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <div>
                            <span style="font-family: JetBrains Mono; font-weight: 600; color: {color}; font-size: 0.9rem;">{method.upper()}</span>
                            <span style="font-family: JetBrains Mono; color: var(--text-dim); font-size: 0.75rem; margin-left: 8px;">{count} bug{'s' if count != 1 else ''}</span>
                        </div>
                        <span style="font-family: JetBrains Mono; font-size: 0.7rem; color: var(--text-secondary);">
                            Plugins: {', '.join(plugins)}
                        </span>
                    </div>
                    <div style="font-family: Outfit; font-size: 0.75rem; color: var(--text-secondary); margin-top: 4px;">{desc}</div>
                    <div class="injection-bar-fill" style="width: {pct}%; background: {color};"></div>
                </div>
                """, unsafe_allow_html=True)
        else:
            st.info("No chaos-relevant bugs — no injection methods needed.")

    # === TAB 6: SCENARIO INDEX ===
    with tab6:
        type_counts = Counter(s.scenario_type for s in scenarios)
        st.markdown(f"#### {len(scenarios)} Scenarios Indexed")

        df_types = pd.DataFrame(
            {"Type": list(type_counts.keys()), "Count": list(type_counts.values())}
        ).sort_values("Count", ascending=False)
        st.bar_chart(df_types.set_index("Type"), color="#aa66ff")

        st.markdown("#### Full Index")
        st.dataframe(
            [{"File": s.file_path, "Type": s.scenario_type, "Plugin": s.plugin_name, "Info": s.description or "—"}
             for s in scenarios],
            use_container_width=True,
        )

        st.markdown("#### Generated Issue Previews")
        for gap in gaps:
            title = build_issue_title(gap)
            body = build_issue_body(gap, "control_plane")
            with st.expander(f"{gap.bug.key}: {gap.bug.summary[:55]}"):
                st.code(title, language=None)
                st.markdown(body)

    # === TAB 7: RUN HISTORY ===
    with tab7:
        history = load_run_history()
        if not history:
            st.info("No previous runs recorded.")
        else:
            df_hist = pd.DataFrame(history)
            df_hist["timestamp"] = pd.to_datetime(df_hist["timestamp"])
            df_hist = df_hist.sort_values("timestamp", ascending=False)

            st.dataframe(
                df_hist[["timestamp", "total_bugs", "relevant", "skipped", "gaps", "high", "medium", "low"]],
                use_container_width=True, hide_index=True,
            )

            if len(df_hist) > 1:
                st.markdown("#### Gaps Over Time")
                st.line_chart(df_hist.set_index("timestamp")[["gaps", "relevant", "skipped"]])
                st.markdown("#### Confidence Distribution")
                st.area_chart(df_hist.set_index("timestamp")[["high", "medium", "low"]])

else:
    # Landing page
    st.markdown("""
    <div style="text-align: center; padding: 40px 0;">
        <div style="font-family: 'JetBrains Mono'; font-size: 4rem; color: var(--accent-red); line-height: 1; opacity: 0.8;">
            &#9760;
        </div>
        <div style="font-family: 'JetBrains Mono'; font-size: 1rem; color: var(--text-secondary); margin-top: 12px;">
            Press <span style="color: var(--accent-red); font-weight: 600;">LAUNCH SCAN</span> to begin
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("#### Agent Architecture")
    st.code("""
    Orchestrator
    ├── Upgrade & Lifecycle    (CVO, MCO, Installer)
    ├── Control Plane          (etcd, kube-apiserver, scheduler)
    ├── Node & Machine         (kubelet, Machine API, Cloud Compute)
    ├── Networking             (OVN-K, DNS, router, ingress)
    ├── Storage                (CSI, Image Registry)
    └── Operators & Platform   (OLM, Console, Auth, Monitoring)

    Pipeline: DISCOVER → FILTER → MAP → ANALYZE → ACT → REMEMBER
    """, language=None)

    st.markdown("#### Agent Coverage")
    agent_data = [
        {"Agent": a.replace("_", " ").title(), "Components": len(AGENT_COMPONENTS.get(a, [])),
         "Component List": ", ".join(AGENT_COMPONENTS.get(a, []))}
        for a in get_all_agents()
    ]
    st.dataframe(agent_data, use_container_width=True, hide_index=True)

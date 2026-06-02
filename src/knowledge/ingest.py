"""Ingest krkn and OCP documentation into ChromaDB from GitHub repos."""

import logging
import re
from pathlib import Path

import yaml

from src.apis.github_client import GitHubClient
from src.knowledge.chromadb_store import ChromaStore, DocChunk

logger = logging.getLogger(__name__)

# Repos to ingest
KRKN_REPOS = {
    "scenarios": {"owner": "krkn-chaos", "repo": "krkn", "path": "scenarios"},
    "website_docs": {"owner": "krkn-chaos", "repo": "website", "path": "content/en/docs"},
    "krkn_hub_docs": {"owner": "krkn-chaos", "repo": "krkn-hub", "path": "docs"},
    "krkn_plugins": {"owner": "krkn-chaos", "repo": "krkn", "path": "krkn/scenario_plugins"},
}

# Max chunk size for ChromaDB (chars)
MAX_CHUNK_SIZE = 1500


def _clean_markdown(text: str) -> str:
    """Strip Hugo shortcodes, frontmatter, and excessive whitespace."""
    # Remove YAML frontmatter
    text = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.DOTALL)
    # Remove Hugo shortcodes like {{< tab >}}, {{% alert %}}
    text = re.sub(r"\{\{[<%].*?[%>]\}\}", "", text)
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    # Collapse whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _infer_component(path: str, content: str) -> str:
    """Infer the OCP component from file path and content.

    Maps to agent domain names to enable filtered searches like:
    chroma.search(query, component="networking")
    """
    text = f"{path} {content[:800]}".lower()

    # Ordered from most specific to least specific
    rules: list[tuple[str, list[str]]] = [
        # Control plane
        ("etcd", ["etcd", "raft", "quorum"]),
        ("kube-apiserver", ["kube-apiserver", "apiserver", "api server", "api-server"]),
        ("kube-scheduler", ["kube-scheduler", "scheduler"]),
        ("kube-controller-manager", ["kube-controller-manager", "controller-manager"]),
        ("hypershift", ["hypershift", "hosted control plane", "hosted-control-plane"]),
        ("oauth", ["oauth", "openid", "oidc", "authentication"]),

        # Networking
        ("ovn-kubernetes", ["ovn-kubernetes", "ovn", "ovnkubernetes"]),
        ("dns", ["coredns", "dns-operator", "cluster-dns"]),
        ("ingress", ["ingress", "router", "haproxy", "route"]),
        ("ptp", ["/ptp", "precision time", "linuxptp"]),
        ("sriov", ["sr-iov", "sriov", "single root"]),
        ("multus", ["multus"]),
        ("metallb", ["metallb", "metal lb", "metal-lb"]),
        ("network-policy", ["network policy", "network_policy", "networkpolicy"]),
        ("sdn", ["openshift-sdn", "sdn"]),

        # Node / Machine
        ("kubelet", ["kubelet"]),
        ("crio", ["cri-o", "crio"]),
        ("machine-api", ["machine api", "machine-api", "machineapi"]),
        ("mco", ["machine config", "machineconfig", "mco"]),
        ("baremetal", ["baremetal", "bare metal", "ironic", "ipmi", "bmc", "redfish"]),
        ("node-tuning", ["node tuning", "tuned", "performance profile", "numa", "hugepage"]),
        ("autoscaler", ["autoscaler", "cluster-autoscaler", "machine-autoscaler"]),

        # Storage
        ("csi", ["csi driver", "csi-driver", "volume", "persistent volume", "pvc", "pv "]),
        ("registry", ["image registry", "registry", "imageregistry"]),
        ("storage", ["storage", "ocs", "odf", "lvm", "lvms"]),

        # Upgrade / Install
        ("cvo", ["cluster version operator", "cvo", "clusterversion"]),
        ("installer", ["installer", "install-config", "ipi ", "upi "]),
        ("upgrade", ["upgrade", "update", "rollback", "channel"]),

        # Operators / Platform
        ("olm", ["operator lifecycle", "olm", "catalogsource", "subscription"]),
        ("console", ["console", "management console", "web console"]),
        ("monitoring", ["monitoring", "prometheus", "alertmanager", "thanos"]),
        ("logging", ["logging", "fluentd", "elasticsearch", "loki"]),
        ("insights", ["insights"]),
        ("credentials", ["cloud credential", "credentialsrequest"]),

        # krkn-specific
        ("pod", ["pod-scenario", "pod_scenario", "pod_disruption", "pod disruption"]),
        ("network-chaos", ["network-chaos", "network_chaos"]),
        ("cpu_hog", ["cpu-hog", "cpu_hog"]),
        ("memory_hog", ["memory-hog", "memory_hog"]),
        ("io_hog", ["io-hog", "io_hog"]),
        ("zone_outage", ["zone-outage", "zone_outage"]),
        ("application", ["application-outage", "application_outage"]),
        ("service", ["service-disruption", "service_disruption", "service-hijacking"]),
        ("time", ["time-scenario", "time_action", "clock skew", "ntp"]),
        ("power", ["power-outage", "shut_down", "shutdown"]),
        ("syn_flood", ["syn-flood", "syn_flood"]),
        ("http_load", ["http-load", "http_load", "vegeta"]),
        ("kubevirt", ["kubevirt"]),
        ("container", ["container_scenario", "container-scenario"]),
    ]

    for component, keywords in rules:
        for kw in keywords:
            if kw in text:
                return component

    return "general"


def _chunk_text(text: str, max_size: int = MAX_CHUNK_SIZE) -> list[str]:
    """Split text into chunks, preferring paragraph boundaries."""
    if len(text) <= max_size:
        return [text]

    chunks = []
    paragraphs = text.split("\n\n")
    current_chunk = ""

    for para in paragraphs:
        if len(current_chunk) + len(para) + 2 > max_size:
            if current_chunk:
                chunks.append(current_chunk.strip())
            # If single paragraph is too big, split by lines
            if len(para) > max_size:
                lines = para.split("\n")
                current_chunk = ""
                for line in lines:
                    if len(current_chunk) + len(line) + 1 > max_size:
                        if current_chunk:
                            chunks.append(current_chunk.strip())
                        current_chunk = line
                    else:
                        current_chunk += "\n" + line if current_chunk else line
            else:
                current_chunk = para
        else:
            current_chunk += "\n\n" + para if current_chunk else para

    if current_chunk.strip():
        chunks.append(current_chunk.strip())

    return chunks


def _list_files_recursive(github: GitHubClient, owner: str, repo: str, path: str,
                          extensions: tuple = (".md", ".yaml", ".yml")) -> list[dict]:
    """List all files recursively from a GitHub repo path."""
    items = github.list_scenario_files(owner, repo, path)
    # list_scenario_files only returns .yaml/.yml, we need .md too
    # Let's use the raw contents API
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    try:
        response = github._session.get(url, timeout=30)
        response.raise_for_status()
        contents = response.json()
    except Exception as e:
        logger.error("Failed to list %s/%s/%s: %s", owner, repo, path, e)
        return []

    if not isinstance(contents, list):
        return []

    files = []
    for item in contents:
        if item["type"] == "dir":
            files.extend(_list_files_recursive(github, owner, repo, item["path"], extensions))
        elif any(item["name"].endswith(ext) for ext in extensions):
            files.append({"name": item["name"], "path": item["path"], "url": item.get("html_url", "")})

    return files


def ingest_scenario_yamls(github: GitHubClient, chroma: ChromaStore) -> int:
    """Ingest krkn scenario YAML files from GitHub."""
    owner, repo = "krkn-chaos", "krkn"
    files = _list_files_recursive(github, owner, repo, "scenarios", extensions=(".yaml", ".yml"))
    logger.info("Found %d scenario YAML files to ingest", len(files))

    chunks = []
    for f in files:
        content = github.get_file_content(owner, repo, f["path"])
        if not content:
            continue

        # Parse YAML to extract scenario details
        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError:
            data = None

        component = _infer_component(f["path"], content)

        # Create a rich text description
        text = f"Scenario file: {f['path']}\n\n"
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    for scenario_type, config in item.items():
                        text += f"Scenario type: {scenario_type}\n"
                        if isinstance(config, dict):
                            text += f"Configuration:\n{yaml.dump(config, default_flow_style=False)}\n"
        elif isinstance(data, dict):
            text += f"Configuration:\n{yaml.dump(data, default_flow_style=False)}\n"
        else:
            text += content

        for chunk in _chunk_text(text):
            chunks.append(DocChunk(
                text=chunk,
                component=component,
                doc_type="scenario",
                source="krkn-chaos/krkn",
                version="",
            ))

    chroma.add_scenario_docs(chunks)
    logger.info("Ingested %d scenario chunks", len(chunks))
    return len(chunks)


def ingest_website_docs(github: GitHubClient, chroma: ChromaStore) -> int:
    """Ingest krkn-chaos.dev website documentation from GitHub."""
    owner, repo = "krkn-chaos", "website"
    files = _list_files_recursive(github, owner, repo, "content/en/docs", extensions=(".md",))
    logger.info("Found %d website doc files to ingest", len(files))

    chunks = []
    for f in files:
        content = github.get_file_content(owner, repo, f["path"])
        if not content:
            continue

        cleaned = _clean_markdown(content)
        if len(cleaned) < 20:
            continue

        component = _infer_component(f["path"], cleaned)

        # Prefix with file context
        text = f"Source: krkn-chaos.dev docs — {f['path']}\n\n{cleaned}"

        for chunk in _chunk_text(text):
            chunks.append(DocChunk(
                text=chunk,
                component=component,
                doc_type="documentation",
                source="krkn-chaos/website",
                version="",
            ))

    chroma.add_krkn_docs(chunks)
    logger.info("Ingested %d website doc chunks", len(chunks))
    return len(chunks)


def ingest_krkn_hub_docs(github: GitHubClient, chroma: ChromaStore) -> int:
    """Ingest krkn-hub scenario documentation from GitHub."""
    owner, repo = "krkn-chaos", "krkn-hub"
    files = _list_files_recursive(github, owner, repo, "docs", extensions=(".md",))
    logger.info("Found %d krkn-hub doc files to ingest", len(files))

    chunks = []
    for f in files:
        content = github.get_file_content(owner, repo, f["path"])
        if not content:
            continue

        cleaned = _clean_markdown(content)
        if len(cleaned) < 20:
            continue

        component = _infer_component(f["path"], cleaned)
        text = f"Source: krkn-hub docs — {f['path']}\n\n{cleaned}"

        for chunk in _chunk_text(text):
            chunks.append(DocChunk(
                text=chunk,
                component=component,
                doc_type="krkn-hub",
                source="krkn-chaos/krkn-hub",
                version="",
            ))

    chroma.add_krkn_docs(chunks)
    logger.info("Ingested %d krkn-hub doc chunks", len(chunks))
    return len(chunks)


def ingest_plugin_code(github: GitHubClient, chroma: ChromaStore) -> int:
    """Ingest krkn plugin Python code docstrings and get_scenario_types."""
    owner, repo = "krkn-chaos", "krkn"
    files = _list_files_recursive(
        github, owner, repo, "krkn/scenario_plugins", extensions=(".py",)
    )
    # Only plugin files, not __init__ or tests
    plugin_files = [f for f in files if f["name"].endswith("_scenario_plugin.py")]
    logger.info("Found %d plugin files to ingest", len(plugin_files))

    chunks = []
    for f in plugin_files:
        content = github.get_file_content(owner, repo, f["path"])
        if not content:
            continue

        component = _infer_component(f["path"], content)

        # Extract class docstrings and get_scenario_types
        text = f"Plugin: {f['path']}\n\n"

        # Extract class name and docstring
        class_match = re.search(
            r'class\s+(\w+ScenarioPlugin).*?:\s*\n\s*"""(.*?)"""',
            content, re.DOTALL
        )
        if class_match:
            text += f"Class: {class_match.group(1)}\n"
            text += f"Description: {class_match.group(2).strip()}\n\n"

        # Extract get_scenario_types return value
        types_match = re.search(
            r'def\s+get_scenario_types\s*\(self\).*?return\s+(\[.*?\])',
            content, re.DOTALL
        )
        if types_match:
            text += f"Scenario types: {types_match.group(1)}\n\n"

        # Extract run method signature and docstring
        run_match = re.search(
            r'def\s+run\s*\(self.*?\).*?:\s*\n\s*"""(.*?)"""',
            content, re.DOTALL
        )
        if run_match:
            text += f"Run method: {run_match.group(1).strip()}\n"

        for chunk in _chunk_text(text):
            chunks.append(DocChunk(
                text=chunk,
                component=component,
                doc_type="plugin",
                source="krkn-chaos/krkn",
                version="",
            ))

    chroma.add_scenario_docs(chunks)
    logger.info("Ingested %d plugin chunks", len(chunks))
    return len(chunks)


def _clean_html(html: str) -> str:
    """Strip HTML tags and extract text content from Sphinx docs."""
    import re
    # Remove script/style blocks
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Remove navigation, sidebar, footer
    html = re.sub(r'<div[^>]*class="[^"]*(?:sidebar|nav|footer|header|search)[^"]*"[^>]*>.*?</div>', "", html, flags=re.DOTALL | re.IGNORECASE)
    # Replace block elements with newlines
    html = re.sub(r"<(?:p|div|br|h[1-6]|li|tr|dt|dd)[^>]*>", "\n", html, flags=re.IGNORECASE)
    # Remove all remaining tags
    html = re.sub(r"<[^>]+>", " ", html)
    # Decode HTML entities
    html = html.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&nbsp;", " ")
    # Collapse whitespace
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()


def ingest_krkn_lib_docs(github: GitHubClient, chroma: ChromaStore) -> int:
    """Ingest krkn-lib API documentation from krkn-lib-docs repo (Sphinx HTML)."""
    owner, repo = "krkn-chaos", "krkn-lib-docs"

    # Only ingest core API docs, skip tests and duplicates
    core_docs = [
        "k8s.krkn_kubernetes.html",
        "ocp.krkn_openshift.html",
        "models.k8s.models.html",
        "models.krkn.models.html",
        "utils.functions.html",
        "prometheus.krkn_prometheus.html",
        "telemetry.k8s.krkn_telemetry_kubernetes.html",
        "telemetry.ocp.krkn_telemetry_openshift.html",
        "elastic.krkn_elastic.html",
        "modules.html",
    ]

    logger.info("Ingesting %d krkn-lib doc files", len(core_docs))

    chunks = []
    for filename in core_docs:
        content = github.get_file_content(owner, repo, filename)
        if not content:
            logger.warning("Failed to fetch %s", filename)
            continue

        cleaned = _clean_html(content)
        if len(cleaned) < 50:
            continue

        # Determine component from filename
        component = "general"
        if "kubernetes" in filename:
            component = "kubernetes"
        elif "openshift" in filename:
            component = "openshift"
        elif "elastic" in filename:
            component = "telemetry"
        elif "prometheus" in filename:
            component = "monitoring"
        elif "models" in filename:
            component = "models"

        text = f"Source: krkn-lib API docs — {filename}\n\n{cleaned}"

        for chunk in _chunk_text(text):
            chunks.append(DocChunk(
                text=chunk,
                component=component,
                doc_type="api-reference",
                source="krkn-chaos/krkn-lib-docs",
                version="",
            ))

    chroma.add_krkn_docs(chunks)
    logger.info("Ingested %d krkn-lib doc chunks", len(chunks))
    return len(chunks)


OCP_SKIP_KEYWORDS = [
    "rosa-", "osd-", "microshift", "windows-node", "windows-container",
    "azure-stack", "alibaba", "ibm-power", "ibm-z-",
    "s2i-", "source-to-image", "jenkins", "gitops", "argo", "tekton", "pipeline",
    "serverless", "knative", "service-mesh", "istio", "kiali",
    "adding-tab", "customizing-web-console",
    "compliance-", "file-integrity",
    "virt-", "virtual-machine-", "vm-dsk", "vm-console",
    "metering-", "cost-",
    "mirror-", "mirroring-", "disconnected-",
    "installing-", "install-config-", "upi-", "ipi-",
    "bare-metal-ipi", "nutanix-", "vsphere-install", "aws-install",
    "build-strategy", "buildconfig", "image-stream",
    "gpu-", "fpga-", "dpdk-", "rdma-",
    "ztp-", "sandboxed-", "migration-",
    "cnf-image-based-upgrade",
    "accepting-", "cloud-experts-", "abi-",
    "accessing-windows",
    "about-redhat-openshift-gitops", "about-ztp", "about-jobset",
    "about-insights-advisor", "about-oadp",
]

OCP_TOPIC_DIRS = [
    "architecture", "etcd", "networking", "nodes", "storage",
    "operators", "updating", "upgrading", "authentication",
    "observability", "backup_and_restore", "machine_configuration",
    "machine_management", "scalability_and_performance",
    "security", "registry", "post_installation_configuration",
]


def _clean_asciidoc(text: str) -> str:
    """Strip AsciiDoc formatting to plain text."""
    # Remove AsciiDoc attributes
    text = re.sub(r"^:.*?:\s*.*$", "", text, flags=re.MULTILINE)
    # Remove comments
    text = re.sub(r"^//.*$", "", text, flags=re.MULTILINE)
    # Remove includes
    text = re.sub(r"^include::.*$", "", text, flags=re.MULTILINE)
    # Remove block delimiters
    text = re.sub(r"^[=\-\.]{4,}$", "", text, flags=re.MULTILINE)
    # Remove image references
    text = re.sub(r"image::.*?\[.*?\]", "", text)
    # Remove anchor IDs
    text = re.sub(r"\[id=[\"'].*?[\"']\]", "", text)
    # Simplify section headers
    text = re.sub(r"^=+\s+", "# ", text, flags=re.MULTILINE)
    # Remove admonition blocks (NOTE, TIP, WARNING, etc.)
    text = re.sub(r"^\[(?:NOTE|TIP|WARNING|IMPORTANT|CAUTION)\]\n====\n.*?\n====", "", text, flags=re.MULTILINE | re.DOTALL)
    # Remove inline formatting
    text = re.sub(r"\{product-title\}", "OpenShift", text)
    text = re.sub(r"\{nbsp\}", " ", text)
    # Collapse whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def ingest_ocp_modules(github: GitHubClient, chroma: ChromaStore) -> int:
    """Ingest OpenShift docs modules from openshift/openshift-docs."""
    owner, repo = "openshift", "openshift-docs"

    # List all modules
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/modules"
    try:
        resp = github._session.get(url, timeout=30)
        resp.raise_for_status()
        all_files = resp.json()
    except Exception as e:
        logger.error("Failed to list OCP modules: %s", e)
        return 0

    adoc_files = [f for f in all_files if f["name"].endswith(".adoc")]

    # Filter out irrelevant files
    relevant = []
    for f in adoc_files:
        name_lower = f["name"].lower()
        if not any(kw in name_lower for kw in OCP_SKIP_KEYWORDS):
            relevant.append(f)

    logger.info("OCP modules: %d total, %d relevant (skipped %d)",
                len(adoc_files), len(relevant), len(adoc_files) - len(relevant))

    chunks = []
    for i, f in enumerate(relevant):
        if i % 50 == 0:
            logger.info("  Processing module %d/%d...", i, len(relevant))

        content = github.get_file_content(owner, repo, f["path"])
        if not content:
            continue

        cleaned = _clean_asciidoc(content)
        if len(cleaned) < 30:
            continue

        component = _infer_component(f["name"], cleaned)
        text = f"Source: OpenShift docs — {f['name']}\n\n{cleaned}"

        for chunk in _chunk_text(text):
            chunks.append(DocChunk(
                text=chunk,
                component=component,
                doc_type="ocp-docs",
                source="openshift/openshift-docs",
                version="",
            ))

    chroma.add_ocp_docs(chunks)
    logger.info("Ingested %d OCP module chunks", len(chunks))
    return len(chunks)


def ingest_ocp_topics(github: GitHubClient, chroma: ChromaStore) -> int:
    """Ingest OCP topic assembly files (architecture, etcd, networking, etc.)."""
    owner, repo = "openshift", "openshift-docs"

    chunks = []
    for topic_dir in OCP_TOPIC_DIRS:
        files = _list_files_recursive(github, owner, repo, topic_dir, extensions=(".adoc",))
        logger.info("  Topic %s: %d files", topic_dir, len(files))

        for f in files:
            content = github.get_file_content(owner, repo, f["path"])
            if not content:
                continue

            cleaned = _clean_asciidoc(content)
            if len(cleaned) < 30:
                continue

            component = _infer_component(f["path"], cleaned)
            text = f"Source: OpenShift docs — {f['path']}\n\n{cleaned}"

            for chunk in _chunk_text(text):
                chunks.append(DocChunk(
                    text=chunk,
                    component=component,
                    doc_type="ocp-docs",
                    source="openshift/openshift-docs",
                    version="",
                ))

    chroma.add_ocp_docs(chunks)
    logger.info("Ingested %d OCP topic chunks", len(chunks))
    return len(chunks)


def ingest_krkn_claude_md(github: GitHubClient, chroma: ChromaStore) -> int:
    """Ingest krkn CLAUDE.md (plugin creation guide)."""
    content = github.get_file_content("krkn-chaos", "krkn", "CLAUDE.md")
    if not content:
        return 0

    chunks = []
    for chunk in _chunk_text(f"Source: krkn CLAUDE.md — Plugin creation guide\n\n{content}"):
        chunks.append(DocChunk(
            text=chunk, component="general",
            doc_type="guide", source="krkn-chaos/krkn", version="",
        ))

    chroma.add_krkn_docs(chunks)
    logger.info("Ingested %d CLAUDE.md chunks", len(chunks))
    return len(chunks)


def ingest_agent_docs(github: GitHubClient, chroma: ChromaStore) -> int:
    """Ingest domain-specific docs defined in agent YAML configs.

    Reads the `docs` field from each config/agents/*.yaml and ingests
    those GitHub paths into ChromaDB tagged with the agent's domain.
    """
    from src.agents.registry import discover_agents

    total_chunks = 0
    agents = discover_agents()

    for agent_name, config in agents.items():
        if not config.docs:
            continue

        logger.info("Ingesting docs for agent '%s' (%d sources)", agent_name, len(config.docs))

        for doc_source in config.docs:
            owner = doc_source["owner"]
            repo = doc_source["repo"]
            path = doc_source["path"]

            files = _list_files_recursive(
                github, owner, repo, path,
                extensions=(".md", ".adoc", ".yaml", ".yml", ".rst"),
            )
            logger.info("  %s/%s/%s: %d files", owner, repo, path, len(files))

            chunks = []
            for f in files:
                content = github.get_file_content(owner, repo, f["path"])
                if not content:
                    continue

                if f["name"].endswith(".adoc"):
                    cleaned = _clean_asciidoc(content)
                elif f["name"].endswith((".md", ".rst")):
                    cleaned = _clean_markdown(content)
                else:
                    cleaned = content

                if len(cleaned) < 30:
                    continue

                text = f"Source: {owner}/{repo} — {f['path']}\n\n{cleaned}"

                for chunk in _chunk_text(text):
                    chunks.append(DocChunk(
                        text=chunk,
                        component=agent_name,
                        doc_type="agent-docs",
                        source=f"{owner}/{repo}",
                        version="",
                    ))

            if chunks:
                chroma.add_ocp_docs(chunks)
                total_chunks += len(chunks)
                logger.info("  Ingested %d chunks for %s", len(chunks), agent_name)

    logger.info("Agent docs ingestion: %d total chunks", total_chunks)
    return total_chunks


def run_full_ingestion(github_token: str, chroma_dir: str = "./chroma_data") -> dict:
    """Run full ingestion pipeline — pull all docs from GitHub, ingest into ChromaDB."""
    github = GitHubClient(token=github_token)
    chroma = ChromaStore(persist_dir=chroma_dir)

    logger.info("Starting full ingestion from GitHub...")

    results = {}

    # krkn ecosystem
    results["scenario_yamls"] = ingest_scenario_yamls(github, chroma)
    results["website_docs"] = ingest_website_docs(github, chroma)
    results["krkn_hub_docs"] = ingest_krkn_hub_docs(github, chroma)
    results["plugin_code"] = ingest_plugin_code(github, chroma)
    results["krkn_lib_docs"] = ingest_krkn_lib_docs(github, chroma)
    results["krkn_claude_md"] = ingest_krkn_claude_md(github, chroma)

    # OpenShift docs
    results["ocp_modules"] = ingest_ocp_modules(github, chroma)
    results["ocp_topics"] = ingest_ocp_topics(github, chroma)

    # Agent-specific docs (from config/agents/*.yaml docs field)
    results["agent_docs"] = ingest_agent_docs(github, chroma)

    results["total"] = sum(results.values())

    logger.info("Ingestion complete: %s", results)
    return results


if __name__ == "__main__":
    import json
    import os
    import sys
    from pathlib import Path

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    # Load GitHub token
    token = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "")
    if not token:
        cursor_cfg = Path.home() / ".cursor" / "mcp.json"
        if cursor_cfg.exists():
            with open(cursor_cfg) as f:
                cfg = json.load(f)
            token = cfg.get("mcpServers", {}).get("github", {}).get("env", {}).get(
                "GITHUB_PERSONAL_ACCESS_TOKEN", ""
            )

    if not token:
        print("ERROR: Set GITHUB_PERSONAL_ACCESS_TOKEN or configure in ~/.cursor/mcp.json")
        sys.exit(1)

    chroma_dir = sys.argv[1] if len(sys.argv) > 1 else "./chroma_data"
    results = run_full_ingestion(token, chroma_dir)
    print(f"\nIngestion results: {json.dumps(results, indent=2)}")

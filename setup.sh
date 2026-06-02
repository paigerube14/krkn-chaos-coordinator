#!/usr/bin/env bash
set -euo pipefail

# krkn-chaos-coordinator setup script
# Usage: ./setup.sh

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}  ✓${NC} $1"; }
warn()  { echo -e "${YELLOW}  !${NC} $1"; }
fail()  { echo -e "${RED}  ✗${NC} $1"; exit 1; }
ask()   { echo -en "${BOLD}  → $1${NC} "; }

echo ""
echo -e "${BOLD}============================================${NC}"
echo -e "${BOLD}  krkn-chaos-coordinator — Setup${NC}"
echo -e "${BOLD}============================================${NC}"
echo ""

# ── Step 1: Check Python ──────────────────────────────────────

echo -e "${CYAN}Step 1/7:${NC} Checking Python..."

PYTHON=""
for candidate in python3.11 python3.12 python3.13 python3; do
    if command -v "$candidate" &>/dev/null; then
        major=$("$candidate" -c "import sys; print(sys.version_info.major)")
        minor=$("$candidate" -c "import sys; print(sys.version_info.minor)")
        if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
            PYTHON="$candidate"
            ok "Found $candidate ($major.$minor)"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    fail "Python 3.11+ is required."
    echo "  Install with:"
    echo "    macOS:  brew install python@3.11"
    echo "    Linux:  sudo dnf install python3.11  (or apt install python3.11)"
    exit 1
fi

# ── Step 2: Create virtual environment ────────────────────────

echo ""
echo -e "${CYAN}Step 2/7:${NC} Setting up Python environment..."

if [ -d "venv" ]; then
    warn "venv/ already exists — reusing"
else
    "$PYTHON" -m venv venv
    ok "Created virtual environment"
fi

source venv/bin/activate
ok "Activated venv ($(python --version 2>&1))"

pip install --quiet --upgrade pip
pip install --quiet -e ".[dev]"
ok "Installed all dependencies"

# ── Step 3: Clone krkn repo ──────────────────────────────────

echo ""
echo -e "${CYAN}Step 3/7:${NC} Checking krkn repo..."

KRKN_PATH="${KRKN_REPO_PATH:-$HOME/krkn}"

if [ -d "$KRKN_PATH/scenarios" ]; then
    ok "krkn repo found at $KRKN_PATH"
else
    info "The krkn repo is required — it contains chaos scenario YAMLs that the"
    info "MAP phase uses to check if coverage already exists."
    echo ""
    ask "Clone krkn to $KRKN_PATH? [Y/n]"
    read -r answer
    if [[ "$answer" =~ ^[Nn] ]]; then
        ask "Enter path to your existing krkn clone:"
        read -r KRKN_PATH
        if [ ! -d "$KRKN_PATH/scenarios" ]; then
            fail "$KRKN_PATH/scenarios not found. Clone krkn first: git clone https://github.com/krkn-chaos/krkn.git"
        fi
    else
        git clone --quiet https://github.com/krkn-chaos/krkn.git "$KRKN_PATH"
        ok "Cloned krkn to $KRKN_PATH"
    fi
fi

# ── Step 4: Configure credentials ─────────────────────────────

echo ""
echo -e "${CYAN}Step 4/7:${NC} Configuring credentials..."

NEEDS_EDIT=false

if [ -f ".env" ]; then
    # Check if it still has placeholder values
    if grep -q "your-jira-api-token\|your-email@redhat.com\|your-github-pat\|your-neo4j-password\|your-github-token" .env 2>/dev/null; then
        warn ".env exists but has placeholder values — let's fill them in"
        NEEDS_EDIT=true
    else
        ok ".env already configured"
    fi
else
    cp .env.example .env
    ok "Created .env from template"
    NEEDS_EDIT=true
fi

if [ "$NEEDS_EDIT" = true ]; then
    echo ""
    echo -e "  ${BOLD}You need 3 credentials to run the project:${NC}"
    echo ""

    # JIRA
    echo -e "  ${BOLD}1. JIRA API Token${NC}"
    echo "     Go to: https://id.atlassian.com/manage-profile/security/api-tokens"
    echo "     Click 'Create API token', name it anything, copy the token."
    echo ""
    ask "Enter your Red Hat email (JIRA username):"
    read -r jira_user
    if [ -n "$jira_user" ]; then
        sed -i.bak "s|JIRA_USERNAME=.*|JIRA_USERNAME=$jira_user|" .env
    fi

    ask "Paste your JIRA API token (or press Enter to skip):"
    read -r jira_token
    if [ -n "$jira_token" ]; then
        sed -i.bak "s|JIRA_API_TOKEN=.*|JIRA_API_TOKEN=$jira_token|" .env
    else
        warn "Skipped — you'll need to add JIRA_API_TOKEN to .env manually"
    fi

    # GitHub
    echo ""
    echo -e "  ${BOLD}2. GitHub Personal Access Token${NC}"
    echo "     Go to: https://github.com/settings/tokens"
    echo "     Click 'Generate new token (classic)', select 'repo' scope, copy the token."
    echo ""
    ask "Paste your GitHub token (or press Enter to skip):"
    read -r gh_token
    if [ -n "$gh_token" ]; then
        sed -i.bak "s|GITHUB_TOKEN=.*|GITHUB_TOKEN=$gh_token|" .env
    else
        warn "Skipped — you'll need to add GITHUB_TOKEN to .env manually"
    fi

    # Neo4j password
    echo ""
    echo -e "  ${BOLD}3. Neo4j Password${NC}"
    echo "     This is the password for the local Neo4j database."
    echo "     Default is 'password' — fine for local development."
    echo ""
    ask "Neo4j password [password]:"
    read -r neo4j_pw
    neo4j_pw="${neo4j_pw:-password}"
    sed -i.bak "s|NEO4J_PASSWORD=.*|NEO4J_PASSWORD=$neo4j_pw|" .env

    # krkn repo path
    sed -i.bak "s|KRKN_REPO_PATH=.*|KRKN_REPO_PATH=$KRKN_PATH|" .env 2>/dev/null || true

    # Clean up sed backup files
    rm -f .env.bak

    ok "Credentials saved to .env"
fi

# ── Step 5: Start Neo4j ──────────────────────────────────────

echo ""
echo -e "${CYAN}Step 5/7:${NC} Setting up Neo4j..."

CONTAINER_ENGINE=""
if command -v podman &>/dev/null; then
    CONTAINER_ENGINE="podman"
elif command -v docker &>/dev/null; then
    CONTAINER_ENGINE="docker"
fi

if [ -z "$CONTAINER_ENGINE" ]; then
    warn "Neither podman nor docker found"
    echo "  Install with:"
    echo "    macOS:  brew install podman && podman machine init && podman machine start"
    echo "    Linux:  sudo dnf install podman  (or apt install docker.io)"
    echo ""
    warn "Skipping Neo4j setup — you'll need to start it manually"
else
    # Read neo4j password from .env
    NEO4J_PW=$(grep "^NEO4J_PASSWORD=" .env 2>/dev/null | cut -d= -f2 || echo "password")

    if $CONTAINER_ENGINE ps --format '{{.Names}}' 2>/dev/null | grep -q "^neo4j-coordinator$"; then
        ok "Neo4j is already running"
    elif $CONTAINER_ENGINE ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^neo4j-coordinator$"; then
        info "Starting existing Neo4j container..."
        $CONTAINER_ENGINE start neo4j-coordinator >/dev/null
        ok "Neo4j started"
    else
        info "Creating Neo4j container..."
        $CONTAINER_ENGINE run -d --name neo4j-coordinator \
            -p 7474:7474 -p 7687:7687 \
            -e "NEO4J_AUTH=neo4j/$NEO4J_PW" \
            neo4j:5-community >/dev/null
        ok "Neo4j created and started"
    fi

    # Wait for ready
    info "Waiting for Neo4j..."
    for i in $(seq 1 20); do
        if curl -s -o /dev/null -w "%{http_code}" http://localhost:7474 2>/dev/null | grep -q "200"; then
            ok "Neo4j ready at http://localhost:7474"
            break
        fi
        if [ "$i" -eq 20 ]; then
            warn "Neo4j not responding yet — it may still be starting"
        fi
        sleep 2
    done
fi

# ── Step 6: Verify everything ─────────────────────────────────

echo ""
echo -e "${CYAN}Step 6/7:${NC} Verifying connections..."

# Check .env has real values
HAS_JIRA=true
HAS_GITHUB=true
if grep -q "your-jira-api-token" .env 2>/dev/null; then HAS_JIRA=false; fi
if grep -q "your-github-pat\|your-github-token" .env 2>/dev/null; then HAS_GITHUB=false; fi

if [ "$HAS_JIRA" = true ]; then
    PYTHONPATH=. python -c "
from dotenv import load_dotenv; load_dotenv()
import os
from src.apis.jira_client import JiraClient, JiraConfig
try:
    jira = JiraClient(JiraConfig(url=os.environ['JIRA_URL'], username=os.environ['JIRA_USERNAME'], api_token=os.environ['JIRA_API_TOKEN']))
    bugs = jira.get_bugs_by_components(['Etcd'], days=7, max_results=3, release='4.21')
    print(f'  \033[0;32m✓\033[0m JIRA connected — found {len(bugs)} Etcd bugs')
except Exception as e:
    print(f'  \033[0;31m✗\033[0m JIRA failed: {e}')
" 2>/dev/null || true
else
    warn "JIRA token not set — skipping verification"
fi

PYTHONPATH=. python -c "
from dotenv import load_dotenv; load_dotenv()
import os
from src.knowledge.neo4j_store import Neo4jStore
try:
    store = Neo4jStore(password=os.environ.get('NEO4J_PASSWORD', 'password'))
    connected = store.connect()
    if connected:
        keys = store.get_analyzed_bug_keys()
        print(f'  \033[0;32m✓\033[0m Neo4j connected — {len(keys)} bugs in graph')
    else:
        print(f'  \033[0;31m✗\033[0m Neo4j failed to connect')
    store.close()
except Exception as e:
    print(f'  \033[0;31m✗\033[0m Neo4j failed: {e}')
" 2>/dev/null || true

PYTHONPATH=. python -c "
from src.agents.registry import discover_agents
agents = discover_agents()
names = ', '.join(sorted(agents.keys()))
print(f'  \033[0;32m✓\033[0m {len(agents)} agents discovered: {names}')
" 2>/dev/null || true

# ── Step 7: Run tests ─────────────────────────────────────────

echo ""
echo -e "${CYAN}Step 7/7:${NC} Running tests..."

PYTHONPATH=. python -m pytest tests/unit/ -q --tb=no 2>/dev/null | tail -1 || true

# ── Done ──────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}============================================${NC}"
echo -e "  ${GREEN}${BOLD}Setup complete!${NC}"
echo -e "${BOLD}============================================${NC}"
echo ""
echo -e "  ${BOLD}What was set up:${NC}"
echo "    • Python virtual environment (venv/)"
echo "    • All Python dependencies installed"
echo "    • krkn repo at $KRKN_PATH"
echo "    • Neo4j database"
echo "    • Environment variables (.env)"
echo ""
echo -e "  ${BOLD}Next steps:${NC}"
echo ""
echo "    1. Activate the environment:"
echo -e "       ${CYAN}source venv/bin/activate${NC}"
echo ""
echo "    2. Ingest the knowledge base (one-time, ~6 min):"
echo -e "       ${CYAN}PYTHONPATH=. python -m src.knowledge.ingest ./chroma_data${NC}"
echo ""
echo "    3. Run the coordinator:"
echo -e "       ${CYAN}PYTHONPATH=. python src/main.py --release 4.21 --use-llm${NC}"
echo ""
echo "    Or use Claude Code:"
echo -e "       ${CYAN}claude${NC}  →  type ${CYAN}/run-scan${NC}"
echo ""

if [ "$HAS_JIRA" = false ] || [ "$HAS_GITHUB" = false ]; then
    echo -e "  ${YELLOW}${BOLD}⚠ Don't forget to add your API tokens to .env:${NC}"
    [ "$HAS_JIRA" = false ] && echo "    • JIRA_API_TOKEN — https://id.atlassian.com/manage-profile/security/api-tokens"
    [ "$HAS_GITHUB" = false ] && echo "    • GITHUB_TOKEN   — https://github.com/settings/tokens"
    echo ""
fi

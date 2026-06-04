"""JIRA REST API client for querying OCPBUGS."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

from src.models import Bug

logger = logging.getLogger(__name__)


def _next_version(release: str) -> str:
    """Return the next minor version string. '4.21' → '4.22', '5.0' → '5.1'."""
    parts = release.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def _extract_text_from_adf(doc: dict) -> str:
    """Extract plain text from Atlassian Document Format (ADF).

    JIRA REST API v3 returns descriptions as ADF (nested JSON) instead of
    plain strings. This recursively extracts text nodes.
    """
    parts: list[str] = []

    def _walk(node: dict | list) -> None:
        if isinstance(node, list):
            for item in node:
                _walk(item)
            return
        if not isinstance(node, dict):
            return
        if node.get("type") == "text":
            parts.append(node.get("text", ""))
        for child in node.get("content", []):
            _walk(child)

    _walk(doc)
    return " ".join(parts)


@dataclass(frozen=True)
class JiraConfig:
    url: str
    username: str
    api_token: str


class JiraClient:
    """Query JIRA REST API for OpenShift bugs by component."""

    def __init__(self, config: JiraConfig):
        self._config = config
        self._session = requests.Session()
        self._session.auth = (config.username, config.api_token)
        self._session.headers.update({"Accept": "application/json"})

    def get_bugs_by_components(
        self,
        components: list[str],
        days: int = 14,
        max_results: int = 2000,
        priority_filter: bool = True,
        release: str | None = None,
    ) -> list[Bug]:
        """Query OCPBUGS for recent bugs in the given components.

        When priority_filter is True, fetches Critical/Major/Blocker bugs first,
        then backfills with remaining bugs up to max_results.

        4-tier version query when release is set (e.g. "4.21"):
          Tier 1: bugs tagged with target release (>= 4.21, < 4.22)
          Tier 2: open bugs from older versions (unfixed, likely still present)
          Tier 3: open bugs from newer versions (if reported on 5.0, likely affects 4.21)
          Tier 4: bugs with no affectedVersion set
        """
        component_list = ", ".join(f'"{c}"' for c in components)

        if release:
            next_minor = _next_version(release)
            bugs = self._four_tier_version_query(
                component_list, release, next_minor, days, max_results, priority_filter,
            )
            return bugs

        if not priority_filter:
            jql = (
                f"project = OCPBUGS AND component IN ({component_list})"
                f" AND created >= -{days}d ORDER BY created DESC"
            )
            return self._search(jql, max_results)

        return self._priority_then_backfill(component_list, "", days, max_results)

    def _four_tier_version_query(
        self,
        component_list: str,
        release: str,
        next_minor: str,
        days: int,
        max_results: int,
        priority_filter: bool,
    ) -> list[Bug]:
        """Fetch bugs using 4-tier version matching."""
        seen_keys: set[str] = set()
        all_bugs: list[Bug] = []

        # Tier 1: Bugs explicitly tagged with the target release
        tier1_clause = f' AND affectedVersion >= "{release}" AND affectedVersion < "{next_minor}"'
        tier1 = self._priority_then_backfill(
            component_list, tier1_clause, days, max_results,
        ) if priority_filter else self._search(
            f"project = OCPBUGS AND component IN ({component_list})"
            f"{tier1_clause} AND created >= -{days}d ORDER BY created DESC",
            max_results,
        )
        for b in tier1:
            if b.key not in seen_keys:
                seen_keys.add(b.key)
                all_bugs.append(b)
        logger.info("Version tier 1 (%s.*): %d bugs", release, len(tier1))

        if len(all_bugs) >= max_results:
            return all_bugs[:max_results]

        # Tier 2: Open bugs from older versions (unfixed, likely still present)
        remaining = max_results - len(all_bugs)
        tier2_jql = (
            f"project = OCPBUGS AND component IN ({component_list})"
            f' AND affectedVersion < "{release}"'
            f' AND status NOT IN (Closed, Verified, "Release Pending")'
            f" AND created >= -{days}d ORDER BY priority ASC, created DESC"
        )
        tier2 = self._search(tier2_jql, remaining)
        for b in tier2:
            if b.key not in seen_keys:
                seen_keys.add(b.key)
                all_bugs.append(b)
        logger.info("Version tier 2 (older, open): %d bugs", len(tier2))

        if len(all_bugs) >= max_results:
            return all_bugs[:max_results]

        # Tier 3: Open bugs from newer versions (if it exists on 5.0, it likely exists on 4.21)
        remaining = max_results - len(all_bugs)
        tier3_jql = (
            f"project = OCPBUGS AND component IN ({component_list})"
            f' AND affectedVersion >= "{next_minor}"'
            f' AND status NOT IN (Closed, Verified, "Release Pending")'
            f" AND created >= -{days}d ORDER BY priority ASC, created DESC"
        )
        tier3 = self._search(tier3_jql, remaining)
        for b in tier3:
            if b.key not in seen_keys:
                seen_keys.add(b.key)
                all_bugs.append(b)
        logger.info("Version tier 3 (newer, open): %d bugs", len(tier3))

        if len(all_bugs) >= max_results:
            return all_bugs[:max_results]

        # Tier 4: Bugs with no affectedVersion set
        remaining = max_results - len(all_bugs)
        tier4_jql = (
            f"project = OCPBUGS AND component IN ({component_list})"
            f" AND affectedVersion IS EMPTY"
            f" AND created >= -{days}d ORDER BY created DESC"
        )
        tier4 = self._search(tier4_jql, remaining)
        for b in tier4:
            if b.key not in seen_keys:
                seen_keys.add(b.key)
                all_bugs.append(b)
        logger.info("Version tier 4 (no version): %d bugs", len(tier4))

        return all_bugs[:max_results]

    def _priority_then_backfill(
        self,
        component_list: str,
        version_clause: str,
        days: int,
        max_results: int,
    ) -> list[Bug]:
        """Fetch priority bugs first, then backfill with remaining."""
        priority_jql = (
            f"project = OCPBUGS AND component IN ({component_list})"
            f"{version_clause}"
            f" AND priority IN (Blocker, Critical, Major)"
            f" AND created >= -{days}d ORDER BY priority ASC, created DESC"
        )
        priority_bugs = self._search(priority_jql, max_results)

        if len(priority_bugs) >= max_results:
            return priority_bugs

        seen_keys = {b.key for b in priority_bugs}
        remaining = max_results - len(priority_bugs)
        all_jql = (
            f"project = OCPBUGS AND component IN ({component_list})"
            f"{version_clause}"
            f" AND created >= -{days}d ORDER BY created DESC"
        )
        all_bugs = self._search(all_jql, max_results)
        backfill = [b for b in all_bugs if b.key not in seen_keys][:remaining]

        return priority_bugs + backfill

    def get_bugs_by_keys(self, keys: list[str], batch_size: int = 100) -> list[Bug]:
        """Fetch bugs by their JIRA keys. Used for backfilling Neo4j data."""
        all_bugs: list[Bug] = []
        for i in range(0, len(keys), batch_size):
            batch = keys[i : i + batch_size]
            key_list = ", ".join(batch)
            jql = f"key IN ({key_list})"
            all_bugs.extend(self._search(jql, batch_size))
            logger.info("Backfill: fetched %d/%d bugs", len(all_bugs), len(keys))
        return all_bugs

    def _search(self, jql: str, max_results: int) -> list[Bug]:
        """Execute a JQL search with cursor-based pagination and return Bug objects.

        Atlassian's /rest/api/3/search/jql uses nextPageToken (not startAt).
        """
        url = f"{self._config.url}/rest/api/3/search/jql"
        logger.info("JIRA query: %s (max: %d)", jql, max_results)

        bugs = []
        page_size = min(max_results, 100)
        next_token = None

        while len(bugs) < max_results:
            params = {
                "jql": jql,
                "maxResults": page_size,
                "fields": "summary,description,status,priority,components,created",
            }
            if next_token:
                params["nextPageToken"] = next_token

            try:
                response = self._session.get(url, params=params, timeout=30)
                response.raise_for_status()
            except requests.RequestException as e:
                logger.error("JIRA query failed: %s", e)
                break

            data = response.json()
            issues = data.get("issues", [])
            if not issues:
                break

            for issue in issues:
                fields = issue["fields"]
                components = fields.get("components", [])
                component_names = tuple(c["name"] for c in components) if components else ("Unknown",)

                description = fields.get("description", "") or ""
                if isinstance(description, dict):
                    description = _extract_text_from_adf(description)

                bugs.append(
                    Bug(
                        key=issue["key"],
                        summary=fields.get("summary", ""),
                        description=description,
                        component=", ".join(component_names),
                        priority=fields.get("priority", {}).get("name", "Unknown"),
                        status=fields.get("status", {}).get("name", "Unknown"),
                        created=fields.get("created", ""),
                        url=f"{self._config.url}/browse/{issue['key']}",
                        all_components=component_names,
                    )
                )

            # Cursor-based pagination
            next_token = data.get("nextPageToken")
            is_last = data.get("isLast", True)

            if is_last or not next_token:
                break

        logger.info("Found %d bugs (unique)", len(bugs))
        return bugs[:max_results]

#!/usr/bin/env python3
"""
Rules Crawler v3 - academic and research project sources.

Strategy: HCI and applied-AI papers routinely publish GitHub repos alongside
their papers.  Those repos contain agent behavioral rules in two forms:
  1. Explicit rule files (same targets as crawler.py)
  2. System prompts embedded in source code (Python / TS / JSON config)

Sources:
  1. Papers With Code API   - papers tagged agent/HCI/LLM with linked repos
  2. arXiv API              - cs.HC, cs.AI, cs.CL, cs.MA recent papers;
                              extract GitHub URLs from abstracts
  3. Semantic Scholar API   - broader academic search, free, no key required
  4. GitHub topic search    - repos self-tagged as agent research
  5. Embedded prompt search - GitHub code search for inline system prompts
                              in all discovered repos

Output DB: rules3.db
Sources stored as:
  'academic_file'     - standard rule file found in an academic repo
  'academic_embedded' - system prompt extracted from source code

Usage:
  export GITHUB_TOKEN=ghp_...
  python3 crawler3.py [--limit 200]
"""

import asyncio
import hashlib
import json
import os
import re
import sys
import argparse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import aiohttp
import duckdb
from tqdm import tqdm

# Load .env if present
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    for _line in open(_env_path):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = "rules3.db"
CONCURRENCY = 6
DELAY = 0.5

# Standard rule filenames (same list as crawler.py + crawler2.py additions)
RULE_FILENAMES = [
    ".cursorrules", "CLAUDE.md", "AGENTS.md", ".windsurfrules", ".clinerules",
    ".github/copilot-instructions.md", "copilot-instructions.md",
    ".rules", "rules.md", "RULES.md", "system_prompt.md", "system-prompt.md",
    "SYSTEM_PROMPT.md", ".aider.conf.yml", ".continue/config.json",
    "prompt.md", "PROMPT.md", "CONVENTIONS.md", "llms.txt",
    ".roomodes", "GEMINI.md", "memory.md", "MEMORY.md", ".bolt/prompt",
    "CONTRIBUTING.md",
]

# Regex to match files that likely contain agent rules in a repo tree
RULE_FILE_RE = re.compile(
    r"(rules?|prompts?|system.?prompt|agents?|claude|cursor|windsurf|cline|roo|"
    r"copilot|gemini|instructions?|guidelines?|conventions?|memory|persona|behavior)"
    r".*\.(md|txt|yaml|yml|json|cursorrules|mdc)$",
    re.IGNORECASE,
)

# Regex to match source files that may contain embedded system prompts
EMBEDDED_FILE_RE = re.compile(
    r"(prompt|agent|instruction|system|config|assistant|persona|behavior|llm)"
    r".*\.(py|ts|js|json|yaml|yml)$",
    re.IGNORECASE,
)

# Patterns that indicate a string is a system prompt embedded in code
EMBEDDED_PROMPT_RE = re.compile(
    r'(?:system_prompt|SYSTEM_PROMPT|system_message|SYSTEM_MESSAGE|'
    r'instructions?|INSTRUCTIONS?|persona|PERSONA|base_prompt|BASE_PROMPT)'
    r'\s*=\s*(?:f?"""(.*?)"""|f?\'\'\'(.*?)\'\'\'|"((?:[^"\\]|\\.){80,})"'
    r'|\'((?:[^\'\\]|\\.){80,})\')',
    re.DOTALL,
)

# Also catch {"role": "system", "content": "..."} patterns
ROLE_SYSTEM_RE = re.compile(
    r'"role"\s*:\s*"system"\s*,\s*"content"\s*:\s*"((?:[^"\\]|\\.){80,})"',
    re.DOTALL,
)

GITHUB_URL_RE = re.compile(
    r'https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)'
)

# arXiv category + term pairs
ARXIV_SEARCHES = [
    ("cs.HC", "agent"),
    ("cs.AI", "llm agent"),
    ("cs.CL", "agent framework"),
    ("cs.MA", "agent"),          # Multi-Agent Systems
    ("cs.SE", "llm coding agent"),
]

# Semantic Scholar queries
S2_QUERIES = [
    "large language model agent framework",
    "autonomous LLM agent",
    "AI agent human computer interaction",
    "tool-augmented language model agent",
    "multi-agent LLM system",
]

# GitHub topics for research agent repos
GITHUB_TOPICS = [
    "llm-agent",
    "ai-agent",
    "autonomous-agent",
    "conversational-agent",
    "llm-framework",
    "agent-framework",
    "llm-tools",
    "ai-assistant-framework",
]

# GitHub code search queries for embedded system prompts
EMBEDDED_CODE_QUERIES = [
    'SYSTEM_PROMPT language:Python "You are"',
    'system_prompt language:Python "You are a"',
    'filename:prompts.py "You are"',
    'filename:system_prompts.py',
    'filename:agents.py SYSTEM_PROMPT',
    '"role": "system" filename:*.json "You are"',
    'SYSTEM_MESSAGE language:TypeScript "You are"',
]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db(con: duckdb.DuckDBPyConnection):
    con.execute("""
        CREATE TABLE IF NOT EXISTS rules (
            id          VARCHAR PRIMARY KEY,
            source_url  VARCHAR NOT NULL,
            raw_url     VARCHAR,
            file_type   VARCHAR,
            repo_name   VARCHAR,
            repo_stars  INTEGER,
            content     TEXT,
            content_len INTEGER,
            crawled_at  TIMESTAMP,
            source      VARCHAR
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS crawl_log (
            url         VARCHAR,
            status      VARCHAR,
            error       VARCHAR,
            ts          TIMESTAMP DEFAULT now()
        )
    """)
    # Track repos already scanned so we don't re-scan across runs
    con.execute("""
        CREATE TABLE IF NOT EXISTS scanned_repos (
            repo_slug   VARCHAR PRIMARY KEY,
            scanned_at  TIMESTAMP
        )
    """)


def content_id(url: str, content: str) -> str:
    return hashlib.sha256(f"{url}:{content}".encode()).hexdigest()[:16]


def upsert_rule(con, row: dict):
    cid = content_id(row["source_url"], row["content"])
    con.execute("""
        INSERT OR REPLACE INTO rules VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        cid, row["source_url"], row.get("raw_url"), row.get("file_type"),
        row.get("repo_name"), row.get("repo_stars"), row["content"],
        len(row["content"]), datetime.now(timezone.utc), row.get("source"),
    ])


def mark_repo_scanned(con, repo_slug: str):
    con.execute("""
        INSERT OR REPLACE INTO scanned_repos VALUES (?, ?)
    """, [repo_slug, datetime.now(timezone.utc)])


def repo_already_scanned(con, repo_slug: str) -> bool:
    return con.execute(
        "SELECT 1 FROM scanned_repos WHERE repo_slug = ?", [repo_slug]
    ).fetchone() is not None


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def github_headers(token) -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


async def fetch(session: aiohttp.ClientSession, url: str,
                headers=None, params=None, retries=3):
    for attempt in range(retries):
        try:
            async with session.get(
                url, headers=headers, params=params,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as r:
                if r.status in (429, 403):
                    wait = int(r.headers.get("Retry-After", "60"))
                    print(f"\n  [rate-limit] sleeping {wait}s")
                    await asyncio.sleep(wait)
                    continue
                return r.status, await r.read()
        except Exception:
            if attempt == retries - 1:
                return 0, None
            await asyncio.sleep(2 ** attempt)
    return 0, None


async def fetch_text(session, url: str, headers=None):
    status, body = await fetch(session, url, headers=headers)
    if status == 200 and body:
        try:
            return body.decode("utf-8", errors="replace")
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# Repo scanning (rule files + embedded prompts)
# ---------------------------------------------------------------------------

async def scan_repo(session, repo_slug: str, token, con, stars: int = None,
                    source_label: str = "academic_file"):
    """
    Fetch the repo tree and save any rule files and embedded system prompts.
    """
    if repo_already_scanned(con, repo_slug):
        return

    headers = github_headers(token)
    # Get default branch
    status, body = await fetch(session, f"https://api.github.com/repos/{repo_slug}",
                               headers=headers)
    if status != 200 or not body:
        mark_repo_scanned(con, repo_slug)
        return
    repo_info = parse_json_body(body)
    if not repo_info:
        mark_repo_scanned(con, repo_slug)
        return
    default_branch = repo_info.get("default_branch", "main")
    if stars is None:
        stars = repo_info.get("stargazers_count")

    # Fetch full recursive tree
    status, body = await fetch(session,
        f"https://api.github.com/repos/{repo_slug}/git/trees/{default_branch}?recursive=1",
        headers=headers)
    if status != 200 or not body:
        mark_repo_scanned(con, repo_slug)
        return
    tree_data = parse_json_body(body)
    tree = tree_data.get("tree", []) if tree_data else []

    rule_files = [
        item for item in tree
        if item["type"] == "blob" and (
            any(item["path"].endswith("/" + f) or item["path"] == f
                for f in RULE_FILENAMES)
            or RULE_FILE_RE.search(item["path"])
        )
    ]
    embedded_files = [
        item for item in tree
        if item["type"] == "blob"
        and EMBEDDED_FILE_RE.search(item["path"])
        and not any(p in item["path"] for p in ("test", "spec", "node_modules", ".venv"))
    ]

    base_raw = f"https://raw.githubusercontent.com/{repo_slug}/{default_branch}/"
    base_html = f"https://github.com/{repo_slug}/blob/{default_branch}/"

    # Save rule files
    for item in rule_files:
        raw_url = base_raw + item["path"]
        content = await fetch_text(session, raw_url)
        if not content or len(content.strip()) < 30:
            continue
        upsert_rule(con, {
            "source_url": base_html + item["path"],
            "raw_url": raw_url,
            "file_type": item["path"].rsplit(".", 1)[-1] if "." in item["path"] else "txt",
            "repo_name": repo_slug,
            "repo_stars": stars,
            "content": content,
            "source": source_label,
        })
        await asyncio.sleep(DELAY)

    # Extract embedded system prompts from source files
    for item in embedded_files:
        raw_url = base_raw + item["path"]
        content = await fetch_text(session, raw_url)
        if not content:
            continue

        extracted = []
        for m in EMBEDDED_PROMPT_RE.finditer(content):
            # groups 1-4 correspond to the four capture alternatives
            text = next((g for g in m.groups() if g), "").strip()
            if len(text) > 80:
                extracted.append(text)
        for m in ROLE_SYSTEM_RE.finditer(content):
            text = m.group(1).strip()
            if len(text) > 80:
                extracted.append(text)

        for i, prompt_text in enumerate(extracted):
            upsert_rule(con, {
                "source_url": base_html + item["path"],
                "raw_url": raw_url,
                "file_type": item["path"].rsplit(".", 1)[-1],
                "repo_name": repo_slug,
                "repo_stars": stars,
                "content": prompt_text,
                "source": "academic_embedded",
            })
        if extracted:
            await asyncio.sleep(DELAY)

    mark_repo_scanned(con, repo_slug)


# ---------------------------------------------------------------------------
# Source 1: Papers With Code
# ---------------------------------------------------------------------------

def parse_json_body(body: bytes):
    """Parse JSON from a response body, returning None on any failure."""
    if not body:
        return None
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


async def repos_from_pwc(session) -> set:
    """
    Fetch repos from Papers With Code public data dump.
    PWC publishes a JSON link file at their GitHub data repo — much more
    reliable than their web API which now returns HTML.
    Filter to papers whose titles contain agent/HCI-related keywords.
    """
    repos = set()
    DUMP_URL = (
        "https://raw.githubusercontent.com/paperswithcode/"
        "paperswithcode-data/main/links-between-papers-and-code.json.gz"
    )
    AGENT_KEYWORDS = re.compile(
        r"\b(agent|agents|autonomous|conversational|assistant|hci|"
        r"human.computer|tool.use|multi.agent|llm|copilot|agentic)\b",
        re.IGNORECASE,
    )

    import gzip, io
    print("    Downloading PWC links dump (~30 MB)...")
    status, body = await fetch(session, DUMP_URL)
    if status != 200 or not body:
        print(f"    PWC dump fetch failed (status={status})")
        return repos

    try:
        with gzip.open(io.BytesIO(body)) as gz:
            links = json.loads(gz.read())
    except Exception as e:
        print(f"    PWC dump parse error: {e}")
        return repos

    # links is a list of {paper_title, paper_url, repo_url, framework, ...}
    for entry in links:
        title = entry.get("paper_title") or entry.get("title") or ""
        if not AGENT_KEYWORDS.search(title):
            continue
        repo_url = entry.get("repo_url") or entry.get("repository", {})
        if isinstance(repo_url, dict):
            repo_url = repo_url.get("url", "")
        m = GITHUB_URL_RE.search(repo_url or "")
        if m:
            slug = m.group(1).rstrip("/").rstrip(".git")
            if "/" in slug:
                repos.add(slug)

    print(f"  Papers With Code: {len(repos)} repos from data dump")
    return repos


# ---------------------------------------------------------------------------
# Source 2: arXiv
# ---------------------------------------------------------------------------

async def repos_from_arxiv(session) -> set:
    """Search arXiv for agent papers and extract GitHub URLs from abstracts."""
    repos = set()
    ns = "http://www.w3.org/2005/Atom"
    for cat, term in ARXIV_SEARCHES:
        for start in range(0, 300, 100):
            params = {
                "search_query": f"cat:{cat} AND ti:{term}",
                "max_results": 100,
                "start": start,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }
            status, body = await fetch(
                session, "http://export.arxiv.org/api/query", params=params
            )
            if status != 200 or not body:
                break
            try:
                root = ET.fromstring(body.decode("utf-8", errors="replace"))
            except ET.ParseError:
                break
            entries = root.findall(f"{{{ns}}}entry")
            if not entries:
                break
            for entry in entries:
                summary = entry.findtext(f"{{{ns}}}summary") or ""
                # Also check all link hrefs
                links = " ".join(
                    el.get("href", "") for el in entry.findall(f"{{{ns}}}link")
                )
                text = summary + " " + links
                for m in GITHUB_URL_RE.finditer(text):
                    slug = m.group(1).rstrip("/").rstrip(".git")
                    # Filter out github.com/arxiv, github.com/openai etc. (orgs, not repos)
                    if slug.count("/") == 1 and len(slug) > 3:
                        repos.add(slug)
            await asyncio.sleep(1)
    print(f"  arXiv: {len(repos)} repos")
    return repos


# ---------------------------------------------------------------------------
# Source 3: Semantic Scholar
# ---------------------------------------------------------------------------

async def repos_from_semantic_scholar(session) -> set:
    """Search Semantic Scholar for agent papers and extract GitHub links."""
    repos = set()
    for query in S2_QUERIES:
        for offset in range(0, 200, 100):
            status, body = await fetch(
                session,
                "https://api.semanticscholar.org/graph/v1/paper/search",
                params={
                    "query": query,
                    "fields": "title,year,externalIds,openAccessPdf",
                    "limit": 100,
                    "offset": offset,
                },
                headers={"User-Agent": "rules-crawler/1.0"},
            )
            if status != 200 or not body:
                break
            data = parse_json_body(body)
            if not data:
                break
            papers = data.get("data", [])
            if not papers:
                break
            for paper in papers:
                # Semantic Scholar doesn't directly expose GitHub URLs in this endpoint,
                # but we can follow the ArXiv ID to get the paper page and extract links.
                arxiv_id = (paper.get("externalIds") or {}).get("ArXiv")
                if not arxiv_id:
                    continue
                # Fetch arXiv abstract page to extract GitHub URL
                abs_url = f"https://arxiv.org/abs/{arxiv_id}"
                s2, b2 = await fetch(session, abs_url)
                if s2 == 200 and b2:
                    text = b2.decode("utf-8", errors="replace")
                    for m in GITHUB_URL_RE.finditer(text):
                        slug = m.group(1).rstrip("/").rstrip(".git")
                        if slug.count("/") == 1 and len(slug) > 3:
                            repos.add(slug)
                await asyncio.sleep(DELAY)
            await asyncio.sleep(2)
    print(f"  Semantic Scholar: {len(repos)} repos")
    return repos


# ---------------------------------------------------------------------------
# Source 4: GitHub topic search
# ---------------------------------------------------------------------------

async def repos_from_github_topics(session, token) -> set:
    """Find research agent repos via GitHub topic search."""
    repos = set()
    headers = github_headers(token)
    for topic in GITHUB_TOPICS:
        params = {
            "q": f"topic:{topic}",
            "sort": "stars",
            "order": "desc",
            "per_page": 100,
        }
        status, body = await fetch(
            session, "https://api.github.com/search/repositories",
            headers=headers, params=params
        )
        if status != 200 or not body:
            await asyncio.sleep(2)
            continue
        data = parse_json_body(body)
        if not data:
            await asyncio.sleep(2)
            continue
        for repo in data.get("items", []):
            repos.add(repo["full_name"])
        await asyncio.sleep(2)
    print(f"  GitHub topics: {len(repos)} repos")
    return repos


# ---------------------------------------------------------------------------
# Source 5: GitHub code search for embedded system prompts
# (not repo-scoped — finds prompts across all of GitHub)
# ---------------------------------------------------------------------------

async def crawl_embedded_prompts(session, token, con,
                                  limit_per_query: int = 100):
    """
    Use GitHub code search to find files with inline system prompts anywhere
    on GitHub (not just in academic repos).
    """
    headers = github_headers(token)
    sem = asyncio.Semaphore(CONCURRENCY)
    saved = 0

    async def process(item):
        nonlocal saved
        async with sem:
            html_url = item.get("html_url", "")
            raw_url = item.get("download_url") or html_url.replace(
                "github.com", "raw.githubusercontent.com"
            ).replace("/blob/", "/")
            content = await fetch_text(session, raw_url)
            if not content:
                return

            repo = item.get("repository", {})
            extracted = []
            for m in EMBEDDED_PROMPT_RE.finditer(content):
                text = next((g for g in m.groups() if g), "").strip()
                if len(text) > 80:
                    extracted.append(text)
            for m in ROLE_SYSTEM_RE.finditer(content):
                text = m.group(1).strip()
                if len(text) > 80:
                    extracted.append(text)

            for prompt_text in extracted:
                upsert_rule(con, {
                    "source_url": html_url,
                    "raw_url": raw_url,
                    "file_type": item.get("path", "").rsplit(".", 1)[-1],
                    "repo_name": repo.get("full_name"),
                    "repo_stars": repo.get("stargazers_count"),
                    "content": prompt_text,
                    "source": "academic_embedded",
                })
                saved += 1
            await asyncio.sleep(DELAY)

    for query in EMBEDDED_CODE_QUERIES:
        params = {"q": query, "per_page": min(100, limit_per_query), "page": 1}
        status, body = await fetch(
            session, "https://api.github.com/search/code",
            headers=headers, params=params
        )
        if status != 200 or not body:
            await asyncio.sleep(3)
            continue
        data = parse_json_body(body)
        items = data.get("items", []) if data else []
        tasks = [process(item) for item in items]
        for coro in tqdm(asyncio.as_completed(tasks), total=len(tasks),
                         desc=f"  embedded", leave=False):
            await coro
        print(f"    '{query[:50]}' → {len(items)} files")
        await asyncio.sleep(3)

    print(f"  Embedded prompt search: saved {saved} prompts")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(limit: int, token: str):
    con = duckdb.connect(DB_PATH)
    init_db(con)

    print(f"[rules-crawler v3 / academic] DB={DB_PATH}, limit={limit}")
    print(f"  GitHub token: {'set' if token else 'NOT SET'}")

    async with aiohttp.ClientSession() as session:

        # --- Discover repos from academic sources ---
        print("\n[1/5] Collecting repos from Papers With Code...")
        pwc_repos = await repos_from_pwc(session)

        print("\n[2/5] Collecting repos from arXiv paper abstracts...")
        arxiv_repos = await repos_from_arxiv(session)

        print("\n[3/5] Collecting repos from Semantic Scholar...")
        s2_repos = await repos_from_semantic_scholar(session)

        print("\n[4/5] Collecting repos from GitHub topic search...")
        topic_repos = await repos_from_github_topics(session, token)

        all_repos = pwc_repos | arxiv_repos | s2_repos | topic_repos
        # Filter obviously bad slugs (single component, very short, known orgs)
        all_repos = {
            r for r in all_repos
            if "/" in r
            and not any(r.startswith(f"{org}/") for org in (
                "github", "microsoft", "google", "facebook", "apple", "amazon",
                "openai", "anthropic", "huggingface",   # keep research forks but skip main org repos
            ))
        }
        print(f"\n  Total unique academic repos to scan: {len(all_repos)}")

        # --- Scan each repo ---
        sem = asyncio.Semaphore(CONCURRENCY)

        async def scan_one(repo_slug):
            async with sem:
                await scan_repo(session, repo_slug, token, con,
                                source_label="academic_file")
                await asyncio.sleep(DELAY)

        repos_list = sorted(all_repos)
        if limit:
            repos_list = repos_list[:limit]

        tasks = [scan_one(r) for r in repos_list]
        for coro in tqdm(asyncio.as_completed(tasks), total=len(tasks),
                         desc="  scanning repos"):
            await coro

        # --- Embedded system prompts (broad GitHub code search) ---
        print("\n[5/5] GitHub code search for embedded system prompts...")
        await crawl_embedded_prompts(session, token, con, limit_per_query=limit or 100)

    # Summary
    total = con.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
    by_source = con.execute("""
        SELECT source, COUNT(*) as n FROM rules GROUP BY 1 ORDER BY 2 DESC
    """).fetchall()
    scanned = con.execute("SELECT COUNT(*) FROM scanned_repos").fetchone()[0]

    print(f"\n{'='*50}")
    print(f"Done! Total rules stored: {total}  |  Repos scanned: {scanned}")
    print("By source:")
    for row in by_source:
        print(f"  {str(row[0]):30s} {row[1]:>6}")
    print(f"Database: {DB_PATH}")
    con.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Crawl agent rules from academic / research projects"
    )
    parser.add_argument("--limit", type=int, default=0,
                        help="Max repos to scan (0 = all); also caps embedded query results")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"),
                        help="GitHub personal access token")
    args = parser.parse_args()
    asyncio.run(main(args.limit, args.token))

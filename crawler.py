#!/usr/bin/env python3
"""
Rules Crawler - finds agent/AI rules from across the web and stores them in DuckDB.

Sources:
  1. GitHub code search - .cursorrules, CLAUDE.md, AGENTS.md, .windsurfrules, etc.
  2. Awesome-lists - curated rule/prompt repos
  3. cursor.directory - community cursor rules
  4. GitHub Gists - pasted prompts/rules

Usage:
  export GITHUB_TOKEN=ghp_...
  python3 crawler.py [--limit 500]
"""

import asyncio
import hashlib
import json
import os
import re
import sys
import time
import argparse
from datetime import datetime, timezone
from urllib.parse import urlparse, urlencode, quote

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

DB_PATH = "rules.db"

# GitHub file targets to search for
GITHUB_FILE_TARGETS = [
    ".cursorrules",
    "CLAUDE.md",
    "AGENTS.md",
    ".windsurfrules",
    ".clinerules",
    ".github/copilot-instructions.md",
    "copilot-instructions.md",
    ".rules",
    "rules.md",
    "RULES.md",
    "system_prompt.md",
    "system-prompt.md",
    "SYSTEM_PROMPT.md",
    ".aider.conf.yml",          # aider AI coding assistant config
    ".continue/config.json",    # Continue.dev config (contains rules)
    "prompt.md",
    "PROMPT.md",
    "CONVENTIONS.md",           # common in Claude Code projects
    "llms.txt",                 # emerging standard for AI instructions
]

# Extra GitHub code search queries (not filename-based)
GITHUB_EXTRA_QUERIES = [
    "path:.cursor/rules extension:mdc",   # Cursor v0.45+ project rules
]

# Awesome-list repos that aggregate rules/prompts
AWESOME_LISTS = [
    "PatrickJS/awesome-cursorrules",
    "anthropics/prompt-library",
    "f/awesome-chatgpt-prompts",
    "getgrit/gritql",           # contains coding rules
    "nicepkg/gpt-runner",
]

# cursor.directory scrape
CURSOR_DIRECTORY_URL = "https://cursor.directory/api/rules"

CONCURRENCY = 8
DELAY_BETWEEN_REQUESTS = 0.5   # seconds, to be polite to GitHub API


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
            source      VARCHAR    -- 'github_search', 'awesome_list', 'cursor_directory', 'gist'
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


def content_id(url: str, content: str) -> str:
    return hashlib.sha256(f"{url}:{content}".encode()).hexdigest()[:16]


def upsert_rule(con, row: dict):
    cid = content_id(row["source_url"], row["content"])
    con.execute("""
        INSERT OR REPLACE INTO rules VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        cid,
        row["source_url"],
        row.get("raw_url"),
        row.get("file_type"),
        row.get("repo_name"),
        row.get("repo_stars"),
        row["content"],
        len(row["content"]),
        datetime.now(timezone.utc),
        row.get("source"),
    ])


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def make_github_headers(token) -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


async def fetch(session: aiohttp.ClientSession, url: str, headers=None, params=None,
                retries=3):
    for attempt in range(retries):
        try:
            async with session.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status == 429 or r.status == 403:
                    retry_after = int(r.headers.get("Retry-After", "60"))
                    print(f"\n  [rate-limit] {url} → sleeping {retry_after}s")
                    await asyncio.sleep(retry_after)
                    continue
                return r.status, await r.read()
        except Exception as e:
            if attempt == retries - 1:
                return 0, None
            await asyncio.sleep(2 ** attempt)
    return 0, None


# ---------------------------------------------------------------------------
# GitHub code search
# ---------------------------------------------------------------------------

async def github_search_filename(session, filename: str, token,
                                  per_page=100, max_pages=10):
    """Search GitHub for a specific filename and return file metadata."""
    headers = make_github_headers(token)
    results = []
    for page in range(1, max_pages + 1):
        params = {
            "q": f"filename:{filename}",
            "per_page": per_page,
            "page": page,
        }
        status, body = await fetch(session,
            "https://api.github.com/search/code",
            headers=headers, params=params)
        if status != 200 or not body:
            break
        data = json.loads(body)
        items = data.get("items", [])
        if not items:
            break
        results.extend(items)
        # GitHub caps code search at 1000 results
        if len(results) >= data.get("total_count", 0) or len(results) >= 1000:
            break
        await asyncio.sleep(DELAY_BETWEEN_REQUESTS)
    return results


async def fetch_raw_content(session, raw_url: str):
    status, body = await fetch(session, raw_url)
    if status == 200 and body:
        try:
            return body.decode("utf-8", errors="replace")
        except Exception:
            return None
    return None


async def github_search_query(session, query: str, token,
                               per_page=100, max_pages=10):
    """Run an arbitrary GitHub code search query and return file metadata."""
    headers = make_github_headers(token)
    results = []
    for page in range(1, max_pages + 1):
        params = {"q": query, "per_page": per_page, "page": page}
        status, body = await fetch(session,
            "https://api.github.com/search/code",
            headers=headers, params=params)
        if status != 200 or not body:
            break
        data = json.loads(body)
        items = data.get("items", [])
        if not items:
            break
        results.extend(items)
        if len(results) >= data.get("total_count", 0) or len(results) >= 1000:
            break
        await asyncio.sleep(DELAY_BETWEEN_REQUESTS)
    return results


async def crawl_github_search(session, token, con: duckdb.DuckDBPyConnection,
                               limit_per_file: int = 200):
    """Main GitHub code search crawler."""
    sem = asyncio.Semaphore(CONCURRENCY)

    async def process_item(item, file_type):
        async with sem:
            raw_url = item.get("html_url", "").replace(
                "github.com", "raw.githubusercontent.com"
            ).replace("/blob/", "/")
            raw_url = item.get("download_url") or raw_url
            if not raw_url:
                return
            content = await fetch_raw_content(session, raw_url)
            if not content or len(content.strip()) < 20:
                return
            repo = item.get("repository", {})
            upsert_rule(con, {
                "source_url": item.get("html_url", raw_url),
                "raw_url": raw_url,
                "file_type": file_type,
                "repo_name": repo.get("full_name"),
                "repo_stars": repo.get("stargazers_count"),
                "content": content,
                "source": "github_search",
            })
            await asyncio.sleep(DELAY_BETWEEN_REQUESTS)

    for filename in GITHUB_FILE_TARGETS:
        print(f"  Searching GitHub for: {filename}")
        items = await github_search_filename(session, filename, token,
                                              per_page=min(100, limit_per_file))
        items = items[:limit_per_file]
        tasks = [process_item(item, filename) for item in items]
        for coro in tqdm(asyncio.as_completed(tasks), total=len(tasks),
                         desc=f"  {filename}", leave=False):
            await coro
        print(f"    → fetched {len(tasks)} files for {filename}")
        await asyncio.sleep(2)

    for query in GITHUB_EXTRA_QUERIES:
        print(f"  Searching GitHub for: {query}")
        items = await github_search_query(session, query, token,
                                           per_page=min(100, limit_per_file))
        items = items[:limit_per_file]
        tasks = [process_item(item, "mdc") for item in items]
        for coro in tqdm(asyncio.as_completed(tasks), total=len(tasks),
                         desc=f"  mdc", leave=False):
            await coro
        print(f"    → fetched {len(tasks)} files for {query}")
        await asyncio.sleep(2)


# ---------------------------------------------------------------------------
# Awesome-list crawler
# ---------------------------------------------------------------------------

async def crawl_awesome_list(session, repo_slug: str, token,
                              con: duckdb.DuckDBPyConnection):
    """Crawl a repo's default branch for any .md files that look like rules/prompts."""
    headers = make_github_headers(token)
    # Get repo metadata
    status, body = await fetch(session, f"https://api.github.com/repos/{repo_slug}", headers=headers)
    if status != 200 or not body:
        return
    repo_info = json.loads(body)
    stars = repo_info.get("stargazers_count", 0)
    default_branch = repo_info.get("default_branch", "main")

    # Get tree
    status, body = await fetch(session,
        f"https://api.github.com/repos/{repo_slug}/git/trees/{default_branch}?recursive=1",
        headers=headers)
    if status != 200 or not body:
        return
    tree = json.loads(body)

    RULE_FILE_RE = re.compile(
        r"(rules?|prompts?|system.?prompt|agents?|claude|cursor|instructions?|guidelines?)"
        r".*\.(md|txt|yaml|yml|json)$",
        re.IGNORECASE,
    )

    matching = [
        item for item in tree.get("tree", [])
        if item["type"] == "blob" and RULE_FILE_RE.search(item["path"])
    ]

    for item in matching:
        raw_url = f"https://raw.githubusercontent.com/{repo_slug}/{default_branch}/{item['path']}"
        content = await fetch_raw_content(session, raw_url)
        if not content or len(content.strip()) < 50:
            continue
        html_url = f"https://github.com/{repo_slug}/blob/{default_branch}/{item['path']}"
        upsert_rule(con, {
            "source_url": html_url,
            "raw_url": raw_url,
            "file_type": item["path"].rsplit(".", 1)[-1],
            "repo_name": repo_slug,
            "repo_stars": stars,
            "content": content,
            "source": "awesome_list",
        })
        await asyncio.sleep(DELAY_BETWEEN_REQUESTS)

    print(f"    → {repo_slug}: saved {len(matching)} files")


# ---------------------------------------------------------------------------
# cursor.directory
# ---------------------------------------------------------------------------

async def crawl_cursor_directory(session, con: duckdb.DuckDBPyConnection):
    """Fetch rules from cursor.directory (they have a public API/export)."""
    # cursor.directory serves a Next.js app; rules are embedded in page data
    # Try the known static export paths
    urls_to_try = [
        "https://cursor.directory/api/rules",
        "https://cursor.directory/_next/data/rules.json",
        "https://raw.githubusercontent.com/PatrickJS/awesome-cursorrules/main/rules/README.md",
    ]

    # The canonical source is the awesome-cursorrules GitHub repo
    # Let's fetch the index and then individual rule files
    index_url = "https://api.github.com/repos/PatrickJS/awesome-cursorrules/contents/rules"
    status, body = await fetch(session, index_url)
    if status == 200 and body:
        items = json.loads(body)
        print(f"    cursor.directory / awesome-cursorrules: {len(items)} rule dirs")
        for item in items:
            if item["type"] == "dir":
                # fetch .cursorrules inside
                dir_url = item["url"]
                s2, b2 = await fetch(session, dir_url)
                if s2 == 200 and b2:
                    files = json.loads(b2)
                    for f in files:
                        if f["name"].endswith((".cursorrules", ".md", ".txt")):
                            content = await fetch_raw_content(session, f["download_url"])
                            if content and len(content.strip()) > 20:
                                upsert_rule(con, {
                                    "source_url": f["html_url"],
                                    "raw_url": f["download_url"],
                                    "file_type": f["name"].rsplit(".", 1)[-1],
                                    "repo_name": "PatrickJS/awesome-cursorrules",
                                    "repo_stars": None,
                                    "content": content,
                                    "source": "cursor_directory",
                                })
                await asyncio.sleep(DELAY_BETWEEN_REQUESTS)


# ---------------------------------------------------------------------------
# GitHub Gists
# ---------------------------------------------------------------------------

async def crawl_gists(session, token, con: duckdb.DuckDBPyConnection,
                      max_pages=5):
    """Search public gists for rules/system-prompt content via the Gist list API."""
    headers = make_github_headers(token)
    saved = 0

    GIST_FILENAME_RE = re.compile(
        r"(cursorrules|claude|agents?|rules?|system.?prompt|copilot.instructions|"
        r"windsurfrules|clinerules|llms\.txt|conventions)"
        r".*\.(md|txt|cursorrules|mdc|yaml|yml|json)?$",
        re.IGNORECASE,
    )

    for page in range(1, max_pages + 1):
        status, body = await fetch(session,
            "https://api.github.com/gists/public",
            headers=headers,
            params={"per_page": 100, "page": page})
        if status != 200 or not body:
            break
        gists = json.loads(body)
        if not gists:
            break

        for gist in gists:
            gist_id = gist.get("id", "")
            gist_url = gist.get("html_url", "")
            for fname, fmeta in gist.get("files", {}).items():
                if not GIST_FILENAME_RE.search(fname):
                    continue
                raw_url = fmeta.get("raw_url")
                if not raw_url:
                    continue
                content = await fetch_raw_content(session, raw_url)
                if not content or len(content.strip()) < 20:
                    continue
                ext = fname.rsplit(".", 1)[-1] if "." in fname else fname
                upsert_rule(con, {
                    "source_url": gist_url,
                    "raw_url": raw_url,
                    "file_type": ext,
                    "repo_name": None,
                    "repo_stars": gist.get("forks_count"),
                    "content": content,
                    "source": "gist",
                })
                saved += 1
                await asyncio.sleep(DELAY_BETWEEN_REQUESTS)

        await asyncio.sleep(1)

    print(f"    → saved {saved} gist files")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(limit: int, token):
    con = duckdb.connect(DB_PATH)
    init_db(con)

    print(f"[rules-crawler] Starting. DB={DB_PATH}, limit/file={limit}")
    print(f"  GitHub token: {'set' if token else 'NOT SET (rate limits will apply)'}")

    async with aiohttp.ClientSession() as session:

        print("\n[1/4] GitHub code search for known rule filenames...")
        await crawl_github_search(session, token, con, limit_per_file=limit)

        print("\n[2/4] Crawling awesome-lists...")
        for repo in AWESOME_LISTS:
            print(f"  {repo}")
            await crawl_awesome_list(session, repo, token, con)
            await asyncio.sleep(1)

        print("\n[3/4] Crawling cursor.directory / awesome-cursorrules...")
        await crawl_cursor_directory(session, con)

        print("\n[4/4] GitHub Gists search...")
        await crawl_gists(session, token, con)

    # Summary
    total = con.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
    by_type = con.execute("""
        SELECT file_type, COUNT(*) as n
        FROM rules GROUP BY file_type ORDER BY n DESC
    """).fetchall()
    by_source = con.execute("""
        SELECT source, COUNT(*) as n
        FROM rules GROUP BY source ORDER BY n DESC
    """).fetchall()

    print(f"\n{'='*50}")
    print(f"Done! Total rules stored: {total}")
    print(f"\nBy file type:")
    for row in by_type:
        print(f"  {row[0]:30s} {row[1]:>6}")
    print(f"\nBy source:")
    for row in by_source:
        print(f"  {row[0]:30s} {row[1]:>6}")
    print(f"\nDatabase: {DB_PATH}")
    con.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Crawl agent rules from the web into DuckDB")
    parser.add_argument("--limit", type=int, default=200,
                        help="Max files to fetch per filename target (default: 200)")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"),
                        help="GitHub personal access token (or set GITHUB_TOKEN env var)")
    args = parser.parse_args()

    asyncio.run(main(args.limit, args.token))

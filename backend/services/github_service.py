import base64
from concurrent.futures import ThreadPoolExecutor
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple
import requests

from config.settings import settings
from services.security_utils import (
    DEPENDENCY_FILENAMES,
    DOC_FILENAMES,
    is_analyzable_source_file,
    is_ignored_path,
    is_sensitive_path,
    redact_secrets,
)

logger = logging.getLogger(__name__)

PRIORITY_FILENAMES = {
    "main.py",
    "app.py",
    "server.py",
    "index.js",
    "index.ts",
    "app.js",
    "app.ts",
    "server.js",
    "server.ts",
    "main.go",
    "main.rs",
    "lib.rs",
}


def parse_repo_identifier(text: str) -> Optional[Tuple[str, str]]:
    """Extract (owner, repo) from 'owner/repo' or a GitHub URL."""
    if not text:
        return None
    cleaned = text.strip()

    url_match = re.search(
        r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", cleaned
    )
    if url_match:
        owner, repo = url_match.group(1), url_match.group(2)
        repo = re.sub(r"\.git$", "", repo)
        return owner, repo

    # Match standalone owner/repo token in text
    RESERVED = {
        "api", "analyze", "github", "health", "docs", "src", "backend",
        "extension", "sidepanel", "content", "assets", "routes", "services",
        "config", "firebase", "http", "https", "application", "text",
    }
    for match in re.finditer(r"\b([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\b", cleaned):
        owner, repo = match.group(1), match.group(2)
        if owner.lower() in RESERVED or repo.lower() in RESERVED:
            continue
        # Skip file paths with common code extensions
        if "." in repo and repo.rsplit(".", 1)[-1].lower() in {
            "py", "js", "ts", "html", "css", "json", "md", "yml", "yaml", "txt", "sh"
        }:
            continue
        return owner, repo

    return None


def _headers(token: Optional[str] = None) -> Dict[str, str]:
    auth_token = (token or settings.GITHUB_TOKEN or "").strip()
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "DevStart-AI-Backend/1.0",
    }
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    return headers


def get_repo_metadata(owner: str, repo: str, token: Optional[str] = None) -> Dict[str, Any]:
    """Fetch repository details from the GitHub REST API."""
    url = f"{settings.GITHUB_API_BASE}/repos/{owner}/{repo}"
    resp = requests.get(url, headers=_headers(token), timeout=settings.GITHUB_TIMEOUT)
    if resp.status_code == 404:
        raise ValueError(f"Repository '{owner}/{repo}' not found on GitHub.")
    if resp.status_code == 403 and "rate limit" in resp.text.lower():
        raise RuntimeError(
            "GitHub API rate limit exceeded. Configure GITHUB_TOKEN in backend/.env for 5,000 requests/hour."
        )
    resp.raise_for_status()
    data = resp.json()
    return {
        "full_name": data.get("full_name", f"{owner}/{repo}"),
        "owner": data.get("owner", {}).get("login", owner),
        "name": data.get("name", repo),
        "description": data.get("description") or "",
        "default_branch": data.get("default_branch", "main"),
        "language": data.get("language") or "Unknown",
        "visibility": "Private" if data.get("private") else "Public",
        "stars": data.get("stargazers_count", 0),
        "forks": data.get("forks_count", 0),
        "open_issues": data.get("open_issues_count", 0),
        "topics": data.get("topics", []),
        "license": (data.get("license") or {}).get("spdx_id"),
        "html_url": data.get("html_url", f"https://github.com/{owner}/{repo}"),
        "avatar_url": (data.get("owner") or {}).get("avatar_url", ""),
    }


def get_repo_tree(
    owner: str, repo: str, branch: str = "main", token: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Fetch the repository file tree recursively."""
    url = f"{settings.GITHUB_API_BASE}/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
    resp = requests.get(url, headers=_headers(token), timeout=settings.GITHUB_TIMEOUT)
    if not resp.ok:
        # Fallback to root contents if git tree fails
        contents_url = f"{settings.GITHUB_API_BASE}/repos/{owner}/{repo}/contents?ref={branch}"
        c_resp = requests.get(contents_url, headers=_headers(token), timeout=settings.GITHUB_TIMEOUT)
        if not c_resp.ok or not isinstance(c_resp.json(), list):
            return []
        return [
            {
                "path": item.get("path", ""),
                "type": "blob" if item.get("type") == "file" else "tree",
                "size": item.get("size", 0),
            }
            for item in c_resp.json()
            if not is_ignored_path(item.get("path", "")) and not is_sensitive_path(item.get("path", ""))
        ]

    tree_data = resp.json().get("tree", [])
    filtered = []
    for item in tree_data:
        path = item.get("path", "")
        if not path or is_ignored_path(path) or is_sensitive_path(path):
            continue
        filtered.append(
            {
                "path": path,
                "type": item.get("type", "blob"),
                "size": item.get("size", 0),
            }
        )
    return filtered


def get_file_content(
    owner: str,
    repo: str,
    path: str,
    ref: Optional[str] = None,
    token: Optional[str] = None,
) -> Optional[str]:
    """Fetch and decode a single file from GitHub, stripping any secrets."""
    if is_sensitive_path(path):
        logger.warning("Blocked attempt to fetch sensitive file: %s", path)
        return None

    url = f"{settings.GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{path.lstrip('/')}"
    params = {"ref": ref} if ref else {}
    try:
        resp = requests.get(
            url, headers=_headers(token), params=params, timeout=settings.GITHUB_TIMEOUT
        )
        if not resp.ok:
            return None
        data = resp.json()
        if isinstance(data, list) or data.get("type") != "file":
            return None
        size = data.get("size", 0)
        if size > settings.MAX_FILE_SIZE_BYTES:
            return None

        encoded = data.get("content", "")
        if data.get("encoding") == "base64" and encoded:
            raw_bytes = base64.b64decode(encoded)
            text = raw_bytes.decode("utf-8", errors="replace")
            return redact_secrets(text)
    except Exception as exc:
        logger.debug("Could not fetch file %s/%s/%s: %s", owner, repo, path, exc)
    return None


def get_raw_file(
    owner: str,
    repo: str,
    path: str,
    ref: Optional[str] = None,
    token: Optional[str] = None,
) -> Optional[str]:
    """
    Fetch a file's exact content (no redaction) so approved edits can be
    committed without overwriting real values with redaction placeholders.
    Never send this content to an external model; use get_file_content for that.
    """
    if is_sensitive_path(path):
        return None
    url = f"{settings.GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{path.lstrip('/')}"
    params = {"ref": ref} if ref else {}
    try:
        resp = requests.get(
            url, headers=_headers(token), params=params, timeout=settings.GITHUB_TIMEOUT
        )
        if not resp.ok:
            return None
        data = resp.json()
        if isinstance(data, list) or data.get("type") != "file":
            return None
        if data.get("size", 0) > settings.MAX_FILE_SIZE_BYTES:
            return None
        encoded = data.get("content", "")
        if data.get("encoding") == "base64" and encoded:
            return base64.b64decode(encoded).decode("utf-8")
    except Exception as exc:
        logger.debug("Could not fetch raw file %s/%s/%s: %s", owner, repo, path, exc)
    return None


def select_batch_files(tree: List[Dict[str, Any]], max_files: int) -> List[str]:
    """Select a prioritized, manageable batch of source files for analysis."""
    blobs = [
        item
        for item in tree
        if item.get("type") == "blob"
        and is_analyzable_source_file(item.get("path", ""))
        and 0 < item.get("size", 1) <= settings.MAX_FILE_SIZE_BYTES
    ]

    def score_file(item: Dict[str, Any]) -> Tuple[int, int, int, int, str]:
        path = item["path"]
        name = path.rsplit("/", 1)[-1].lower()
        depth = path.count("/")
        is_init = 1 if name == "__init__.py" else 0
        is_priority = 0 if name in PRIORITY_FILENAMES else 1
        is_test = 1 if ("test" in path.lower() or "spec" in path.lower()) else 0
        return (is_init, is_test, is_priority, depth, path)

    blobs.sort(key=score_file)
    return [item["path"] for item in blobs[:max_files]]


def fetch_repo_context(
    owner: str,
    repo: str,
    max_files: Optional[int] = None,
    specific_files: Optional[List[str]] = None,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Retrieve repository metadata, README, CONTRIBUTING, dependency manifests,
    file tree, and a manageable batch of source files for AI analysis.
    """
    batch_limit = max_files or settings.MAX_FILES_PER_BATCH
    metadata = get_repo_metadata(owner, repo, token=token)
    branch = metadata.get("default_branch", "main")
    tree = get_repo_tree(owner, repo, branch=branch, token=token)

    all_paths = [item["path"] for item in tree if item.get("type") == "blob"]

    # 1. Documentation files (README, CONTRIBUTING)
    doc_paths = [
        path
        for path in all_paths
        if path.count("/") == 0 and path.rsplit("/", 1)[-1].lower() in DOC_FILENAMES
    ][:2]

    # 2. Dependency files
    dep_paths = [
        path
        for path in all_paths
        if path.count("/") <= 1 and path.rsplit("/", 1)[-1] in DEPENDENCY_FILENAMES
    ][:3]

    # 3. Source code files (in manageable batches)
    if specific_files:
        target_files = [
            f for f in specific_files if not is_sensitive_path(f) and not is_ignored_path(f)
        ][:batch_limit]
    else:
        target_files = select_batch_files(tree, max_files=batch_limit)

    # Fetch all selected files concurrently to keep response time under 2 seconds
    paths_to_fetch = list(dict.fromkeys(doc_paths + dep_paths + target_files))
    fetched_contents: Dict[str, str] = {}

    def _fetch_one(p: str) -> Tuple[str, Optional[str]]:
        return p, get_file_content(owner, repo, p, ref=branch, token=token)

    with ThreadPoolExecutor(max_workers=8) as executor:
        for p, content in executor.map(_fetch_one, paths_to_fetch):
            if content:
                fetched_contents[p] = content

    docs: Dict[str, str] = {p: fetched_contents[p][:8000] for p in doc_paths if p in fetched_contents}
    dependencies: Dict[str, str] = {p: fetched_contents[p][:4000] for p in dep_paths if p in fetched_contents}

    source_files: Dict[str, str] = {}
    total_chars = 0
    for path in target_files:
        if total_chars >= settings.MAX_TOTAL_CODE_CHARS:
            break
        content = fetched_contents.get(path)
        if content:
            remaining = settings.MAX_TOTAL_CODE_CHARS - total_chars
            clipped = content[:remaining]
            source_files[path] = clipped
            total_chars += len(clipped)

    return {
        "metadata": metadata,
        "branch": branch,
        "file_tree": all_paths[:120],
        "total_files_in_repo": len(all_paths),
        "docs": docs,
        "dependencies": dependencies,
        "source_files": source_files,
        "analyzed_files_count": len(source_files),
    }


def create_pull_request_workflow(
    owner: str,
    repo: str,
    title: str,
    body: str,
    changes: List[Dict[str, str]],
    base_branch: Optional[str] = None,
    new_branch: Optional[str] = None,
    approved: bool = False,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create a branch, commit approved file modifications, and open a Pull Request.
    Enforces explicit user approval before modifying any remote state.
    """
    if not approved:
        raise PermissionError(
            "Pull Request creation requires explicit user approval (approved=True)."
        )

    auth_token = (token or settings.GITHUB_TOKEN or "").strip()
    if not auth_token:
        raise ValueError(
            "A GitHub personal access token (GITHUB_TOKEN) is required to create branches and Pull Requests."
        )

    if not changes:
        raise ValueError("At least one file change is required to create a Pull Request.")

    headers = _headers(auth_token)

    # 1. Determine base branch if not provided
    if not base_branch:
        meta = get_repo_metadata(owner, repo, token=auth_token)
        base_branch = meta.get("default_branch", "main")

    # 2. Get SHA of base branch
    ref_url = f"{settings.GITHUB_API_BASE}/repos/{owner}/{repo}/git/ref/heads/{base_branch}"
    ref_resp = requests.get(ref_url, headers=headers, timeout=settings.GITHUB_TIMEOUT)
    if not ref_resp.ok:
        raise RuntimeError(
            f"Failed to read base branch '{base_branch}' on {owner}/{repo}: {ref_resp.text}"
        )
    base_sha = ref_resp.json()["object"]["sha"]

    # 3. Create new branch
    branch_name = new_branch or f"devstart-ai/improvements-{int(time.time())}"
    create_ref_url = f"{settings.GITHUB_API_BASE}/repos/{owner}/{repo}/git/refs"
    create_ref_resp = requests.post(
        create_ref_url,
        headers=headers,
        json={"ref": f"refs/heads/{branch_name}", "sha": base_sha},
        timeout=settings.GITHUB_TIMEOUT,
    )
    if not create_ref_resp.ok and create_ref_resp.status_code != 422:
        raise RuntimeError(f"Failed to create branch '{branch_name}': {create_ref_resp.text}")

    # 4. Commit each approved file modification
    committed_files: List[str] = []
    for change in changes:
        file_path = change.get("path", "").lstrip("/")
        new_content = change.get("content", "")
        commit_msg = change.get("commit_message") or f"DevStart AI: Improve {file_path}"

        if not file_path or is_sensitive_path(file_path):
            continue

        contents_url = f"{settings.GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{file_path}"
        existing_resp = requests.get(
            contents_url,
            headers=headers,
            params={"ref": branch_name},
            timeout=settings.GITHUB_TIMEOUT,
        )
        existing_sha = existing_resp.json().get("sha") if existing_resp.ok else None

        put_payload: Dict[str, Any] = {
            "message": commit_msg,
            "content": base64.b64encode(new_content.encode("utf-8")).decode("ascii"),
            "branch": branch_name,
        }
        if existing_sha:
            put_payload["sha"] = existing_sha

        put_resp = requests.put(
            contents_url,
            headers=headers,
            json=put_payload,
            timeout=settings.GITHUB_TIMEOUT,
        )
        if not put_resp.ok:
            raise RuntimeError(f"Failed to commit file '{file_path}': {put_resp.text}")
        committed_files.append(file_path)

    # 5. Open Pull Request
    pr_url = f"{settings.GITHUB_API_BASE}/repos/{owner}/{repo}/pulls"
    pr_resp = requests.post(
        pr_url,
        headers=headers,
        json={
            "title": title,
            "body": body,
            "head": branch_name,
            "base": base_branch,
        },
        timeout=settings.GITHUB_TIMEOUT,
    )
    if not pr_resp.ok:
        raise RuntimeError(f"Failed to create Pull Request: {pr_resp.text}")

    pr_data = pr_resp.json()
    return {
        "status": "created",
        "pr_number": pr_data.get("number"),
        "html_url": pr_data.get("html_url"),
        "branch": branch_name,
        "base_branch": base_branch,
        "committed_files": committed_files,
        "title": pr_data.get("title", title),
    }


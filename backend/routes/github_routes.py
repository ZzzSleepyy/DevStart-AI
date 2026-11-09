from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from config.settings import settings
from services.auto_pr_service import AutoPrError, create_automated_pull_request
from services.github_service import (
    create_pull_request_workflow,
    fetch_repo_context,
    get_file_content,
    get_repo_metadata,
    get_repo_tree,
)

router = APIRouter(prefix="/github", tags=["GitHub"])


class FileChangeModel(BaseModel):
    path: str = Field(..., description="Repository-relative file path to create or update")
    content: str = Field(..., description="Full replacement file content approved by the developer")
    commit_message: Optional[str] = Field(None, description="Optional commit message for this file")


class CreatePullRequestPayload(BaseModel):
    owner: Optional[str] = Field(None, description="Repository owner (or provide 'repo' as 'owner/name')")
    repo: str = Field(..., description="Repository name or 'owner/repo'")
    title: str = Field(..., description="Pull Request title")
    body: str = Field("", description="Pull Request description / rationale")
    changes: List[FileChangeModel] = Field(..., description="List of approved file modifications")
    base_branch: Optional[str] = Field(None, description="Target base branch (defaults to repo default_branch)")
    new_branch: Optional[str] = Field(None, description="New branch name to create")
    approved: bool = Field(
        False,
        description="Must be explicitly True after the developer reviews and approves the changes",
    )
    github_token: Optional[str] = Field(None, description="Optional user GitHub token overriding server env")


@router.get("/repo/{owner}/{repo}")
def get_repo_details(
    owner: str,
    repo: str,
    include_context: bool = Query(
        True, description="Include README, CONTRIBUTING, dependencies, and file tree"
    ),
    max_files: int = Query(6, ge=1, le=20, description="Max source files to fetch in batch"),
    x_github_token: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Fetch repository details, documentation, dependencies, and file structure from GitHub."""
    try:
        if include_context:
            context = fetch_repo_context(
                owner, repo, max_files=max_files, token=x_github_token
            )
            return {"status": "ok", **context}

        metadata = get_repo_metadata(owner, repo, token=x_github_token)
        tree = get_repo_tree(
            owner, repo, branch=metadata.get("default_branch", "main"), token=x_github_token
        )
        return {
            "status": "ok",
            "metadata": metadata,
            "file_tree": [item["path"] for item in tree if item.get("type") == "blob"][:120],
        }
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GitHub API error: {exc}") from exc


@router.get("/repo/{owner}/{repo}/file")
def get_repository_file(
    owner: str,
    repo: str,
    path: str = Query(..., description="Path to file inside the repository"),
    ref: Optional[str] = Query(None, description="Branch or commit ref"),
    x_github_token: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """Fetch a single source file from a GitHub repository (with secret redaction)."""
    content = get_file_content(owner, repo, path=path, ref=ref, token=x_github_token)
    if content is None:
        raise HTTPException(
            status_code=404,
            detail=f"File '{path}' could not be retrieved or is restricted for security.",
        )
    return {"status": "ok", "owner": owner, "repo": repo, "path": path, "content": content}


@router.post("/pr")
def create_pull_request(
    payload: CreatePullRequestPayload,
    x_github_token: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """
    Create a new branch, commit approved file changes, and submit a Pull Request on GitHub.
    Requires explicit developer approval (`approved=True`).
    """
    owner = payload.owner
    repo_name = payload.repo
    if "/" in repo_name and not owner:
        owner, repo_name = repo_name.split("/", 1)

    if not owner or not repo_name:
        raise HTTPException(
            status_code=400,
            detail="Both 'owner' and 'repo' (or 'owner/repo') must be provided.",
        )

    if not payload.approved:
        raise HTTPException(
            status_code=400,
            detail="Pull Request creation blocked: Developer must review and approve changes first (approved=True).",
        )

    token = payload.github_token or x_github_token
    try:
        result = create_pull_request_workflow(
            owner=owner,
            repo=repo_name,
            title=payload.title,
            body=payload.body,
            changes=[c.model_dump() for c in payload.changes],
            base_branch=payload.base_branch,
            new_branch=payload.new_branch,
            approved=payload.approved,
            token=token,
        )
        return result
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


class AutoPullRequestPayload(BaseModel):
    repo: str = Field(..., description="Repository as 'owner/repo'")
    title: Optional[str] = Field(None, description="Pull Request title")
    summary: Optional[str] = Field(None, description="Short description placed at the top of the PR body")
    findings: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Scan findings/recommendations ({file, line, severity, title, explanation, ...})",
    )
    approved: bool = Field(False, description="Must be True after the developer approves the draft")
    github_token: Optional[str] = Field(None, description="Optional user GitHub token overriding server env")


@router.post("/auto-pr")
def create_auto_pull_request(
    payload: AutoPullRequestPayload,
    x_github_token: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """
    Generate fixes for scan findings, commit them to a new branch, and open a Pull Request.
    Requires explicit developer approval (`approved=True`).
    """
    if "/" not in payload.repo:
        raise HTTPException(status_code=400, detail="'repo' must be in 'owner/repo' form.")
    owner, repo_name = payload.repo.split("/", 1)

    if not payload.approved:
        raise HTTPException(
            status_code=400,
            detail="Pull Request creation blocked: Developer must review and approve changes first (approved=True).",
        )

    token = (payload.github_token or x_github_token or settings.GITHUB_TOKEN or "").strip()
    if not token:
        raise HTTPException(
            status_code=401,
            detail="A GitHub token is required to open Pull Requests. Add GITHUB_TOKEN to backend/.env or set one in the extension settings.",
        )

    try:
        return create_automated_pull_request(
            owner=owner,
            repo=repo_name,
            findings=payload.findings,
            title=payload.title,
            summary=payload.summary,
            approved=True,
            token=token,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except AutoPrError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

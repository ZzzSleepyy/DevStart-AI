import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from fastapi import APIRouter, Header
from pydantic import BaseModel, Field

from services.gemini_service import chat_with_assistant
from services.github_service import fetch_repo_context, parse_repo_identifier
from services.semgrep_service import run_static_analysis

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Chat Assistant"])

# In-memory cache of recently fetched repositories so consecutive chat turns stay fast
# and well within the extension's 20s timeout and GitHub API rate limits.
_REPO_CACHE: Dict[str, Tuple[float, Dict[str, Any], Dict[str, Any]]] = {}
_CACHE_TTL_SECONDS = 300.0


class ChatTurn(BaseModel):
    role: str = "user"
    content: str = ""


class ChatRequest(BaseModel):
    message: str = Field(..., description="Developer message or prompt from the extension")
    mode: str = Field("understand", description="Current workflow stage in the extension")
    history: List[ChatTurn] = Field(default_factory=list, description="Recent conversation turns")
    repo: Optional[str] = Field(None, description="Optional explicit 'owner/repo' context")


def _resolve_repo_from_conversation(
    message: str, history: List[ChatTurn], explicit_repo: Optional[str] = None
) -> Optional[Tuple[str, str]]:
    """Find the active (owner, repo) from explicit field, current message, or recent chat history."""
    if explicit_repo:
        parsed = parse_repo_identifier(explicit_repo)
        if parsed:
            return parsed

    parsed = parse_repo_identifier(message)
    if parsed:
        return parsed

    for turn in reversed(history):
        parsed = parse_repo_identifier(turn.content)
        if parsed:
            return parsed

    return None


def _get_cached_or_fetch_repo(
    owner: str, repo: str, token: Optional[str] = None
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Return (repo_context, static_analysis) using a 5-minute in-memory cache."""
    key = f"{owner}/{repo}".lower()
    now = time.time()
    if key in _REPO_CACHE:
        ts, ctx, static_res = _REPO_CACHE[key]
        if now - ts < _CACHE_TTL_SECONDS:
            return ctx, static_res

    try:
        ctx = fetch_repo_context(owner, repo, token=token)
        source_files = ctx.get("source_files", {})
        static_res = run_static_analysis(source_files)
        _REPO_CACHE[key] = (now, ctx, static_res)
        return ctx, static_res
    except Exception as exc:
        logger.debug("Could not enrich chat with GitHub repo %s/%s: %s", owner, repo, exc)
        return None, None


def _build_recommendations_and_pr_draft(
    repo_name: str,
    static_analysis: Optional[Dict[str, Any]],
    ai_reply: str = "",
    repo_context: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Build structured recommendations and PR draft from static analysis findings and AI insights."""
    findings = (static_analysis or {}).get("findings", [])
    counts = dict(
        (static_analysis or {}).get(
            "vulnerabilities", {"critical": 0, "high": 0, "medium": 0, "low": 0}
        )
    )
    recommendations: List[Dict[str, Any]] = []
    for idx, finding in enumerate(findings[:10], start=1):
        recommendations.append(
            {
                "id": f"rec-{idx}",
                "file": finding["file"],
                "line": finding["line"],
                "severity": finding["severity"],
                "category": "security" if finding["severity"] in ("critical", "high") else "quality",
                "title": finding["rule_id"].split(".")[-1].replace("-", " ").title(),
                "explanation": f"{finding['message']} {finding['suggestion']}",
                "original_code": finding.get("code_snippet", ""),
                "suggested_code": f"# Recommended fix: {finding['suggestion']}\n{finding.get('code_snippet', '')}",
            }
        )

    # Extract files mentioned in backticks in the AI reply, or fall back to scanned repo files
    ai_files = list(
        dict.fromkeys(
            m
            for m in re.findall(r"`([a-zA-Z0-9_./-]+\.[a-zA-Z0-9]{1,8})`", ai_reply or "")
            if not m.startswith("http")
        )
    )
    scanned_files = list(((repo_context or {}).get("source_files") or {}).keys())
    affected_files = list(
        dict.fromkeys([r["file"] for r in recommendations] + ai_files + scanned_files[:4])
    )

    # Extract concise sentences from the AI reply if static recommendations are empty
    ai_sentences = [
        s.strip()
        for s in re.split(r"(?<=[.!?])\s+", (ai_reply or "").replace("\n", " "))
        if 20 < len(s.strip()) < 180
    ]

    changes = (
        [f"{r['title']} in {r['file']} (line {r['line']})" for r in recommendations[:5]]
        or ai_sentences[:4]
        or [f"Inspected {repo_name} source files ({', '.join(affected_files[:3]) or 'main branch'})"]
    )

    pr_draft = {
        "repo": repo_name,
        "title": f"Harden {repo_name}",
        "summary": (
            f"Automatic inspection found {len(findings)} static issue(s) in {repo_name}."
            if findings
            else (ai_sentences[0] if ai_sentences else f"Automatic inspection completed for {repo_name}.")
        ),
        "why": "Groups the highest-priority security and code quality findings into one reviewable change before they reach production.",
        "scope": f"Targeted fixes across {', '.join(affected_files[:4]) or 'repository source files'}.",
        "vulnerabilities": counts,
        "files": affected_files[:6],
        "changes": changes,
    }
    return recommendations, pr_draft


@router.post("/chat")
def chat_endpoint(
    payload: ChatRequest,
    x_github_token: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """
    Primary chat & automatic inspection endpoint used by the DevStart AI Chrome Extension.
    Contract:
      Request:  { "message": str, "mode": str, "history": [{ "role": str, "content": str }], "repo"?: str }
      Response: { "reply": str, "mode": str, "stage": str, "vulnerabilities": {...}, "pr_draft": {...} }
    """
    repo_context = None
    static_analysis = None

    repo_tuple = _resolve_repo_from_conversation(
        payload.message, payload.history, payload.repo
    )
    if repo_tuple:
        owner, repo = repo_tuple
        repo_context, static_analysis = _get_cached_or_fetch_repo(
            owner, repo, token=x_github_token
        )

    # Also scan inline code in the user's message if no repo findings exist yet
    if not static_analysis or static_analysis.get("total_issues", 0) == 0:
        inline_scan = run_static_analysis({"message_snippet.py": payload.message})
        if inline_scan.get("total_issues", 0) > 0:
            static_analysis = inline_scan

    result = chat_with_assistant(
        message=payload.message,
        mode=payload.mode,
        history=[turn.model_dump() for turn in payload.history],
        repo_context=repo_context,
        static_analysis=static_analysis,
    )

    full_name = f"{repo_tuple[0]}/{repo_tuple[1]}" if repo_tuple else "repository"
    recommendations, pr_draft = _build_recommendations_and_pr_draft(
        full_name,
        static_analysis,
        ai_reply=result.get("reply", ""),
        repo_context=repo_context,
    )
    vulnerabilities = dict(pr_draft["vulnerabilities"])
    ai_vulns = result.get("ai_vulnerabilities")
    if isinstance(ai_vulns, dict):
        for sev in ("critical", "high", "medium", "low"):
            vulnerabilities[sev] = max(
                int(vulnerabilities.get(sev, 0)), int(ai_vulns.get(sev, 0))
            )
        pr_draft["vulnerabilities"] = vulnerabilities

    return {
        "reply": result["reply"],
        "mode": result["mode"],
        "stage": result["mode"],
        "vulnerabilities": vulnerabilities,
        "pr_draft": pr_draft,
        "recommendations": recommendations,
    }

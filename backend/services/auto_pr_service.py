"""
Automated Pull Request generation.

Turns scan findings into concrete, reviewable file edits and opens a Pull
Request with them. Edits are produced as exact find/replace pairs so they can
be applied to the real file content without ever sending unredacted code to
the model or committing redaction placeholders back to the repository.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from services.gemini_service import _call_gemini, _extract_json
from services.github_service import (
    create_pull_request_workflow,
    fetch_repo_context,
    get_file_content,
    get_raw_file,
    get_repo_metadata,
)
from services.security_utils import is_ignored_path, is_sensitive_path
from services.semgrep_service import run_static_analysis

logger = logging.getLogger(__name__)

MAX_FILES_PER_PR = 5
REDACTION_MARKER = re.compile(r"\[REDACTED_[A-Z_]+\]")
# Placeholder "fixes" produced by the static fallback are not real code changes.
PLACEHOLDER_FIX = re.compile(r"^\s*#\s*Recommended fix:", re.MULTILINE)


class AutoPrError(Exception):
    """Raised when no Pull Request can be produced, with a user-facing reason."""


def _normalize_findings(findings: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    by_file: Dict[str, List[Dict[str, Any]]] = {}
    for item in findings or []:
        path = str(item.get("file") or "").strip().lstrip("/")
        if not path or is_sensitive_path(path) or is_ignored_path(path):
            continue
        by_file.setdefault(path, []).append(item)
    return by_file


def _apply_edit(content: str, find: str, replace: str) -> Optional[str]:
    """Apply one exact replacement; tolerate CRLF files. Returns None if not applicable."""
    if not find or find == replace:
        return None
    if REDACTION_MARKER.search(find) or REDACTION_MARKER.search(replace):
        return None
    if "\r\n" in content:
        # Match the file's line endings so the diff only shows the real change.
        find = find.replace("\r\n", "\n").replace("\n", "\r\n")
        replace = replace.replace("\r\n", "\n").replace("\n", "\r\n")
    if content.count(find) == 1:
        return content.replace(find, replace, 1)
    return None


def _edits_from_recommendations(findings: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    edits = []
    for item in findings:
        original = item.get("original_code") or ""
        suggested = item.get("suggested_code") or ""
        if original and suggested and original != suggested and not PLACEHOLDER_FIX.search(suggested):
            edits.append({"find": original, "replace": suggested, "reason": item.get("title", "")})
    return edits


def _edits_from_gemini(path: str, redacted_content: str, findings: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    issues = [
        {
            "line": f.get("line"),
            "severity": f.get("severity"),
            "title": f.get("title") or f.get("rule_id"),
            "detail": f.get("explanation") or f.get("message"),
        }
        for f in findings[:8]
    ]
    prompt = f"""You are fixing security and code quality issues in `{path}`.

Issues found by the scanner:
{issues}

Full file content:
<<<FILE
{redacted_content[:20000]}
FILE>>>

Return ONLY JSON in this exact shape:
{{"edits": [{{"find": "exact text copied from the file", "replace": "fixed text", "reason": "short reason"}}]}}

Rules:
- "find" must be copied verbatim from the file (same whitespace and indentation) and must occur exactly once.
- Keep each edit minimal: only the lines needed to fix the issue.
- Never include text like [REDACTED_...] in "find" or "replace". If a fix would require it, read the secret from an environment variable instead and leave that line out of "find".
- Do not change unrelated code. If an issue cannot be fixed safely, omit it.
"""
    text = _call_gemini(prompt, max_output_tokens=4096)
    parsed = _extract_json(text) if text else None
    edits = (parsed or {}).get("edits") if isinstance(parsed, dict) else None
    if not isinstance(edits, list):
        return []
    return [
        {"find": str(e.get("find", "")), "replace": str(e.get("replace", "")), "reason": str(e.get("reason", ""))}
        for e in edits
        if isinstance(e, dict)
    ]


def build_file_changes(
    owner: str,
    repo: str,
    branch: str,
    findings_by_file: Dict[str, List[Dict[str, Any]]],
    token: Optional[str] = None,
) -> Tuple[List[Dict[str, str]], List[str]]:
    """Return (changes for create_pull_request_workflow, human-readable change notes)."""
    changes: List[Dict[str, str]] = []
    notes: List[str] = []

    for path, findings in list(findings_by_file.items())[:MAX_FILES_PER_PR]:
        raw = get_raw_file(owner, repo, path, ref=branch, token=token)
        if raw is None:
            # The finding refers to something that is not a file in this repo.
            continue

        candidate_edits = _edits_from_recommendations(findings)
        redacted = get_file_content(owner, repo, path, ref=branch, token=token) or ""
        if redacted:
            candidate_edits += _edits_from_gemini(path, redacted, findings)

        updated = raw
        applied_reasons: List[str] = []
        for edit in candidate_edits:
            result = _apply_edit(updated, edit["find"], edit["replace"])
            if result is not None:
                updated = result
                if edit.get("reason"):
                    applied_reasons.append(edit["reason"])

        if updated != raw:
            reason = applied_reasons[0] if applied_reasons else "security and quality fixes"
            changes.append(
                {
                    "path": path,
                    "content": updated,
                    "commit_message": f"DevStart AI: {reason} in {path}"[:120],
                }
            )
            notes.extend(f"`{path}`: {r}" for r in (applied_reasons or [reason]))

    return changes, notes


def create_automated_pull_request(
    owner: str,
    repo: str,
    findings: Optional[List[Dict[str, Any]]] = None,
    title: Optional[str] = None,
    summary: Optional[str] = None,
    approved: bool = False,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    if not approved:
        raise PermissionError("Pull Request creation requires explicit user approval (approved=True).")

    metadata = get_repo_metadata(owner, repo, token=token)
    branch = metadata.get("default_branch", "main")

    findings_by_file = _normalize_findings(findings or [])
    if not findings_by_file:
        # Nothing usable from the client: rescan the repository on the server.
        context = fetch_repo_context(owner, repo, token=token)
        static = run_static_analysis(context.get("source_files", {}))
        findings_by_file = _normalize_findings(static.get("findings", []))

    if not findings_by_file:
        raise AutoPrError("The scan found no issues in repository files, so there is nothing to put in a Pull Request.")

    changes, notes = build_file_changes(owner, repo, branch, findings_by_file, token=token)
    if not changes:
        raise AutoPrError(
            "No safe code changes could be generated for the findings. "
            "Make sure GEMINI_API_KEY is configured on the backend, or fix the issues manually."
        )

    full_name = f"{owner}/{repo}"
    body_lines = [
        summary or f"Automated fixes for issues found by DevStart AI in {full_name}.",
        "",
        "### Changes",
        *[f"- {n}" for n in notes],
        "",
        "_Generated by DevStart AI after explicit approval. Please review before merging._",
    ]
    return create_pull_request_workflow(
        owner=owner,
        repo=repo,
        title=title or f"DevStart AI: harden {full_name}",
        body="\n".join(body_lines),
        changes=changes,
        base_branch=branch,
        approved=True,
        token=token,
    )

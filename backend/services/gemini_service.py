import json
import logging
import re
from typing import Any, Dict, List, Optional
import requests

from config.settings import settings
from services.security_utils import redact_secrets

logger = logging.getLogger(__name__)

VALID_MODES = ("understand", "onboard", "analyze", "suggest", "pr")

MODE_SIGNALS = {
    "understand": [
        r"\b(what|how) (does|is|do)\b",
        r"\bmap\b",
        r"\bstructure\b",
        r"\borgani[sz]",
        r"\boverview\b",
        r"\barchitecture\b",
        r"\bentry points?\b",
        r"\bstart reading\b",
    ],
    "onboard": [
        r"\b(getting started|onboard(ing)?|explore)\b",
        r"\b(set ?up|install|dependenc|environment|env var|configure|checklist)\b",
        r"\bhow do i (run|start|install|set)\b",
    ],
    "analyze": [
        r"\b(bug|bugs|risk|risky|scan|inspect|analy[sz]e|smell|security|vulnerab|issue|lint)\b",
        r"\bwhat.*(wrong|risky|broken)\b",
    ],
    "suggest": [
        r"\b(insights?|suggest|improve|refactor|simplif|readab|optimi[sz]e|fix|clean|rewrite)\b",
        r"\bhow can i improve\b",
    ],
    "pr": [
        r"\bpull request\b",
        r"\b(open|create|draft|make) (a )?pr\b",
        r"\breview (the )?(changes|diff|pr)\b",
        r"\b(branch|branches|commit|merge|ship)\b",
    ],
}


def infer_mode(message: str, current_mode: str = "understand") -> str:
    """Infer the DevStart AI workflow mode from the user's message."""
    lower = (message or "").lower()
    best_mode = current_mode if current_mode in VALID_MODES else "understand"
    best_score = 0

    for mode in VALID_MODES:
        score = sum(1 for pat in MODE_SIGNALS[mode] if re.search(pat, lower))
        if score > best_score:
            best_score = score
            best_mode = mode

    return best_mode


def _call_gemini(
    prompt: str,
    system_instruction: Optional[str] = None,
    max_output_tokens: int = 1200,
) -> Optional[str]:
    """Call Google Gemini API via REST with automatic model fallback and strict timeouts."""
    api_key = settings.GEMINI_API_KEY
    if not api_key or api_key == "your_gemini_api_key":
        return None

    candidate_models = [
        settings.GEMINI_MODEL,
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-3.8-flash",
    ]
    models_to_try = list(dict.fromkeys(m.replace("models/", "") for m in candidate_models if m))
    safe_prompt = redact_secrets(prompt)

    payload: Dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": safe_prompt}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": max_output_tokens},
    }
    if system_instruction:
        payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

    for model_name in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
        try:
            resp = requests.post(
                url,
                params={"key": api_key},
                json=payload,
                timeout=12,
            )
            if not resp.ok:
                logger.debug("Gemini model %s returned HTTP %s: %s", model_name, resp.status_code, resp.text[:200])
                continue
            data = resp.json()
            candidates = data.get("candidates") or []
            if candidates:
                parts = (candidates[0].get("content") or {}).get("parts") or []
                texts = [p.get("text", "") for p in parts if p.get("text")]
                if texts:
                    return "\n".join(texts).strip()
        except Exception as model_exc:
            logger.debug("Gemini model %s failed: %s", model_name, model_exc)
            continue

    return None


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Extract a JSON object from a Gemini markdown or raw response."""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    raw = fenced.group(1) if fenced else text
    brace_match = re.search(r"\{[\s\S]*\}", raw)
    if not brace_match:
        return None
    try:
        return json.loads(brace_match.group(0))
    except Exception:
        return None


def generate_onboarding_guide(repo_context: Dict[str, Any]) -> Dict[str, Any]:
    """Generate a step-by-step onboarding checklist and repository overview."""
    meta = repo_context.get("metadata", {})
    repo_name = meta.get("full_name", "repository")
    docs = repo_context.get("docs", {})
    deps = repo_context.get("dependencies", {})
    tree = repo_context.get("file_tree", [])

    prompt = f"""You are DevStart AI, an AI-Powered Developer Assistant.
Analyze this repository and return a JSON object with a step-by-step developer onboarding checklist.

Repository: {repo_name}
Primary Language: {meta.get("language", "Unknown")}
Description: {meta.get("description", "")}
File Tree (sample): {json.dumps(tree[:50])}
Documentation: {json.dumps(docs)}
Dependency Files: {json.dumps(deps)}

Return ONLY valid JSON with this exact schema:
{{
  "summary": "2-3 sentence overview of what the project does and its architecture.",
  "entry_points": ["list of key files to read first"],
  "prerequisites": ["list of required tools/runtimes/keys"],
  "setup_checklist": [
    {{"step": 1, "title": "Step title", "command": "optional CLI command", "description": "What to do"}}
  ],
  "contribution_opportunities": ["list of concrete ways a developer can contribute"]
}}"""

    ai_text = _call_gemini(prompt)
    parsed = _extract_json(ai_text) if ai_text else None
    if parsed and "setup_checklist" in parsed:
        return parsed

    # Grounded fallback constructed from fetched repository metadata
    dep_files = list(deps.keys())
    entry_candidates = [
        p for p in tree if p.rsplit("/", 1)[-1] in {"main.py", "app.py", "index.js", "sidepanel.js", "server.js"}
    ][:5] or tree[:4]

    checklist = [
        {
            "step": 1,
            "title": "Clone and inspect the repository",
            "command": f"git clone https://github.com/{repo_name}.git",
            "description": f"Clone `{repo_name}` locally and switch to the `{meta.get('default_branch', 'main')}` branch.",
        },
        {
            "step": 2,
            "title": "Install project dependencies",
            "command": "pip install -r requirements.txt" if any("requirements.txt" in d for d in dep_files) else "npm install",
            "description": f"Detected dependency manifests: {', '.join(dep_files) if dep_files else 'standard runtime packages'}.",
        },
        {
            "step": 3,
            "title": "Configure environment variables",
            "command": "cp .env.example .env",
            "description": "Add required API keys and credentials to a local `.env` file without committing secrets.",
        },
        {
            "step": 4,
            "title": "Review entry points and run locally",
            "command": "",
            "description": f"Start reading from {', '.join(f'`{e}`' for e in entry_candidates)}.",
        },
    ]

    return {
        "summary": meta.get("description")
        or f"{repo_name} is a {meta.get('language', 'software')} project with {len(tree)} tracked files.",
        "entry_points": entry_candidates,
        "prerequisites": [meta.get("language", "Runtime"), "Git"],
        "setup_checklist": checklist,
        "contribution_opportunities": [
            "Improve input validation and error handling in API endpoints",
            "Add unit and integration test coverage for core workflows",
            "Enhance documentation and onboarding instructions",
        ],
    }


def analyze_code_with_ai(
    files: Dict[str, str],
    static_analysis: Dict[str, Any],
    repo_name: str = "repository",
) -> Dict[str, Any]:
    """
    Combine Semgrep static analysis with Gemini AI code inspection to produce:
    - Detailed findings (file, line, severity, explanation)
    - Proposed code improvements (original_code -> suggested_code)
    - Vulnerability counts (critical, high, medium, low)
    - A Pull Request draft ready for user review
    """
    semgrep_findings = static_analysis.get("findings", [])
    counts = dict(static_analysis.get("vulnerabilities", {"critical": 0, "high": 0, "medium": 0, "low": 0}))

    prompt = f"""You are DevStart AI, a senior security and code quality reviewer.
Examine the following source files from `{repo_name}` and the static analysis results from Semgrep.
Identify bugs, inefficient code, security concerns, and code quality improvements.
For each recommendation, explain the problem clearly and provide a concrete code modification (`original_code` and `suggested_code`) that a developer can review and approve for a Pull Request.

Semgrep Static Analysis Findings:
{json.dumps(semgrep_findings[:15], indent=2)}

Source Files (Batch):
{json.dumps({k: v[:4000] for k, v in list(files.items())[:6]}, indent=2)}

Return ONLY valid JSON matching this schema:
{{
  "summary": "Brief executive summary of code health and key findings.",
  "vulnerabilities": {{"critical": 0, "high": 0, "medium": 0, "low": 0}},
  "recommendations": [
    {{
      "id": "rec-1",
      "file": "path/to/file",
      "line": 10,
      "severity": "high",
      "category": "security | bug | performance | quality",
      "title": "Short issue title",
      "explanation": "Why this is an issue and how the fix improves the project.",
      "original_code": "exact snippet before change",
      "suggested_code": "improved replacement snippet"
    }}
  ],
  "pr_draft": {{
    "repo": "{repo_name}",
    "title": "Harden {repo_name}",
    "summary": "One-sentence summary of the grouped improvements.",
    "why": "Why these changes matter for reliability and security.",
    "scope": "Summary of affected files and scope.",
    "changes": ["Bullet 1", "Bullet 2", "Bullet 3"]
  }}
}}"""

    ai_text = _call_gemini(prompt)
    parsed = _extract_json(ai_text) if ai_text else None

    if parsed and isinstance(parsed.get("recommendations"), list):
        ai_vulns = parsed.get("vulnerabilities") or {}
        merged_counts = {
            sev: max(int(counts.get(sev, 0)), int(ai_vulns.get(sev, 0)))
            for sev in ("critical", "high", "medium", "low")
        }
        pr_draft = parsed.get("pr_draft") or {}
        pr_draft["repo"] = repo_name
        pr_draft["vulnerabilities"] = merged_counts
        parsed["vulnerabilities"] = merged_counts
        parsed["pr_draft"] = pr_draft
        parsed["static_analysis"] = static_analysis
        return parsed

    # Fallback: build structured recommendations from static analysis + file inspection
    recommendations: List[Dict[str, Any]] = []
    for idx, finding in enumerate(semgrep_findings[:10], start=1):
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

    # If no static rules matched, provide a constructive code quality recommendation on the first file
    if not recommendations and files:
        first_file = next(iter(files.keys()))
        first_lines = files[first_file].splitlines()[:8]
        snippet = "\n".join(first_lines)
        counts["low"] = max(counts.get("low", 0), 1)
        recommendations.append(
            {
                "id": "rec-1",
                "file": first_file,
                "line": 1,
                "severity": "low",
                "category": "quality",
                "title": "Add input validation and structured error logging",
                "explanation": f"Adding explicit validation and error boundaries in `{first_file}` improves maintainability and prevents unhandled runtime exceptions.",
                "original_code": snippet,
                "suggested_code": snippet,
            }
        )

    affected_files = list(dict.fromkeys(r["file"] for r in recommendations))
    pr_draft = {
        "repo": repo_name,
        "title": f"Harden {repo_name}",
        "summary": f"Automatic inspection found {len(recommendations)} improvement opportunity(s) in {repo_name}.",
        "why": "Groups the highest-priority security and code quality findings into one reviewable change before they reach production.",
        "scope": f"Targeted fixes across {', '.join(affected_files[:4]) or 'repository source files'}.",
        "vulnerabilities": counts,
        "changes": [
            f"{r['title']} in `{r['file']}` (line {r['line']})" for r in recommendations[:5]
        ]
        or ["Repository inspection findings were reviewed automatically"],
    }

    return {
        "summary": f"Analyzed {len(files)} file(s) in `{repo_name}` and identified {len(recommendations)} actionable recommendation(s).",
        "vulnerabilities": counts,
        "recommendations": recommendations,
        "pr_draft": pr_draft,
        "static_analysis": static_analysis,
    }


def chat_with_assistant(
    message: str,
    mode: str = "understand",
    history: Optional[List[Dict[str, str]]] = None,
    repo_context: Optional[Dict[str, Any]] = None,
    static_analysis: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Process a chat message from the Chrome Extension panel (`POST /api/chat`).
    Returns `{"reply": "...", "mode": "..."}` matching `extension/sidepanel/sidepanel.js`.
    """
    next_mode = infer_mode(message, mode)
    history = history or []

    # Build context block if repository data was fetched
    context_sections: List[str] = []
    if repo_context:
        meta = repo_context.get("metadata", {})
        context_sections.append(
            f"Repository: {meta.get('full_name')} ({meta.get('language')}) - {meta.get('description')}"
        )
        if repo_context.get("file_tree"):
            context_sections.append(
                f"Files in repo (sample): {', '.join(repo_context['file_tree'][:40])}"
            )
        if repo_context.get("docs"):
            for doc_name, doc_text in repo_context["docs"].items():
                context_sections.append(f"--- {doc_name} ---\n{doc_text[:2500]}")
        if repo_context.get("dependencies"):
            for dep_name, dep_text in repo_context["dependencies"].items():
                context_sections.append(f"--- {dep_name} ---\n{dep_text[:1500]}")
        if repo_context.get("source_files"):
            for src_name, src_text in list(repo_context["source_files"].items())[:4]:
                context_sections.append(f"--- {src_name} ---\n{src_text[:2000]}")

    if static_analysis and static_analysis.get("findings"):
        context_sections.append(
            f"Semgrep Static Analysis Findings:\n{json.dumps(static_analysis['findings'][:10], indent=2)}"
        )

    history_str = "\n".join(
        f"{turn.get('role', 'user').upper()}: {turn.get('content', '')}"
        for turn in history[-6:]
    )

    system_instruction = (
        "You are DevStart AI, an AI-powered developer assistant inside a Chrome Extension for GitHub. "
        "Keep answers concise, practical, and directly tied to specific file names wrapped in backticks "
        "(e.g. `backend/main.py`) because the UI formats inline backticks as code badges. "
        "Avoid markdown headers (#) or multi-line code blocks if a concise explanation with inline `code` references is clearer. "
        "Never suggest automatically merging changes without developer approval. "
        "IMPORTANT: At the very end of your response, on a new line, always output the exact tag "
        "[VULNERABILITIES: critical=0, high=0, medium=0, low=0] replacing the zeros with the actual count of "
        "critical, high, medium, and low bugs, syntax errors, security issues, or code smells you found in the repository files or message."
    )

    prompt = f"""Current Workflow Stage: {next_mode}
Conversation History:
{history_str}

Repository & Static Analysis Context:
{chr(10).join(context_sections) if context_sections else "No specific repository fetched yet."}

Developer Message:
{message}

Respond helpfully and concisely (2-5 sentences), referencing specific files in backticks where applicable, and end with [VULNERABILITIES: critical=X, high=Y, medium=Z, low=W]:"""

    ai_reply = _call_gemini(prompt, system_instruction=system_instruction)
    if ai_reply:
        ai_vulns = None
        vuln_match = re.search(
            r"\[VULNERABILITIES:\s*critical=(\d+)\s*,\s*high=(\d+)\s*,\s*medium=(\d+)\s*,\s*low=(\d+)\s*\]",
            ai_reply,
            flags=re.IGNORECASE,
        )
        if vuln_match:
            ai_vulns = {
                "critical": int(vuln_match.group(1)),
                "high": int(vuln_match.group(2)),
                "medium": int(vuln_match.group(3)),
                "low": int(vuln_match.group(4)),
            }
            ai_reply = re.sub(
                r"\s*\[VULNERABILITIES:[^\]]*\]\s*", "", ai_reply, flags=re.IGNORECASE
            )
        # Clean up excessive markdown headings so it renders cleanly in sidepanel's formatInline
        cleaned = re.sub(r"^#+\s*", "", ai_reply, flags=re.MULTILINE).strip()
        return {"reply": cleaned, "mode": next_mode, "ai_vulnerabilities": ai_vulns}

    # Grounded fallback using live repo_context + static_analysis when GEMINI_API_KEY is not set
    if repo_context:
        meta = repo_context.get("metadata", {})
        repo_name = meta.get("full_name", "the repository")
        files = list((repo_context.get("source_files") or {}).keys())
        deps = list((repo_context.get("dependencies") or {}).keys())
        findings = (static_analysis or {}).get("findings", [])

        if next_mode == "understand":
            file_list = ", ".join(f"`{f}`" for f in files[:4]) or "`README.md`"
            desc = meta.get("description") or f"a {meta.get('language', 'software')} project"
            return {
                "reply": f"`{repo_name}` is {desc}. Key entry points and source files include {file_list} across {repo_context.get('total_files_in_repo', len(files))} tracked files.",
                "mode": next_mode,
            }
        if next_mode == "onboard":
            dep_str = ", ".join(f"`{d}`" for d in deps) if deps else "the project's dependency manifest"
            return {
                "reply": f"To set up `{repo_name}`: clone the `{meta.get('default_branch', 'main')}` branch, install dependencies from {dep_str}, configure your `.env` variables, and run the main entry point.",
                "mode": next_mode,
            }
        if next_mode == "analyze":
            if findings:
                top = findings[:3]
                summary = "; ".join(
                    f"`{f['file']}` (line {f['line']}, {f['severity']}): {f['message']}"
                    for f in top
                )
                return {
                    "reply": f"Scanned `{repo_name}` ({len(files)} files) and found {len(findings)} issue(s): {summary}",
                    "mode": next_mode,
                }
            file_list = ", ".join(f"`{f}`" for f in files[:3]) or "repository files"
            return {
                "reply": f"Inspected {file_list} in `{repo_name}`. No critical vulnerabilities were triggered by static rules; consider adding stricter input validation and test coverage.",
                "mode": next_mode,
            }
        if next_mode == "suggest":
            if findings:
                first = findings[0]
                return {
                    "reply": f"Priority fix in `{first['file']}` (line {first['line']}): {first['suggestion']} Addressing this first reduces `{first['severity']}`-severity risk before opening a PR.",
                    "mode": next_mode,
                }
            target = f"`{files[0]}`" if files else "`main.py`"
            return {
                "reply": f"Start by isolating validation and error handling in {target} so each route and service can be tested independently before you open a pull request.",
                "mode": next_mode,
            }
        if next_mode == "pr":
            return {
                "reply": f"Prepared a reviewable PR draft for `{repo_name}` grouping {len(findings)} finding(s). Review the affected files in the Branches tab—no branch or PR is created on GitHub until you explicitly approve.",
                "mode": next_mode,
            }

    # Generic fallback when no repo is mentioned and no Gemini key is configured
    default_replies = {
        "understand": "This project splits into a `Chrome extension` and a `FastAPI backend`. Open a GitHub repository tab or include `owner/repo` so I can map its exact files.",
        "onboard": "Create a virtual environment, install `backend/requirements.txt`, configure `GEMINI_API_KEY` and `GITHUB_TOKEN` in `backend/.env`, and start the server with `uvicorn main:app --reload`.",
        "analyze": "Provide a repository name (`owner/repo`) or code snippet and I will run `Semgrep` static checks alongside `Gemini` analysis, tying every finding to its file and line number.",
        "suggest": "I can generate concrete before/after code replacements for any flagged file so you can review and edit them before creating a pull request.",
        "pr": "Once you review and approve the suggested changes, use the Pull Request endpoint or button to create a new branch and submit a PR via the GitHub API.",
    }
    return {
        "reply": default_replies.get(next_mode, default_replies["understand"]),
        "mode": next_mode,
    }

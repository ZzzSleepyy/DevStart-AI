import ast
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

BUNDLED_RULES_PATH = Path(__file__).resolve().parent / "semgrep_rules.yaml"

# Comprehensive static analysis rule patterns modeled on Semgrep Community rules
STATIC_RULES: List[Dict[str, Any]] = [
    {
        "id": "security.code-execution.eval-exec",
        "pattern": re.compile(r"(?<!['\"\w])\b(eval|exec)\s*\((?!\s*\))"),
        "severity": "critical",
        "message": "Dynamic code execution via eval()/exec() can lead to remote code execution (RCE).",
        "suggestion": "Replace eval()/exec() with safe parsing (e.g., json.loads() or ast.literal_eval()).",
        "languages": {".py", ".js", ".ts", ".jsx", ".tsx"},
    },
    {
        "id": "security.subprocess.shell-true",
        "pattern": re.compile(r"subprocess\.(run|Popen|call|check_output)\s*\(.*shell\s*=\s*True"),
        "severity": "high",
        "message": "Subprocess invoked with shell=True is vulnerable to command injection if user input is included.",
        "suggestion": "Pass command arguments as a list and set shell=False.",
        "languages": {".py"},
    },
    {
        "id": "security.os-system.command-injection",
        "pattern": re.compile(r"(?<!['\"\w])\bos\.(system|popen)\s*\((?!\s*\))"),
        "severity": "high",
        "message": "Executing shell commands via os.system()/os.popen() is prone to command injection.",
        "suggestion": "Use subprocess.run([...], shell=False, check=True) instead.",
        "languages": {".py"},
    },
    {
        "id": "security.deserialization.unsafe-load",
        "pattern": re.compile(r"\b(pickle\.loads?|yaml\.load\s*\([^)]*(?!Loader\s*=\s*yaml\.SafeLoader))\b"),
        "severity": "critical",
        "message": "Unsafe deserialization (pickle or yaml.load without SafeLoader) can execute arbitrary code.",
        "suggestion": "Use json.loads() or yaml.safe_load() for untrusted input.",
        "languages": {".py"},
    },
    {
        "id": "security.xss.innerhtml",
        "pattern": re.compile(r"(\.innerHTML\s*=(?![\s]*['\"]\s*['\"]\s*(?:;|$))|dangerouslySetInnerHTML)"),
        "severity": "medium",
        "message": "Direct HTML injection via innerHTML or dangerouslySetInnerHTML can introduce DOM-based XSS if unescaped input is rendered.",
        "suggestion": "Use textContent, createElement(), or sanitize HTML before rendering.",
        "languages": {".js", ".ts", ".jsx", ".tsx", ".html"},
    },
    {
        "id": "security.sql.string-formatting",
        "pattern": re.compile(
            r"(?i)(SELECT|INSERT|UPDATE|DELETE)\s+[\w\*\,\s]+FROM\s+[\w]+.*(%s|\+\s*\w+|\{[a-zA-Z_])"
        ),
        "severity": "critical",
        "message": "Potential SQL query constructed via string interpolation or concatenation.",
        "suggestion": "Use parameterized queries or prepared statements provided by your database driver.",
        "languages": {".py", ".js", ".ts", ".java", ".php", ".rb", ".go"},
    },
    {
        "id": "security.crypto.weak-hash",
        "pattern": re.compile(r"\b(hashlib\.(md5|sha1)|crypto\.createHash\(['\"](md5|sha1)['\"]\))\b"),
        "severity": "medium",
        "message": "Weak cryptographic hash algorithm (MD5/SHA-1) detected.",
        "suggestion": "Use SHA-256 (hashlib.sha256) or bcrypt/argon2 for password hashing.",
        "languages": {".py", ".js", ".ts"},
    },
    {
        "id": "security.cors.wildcard-credentials",
        "pattern": re.compile(r"allow_origins\s*=\s*\[\s*['\"]\*['\"]\s*\]"),
        "severity": "medium",
        "message": "Wildcard CORS origin ('*') allows any website to make cross-origin requests to the API.",
        "suggestion": "Restrict allow_origins or set allow_credentials=False when using '*'.",
        "languages": {".py", ".js", ".ts"},
    },
    {
        "id": "quality.exception.bare-except",
        "pattern": re.compile(r"^\s*except\s*:\s*$"),
        "severity": "low",
        "message": "Bare 'except:' catches SystemExit and KeyboardInterrupt and hides unexpected errors.",
        "suggestion": "Catch specific exceptions (e.g., 'except Exception as exc:') and log the error.",
        "languages": {".py"},
    },
    {
        "id": "quality.exception.empty-catch",
        "pattern": re.compile(r"catch\s*(?:\([^)]*\))?\s*\{\s*(?:/\*.*?\*/|//.*)?\s*\}"),
        "severity": "low",
        "message": "Silent catch block swallows errors and makes debugging difficult.",
        "suggestion": "Log or handle the caught error explicitly.",
        "languages": {".js", ".ts", ".jsx", ".tsx", ".java"},
    },
    {
        "id": "security.http.missing-timeout",
        "pattern": re.compile(r"\brequests\.(get|post|put|delete|patch)\s*\((?!.*timeout\s*=).+\)"),
        "severity": "medium",
        "message": "HTTP request via requests library without an explicit timeout can hang indefinitely.",
        "suggestion": "Add a timeout parameter (e.g., timeout=10) to all requests calls.",
        "languages": {".py"},
    },
    {
        "id": "bug.division-by-zero",
        "pattern": re.compile(r"(?<![:/])\s*/\s*0(?![.0-9xX])"),
        "severity": "high",
        "message": "Potential division by zero detected, which will raise a runtime exception.",
        "suggestion": "Validate that the divisor is non-zero before dividing.",
        "languages": {".py", ".js", ".ts", ".java", ".c", ".cpp", ".go", ".rs"},
    },
    {
        "id": "quality.python.mutable-default-arg",
        "pattern": re.compile(r"^\s*def\s+\w+\s*\([^)]*=\s*(\[\]|\{\}|set\(\))"),
        "severity": "medium",
        "message": "Mutable default argument in function definition is shared across all calls and causes state bugs.",
        "suggestion": "Use None as the default value and initialize the collection inside the function.",
        "languages": {".py"},
    },
    {
        "id": "quality.todo-fixme-marker",
        "pattern": re.compile(r"^\s*(#|//|/\*)\s*(TODO|FIXME|BUG|HACK|XXX)\b"),
        "severity": "low",
        "message": "Unresolved TODO/FIXME/BUG marker indicates incomplete or known-fragile logic.",
        "suggestion": "Resolve the unfinished logic or track it as an issue before release.",
        "languages": {".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs", ".c", ".cpp"},
    },
    {
        "id": "security.postmessage.wildcard-origin",
        "pattern": re.compile(r"\.postMessage\s*\([^)]*,\s*['\"]\*['\"]\s*\)"),
        "severity": "medium",
        "message": "window.postMessage called with wildcard targetOrigin ('*') can leak messages to untrusted frames.",
        "suggestion": "Specify an exact targetOrigin instead of '*'.",
        "languages": {".js", ".ts", ".jsx", ".tsx"},
    },
]

SEMGREP_SEVERITY_MAP = {
    "ERROR": "high",
    "CRITICAL": "critical",
    "HIGH": "high",
    "WARNING": "medium",
    "MEDIUM": "medium",
    "INFO": "low",
    "LOW": "low",
}


def _find_semgrep_binary() -> str | None:
    """Locate the semgrep executable in venv or PATH (skipped on Windows unless SEMGREP_CLI_ENABLED=1)."""
    if sys.platform == "win32" and os.getenv("SEMGREP_CLI_ENABLED", "").lower() not in ("1", "true", "yes"):
        return None

    venv_scripts = Path(sys.executable).parent
    for candidate in [
        venv_scripts / "semgrep.exe",
        venv_scripts / "semgrep",
        shutil.which("semgrep"),
    ]:
        if candidate and Path(str(candidate)).exists():
            return str(candidate)
    return None


def _run_semgrep_cli(files: Dict[str, str]) -> List[Dict[str, Any]]:
    """Run Semgrep CLI using bundled Semgrep YAML rules so it executes fast offline on Linux/Render."""
    semgrep_bin = _find_semgrep_binary()
    if not semgrep_bin or not files or not BUNDLED_RULES_PATH.exists():
        return []

    findings: List[Dict[str, Any]] = []
    try:
        with tempfile.TemporaryDirectory(prefix="devstart_semgrep_") as tmpdir:
            tmp_root = Path(tmpdir)
            for rel_path, content in files.items():
                safe_rel = rel_path.lstrip("/\\").replace("..", "__")
                dest = tmp_root / safe_rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content, encoding="utf-8", errors="replace")

            cmd = [
                semgrep_bin,
                "scan",
                f"--config={BUNDLED_RULES_PATH}",
                "--metrics=off",
                "--json",
                "--quiet",
                "--timeout=5",
                str(tmp_root),
            ]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
                env={**os.environ, "SEMGREP_SEND_METRICS": "off"},
            )
            if proc.stdout:
                data = json.loads(proc.stdout)
                for res in data.get("results", []):
                    raw_path = res.get("path", "")
                    try:
                        rel = Path(raw_path).resolve().relative_to(tmp_root.resolve()).as_posix()
                    except Exception:
                        rel = Path(raw_path).name

                    extra = res.get("extra", {})
                    raw_sev = str(extra.get("severity", "WARNING")).upper()
                    check_id = res.get("check_id", "semgrep.finding")
                    if "eval-exec" in check_id or "unsafe-load" in check_id:
                        severity = "critical"
                    else:
                        severity = SEMGREP_SEVERITY_MAP.get(raw_sev, "medium")

                    findings.append(
                        {
                            "rule_id": check_id,
                            "file": rel,
                            "line": res.get("start", {}).get("line", 1),
                            "end_line": res.get("end", {}).get("line", 1),
                            "severity": severity,
                            "message": extra.get("message", "Potential issue detected by Semgrep."),
                            "code_snippet": (extra.get("lines") or "").strip(),
                            "suggestion": extra.get("fix") or "Review and refactor the flagged pattern.",
                            "source": "semgrep",
                        }
                    )
    except Exception as exc:
        logger.debug("Semgrep CLI scan skipped or failed (%s); using built-in static rules.", exc)

    return findings


def _run_builtin_static_rules(files: Dict[str, str]) -> List[Dict[str, Any]]:
    """Run built-in static pattern checks and AST validation across files."""
    findings: List[Dict[str, Any]] = []
    for file_path, content in files.items():
        if not content:
            continue
        # Skip scanning the rule definition file itself to avoid self-matching rule strings
        if file_path.endswith("semgrep_service.py"):
            continue

        ext = Path(file_path).suffix.lower() or ".py"
        lines = content.splitlines()

        # Check Python files for syntax errors using AST
        if ext == ".py":
            try:
                ast.parse(content, filename=file_path)
            except SyntaxError as syn_err:
                err_line = syn_err.lineno or 1
                findings.append(
                    {
                        "rule_id": "bug.python.syntax-error",
                        "file": file_path,
                        "line": err_line,
                        "end_line": err_line,
                        "severity": "critical",
                        "message": f"Python SyntaxError: {syn_err.msg}",
                        "code_snippet": (syn_err.text or "").strip()[:200],
                        "suggestion": "Fix the syntax error before running or deploying this module.",
                        "source": "semgrep-ast",
                    }
                )

        for line_idx, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped:
                continue

            for rule in STATIC_RULES:
                if ext not in rule["languages"]:
                    continue
                # Skip comment lines except for the TODO/FIXME rule
                if rule["id"] != "quality.todo-fixme-marker" and (
                    stripped.startswith("#") or stripped.startswith("//") or stripped.startswith("*")
                ):
                    continue
                if rule["pattern"].search(line):
                    findings.append(
                        {
                            "rule_id": rule["id"],
                            "file": file_path,
                            "line": line_idx,
                            "end_line": line_idx,
                            "severity": rule["severity"],
                            "message": rule["message"],
                            "code_snippet": stripped[:200],
                            "suggestion": rule["suggestion"],
                            "source": "semgrep-static",
                        }
                    )
    return findings


def run_static_analysis(files: Dict[str, str]) -> Dict[str, Any]:
    """
    Execute static analysis (Semgrep Community Edition + built-in pattern rules)
    on a mapping of {file_path: content} and return findings and severity counts.
    """
    cli_findings = _run_semgrep_cli(files)
    builtin_findings = _run_builtin_static_rules(files)

    # Deduplicate by (file, line, rule_id suffix)
    seen = set()
    merged: List[Dict[str, Any]] = []
    for item in cli_findings + builtin_findings:
        short_rule = item["rule_id"].split(".")[-1]
        key = (item["file"], item["line"], short_rule)
        if key not in seen:
            seen.add(key)
            merged.append(item)

    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for item in merged:
        sev = item.get("severity", "low").lower()
        if sev in counts:
            counts[sev] += 1
        else:
            counts["low"] += 1

    return {
        "engine": "semgrep" if cli_findings else "semgrep-rules-engine",
        "total_issues": len(merged),
        "vulnerabilities": counts,
        "findings": merged,
    }

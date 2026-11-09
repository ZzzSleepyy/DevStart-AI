import re
from pathlib import PurePosixPath
from typing import List, Tuple

# Files or directories that must NEVER be sent to an AI model or scanned as source code
SENSITIVE_FILENAMES = {
    ".env",
    ".env.local",
    ".env.development",
    ".env.production",
    ".env.test",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
    "client_secret.json",
    "secrets.yaml",
    "secrets.yml",
    "secrets.json",
}

SENSITIVE_EXTENSIONS = {
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".keystore",
    ".jks",
    ".cer",
    ".crt",
    ".der",
    ".kdbx",
}

IGNORED_DIRS = {
    ".git",
    "node_modules",
    "venv",
    ".venv",
    "env",
    "__pycache__",
    "dist",
    "build",
    "out",
    "target",
    "vendor",
    ".next",
    ".nuxt",
    "coverage",
    ".idea",
    ".vscode",
}

IGNORED_EXTENSIONS = {
    ".lock",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".webp",
    ".mp4",
    ".mp3",
    ".wav",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".7z",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".pyc",
    ".class",
    ".jar",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".min.js",
    ".min.css",
    ".map",
}

SOURCE_CODE_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".java",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".ino",
    ".pde",
    ".cs",
    ".swift",
    ".kt",
    ".scala",
    ".lua",
    ".sh",
    ".html",
    ".css",
}

DEPENDENCY_FILENAMES = {
    "package.json",
    "requirements.txt",
    "pyproject.toml",
    "Pipfile",
    "setup.py",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "Gemfile",
    "composer.json",
}

DOC_FILENAMES = {
    "readme.md",
    "readme.rst",
    "readme.txt",
    "readme",
    "contributing.md",
    "contributing.rst",
    "contributing",
}

# Regex patterns to redact secrets before sending text to an AI model
SECRET_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # Private key blocks
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
    # GitHub tokens (classic & fine-grained)
    (
        re.compile(r"\b(ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|gho_[A-Za-z0-9]{30,})\b"),
        "[REDACTED_GITHUB_TOKEN]",
    ),
    # Google API keys
    (
        re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"),
        "[REDACTED_GOOGLE_API_KEY]",
    ),
    # AWS Access Key ID
    (
        re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
        "[REDACTED_AWS_KEY]",
    ),
    # Slack tokens
    (
        re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}\b"),
        "[REDACTED_SLACK_TOKEN]",
    ),
    # Generic high-confidence secret assignments in code/configs
    (
        re.compile(
            r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|private[_-]?key)\s*[:=]\s*(['\"])([^'\"\s]{8,})\2"
        ),
        r"\1 = \2[REDACTED_SECRET]\2",
    ),
]


def is_sensitive_path(file_path: str) -> bool:
    """Return True if a file path appears to be a credential, key, or .env file."""
    norm = file_path.replace("\\", "/").lstrip("/")
    p = PurePosixPath(norm)
    name_lower = p.name.lower()

    if name_lower in SENSITIVE_FILENAMES:
        return True
    if name_lower.startswith(".env.") and name_lower != ".env.example" and name_lower != ".env.sample":
        return True
    if "service-account" in name_lower or "service_account" in name_lower:
        return True
    if any( name_lower.endswith(ext) for ext in SENSITIVE_EXTENSIONS ):
        return True

    return False


def is_ignored_path(file_path: str) -> bool:
    """Return True if a path belongs to ignored directories, lockfiles, or binaries."""
    norm = file_path.replace("\\", "/").lstrip("/")
    p = PurePosixPath(norm)
    parts = [part.lower() for part in p.parts]

    if any(part in IGNORED_DIRS for part in parts):
        return True

    name_lower = p.name.lower()
    if name_lower in {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "cargo.lock"}:
        return True

    if any(name_lower.endswith(ext) for ext in IGNORED_EXTENSIONS):
        return True

    return False


def is_analyzable_source_file(file_path: str) -> bool:
    """Return True if a file is a safe, analyzable source code file."""
    if is_sensitive_path(file_path) or is_ignored_path(file_path):
        return False
    p = PurePosixPath(file_path.replace("\\", "/"))
    return p.suffix.lower() in SOURCE_CODE_EXTENSIONS


def redact_secrets(content: str) -> str:
    """Redact any embedded tokens, API keys, or private credentials before AI submission."""
    if not content:
        return ""
    sanitized = content
    for pattern, replacement in SECRET_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized

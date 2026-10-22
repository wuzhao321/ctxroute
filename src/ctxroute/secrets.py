"""Local key handling. Never include credential values in diagnostics."""

import os
from collections.abc import Mapping
from pathlib import Path


def validate_key(value: str) -> str:
    if not value or len(value) > 512 or not value.isascii() or any(c.isspace() for c in value):
        raise ValueError("API key must be nonempty ASCII without whitespace")
    if not all(c.isprintable() for c in value):
        raise ValueError("API key contains invalid characters")
    return value


def load_key(root: Path, environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    if "DEEPSEEK_API_KEY" in env:
        return validate_key(env["DEEPSEEK_API_KEY"])
    path = root / ".env"
    if not path.exists():
        raise ValueError("API key not set; run scripts/setup_key.py in your terminal")
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError(".env must be a private regular file (chmod 600 .env)")
    values: list[str] = []
    for line in path.read_text().splitlines():
        if line.startswith("DEEPSEEK_API_KEY="):
            values.append(line.partition("=")[2])
    if len(values) != 1:
        raise ValueError(".env must contain exactly one DEEPSEEK_API_KEY entry")
    # Our setup helper writes a literal, unquoted value; no shell evaluation.
    return validate_key(values[0])


def save_new_key(root: Path, key: str) -> None:
    value = validate_key(key)
    if ".env" not in (root / ".gitignore").read_text().splitlines():
        raise ValueError("Create a .gitignore entry for .env first")
    fd = os.open(root / ".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(f"DEEPSEEK_API_KEY={value}\n")

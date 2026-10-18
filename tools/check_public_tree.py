#!/usr/bin/env python3
"""Check source and built distributions for private deployment artifacts."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {
    ".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    "build", "dist", ".coverage", "htmlcov", ".playwright",
}
PRIVATE_NAMES = {
    ".env", "config.toml", "packageInfo", "Config", ".aws", ".midway", ".brazil",
    ".codex-investigations", ".builder-mcp", ".builder-mcp-test-tools",
}
CHECKS = {
    "private corporate endpoint": re.compile(
        r"(?:https?://|@)(?:[a-z0-9-]+\.)*(?:amazon\.(?:com|dev)|"
        r"audible\.com|a2z\.com|aws\.dev|labcollab\.net)\b", re.I,
    ),
    "cloud SDK import": re.compile(r"^\s*(?:from|import)\s+(?:boto3|botocore)\b", re.M),
    "private build dependency": re.compile(r"amzn[-]shrubbery|amzn[-]python|brazil[-]build", re.I),
    "cloud access key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}


def check(name: str, data: bytes) -> list[str]:
    parts = Path(name).parts
    problems = []
    if any(part in PRIVATE_NAMES for part in parts):
        problems.append(f"{name}: private configuration/artifact name")
    if any(part.startswith("output_") or part == "output" for part in parts):
        problems.append(f"{name}: generated evidence must not be distributed")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return problems
    # Report location and category without echoing possible credential values.
    for category, pattern in CHECKS.items():
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            problems.append(f"{name}:{line}: {category}")
    return problems


def scan_source() -> list[str]:
    problems = []
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT)
        if any(part in EXCLUDED or part.endswith(".egg-info") for part in relative.parts):
            continue
        if path.is_symlink():
            problems.append(f"{relative}: symbolic link is not a public source asset")
        elif path.is_file():
            problems.extend(check(str(relative), path.read_bytes()))
    return problems


def scan_archives(directory: Path) -> list[str]:
    problems = []
    archives = sorted(directory.glob("*.whl")) + sorted(directory.glob("*.tar.gz"))
    if not archives:
        return [f"{directory}: no built wheel or source archive found"]
    for archive in archives:
        if archive.suffix == ".whl":
            with zipfile.ZipFile(archive) as handle:
                for member in handle.infolist():
                    if not member.is_dir():
                        problems.extend(check(f"{archive.name}/{member.filename}", handle.read(member)))
        else:
            with tarfile.open(archive) as handle:
                for member in handle:
                    if member.issym() or member.islnk():
                        problems.append(f"{archive.name}/{member.name}: archive contains a link")
                    elif member.isfile():
                        stream = handle.extractfile(member)
                        assert stream is not None
                        problems.extend(check(f"{archive.name}/{member.name}", stream.read()))
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archives", type=Path, help="also inspect built wheel and source archives")
    args = parser.parse_args()
    problems = scan_source()
    if args.archives:
        problems.extend(scan_archives(args.archives))
    if problems:
        print("\n".join(problems))
        return 1
    print("Public source checks passed" + ("; distribution checks passed." if args.archives else "."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

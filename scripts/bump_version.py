#!/usr/bin/env python3
"""Bump the project version in pyproject.toml and create a matching git tag.

Usage:
    python scripts/bump_version.py 1.2.3
    python scripts/bump_version.py patch
    python scripts/bump_version.py minor
    python scripts/bump_version.py major
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"


def read_version() -> str:
    content = PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*["\']([^"\']+)["\']', content, re.MULTILINE)
    if not match:
        raise ValueError("Could not find a version in pyproject.toml")
    return match.group(1)


def write_version(new_version: str) -> None:
    content = PYPROJECT.read_text(encoding="utf-8")
    updated = re.sub(
        r'^(version\s*=\s*["\'])([^"\']+)(["\'])',
        rf'\g<1>{new_version}\g<3>',
        content,
        count=1,
        flags=re.MULTILINE,
    )
    if updated == content:
        raise ValueError("Failed to update version in pyproject.toml")
    PYPROJECT.write_text(updated, encoding="utf-8")


def parse_version(version: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        raise ValueError("Version must use semantic format: X.Y.Z")
    return tuple(int(part) for part in match.groups())


def bump_version(current: str, kind: str) -> str:
    major, minor, patch = parse_version(current)
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    if kind == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError("Bump kind must be one of: major, minor, patch")


def git_tag_exists(tag: str) -> bool:
    try:
        subprocess.run(
            ["git", "rev-parse", "--verify", tag],
            check=True,
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def create_tag(tag: str) -> None:
    try:
        subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], check=True, cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        print(f"Git repository not initialized; skipped tag creation for: {tag}")
        return

    if git_tag_exists(tag):
        raise ValueError(f"Git tag already exists: {tag}")
    subprocess.run(["git", "tag", tag], check=True, cwd=str(ROOT))
    print(f"Created git tag: {tag}")


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 1

    target = sys.argv[1]
    current = read_version()

    if target in {"major", "minor", "patch"}:
        new_version = bump_version(current, target)
    else:
        try:
            parse_version(target)
        except ValueError as exc:
            raise ValueError("Version must be X.Y.Z or one of: major, minor, patch") from exc
        new_version = target

    write_version(new_version)
    print(f"Updated pyproject.toml version to {new_version}")

    tag = f"v{new_version}"
    create_tag(tag)
    print(f"Ready to push with: git push origin {tag}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)

"""Validate pushed commit titles and bump the project version when appropriate."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

COMMIT_PATTERN = re.compile(
    r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)"
    r"(?:\([a-zA-Z0-9._/-]+\))?(!)?: .+"
)
VERSION_PATTERN = re.compile(
    r'(?m)^(version[ \t]*=[ \t]*)"([^\"]+)"(?=[ \t]*\r?$)'
)


def _commits(base: str, head: str) -> list[str]:
    if not re.fullmatch(r"[0-9a-fA-F]{40}", head):
        raise ValueError("head must be a full Git commit SHA")
    is_first_push = not re.fullmatch(r"[0-9a-fA-F]{40}", base) or set(base) == {"0"}
    command = (
        ["git", "show", "-s", "--format=%B", head]
        if is_first_push
        else ["git", "log", "--no-merges", "--format=%B%x00", "%s..%s" % (base, head)]
    )
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if is_first_push:
        return [completed.stdout.strip()] if completed.stdout.strip() else []
    return [message.strip() for message in completed.stdout.split("\0") if message.strip()]


def _validate(messages: list[str]) -> None:
    for message in messages:
        subject = message.splitlines()[0]
        if not COMMIT_PATTERN.fullmatch(subject):
            raise ValueError(
                "无效提交标题：%s\n请使用 feat/fix/docs/refactor/test/build/ci/chore 等前缀，"
                "格式如 feat(runtime): add host tool query" % subject
            )


def _next_version(version: str, messages: list[str]) -> str | None:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if match is None:
        raise ValueError("当前版本必须为 MAJOR.MINOR.PATCH：%s" % version)
    major, minor, patch = map(int, match.groups())
    bump = 0
    for message in messages:
        subject = message.splitlines()[0]
        if re.match(r"^\w+(?:\([^)]*\))?!:", subject) or re.search(
            r"(?m)^BREAKING CHANGE:", message
        ):
            bump = max(bump, 3)
        elif subject.startswith("feat"):
            bump = max(bump, 2)
        elif subject.startswith(("fix", "perf", "revert")):
            bump = max(bump, 1)
    if bump == 0:
        return None
    if bump == 3:
        return "%s.0.0" % (major + 1)
    if bump == 2:
        return "%s.%s.0" % (major, minor + 1)
    return "%s.%s.%s" % (major, minor, patch + 1)


def _set_version(path: Path, version: str) -> None:
    with path.open("r", encoding="utf-8", newline="") as stream:
        source = stream.read()
    updated, count = VERSION_PATTERN.subn(r'\g<1>"%s"' % version, source, count=1)
    if count != 1:
        raise ValueError("pyproject.toml 中没有找到唯一的 project.version 字段")
    with path.open("w", encoding="utf-8", newline="") as stream:
        stream.write(updated)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--project", type=Path, default=Path("pyproject.toml"))
    args = parser.parse_args(argv)
    try:
        messages = _commits(args.base, args.head)
        _validate(messages)
        source = args.project.read_text(encoding="utf-8")
        version_match = VERSION_PATTERN.search(source)
        if version_match is None:
            raise ValueError("pyproject.toml 中没有找到 project.version")
        version = _next_version(version_match.group(2), messages)
        if version is not None and args.apply:
            _set_version(args.project, version)
        print(version or "no-release")
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

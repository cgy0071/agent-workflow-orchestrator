"""Check repository text for invalid UTF-8, lossy replacement, and mojibake."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
import unicodedata


TEXT_SUFFIXES = {".md", ".py", ".json", ".yaml", ".yml", ".toml", ".txt", ".sh", ".ps1"}
SKIP_DIRS = {".git", "__pycache__", "runtime", ".venv", "venv", "node_modules"}
DEFAULT_CJK = ["SKILL.zh-CN.md", "README.zh-CN.md", "references/roles.zh-CN.md",
               "references/task-and-review.zh-CN.md", "references/harness-adapters.zh-CN.md",
               "references/monitoring.zh-CN.md"]
DEFAULT_ASCII = ["SKILL.md", "README.md", "agents/openai.yaml", "references/roles.md",
                 "references/task-and-review.md", "references/harness-adapters.md",
                 "references/monitoring.md"]


def check_bytes(data, expect_cjk=False, expect_ascii=False):
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        return ["invalid UTF-8 at byte " + str(exc.start)]
    issues = []
    if text.startswith("\ufeff"):
        issues.append("UTF-8 BOM; save text without BOM")
    if "\ufffd" in text:
        issues.append("Unicode replacement character U+FFFD")
    if re.search(r"\?{3,}", text):
        issues.append("three or more consecutive question marks; possible lossy conversion")
    if any(unicodedata.category(c) == "Cc" and c not in "\r\n\t" for c in text):
        issues.append("unexpected control character")
    for marker in ("\u951f\u65a4\u62f7", "\u00ef\u00bb\u00bf", "\u00e2\u20ac\u2122", "\u00e2\u20ac\u0153"):
        if marker in text:
            issues.append("known mojibake sequence " + marker.encode("unicode_escape").decode("ascii"))
    if expect_cjk and not any(0x4E00 <= ord(c) <= 0x9FFF for c in text):
        issues.append("expected Chinese text has no CJK characters")
    if expect_ascii and not text.isascii():
        issues.append("English entry point contains non-ASCII characters")
    return issues


def scan(root, expect_cjk, expect_ascii=()):
    root = Path(root).resolve()
    expected = set(expect_cjk)
    ascii_files = set(expect_ascii)
    problems = {}
    count = 0
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if not path.is_file() or SKIP_DIRS.intersection(rel.parts):
            continue
        if path.suffix not in TEXT_SUFFIXES and path.name not in (".gitignore", ".gitattributes"):
            continue
        count += 1
        issues = check_bytes(path.read_bytes(), rel.as_posix() in expected,
                             rel.as_posix() in ascii_files)
        if issues:
            problems[rel.as_posix()] = issues
    for name in expected | ascii_files:
        if not (root / name).is_file():
            problems.setdefault(name, []).append("expected text file missing")
    return count, problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--expect-cjk", nargs="*", default=DEFAULT_CJK,
                        help="files expected to contain Chinese; an empty list disables this language check")
    parser.add_argument("--expect-ascii", nargs="*", default=DEFAULT_ASCII,
                        help="English entry points expected to be ASCII-only; an empty list disables this check")
    args = parser.parse_args()
    count, problems = scan(args.root, args.expect_cjk, args.expect_ascii)
    for name, issues in problems.items():
        for issue in issues:
            print(name + ": " + issue)
    print(str(count) + " text files checked; " + str(len(problems)) + " files with encoding issues.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

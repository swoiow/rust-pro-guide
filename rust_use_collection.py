#!/usr/bin/env python3

import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path


# ============================================================
# 基础匹配
# ============================================================

# use xxx::yyy;
# 支持简单多行 use：
#
# use std::{
#     fs::File,
#     path::Path,
# };
#
USE_RE = re.compile(
    r"""
    (?mx)
    ^[ \t]*
    use
    [ \t]+
    (?P<body>
        (?:
            [^;/"']
            |
            "(?:\\.|[^"\\])*"
        )+
    )
    ;
    """,
    re.VERBOSE,
)


# 代码中直接使用：
#
# std::fs::read
# tokio::runtime::Runtime
# crate::foo::bar
# super::foo
#
# 暂时不尝试完整解析 Rust AST，
# 目标是先做统计。
PATH_RE = re.compile(
    r"""
    (?<![\w:])
    (?P<path>
        [A-Za-z_][A-Za-z0-9_]*
        (?:
            ::
            [A-Za-z_][A-Za-z0-9_]*
        ){1,}
    )
    """,
    re.VERBOSE,
)


COMMENT_LINE_RE = re.compile(r"//.*?$", re.MULTILINE)
COMMENT_BLOCK_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


# ============================================================
# 工具函数
# ============================================================

def line_number(text: str, position: int) -> int:
    return text.count("\n", 0, position) + 1


def normalize_spaces(value: str) -> str:
    return " ".join(value.split())


def remove_comments_preserve_length(source: str) -> str:
    """
    删除注释内容，但保持字符串长度基本不变，
    这样 match.start() 仍可映射到原始文件位置。

    注意：
    这不是完整 Rust lexer。
    第一版统计工具主要用于减少注释中的误报。
    """

    chars = list(source)

    for match in COMMENT_BLOCK_RE.finditer(source):
        for i in range(match.start(), match.end()):
            if chars[i] != "\n":
                chars[i] = " "

    temp = "".join(chars)

    for match in COMMENT_LINE_RE.finditer(temp):
        for i in range(match.start(), match.end()):
            if chars[i] != "\n":
                chars[i] = " "

    return "".join(chars)


def classify_root(path: str) -> str:
    root = path.split("::", 1)[0]

    if root == "std":
        return "std"

    if root == "core":
        return "core"

    if root == "alloc":
        return "alloc"

    if root == "crate":
        return "crate"

    if root == "self":
        return "self"

    if root == "super":
        return "super"

    return "external_or_module"


def extract_root(path: str) -> str:
    return path.split("::", 1)[0]


# ============================================================
# use 统计
# ============================================================

def collect_use_statements(source: str):
    cleaned = remove_comments_preserve_length(source)

    results = []

    ranges = []

    for match in USE_RE.finditer(cleaned):
        body = normalize_spaces(match.group("body"))
        start = match.start()
        end = match.end()

        raw = source[start:end]
        line = line_number(source, start)

        # use 的第一个 root。
        #
        # std::{fs::File, io::Read}
        # -> std
        #
        # tokio::runtime::Runtime
        # -> tokio
        root_match = re.match(
            r"(?:pub(?:\([^)]*\))?\s+)?"
            r"(?P<root>[A-Za-z_][A-Za-z0-9_]*)",
            body,
        )

        root = root_match.group("root") if root_match else ""

        results.append(
            {
                "kind": "use",
                "root": root,
                "category": classify_root(root) if root else "unknown",
                "path": body,
                "line": line,
                "example": normalize_spaces(raw),
            }
        )

        ranges.append((start, end))

    return results, ranges


# ============================================================
# 完整路径统计
# ============================================================

def inside_ranges(position: int, ranges) -> bool:
    for start, end in ranges:
        if start <= position < end:
            return True

    return False


def collect_qualified_paths(source: str, use_ranges):
    cleaned = remove_comments_preserve_length(source)

    results = []

    for match in PATH_RE.finditer(cleaned):
        start = match.start()

        # use xxx::yyy; 已经单独统计，
        # 不再作为 qualified_path 重复统计。
        if inside_ranges(start, use_ranges):
            continue

        path = match.group("path")

        # 排除某些明显不是我们关心的形式。
        #
        # 比如 enum variant / 类型关联函数仍会被统计：
        #
        # Result::Ok
        # String::from
        #
        # 第一版保留，因为后面可以按 root 过滤。
        root = extract_root(path)

        results.append(
            {
                "kind": "qualified_path",
                "root": root,
                "category": classify_root(path),
                "path": path,
                "line": line_number(source, start),
                "example": get_line(source, line_number(source, start)),
            }
        )

    return results


def get_line(source: str, lineno: int) -> str:
    lines = source.splitlines()

    if 1 <= lineno <= len(lines):
        return lines[lineno - 1].strip()

    return ""


# ============================================================
# 单文件分析
# ============================================================

def analyze_file(path: Path, project_root: Path):
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        source = path.read_text(
            encoding="utf-8",
            errors="replace",
        )

    relative = path.relative_to(project_root).as_posix()

    use_items, use_ranges = collect_use_statements(source)
    path_items = collect_qualified_paths(source, use_ranges)

    results = []

    for item in use_items + path_items:
        results.append(
            {
                "file": relative,
                **item,
            }
        )

    return results


# ============================================================
# 聚合
# ============================================================

def aggregate(records):
    """
    最核心统计：

        文件 + 写法 + path + 次数
    """

    grouped = {}

    for record in records:
        key = (
            record["file"],
            record["kind"],
            record["category"],
            record["root"],
            record["path"],
        )

        if key not in grouped:
            grouped[key] = {
                "file": record["file"],
                "kind": record["kind"],
                "category": record["category"],
                "root": record["root"],
                "path": record["path"],
                "count": 0,
                "lines": [],
                "example": record["example"],
            }

        item = grouped[key]

        item["count"] += 1
        item["lines"].append(record["line"])

    result = list(grouped.values())

    for item in result:
        item["lines"] = ",".join(
            map(str, sorted(set(item["lines"])))
        )

    result.sort(
        key=lambda x: (
            x["file"],
            x["kind"],
            -x["count"],
            x["path"],
        )
    )

    return result


# ============================================================
# CSV
# ============================================================

def write_csv(path: Path, rows):
    fieldnames = [
        "file",
        "kind",
        "category",
        "root",
        "path",
        "count",
        "lines",
        "example",
    ]

    with path.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames,
        )

        writer.writeheader()
        writer.writerows(rows)


# ============================================================
# 控制台摘要
# ============================================================

def print_summary(rows):
    print()
    print("=" * 100)
    print("Rust path usage statistics")
    print("=" * 100)

    print(
        f"{'FILE':40} "
        f"{'KIND':16} "
        f"{'COUNT':>7} "
        f"PATH"
    )

    print("-" * 100)

    for row in rows:
        file_name = row["file"]

        if len(file_name) > 38:
            file_name = "..." + file_name[-35:]

        print(
            f"{file_name:40} "
            f"{row['kind']:16} "
            f"{row['count']:>7} "
            f"{row['path']}"
        )


def print_std_summary(rows):
    std_rows = [
        row
        for row in rows
        if row["category"] == "std"
    ]

    if not std_rows:
        print()
        print("No std usage found.")
        return

    print()
    print("=" * 100)
    print("STD usage")
    print("=" * 100)

    for row in std_rows:
        print(
            f"{row['file']}:{row['lines']} "
            f"[{row['kind']}] "
            f"x{row['count']} "
            f"{row['path']}"
        )


# ============================================================
# main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description=(
            "统计 Rust 项目中的 use 和 qualified path 使用情况"
        )
    )

    parser.add_argument(
        "project",
        nargs="?",
        default=".",
        help="Rust 项目目录，默认当前目录",
    )

    parser.add_argument(
        "-o",
        "--output",
        default="rust_path_stats.csv",
        help="CSV 输出文件",
    )

    parser.add_argument(
        "--include-tests",
        action="store_true",
        help="包含 target 等通常忽略的目录",
    )

    args = parser.parse_args()

    project_root = Path(args.project).resolve()

    ignored_dirs = {
        "target",
        ".git",
        ".idea",
        ".vscode",
        "node_modules",
    }

    records = []

    for rust_file in project_root.rglob("*.rs"):
        relative_parts = rust_file.relative_to(
            project_root
        ).parts

        if (
            not args.include_tests
            and any(
                part in ignored_dirs
                for part in relative_parts
            )
        ):
            continue

        records.extend(
            analyze_file(
                rust_file,
                project_root,
            )
        )

    rows = aggregate(records)

    print_summary(rows)
    print_std_summary(rows)

    output = Path(args.output)

    if not output.is_absolute():
        output = Path.cwd() / output

    write_csv(output, rows)

    print()
    print(f"CSV written to: {output}")


if __name__ == "__main__":
    main()
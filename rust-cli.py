from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - only for older Python
    import tomli as tomllib  # type: ignore[no-redef]


# =========================
# Global user-editable settings
# =========================

TOOLCHAIN = "nightly"
FMT_COMMAND = ["cargo", f"+{TOOLCHAIN}", "fmt"]
CHECK_COMMAND = ["cargo", f"+{TOOLCHAIN}", "c"]
CHECK_FALLBACK_COMMAND = ["cargo", f"+{TOOLCHAIN}", "check"]
BUILD_COMMAND = ["cargo", f"+{TOOLCHAIN}", "build", "--release"]
CLEAN_COMMAND = ["cargo", f"+{TOOLCHAIN}", "clean"]

COPY_BUILD_ARTIFACTS_TO_ROOT = True
OVERWRITE_EXISTING_ARTIFACTS = True
FALLBACK_TO_CARGO_CHECK = True

# If CARGO_TARGET_DIR points to a shared global target dir, bare `cargo clean`
# may remove too much. When possible, use `cargo clean -p <package>`.
CLEAN_PACKAGE_SCOPED_WHEN_GLOBAL_TARGET = True

# If non-empty, these names override Cargo.toml package/bin detection.
# Windows example: BUILD_ARTIFACT_NAMES = ["codex-mimo-shim.exe"]
BUILD_ARTIFACT_NAMES: list[str] = []

# Search from current working directory upward until Cargo.toml is found.
# This makes the script usable from a global bin directory and from project subdirs.
SEARCH_PARENTS_FOR_CARGO_TOML = True

RUSTFMT_TOML_TEMPLATE = """# 常见的 rustfmt.toml 配置示例
max_width = 100
hard_tabs = false
merge_derives = true

imports_granularity = "Module"
# imports_granularity = "Item"  # LLM / AI friendly

group_imports = "StdExternalCrate"
"""


@dataclass(frozen=True)
class ProjectContext:
    root: Path
    cargo_toml: Path
    rustfmt_toml: Path
    target_dir: Path
    release_dir: Path
    target_dir_source: str


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    returncode: int


@dataclass(frozen=True)
class CargoTargets:
    package_name: str | None
    bin_names: list[str]


def resolve_cargo_target_dir(project_root: Path) -> tuple[Path, str]:
    """Resolve Cargo target dir.

    Supports:

        export CARGO_TARGET_DIR="$HOME/.cargo/global_target"

    If CARGO_TARGET_DIR is relative, resolve it against the project root.
    """

    raw_value = os.environ.get("CARGO_TARGET_DIR", "").strip()
    if not raw_value:
        return project_root / "target", "default"

    expanded_value = os.path.expandvars(os.path.expanduser(raw_value))
    target_dir = Path(expanded_value)

    if not target_dir.is_absolute():
        target_dir = project_root / target_dir

    return target_dir.resolve(), "CARGO_TARGET_DIR"


def find_project_root(start: Path) -> ProjectContext:
    """Find the Rust project root by locating Cargo.toml.

    The script is intended to live in a global bin directory. Therefore the
    working directory, not the script directory, determines the Rust project.
    """

    start = start.resolve()
    candidates = [start]
    if SEARCH_PARENTS_FOR_CARGO_TOML:
        candidates.extend(start.parents)

    for directory in candidates:
        cargo_toml = directory / "Cargo.toml"
        if cargo_toml.is_file():
            target_dir, target_dir_source = resolve_cargo_target_dir(directory)

            return ProjectContext(
                root=directory,
                cargo_toml=cargo_toml,
                rustfmt_toml=directory / "rustfmt.toml",
                target_dir=target_dir,
                release_dir=target_dir / "release",
                target_dir_source=target_dir_source,
            )

    raise FileNotFoundError(
        f"Cargo.toml not found from current directory upward: {start}. "
        "Run this command inside a Rust project."
    )


def ensure_rustfmt_toml(ctx: ProjectContext) -> None:
    if ctx.rustfmt_toml.exists():
        print(f"[skip] rustfmt.toml already exists: {ctx.rustfmt_toml}")
        return

    ctx.rustfmt_toml.write_text(RUSTFMT_TOML_TEMPLATE, encoding="utf-8")
    print(f"[create] rustfmt.toml created: {ctx.rustfmt_toml}")


def run_command(command: list[str], *, cwd: Path) -> CommandResult:
    print(f"[run] {' '.join(command)}")
    print(f"[cwd] {cwd}")

    try:
        completed = subprocess.run(command, cwd=str(cwd), check=False)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Command not found: {command[0]}. Is Cargo/Rust installed and on PATH?") from exc

    if completed.returncode != 0:
        print(f"[fail] {' '.join(command)} -> exit={completed.returncode}")
    else:
        print(f"[ok] {' '.join(command)}")

    return CommandResult(command=command, returncode=completed.returncode)


def run_format(ctx: ProjectContext) -> None:
    result = run_command(FMT_COMMAND, cwd=ctx.root)
    if result.returncode != 0:
        raise RuntimeError("cargo fmt failed")


def run_check(ctx: ProjectContext) -> None:
    result = run_command(CHECK_COMMAND, cwd=ctx.root)
    if result.returncode == 0:
        return

    if FALLBACK_TO_CARGO_CHECK and CHECK_COMMAND[-1] == "c":
        print("[retry] cargo +nightly c failed; falling back to cargo +nightly check")
        fallback_result = run_command(CHECK_FALLBACK_COMMAND, cwd=ctx.root)
        if fallback_result.returncode == 0:
            return

    raise RuntimeError("cargo check failed")


def run_build(ctx: ProjectContext) -> None:
    result = run_command(BUILD_COMMAND, cwd=ctx.root)
    if result.returncode != 0:
        raise RuntimeError("cargo build failed")


def load_cargo_toml(ctx: ProjectContext) -> dict[str, Any]:
    try:
        with ctx.cargo_toml.open("rb") as file:
            data = tomllib.load(file)
    except Exception as exc:
        raise RuntimeError(f"Failed to parse Cargo.toml: {ctx.cargo_toml}") from exc

    if not isinstance(data, dict):
        raise RuntimeError(f"Invalid Cargo.toml structure: {ctx.cargo_toml}")
    return data


def parse_cargo_targets(ctx: ProjectContext) -> CargoTargets:
    data = load_cargo_toml(ctx)

    package = data.get("package")
    package_name = None
    if isinstance(package, dict):
        raw_package_name = package.get("name")
        if isinstance(raw_package_name, str) and raw_package_name.strip():
            package_name = raw_package_name.strip()

    bin_names: list[str] = []
    raw_bins = data.get("bin")
    if isinstance(raw_bins, list):
        for item in raw_bins:
            if not isinstance(item, dict):
                continue

            name = item.get("name")
            if isinstance(name, str) and name.strip():
                bin_names.append(name.strip())

    # If Cargo.toml only has [package] and no [[bin]], Cargo defaults to package.name.
    if not bin_names and package_name:
        bin_names.append(package_name)

    deduped: list[str] = []
    seen: set[str] = set()
    for name in bin_names:
        if name in seen:
            continue

        seen.add(name)
        deduped.append(name)

    return CargoTargets(package_name=package_name, bin_names=deduped)


def is_global_cargo_target_dir(ctx: ProjectContext) -> bool:
    default_target_dir = (ctx.root / "target").resolve()
    return ctx.target_dir != default_target_dir


def build_clean_command(ctx: ProjectContext) -> list[str]:
    command = list(CLEAN_COMMAND)
    targets = parse_cargo_targets(ctx)

    if (
        CLEAN_PACKAGE_SCOPED_WHEN_GLOBAL_TARGET
        and is_global_cargo_target_dir(ctx)
        and targets.package_name
    ):
        print(
            "[safe] global CARGO_TARGET_DIR detected; "
            f"using package-scoped clean: -p {targets.package_name}"
        )
        command.extend(["-p", targets.package_name])
        return command

    if is_global_cargo_target_dir(ctx):
        print(
            "[warn] global CARGO_TARGET_DIR detected, but package name was not found. "
            "Bare cargo clean may remove shared target artifacts."
        )

    return command


def run_clean(ctx: ProjectContext) -> None:
    command = build_clean_command(ctx)
    result = run_command(command, cwd=ctx.root)
    if result.returncode != 0:
        raise RuntimeError("cargo clean failed")


def executable_filename(name: str) -> str:
    if sys.platform.startswith("win") and not name.lower().endswith(".exe"):
        return f"{name}.exe"
    return name


def is_executable_candidate(path: Path) -> bool:
    if not path.is_file():
        return False

    ignored_suffixes = {
        ".d",
        ".rlib",
        ".rmeta",
        ".pdb",
        ".lib",
        ".a",
        ".so",
        ".dll",
        ".dylib",
    }
    if path.suffix.lower() in ignored_suffixes:
        return False

    if sys.platform.startswith("win"):
        return path.suffix.lower() == ".exe"

    return bool(path.stat().st_mode & 0o111)


def configured_artifact_paths(ctx: ProjectContext) -> list[Path]:
    if BUILD_ARTIFACT_NAMES:
        return [ctx.release_dir / name for name in BUILD_ARTIFACT_NAMES]

    targets = parse_cargo_targets(ctx)
    paths: list[Path] = []
    for bin_name in targets.bin_names:
        expected = ctx.release_dir / executable_filename(bin_name)
        paths.append(expected)

        # Useful when cross-building or copying Windows exe from non-Windows host.
        exe_variant = ctx.release_dir / f"{bin_name}.exe"
        if exe_variant != expected:
            paths.append(exe_variant)

    return paths


def find_build_artifacts(ctx: ProjectContext) -> list[Path]:
    if not ctx.release_dir.exists():
        raise FileNotFoundError(f"Release directory not found: {ctx.release_dir}")

    configured = configured_artifact_paths(ctx)
    existing_configured = [path for path in configured if path.exists() and path.is_file()]
    if existing_configured:
        return existing_configured

    if configured:
        expected = ", ".join(str(path) for path in configured)
        print(f"[warn] artifacts inferred from Cargo.toml were not found: {expected}")
        print("[warn] falling back to executable scan in target/release")

    candidates = [path for path in ctx.release_dir.iterdir() if is_executable_candidate(path)]
    candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return candidates


def copy_artifacts_to_root(ctx: ProjectContext) -> list[Path]:
    artifacts = find_build_artifacts(ctx)
    if not artifacts:
        print(f"[warn] no executable artifacts found in {ctx.release_dir}")
        return []

    copied: list[Path] = []
    for source in artifacts:
        destination = ctx.root / source.name
        if destination.exists() and not OVERWRITE_EXISTING_ARTIFACTS:
            print(f"[skip] artifact exists: {destination}")
            continue

        shutil.copy2(source, destination)
        copied.append(destination)
        print(f"[copy] {source} -> {destination}")

    return copied


def show_project_info(ctx: ProjectContext) -> None:
    targets = parse_cargo_targets(ctx)

    print(f"[project] root={ctx.root}")
    print(f"[project] cargo_toml={ctx.cargo_toml}")
    print(f"[project] package={targets.package_name or '-'}")
    print(f"[project] bins={', '.join(targets.bin_names) if targets.bin_names else '-'}")
    print(f"[project] target_dir={ctx.target_dir}")
    print(f"[project] target_dir_source={ctx.target_dir_source}")
    print(f"[project] release_dir={ctx.release_dir}")


def task_check(ctx: ProjectContext) -> None:
    show_project_info(ctx)
    ensure_rustfmt_toml(ctx)
    run_format(ctx)
    run_check(ctx)
    print("[done] fmt + check completed")


def task_build(ctx: ProjectContext) -> None:
    show_project_info(ctx)
    ensure_rustfmt_toml(ctx)
    run_format(ctx)
    run_build(ctx)

    if COPY_BUILD_ARTIFACTS_TO_ROOT:
        copied = copy_artifacts_to_root(ctx)
        if copied:
            print("[done] build completed; artifacts copied to project root")
        else:
            print("[done] build completed; no artifacts copied")
    else:
        print("[done] build completed")


def task_clean(ctx: ProjectContext) -> None:
    show_project_info(ctx)
    run_clean(ctx)
    print("[done] clean completed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Global Rust project helper: find Cargo.toml, create rustfmt.toml, "
            "run nightly fmt/check/build/clean, copy release artifact to project root."
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["check", "c", "build", "b", "clean", "cl"],
        default="check",
        help=(
            "check/c: rustfmt + cargo +nightly c. "
            "build/b: rustfmt + cargo +nightly build --release + copy executable. "
            "clean/cl: cargo +nightly clean."
        ),
    )
    parser.add_argument(
        "--project",
        type=Path,
        default=None,
        help="Start directory for Cargo.toml discovery. Default: current working directory.",
    )
    parser.add_argument(
        "--no-parent-search",
        action="store_true",
        help="Only accept Cargo.toml in the start directory; do not search parents.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    global SEARCH_PARENTS_FOR_CARGO_TOML
    if args.no_parent_search:
        SEARCH_PARENTS_FOR_CARGO_TOML = False

    start = args.project if args.project is not None else Path.cwd()

    try:
        ctx = find_project_root(start)

        if args.command in {"build", "b"}:
            task_build(ctx)
        elif args.command in {"clean", "cl"}:
            task_clean(ctx)
        else:
            task_check(ctx)

        return 0
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
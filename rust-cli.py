from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, replace
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
CLIPPY_COMMAND = ["cargo", f"+{TOOLCHAIN}", "clippy"]
TEST_COMMAND = ["cargo", f"+{TOOLCHAIN}", "test"]
BUILD_COMMAND = ["cargo", f"+{TOOLCHAIN}", "build", "--release"]
CLEAN_COMMAND = ["cargo", f"+{TOOLCHAIN}", "clean"]
LINUX_ZIG_PATH = r"C:\zig-x86_64"
LINUX_RUSTFLAGS = "-C target-cpu=x86-64-v3 -C llvm-args=-unroll-threshold=500 -C llvm-args=-vectorize-loops=true"

# Release profile overrides are intentionally centralized here instead of being
# copied into every project Cargo.toml.
RELEASE_SPEED_PROFILE_OVERRIDES = [
    "profile.release.opt-level=3",
    "profile.release.debug=0",
    "profile.release.debug-assertions=false",
    "profile.release.overflow-checks=false",
    "profile.release.lto=true",
    'profile.release.panic="abort"',
    "profile.release.incremental=false",
    "profile.release.codegen-units=1",
    "profile.release.rpath=false",
    'profile.release.strip="symbols"',
    'profile.release.split-debuginfo="off"',
]
RELEASE_SIZE_PROFILE_OVERRIDES = [
    'profile.staging.opt-level="z"',
    "profile.staging.debug=0",
    "profile.staging.debug-assertions=false",
    "profile.staging.overflow-checks=false",
    "profile.staging.lto=true",
    'profile.staging.panic="abort"',
    "profile.staging.incremental=false",
    "profile.staging.codegen-units=1",
    "profile.staging.rpath=false",
    'profile.staging.strip="symbols"',
    'profile.staging.split-debuginfo="off"',
]


COPY_BUILD_ARTIFACTS_TO_ROOT = True
OVERWRITE_EXISTING_ARTIFACTS = True
FALLBACK_TO_CARGO_CHECK = True

USE_SCCACHE_IF_AVAILABLE = True
FORCE_SCCACHE = False
SCCACHE_CACHE_SIZE = "10G"
DEV_CARGO_INCREMENTAL = "1"
RELEASE_CARGO_INCREMENTAL = "0"

# AI-first text output:
# - stable section names
# - stable field names
# - command stdout/stderr wrapped by explicit begin/end markers
# - no JSON mode
OUTPUT_SCHEMA = "rust-cli-text-v3"
CAPTURE_COMMAND_OUTPUT = True
SHOW_EMPTY_COMMAND_OUTPUT_BLOCKS = False
MAX_CAPTURED_OUTPUT_CHARS = 120_000

# Color policy:
# - auto: color only when stdout is an interactive terminal.
# - always: force ANSI color.
# - never: disable ANSI color.
# The stable text shape is unchanged. Color is only applied at render time.
DEFAULT_COLOR_MODE = "auto"
COLOR_AUTO = "auto"
COLOR_ALWAYS = "always"
COLOR_NEVER = "never"

ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_RED = "\033[31m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_BLUE = "\033[34m"
ANSI_CYAN = "\033[36m"
ANSI_GRAY = "\033[90m"

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

# imports_granularity = "Module"
imports_granularity = "Item"  # LLM / AI friendly

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
    build_target: str | None = None


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    returncode: int
    duration_ms: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool


@dataclass(frozen=True)
class CargoTargets:
    package_name: str | None
    bin_names: list[str]


class TextReporter:
    """Stable plain-text reporter.

    Design goal:
    - AI friendly first: predictable fields, stable step names, explicit boundaries.
    - Human friendly second: still readable in a terminal without needing JSON tooling.
    """

    def __init__(self, *, color_mode: str = DEFAULT_COLOR_MODE) -> None:
        self._started_at = time.perf_counter()
        self._step_index = 0
        self._last_section: str | None = None
        self.color_mode = color_mode
        self.color_enabled = False

    def set_color_mode(self, color_mode: str) -> None:
        self.color_mode = color_mode
        self.color_enabled = should_use_color(color_mode, stream=sys.stdout)

    def begin(self, *, task: str) -> None:
        self.section("rust_cli_report")
        self.kv("schema", OUTPUT_SCHEMA)
        self.kv("status", "running")
        self.kv("task", task)
        self.kv("python", sys.version.split()[0])
        self.kv("platform", sys.platform)

    def finish(self, *, status: str, task: str) -> None:
        duration_ms = int((time.perf_counter() - self._started_at) * 1000)
        self.section("summary")
        self.kv("status", status)
        self.kv("task", task)
        self.kv("duration_ms", duration_ms)

    def section(self, name: str) -> None:
        if self._last_section is not None:
            print()
        header = f"== {name} =="
        print(colorize(header, ANSI_BOLD + ANSI_CYAN, enabled=self.color_enabled))
        self._last_section = name

    def kv(self, key: str, value: object) -> None:
        formatted_value = format_value(value)
        rendered_value = color_value(key, formatted_value, enabled=self.color_enabled)
        print(f"{key}: {rendered_value}")

    def note(self, code: str, message: str) -> None:
        self.section("note")
        self.kv("code", code)
        self.kv("message", message)

    def step_begin(self, name: str) -> None:
        self._step_index += 1
        self.section(f"step.{self._step_index}")
        self.kv("name", name)
        self.kv("status", "running")

    def step_end(self, *, status: str, detail: str | None = None) -> None:
        self.kv("status", status)
        if detail:
            self.kv("detail", detail)

    def command_result(self, result: "CommandResult", *, cwd: Path) -> None:
        self.kv("command", format_command(result.command))
        self.kv("command_args", " | ".join(result.command))
        self.kv("cwd", cwd)
        self.kv("exit_code", result.returncode)
        self.kv("duration_ms", result.duration_ms)

        if result.stdout_truncated:
            self.kv("stdout_truncated", "yes")
        if result.stderr_truncated:
            self.kv("stderr_truncated", "yes")

        self.output_block("stdout", result.stdout)
        self.output_block("stderr", result.stderr)

    def output_block(self, name: str, text: str) -> None:
        if not text and not SHOW_EMPTY_COMMAND_OUTPUT_BLOCKS:
            return

        print(f"{name}.begin")
        if text:
            print(text.rstrip())
        print(f"{name}.end")

    def error(self, code: str, exc: BaseException) -> None:
        self.section("error")
        self.kv("code", code)
        self.kv("type", type(exc).__name__)
        self.kv("message", str(exc))


REPORTER = TextReporter()


def should_use_color(mode: str, *, stream: object) -> bool:
    if os.environ.get("NO_COLOR"):
        return False

    if os.environ.get("CLICOLOR_FORCE") == "1":
        return True

    if os.environ.get("CLICOLOR") == "0":
        return False

    if mode == COLOR_ALWAYS:
        return True

    if mode == COLOR_NEVER:
        return False

    return bool(getattr(stream, "isatty", lambda: False)())


def colorize(text: str, color: str, *, enabled: bool) -> str:
    if not enabled:
        return text
    return f"{color}{text}{ANSI_RESET}"


def color_value(key: str, value: str, *, enabled: bool) -> str:
    if not enabled:
        return value

    if key == "status":
        return color_status(value, enabled=enabled)

    if key == "exit_code":
        if value == "0":
            return colorize(value, ANSI_GREEN, enabled=enabled)
        return colorize(value, ANSI_RED, enabled=enabled)

    if key in {"code", "type"}:
        return colorize(value, ANSI_YELLOW, enabled=enabled)

    return value


def color_status(status: str, *, enabled: bool) -> str:
    if status == "ok":
        return colorize(status, ANSI_GREEN, enabled=enabled)
    if status == "failed":
        return colorize(status, ANSI_RED, enabled=enabled)
    if status == "skipped":
        return colorize(status, ANSI_YELLOW, enabled=enabled)
    if status == "created":
        return colorize(status, ANSI_GREEN, enabled=enabled)
    if status == "running":
        return colorize(status, ANSI_BLUE, enabled=enabled)
    return colorize(status, ANSI_GRAY, enabled=enabled)


def format_value(value: object) -> str:
    if value is None:
        return "-"

    text = str(value)
    if not text:
        return "-"

    # Keep values one-line and stable. Paths with spaces remain readable.
    return text.replace("\r\n", "\\n").replace("\n", "\\n")


def format_command(command: list[str]) -> str:
    if sys.platform.startswith("win"):
        return subprocess.list2cmdline(command)
    return shlex.join(command)


def trim_command_output(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_CAPTURED_OUTPUT_CHARS:
        return text, False

    keep_head = MAX_CAPTURED_OUTPUT_CHARS // 3
    keep_tail = MAX_CAPTURED_OUTPUT_CHARS - keep_head
    trimmed = text[:keep_head] + "\n... output truncated: keeping head and tail ...\n" + text[-keep_tail:]
    return trimmed, True


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
        f"Cargo.toml not found from current directory upward: {start}. Run this command inside a Rust project."
    )


def ensure_rustfmt_toml(ctx: ProjectContext) -> None:
    REPORTER.step_begin("ensure_rustfmt_toml")
    REPORTER.kv("path", ctx.rustfmt_toml)

    if ctx.rustfmt_toml.exists():
        REPORTER.step_end(status="skipped", detail="rustfmt.toml already exists")
        return

    ctx.rustfmt_toml.write_text(RUSTFMT_TOML_TEMPLATE, encoding="utf-8")
    REPORTER.step_end(status="created", detail="rustfmt.toml created")


def build_cargo_command(
    subcommand: str,
    *args: str,
    config_overrides: list[str] | None = None,
) -> list[str]:
    """Build a cargo command with global --config overrides before the subcommand."""

    command = ["cargo", f"+{TOOLCHAIN}"]
    for override in config_overrides or []:
        command.extend(["--config", override])
    command.append(subcommand)
    command.extend(args)
    return command


def append_env_flags(current_env: dict[str, str], key: str, flags: list[str]) -> None:
    existing = current_env.get(key, "").strip()
    suffix = " ".join(flags)
    current_env[key] = f"{existing} {suffix}".strip() if existing else suffix


def build_command_env(
    *,
    env_profile: str,
    native_cpu: bool = False,
    linux: bool = False,
) -> dict[str, str]:
    """Create command environment without sacrificing dev incremental builds."""

    current_env = os.environ.copy()

    # if env_profile == "dev":
    #     current_env["CARGO_INCREMENTAL"] = DEV_CARGO_INCREMENTAL
    # elif env_profile == "release":
    #     current_env["CARGO_INCREMENTAL"] = RELEASE_CARGO_INCREMENTAL

    if env_profile in {"dev", "release"} and USE_SCCACHE_IF_AVAILABLE:
        existing_wrapper = current_env.get("RUSTC_WRAPPER", "").strip()
        if existing_wrapper:
            wrapper_name = Path(existing_wrapper).name.lower()
            if wrapper_name in {"sccache", "sccache.exe"}:
                current_env.setdefault("SCCACHE_CACHE_SIZE", SCCACHE_CACHE_SIZE)
        elif shutil.which("sccache") is not None or FORCE_SCCACHE:
            current_env["RUSTC_WRAPPER"] = "sccache"
            current_env.setdefault("SCCACHE_CACHE_SIZE", SCCACHE_CACHE_SIZE)

    if linux:
        current_env["RUSTFLAGS"] = LINUX_RUSTFLAGS
        current_env["PATH"] = LINUX_ZIG_PATH + os.pathsep + current_env.get("PATH", "")
    elif native_cpu:
        append_env_flags(current_env, "RUSTFLAGS", ["-C", "target-cpu=native"])

    return current_env


def run_command(
    command: list[str],
    *,
    cwd: Path,
    step_name: str,
    env_profile: str = "dev",
    native_cpu: bool = False,
    linux: bool = False,
) -> CommandResult:
    REPORTER.step_begin(step_name)

    current_env = build_command_env(env_profile=env_profile, native_cpu=native_cpu, linux=linux)
    REPORTER.kv("env_profile", env_profile)
    # REPORTER.kv("cargo_incremental", current_env.get("CARGO_INCREMENTAL", "-"))
    REPORTER.kv("rustc_wrapper", current_env.get("RUSTC_WRAPPER", "-"))
    if native_cpu or linux:
        REPORTER.kv("rustflags", current_env.get("RUSTFLAGS", "-"))

    started_at = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            check=False,
            capture_output=CAPTURE_COMMAND_OUTPUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=current_env,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Command not found: {command[0]}. Is Cargo/Rust installed and on PATH?") from exc

    duration_ms = int((time.perf_counter() - started_at) * 1000)
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    stdout, stdout_truncated = trim_command_output(stdout)
    stderr, stderr_truncated = trim_command_output(stderr)

    result = CommandResult(
        command=command,
        returncode=completed.returncode,
        duration_ms=duration_ms,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )
    REPORTER.command_result(result, cwd=cwd)

    if result.returncode == 0:
        REPORTER.step_end(status="ok")
    else:
        REPORTER.step_end(status="failed", detail=f"exit_code={result.returncode}")

    return result


def run_format(ctx: ProjectContext) -> None:
    result = run_command(FMT_COMMAND, cwd=ctx.root, step_name="cargo_fmt", env_profile="none")
    if result.returncode != 0:
        raise RuntimeError("cargo fmt failed")


def run_check(ctx: ProjectContext) -> None:
    result = run_command(CHECK_COMMAND, cwd=ctx.root, step_name="cargo_check_alias", env_profile="dev")
    if result.returncode == 0:
        return

    if FALLBACK_TO_CARGO_CHECK and CHECK_COMMAND[-1] == "c":
        REPORTER.note(
            "cargo_check_alias_failed",
            "cargo +nightly c failed; falling back to cargo +nightly check",
        )
        fallback_result = run_command(
            CHECK_FALLBACK_COMMAND,
            cwd=ctx.root,
            step_name="cargo_check_fallback",
            env_profile="dev",
        )
        if fallback_result.returncode == 0:
            return

    raise RuntimeError("cargo check failed")


def run_test(ctx: ProjectContext) -> None:
    result = run_command(TEST_COMMAND, cwd=ctx.root, step_name="cargo_test", env_profile="dev")
    if result.returncode != 0:
        raise RuntimeError("cargo test failed")


def release_profile_overrides(release_mode: str) -> list[str]:
    if release_mode == "release-speed":
        return RELEASE_SPEED_PROFILE_OVERRIDES
    if release_mode == "release-size":
        return RELEASE_SIZE_PROFILE_OVERRIDES
    return []


def release_build_command(release_mode: str, *, target: str | None = None) -> list[str]:
    overrides = release_profile_overrides(release_mode)
    if target:
        return build_cargo_command(
            "zigbuild",
            "--release",
            "--target",
            target,
            config_overrides=overrides,
        )
    if overrides:
        return build_cargo_command("build", "--release", config_overrides=overrides)
    return list(BUILD_COMMAND)


def show_release_profile(release_mode: str, *, native_cpu: bool) -> None:
    REPORTER.section("release_profile")
    REPORTER.kv("mode", release_mode)
    REPORTER.kv("source", "Cargo.toml [profile.release]" if release_mode == "build" else "wrapper --config overrides")
    REPORTER.kv("native_cpu", native_cpu)
    overrides = release_profile_overrides(release_mode)
    REPORTER.kv("override_count", len(overrides))
    for override in overrides:
        REPORTER.kv("override", override)


def run_build(ctx: ProjectContext, *, release_mode: str = "build", native_cpu: bool = False) -> None:
    command = release_build_command(release_mode, target=ctx.build_target)
    result = run_command(
        command,
        cwd=ctx.root,
        step_name=f"cargo_build_{release_mode}",
        env_profile="release",
        native_cpu=native_cpu,
        linux=ctx.build_target is not None,
    )
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

    if CLEAN_PACKAGE_SCOPED_WHEN_GLOBAL_TARGET and is_global_cargo_target_dir(ctx) and targets.package_name:
        REPORTER.note(
            "package_scoped_clean",
            f"global CARGO_TARGET_DIR detected; using cargo clean -p {targets.package_name}",
        )
        command.extend(["-p", targets.package_name])
        return command

    if is_global_cargo_target_dir(ctx):
        REPORTER.note(
            "global_target_dir_bare_clean",
            "global CARGO_TARGET_DIR detected, but package name was not found. "
            "Bare cargo clean may remove shared target artifacts.",
        )

    return command


def run_clean(ctx: ProjectContext) -> None:
    command = build_clean_command(ctx)
    result = run_command(command, cwd=ctx.root, step_name="cargo_clean", env_profile="none")
    if result.returncode != 0:
        raise RuntimeError("cargo clean failed")


def executable_filename(name: str) -> str:
    if sys.platform.startswith("win") and not name.lower().endswith(".exe"):
        return f"{name}.exe"
    return name


def is_executable_candidate(path: Path, *, linux: bool = False) -> bool:
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

    if linux:
        return path.suffix == ""

    if sys.platform.startswith("win"):
        return path.suffix.lower() == ".exe"

    return bool(path.stat().st_mode & 0o111)


def configured_artifact_paths(ctx: ProjectContext) -> list[Path]:
    if BUILD_ARTIFACT_NAMES:
        return [ctx.release_dir / name for name in BUILD_ARTIFACT_NAMES]

    targets = parse_cargo_targets(ctx)
    paths: list[Path] = []
    for bin_name in targets.bin_names:
        expected = ctx.release_dir / (bin_name if ctx.build_target else executable_filename(bin_name))
        paths.append(expected)

        if ctx.build_target:
            continue

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
        REPORTER.note(
            "inferred_artifacts_missing",
            f"artifacts inferred from Cargo.toml were not found: {expected}. "
            "Falling back to executable scan in target/release.",
        )

    candidates = [
        path for path in ctx.release_dir.iterdir() if is_executable_candidate(path, linux=ctx.build_target is not None)
    ]
    candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return candidates


def copy_artifacts_to_root(ctx: ProjectContext) -> list[Path]:
    REPORTER.step_begin("copy_release_artifacts")
    REPORTER.kv("enabled", COPY_BUILD_ARTIFACTS_TO_ROOT)
    REPORTER.kv("overwrite", OVERWRITE_EXISTING_ARTIFACTS)
    REPORTER.kv("release_dir", ctx.release_dir)

    artifacts = find_build_artifacts(ctx)
    if not artifacts:
        REPORTER.kv("copied_count", 0)
        REPORTER.step_end(status="skipped", detail="no executable artifacts found")
        return []

    copied: list[Path] = []
    for source in artifacts:
        destination = ctx.root / source.name
        if destination.exists() and not OVERWRITE_EXISTING_ARTIFACTS:
            REPORTER.kv("skipped_artifact", destination)
            continue

        shutil.copy2(source, destination)
        copied.append(destination)
        REPORTER.kv("copied_artifact", f"{source} -> {destination}")

    REPORTER.kv("copied_count", len(copied))
    REPORTER.step_end(status="ok")
    return copied


def show_project_info(ctx: ProjectContext) -> None:
    targets = parse_cargo_targets(ctx)

    REPORTER.section("project")
    REPORTER.kv("root", ctx.root)
    REPORTER.kv("cargo_toml", ctx.cargo_toml)
    REPORTER.kv("package", targets.package_name or "-")
    REPORTER.kv("bins", ", ".join(targets.bin_names) if targets.bin_names else "-")
    REPORTER.kv("target_dir", ctx.target_dir)
    REPORTER.kv("target_dir_source", ctx.target_dir_source)
    REPORTER.kv("release_dir", ctx.release_dir)
    if ctx.build_target:
        REPORTER.kv("build_target", ctx.build_target)


def task_check(ctx: ProjectContext) -> None:
    show_project_info(ctx)
    ensure_rustfmt_toml(ctx)
    run_format(ctx)
    run_check(ctx)


def task_test(ctx: ProjectContext) -> None:
    show_project_info(ctx)
    ensure_rustfmt_toml(ctx)
    run_format(ctx)
    run_test(ctx)


def task_clippy(ctx: ProjectContext) -> None:
    show_project_info(ctx)
    ensure_rustfmt_toml(ctx)
    run_format(ctx)
    result = run_command(CLIPPY_COMMAND, cwd=ctx.root, step_name="cargo_clippy")
    if result.returncode != 0:
        raise RuntimeError("cargo clippy failed")


def task_build(ctx: ProjectContext, *, release_mode: str = "build", native_cpu: bool = False) -> None:
    show_project_info(ctx)
    show_release_profile(release_mode, native_cpu=native_cpu)
    ensure_rustfmt_toml(ctx)
    run_format(ctx)
    run_build(ctx, release_mode=release_mode, native_cpu=native_cpu)

    if COPY_BUILD_ARTIFACTS_TO_ROOT:
        copy_artifacts_to_root(ctx)
    else:
        REPORTER.note("copy_release_artifacts_disabled", "release artifact copy is disabled")


def task_clean(ctx: ProjectContext) -> None:
    show_project_info(ctx)
    run_clean(ctx)


def normalize_task(command: str) -> str:
    aliases = {
        "check": "check",
        "c": "check",
        "test": "test",
        "t": "test",
        "clippy": "clippy",
        "build": "build",
        "b": "build",
        "release": "build",
        "release-speed": "release-speed",
        "speed": "release-speed",
        "fast": "release-speed",
        "rs": "release-speed",
        "release-size": "release-size",
        "size": "release-size",
        "small": "release-size",
        "rz": "release-size",
        "clean": "clean",
        "cl": "clean",
    }
    return aliases.get(command, "check")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Global Rust project helper: find Cargo.toml, create rustfmt.toml, "
            "run nightly fmt/check/clippy/test/build/clean, and build semantic release artifacts."
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=[
            "check",
            "c",
            "test",
            "t",
            "clippy",
            "build",
            "b",
            "release",
            "release-speed",
            "speed",
            "fast",
            "rs",
            "release-size",
            "size",
            "small",
            "rz",
            "clean",
            "cl",
        ],
        default="check",
        help=(
            "check/c: rustfmt + cargo +nightly c, with dev incremental enabled. "
            "test/t: rustfmt + cargo +nightly test, with dev incremental enabled. "
            "clippy: rustfmt + cargo +nightly clippy. "
            "build/b/release: baseline cargo build --release from Cargo.toml. "
            "Append linux [musl|gnu] to build with cargo zigbuild (default: musl). "
            "speed/rs: release build optimized for runtime speed via wrapper --config. "
            "size/rz: release build optimized for smaller artifact via wrapper --config. "
            "clean/cl: cargo +nightly clean."
        ),
    )
    parser.add_argument(
        "platform",
        nargs="?",
        choices=["linux"],
        help="Cross-build release artifacts for Linux x86_64 using Zig.",
    )
    parser.add_argument(
        "libc",
        nargs="?",
        choices=["musl", "gnu"],
        default="musl",
        help="Linux target ABI (default: musl).",
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
    parser.add_argument(
        "--color",
        choices=[COLOR_AUTO, COLOR_ALWAYS, COLOR_NEVER],
        default=DEFAULT_COLOR_MODE,
        help=(
            "Colorize human-facing terminal output. "
            "auto: color only for interactive terminals; "
            "always: force ANSI color; never: disable color."
        ),
    )
    parser.add_argument(
        "--native-cpu",
        action="store_true",
        help=(
            "Append RUSTFLAGS='-C target-cpu=native' for release builds. "
            "Use only when the artifact is intended for the build machine's CPU family."
        ),
    )
    parser.add_argument(
        "--no-copy",
        action="store_true",
        help="Do not copy release executable artifacts from target/release to the project root.",
    )
    args = parser.parse_args()
    if args.platform and normalize_task(args.command) not in {"build", "release-speed", "release-size"}:
        parser.error("linux is only supported for release build commands")
    if args.platform and args.native_cpu:
        parser.error("linux uses target-cpu=x86-64-v3; --native-cpu cannot be combined with it")
    return args


def main() -> int:
    args = parse_args()
    REPORTER.set_color_mode(args.color)
    task = normalize_task(args.command)

    REPORTER.begin(task=task)

    global SEARCH_PARENTS_FOR_CARGO_TOML, COPY_BUILD_ARTIFACTS_TO_ROOT
    if args.no_parent_search:
        SEARCH_PARENTS_FOR_CARGO_TOML = False
    if args.no_copy:
        COPY_BUILD_ARTIFACTS_TO_ROOT = False

    start = args.project if args.project is not None else Path.cwd()

    try:
        ctx = find_project_root(start)
        if args.platform == "linux":
            target = f"x86_64-unknown-linux-{args.libc}"
            ctx = replace(ctx, build_target=target, release_dir=ctx.target_dir / target / "release")

        if task in {"build", "release-speed", "release-size"}:
            task_build(ctx, release_mode=task, native_cpu=args.native_cpu)
        elif task == "test":
            task_test(ctx)
        elif task == "clippy":
            task_clippy(ctx)
        elif task == "clean":
            task_clean(ctx)
        else:
            if args.native_cpu:
                REPORTER.note("native_cpu_ignored", "--native-cpu only affects release build commands")
            task_check(ctx)

        REPORTER.finish(status="ok", task=task)
        return 0
    except Exception as exc:
        REPORTER.error("fatal", exc)
        REPORTER.finish(status="failed", task=task)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

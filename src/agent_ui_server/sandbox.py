"""Shared Bubblewrap policy with adapter-specific runtime/config profiles.

No shell script and no unsandboxed fallback. Profile builders return argv;
callers must also clear the environment of the Bubblewrap process itself.
"""
from __future__ import annotations

import json
import os
import pwd
import shutil
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .sandbox_paths import overlaps, validate_paths


def prepare_scratch() -> Path:
    """Private, host-backed scratch shared across all of this user's turns."""
    scratch = Path(f"/tmp/agent-sandbox-{os.getuid()}")
    scratch.mkdir(mode=0o700, exist_ok=True)
    # Open without following a symlink, and validate/chmod the same inode.
    fd = os.open(scratch, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError(f"Unsafe scratch directory: {scratch}")
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)
    return scratch


def git_metadata_directories(cwd: Path, home: Path) -> list[Path]:
    """Extra mounts for a worktree's gitfile and shared repository metadata.

    Read Git's gitfile/commondir pointers rather than running Git on the host:
    discovery must not inherit GIT_DIR overrides or execute project code. Keep
    the original absolute paths (including symlink spellings), since the
    gitfile inside the sandbox still points at those paths.
    """
    gitfile = cwd / ".git"
    if not gitfile.is_file():
        # Ordinary repositories already have their .git directory inside cwd.
        return []

    def read_path(path: Path, prefix: str = "") -> Path:
        # These are project-controlled files, not arbitrary-size text inputs.
        with path.open("rb") as stream:
            raw = stream.read(8193)
        value = os.fsdecode(raw).rstrip("\r\n")
        if len(raw) > 8192 or not value.startswith(prefix) or not value[len(prefix):]:
            raise ValueError(f"Invalid Git metadata pointer: {path}")
        return Path(os.path.abspath(path.parent / value[len(prefix):]))

    git_dir = read_path(gitfile, "gitdir: ")
    common_file = git_dir / "commondir"
    common_dir = read_path(common_file) if common_file.exists() else git_dir
    if not (
        (git_dir / "HEAD").is_file()
        and (common_dir / "objects").is_dir()
        and (common_dir / "refs").is_dir()
    ):
        raise ValueError(f"Invalid Git metadata directory: {git_dir}")

    directories: list[Path] = []
    for directory in (common_dir, git_dir):
        resolved = directory.resolve(strict=True)
        if home.is_relative_to(directory) or home.is_relative_to(resolved):
            raise ValueError("Sandbox Git metadata directory must not expose home")
        if directory.is_relative_to(cwd) and resolved.is_relative_to(cwd):
            continue
        # The usual <repo>/.git/worktrees/<name> is already in the common mount.
        # A symlink out of that tree still needs its own explicit mount.
        if any(
            directory.is_relative_to(parent)
            and resolved.is_relative_to(parent.resolve())
            for parent in directories
        ):
            continue
        directories.append(directory)
    return directories


@dataclass(frozen=True)
class SandboxMount:
    option: str
    source: str | None
    destination: str

    def argv(self) -> list[str]:
        if self.source is None:
            return [self.option, self.destination, "--remount-ro", self.destination]
        return [self.option, self.source, self.destination]


@dataclass(frozen=True)
class SandboxFilesystem:
    cwd: Path
    mounts: list[SandboxMount]

    def describe(self) -> str:
        """Describe the exact mount plan, not a second filesystem discovery pass."""
        lines = [
            "Bubblewrap filesystem view for this turn (paths are JSON-quoted):",
            f"Working directory: {json.dumps(str(self.cwd))}",
        ]
        for writable, heading in ((True, "Writable mounts:"), (False, "Read-only mounts:")):
            lines.append(heading)
            for mount in self.mounts:
                if (mount.option == "--bind") != writable:
                    continue
                detail = ""
                if mount.source is None:
                    detail = " (synthetic filesystem, not the host's contents)"
                elif mount.option.endswith("-try"):
                    detail = " (only if present on the server)"
                if mount.source is not None and mount.source != mount.destination:
                    detail += f" (server source: {json.dumps(mount.source)})"
                lines.append(f"- {json.dumps(mount.destination)}{detail}")
        lines.extend([
            "The synthetic root and parent directories are read-only. More-specific mounts "
            "override parent mounts. These are mount permissions, not guarantees of access; "
            "ordinary filesystem permissions still apply.",
            "Unlisted host paths may be hidden or read-only. Directory listings describe "
            "this sandbox view, not the server filesystem. Sandbox /tmp is backed by the "
            "server source listed above, not the server's /tmp directory.",
        ])
        return "\n".join(lines)


def sandbox_command(
    command: list[str],
    working_dir: str,
    *,
    read_only: list[tuple[Path, str]],
    writable: list[Path],
    environment: dict[str, str],
    sandbox_paths: list[dict[str, Any]] | None = None,
    command_suffix: Callable[[SandboxFilesystem], list[str]] | None = None,
) -> list[str]:
    """Common filesystem, namespace, scratch, Git, and environment policy.

    Adapters supply only their runtime/resources, config directories, and
    explicit environment additions. No inherited environment is forwarded.
    """
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise FileNotFoundError("Bubblewrap (bwrap) is required for sandboxed turns")
    home = Path.home().resolve()
    cwd = Path(working_dir).resolve(strict=True)
    if not cwd.is_dir():
        raise NotADirectoryError(working_dir)
    if home.is_relative_to(cwd):
        raise ValueError("Sandbox working directory must not expose the home directory")
    for source in [*writable, *(source for source, _ in read_only)]:
        if home.is_relative_to(source.resolve()):
            raise ValueError("Sandbox mount must not expose the home directory")

    metadata = git_metadata_directories(cwd, home)
    extra_mounts = validate_paths(sandbox_paths or [])
    builtins = [(p.resolve(), p) for p in [*writable, cwd, *metadata]]
    builtins.extend((source.resolve(), Path(target)) for source, target in read_only)
    for mount in extra_mounts:
        for source, target in builtins:
            if overlaps(mount.source, source) or overlaps(mount.destination, target):
                raise ValueError(f"Sandbox path {mount.destination} overlaps built-in mount {target}")
    scratch = prepare_scratch()
    mounts = [
        SandboxMount("--ro-bind", "/usr", "/usr"),
        SandboxMount("--ro-bind", "/bin", "/bin"),
        SandboxMount("--ro-bind", "/lib", "/lib"),
        SandboxMount("--ro-bind-try", "/lib64", "/lib64"),
        SandboxMount("--ro-bind", "/etc/ssl/certs", "/etc/ssl/certs"),
        SandboxMount("--ro-bind", str(Path("/etc/resolv.conf").resolve()), "/etc/resolv.conf"),
        SandboxMount("--ro-bind", "/etc/hosts", "/etc/hosts"),
        SandboxMount("--ro-bind", "/etc/nsswitch.conf", "/etc/nsswitch.conf"),
        SandboxMount("--dev", None, "/dev"),
        SandboxMount("--proc", None, "/proc"),
        SandboxMount("--bind", str(scratch), "/tmp"),
    ]
    # Linked worktrees expose Git metadata, not the main checkout's files.
    mounts.extend(SandboxMount("--bind", str(p), str(p)) for p in [*writable, cwd, *metadata])
    mounts.extend(SandboxMount("--ro-bind", str(source), target) for source, target in read_only)
    mounts.extend(SandboxMount("--bind" if m.write else "--ro-bind",
                               str(m.source), str(m.destination)) for m in extra_mounts)
    filesystem = SandboxFilesystem(cwd, mounts)
    args = [
        bwrap,
        "--unshare-all", "--share-net", "--die-with-parent", "--new-session",
        "--cap-drop", "ALL",
    ]
    for mount in filesystem.mounts:
        args.extend(mount.argv())
    args.extend(["--chdir", str(filesystem.cwd), "--remount-ro", "/", "--clearenv"])
    try:
        user = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        # Minimal containers (and a server inside another sandbox) may not
        # expose /etc/passwd even though they have a valid numeric uid.
        user = os.environ.get("USER") or str(os.getuid())
    env = {
        "HOME": str(home),
        "USER": user,
        "PATH": "/usr/bin:/bin",
        "TERM": os.environ.get("TERM") or "xterm-256color",
        "LANG": "C.UTF-8",
        "TMPDIR": "/tmp",
        "XDG_CACHE_HOME": "/tmp/cache",
        **environment,
    }
    for name, value in env.items():
        args.extend(["--setenv", name, value])
    suffix = command_suffix(filesystem) if command_suffix is not None else []
    return [*args, "--", *command, *suffix]


def _executable(name: str) -> Path:
    executable = shutil.which(name)
    if not executable:
        raise FileNotFoundError(f"Agent executable not found: {name}")
    path = Path(executable).absolute()
    # Keep the launcher's symlink name, but normalize its parent directory.
    return path.parent.resolve() / path.name


def pi_sandbox_command(command: list[str], working_dir: str, *,
                       sandbox_paths: list[dict[str, Any]] | None = None,
                       system_prompt: Callable[[SandboxFilesystem], str] | None = None) -> list[str]:
    """Pi installer/system runtime, config and explicit server extensions."""
    executable = _executable(command[0])
    bin_dir = executable.parent
    runtime = bin_dir.parent
    home = Path.home().resolve()
    read_only: list[tuple[Path, str]] = []
    if not executable.is_relative_to(Path("/usr")):
        if bin_dir.name != "bin" or not (bin_dir / "node").is_file():
            raise ValueError(
                "Sandboxed Pi requires a runtime with bin/pi and bin/node, "
                "or a system installation under /usr"
            )
        read_only.append((runtime, str(runtime)))
    config = home / ".pi"
    config.mkdir(mode=0o700, exist_ok=True)
    inner = [str(executable), *command[1:]]
    for index, arg in enumerate(inner[:-1]):
        if arg != "-e":
            continue
        source = Path(inner[index + 1]).resolve(strict=True)
        # The gate is standalone; web extensions import sibling modules.
        # Mount directories for directory extensions and for multi-file web
        # extensions, but never expose a whole home as an extension bundle.
        if source.is_dir() or index != inner.index("-e"):
            directory = source if source.is_dir() else source.parent
            if home.is_relative_to(directory):
                raise ValueError("Sandbox extension directory must not expose home")
            target = Path(f"/opt/agent-ui/extension-{index}")
            read_only.append((directory, str(target)))
            inner[index + 1] = str(target if source.is_dir() else target / source.name)
        else:
            target = f"/opt/agent-ui/extension-{index}/{source.name}"
            read_only.append((source, target))
            inner[index + 1] = target

    return sandbox_command(
        inner, working_dir, read_only=read_only, writable=[config],
        environment={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        sandbox_paths=sandbox_paths,
        command_suffix=(lambda fs: ["--append-system-prompt", system_prompt(fs)]) if system_prompt else None,
    )


def _claude_config() -> Path:
    """Keep atomic config writes inside the allowed config directory.

    CLAUDE_CONFIG_DIR relocates the normally home-level .claude.json as well
    as credentials/sessions. Import legacy global state once; never overwrite
    a config already used by another turn or a user-configured profile.
    """
    home = Path.home().resolve()
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    config = Path(override).expanduser() if override else home / ".claude"
    if not config.is_absolute():
        raise ValueError("CLAUDE_CONFIG_DIR must be absolute")
    config = config.resolve()
    if home.is_relative_to(config):
        raise ValueError("Claude config directory must not expose home")
    config.mkdir(mode=0o700, parents=True, exist_ok=True)
    source = home / ".claude.json"
    target = config / ".claude.json"
    if not override and source.is_file() and not target.exists() and not (config / ".config.json").exists():
        # Publish a complete 0600 copy without replacing a concurrent import.
        fd, name = tempfile.mkstemp(prefix=".sandbox-config-", dir=config)
        try:
            with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
                shutil.copyfileobj(input_file, output)
            try:
                os.link(name, target)
            except FileExistsError:
                pass
        finally:
            os.unlink(name)
    return config


def claude_sandbox_command(command: list[str], working_dir: str, *,
                           sandbox_paths: list[dict[str, Any]] | None = None,
                           system_prompt: Callable[[SandboxFilesystem], str] | None = None) -> list[str]:
    """Claude native binary or npm Node launcher, with persistent config."""
    launcher = _executable(command[0])
    executable = launcher.resolve(strict=True)
    read_only: list[tuple[Path, str]] = []
    path = "/usr/bin:/bin"
    with executable.open("rb") as stream:
        header = stream.read(128)
    if header.startswith(b"#!") and b"node" in header.split(b"\n", 1)[0]:
        # npm's symlink points into its package. Include sibling modules and
        # vendored tools, not the whole npm prefix (which may be inside home).
        package = executable.parent
        while not (package / "package.json").is_file():
            if package.parent == package:
                raise ValueError("Cannot locate Claude Code npm package")
            package = package.parent
        read_only.append((package, str(package)))
        node = launcher.parent / "node"
        if not node.is_file():
            node = _executable("node")
        node = node.resolve(strict=True)
        if not node.is_relative_to(Path("/usr")):
            read_only.append((node, "/opt/agent-ui/node/bin/node"))
            path = "/opt/agent-ui/node/bin:" + path
        else:
            path = str(node.parent) + ":" + path
    else:
        # Native installers use ~/.local/bin/claude -> a versioned binary.
        # Bind only that file; no need to expose ~/.local or all of home.
        read_only.append((executable, str(executable)))
    config = _claude_config()
    return sandbox_command(
        [str(executable), *command[1:]], working_dir,
        read_only=read_only, writable=[config],
        sandbox_paths=sandbox_paths,
        command_suffix=(lambda fs: ["--append-system-prompt", system_prompt(fs)]) if system_prompt else None,
        environment={
            "PATH": path,
            "CLAUDE_CONFIG_DIR": str(config),
            "DISABLE_AUTOUPDATER": "1",
        },
    )

#!/usr/bin/env python3
"""Build the pinned public Codex source on macOS, Linux, or Windows."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "api/services/codex/source.json"


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_digest(source):
    """Verify build inputs even when Docker strips the submodule's Git metadata."""
    digest = hashlib.sha256()
    for directory in (".cargo", "codex-rs", "third_party"):
        for current, dirs, files in os.walk(source / directory):
            dirs[:] = sorted(
                name
                for name in dirs
                if name not in {".git", "target", "node_modules", "__pycache__"}
            )
            for name in sorted(files):
                if name.endswith((".pyc", ".pyo")) or name == ".DS_Store":
                    continue
                path = Path(current) / name
                digest.update(path.relative_to(source).as_posix().encode() + b"\0")
                content = (
                    os.readlink(path).encode()
                    if path.is_symlink()
                    else path.read_bytes()
                )
                digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


def build(source, output, profile):
    lock = json.loads(LOCK.read_text())
    workspace = source / "codex-rs"
    if not (workspace / "Cargo.lock").is_file():
        raise RuntimeError("Run git submodule update --init codex first.")
    # Docker's build context excludes Git metadata. Its source still comes from
    # the checked-out gitlink; verify the workspace version, toolchain, and lock.
    if (source / ".git").exists():
        revision = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip()
        if revision != lock["revision"]:
            raise RuntimeError(
                "Codex checkout differs from source.json; update the pin together."
            )
        dirty = subprocess.check_output(
            ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=no"],
            text=True,
        )
        if dirty:
            raise RuntimeError(
                "Codex has source changes; commit and update the source pin first."
            )
    version = tomllib.loads((workspace / "Cargo.toml").read_text())["workspace"][
        "package"
    ]["version"]
    toolchain = tomllib.loads((workspace / "rust-toolchain.toml").read_text())[
        "toolchain"
    ]["channel"]
    if (version, toolchain, sha256(workspace / "Cargo.lock")) != (
        lock["version"],
        lock["rust_toolchain"],
        lock["cargo_lock_sha256"],
    ):
        raise RuntimeError(
            "Codex source version/toolchain/Cargo.lock does not match source.json."
        )
    if not shutil.which("cargo"):
        raise RuntimeError("Install Rust with rustup, then rerun this command.")
    if source_digest(source) != lock["source_sha256"]:
        raise RuntimeError("Codex source files differ from the pinned build inputs.")
    # Keep installed binaries outside the disposable build tree. A unique tree
    # also prevents one build's cleanup from deleting another build's files.
    scratch_root = ROOT / ".runtime"
    scratch_root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="codex-build-", dir=scratch_root) as scratch:
        compile_and_install(source, output, profile, lock, Path(scratch))


def compile_and_install(source, output, profile, lock, scratch):
    """Install the verified executable before the caller removes intermediates."""
    version = lock["version"]
    toolchain = lock["rust_toolchain"]
    # Upstream release tags bump Cargo.toml but leave path-package versions at
    # 0.0.0 in Cargo.lock. Normalize only those versions in a disposable copy;
    # never resolve/update third-party dependencies or dirty the submodule.
    build_source = scratch / "source"
    shutil.copytree(
        source,
        build_source,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".git", "target", "node_modules", "__pycache__"),
        symlinks=True,
    )
    workspace = build_source / "codex-rs"
    metadata = json.loads(
        subprocess.check_output(
            [
                "cargo",
                f"+{toolchain}",
                "metadata",
                "--no-deps",
                "--format-version",
                "1",
            ],
            cwd=workspace,
            text=True,
        )
    )
    versions = {package["name"]: package["version"] for package in metadata["packages"]}
    chunks = (workspace / "Cargo.lock").read_text().split("[[package]]")
    for index in range(1, len(chunks)):
        package = tomllib.loads("[[package]]" + chunks[index])["package"][0]
        if "source" not in package and package["name"] in versions:
            chunks[index] = re.sub(
                r'(?m)^version = "[^"]+"$',
                f'version = "{versions[package["name"]]}"',
                chunks[index],
                count=1,
            )
    (workspace / "Cargo.lock").write_text("[[package]]".join(chunks))
    target = scratch / "target"
    env = {
        **os.environ,
        "CARGO_TARGET_DIR": str(target),
        # These build trees are never reused, so incremental data is wasted space.
        "CARGO_INCREMENTAL": "0",
        "STABLE_GIT_COMMIT": lock["revision"],
    }
    subprocess.run(
        [
            "cargo",
            f"+{toolchain}",
            "build",
            "--locked",
            "--profile",
            profile,
            "-p",
            "codex-app-server",
            "--bin",
            "codex-app-server",
        ],
        cwd=workspace,
        env=env,
        check=True,
    )
    name = "codex-app-server.exe" if os.name == "nt" else "codex-app-server"
    built = target / profile / name
    version_output = subprocess.check_output(
        [str(built), "--version"], text=True
    ).strip()
    if version_output != f"codex-app-server {version}":
        raise RuntimeError(f"Unexpected built version: {version_output}")
    output.mkdir(parents=True, exist_ok=True)
    staged = output / f"{name}.tmp"
    shutil.copy2(built, staged)
    os.replace(staged, output / name)
    # Retain upstream licensing alongside the source-built executable.
    shutil.copy2(source / "LICENSE", output / "LICENSE")
    notice = source / "NOTICE"
    if notice.is_file():
        shutil.copy2(notice, output / "NOTICE")
    manifest = {**lock, "profile": profile, "binary_sha256": sha256(output / name)}
    metadata = output / "build.json.tmp"
    metadata.write_text(json.dumps(manifest, indent=2) + "\n")
    os.replace(metadata, output / "build.json")
    print(f"Built {output / name} from {lock['revision']}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "codex")
    parser.add_argument("--output", type=Path, default=ROOT / ".runtime/codex")
    parser.add_argument(
        "--profile", choices=["release", "dev-small"], default="release"
    )
    args = parser.parse_args()
    try:
        build(args.source.resolve(), args.output.resolve(), args.profile)
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f"Codex build failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

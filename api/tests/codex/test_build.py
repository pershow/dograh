"""Build cleanup must preserve installed binaries and the original source."""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import build_codex


@pytest.fixture
def build_fixture(tmp_path, monkeypatch):
    source = tmp_path / "codex"
    workspace = source / "codex-rs"
    workspace.mkdir(parents=True)
    (workspace / "Cargo.toml").write_text('[workspace.package]\nversion = "1.2.3"\n')
    (workspace / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.95.0"\n')
    cargo_lock = '[[package]]\nname = "codex-app-server"\nversion = "0.0.0"\n'
    (workspace / "Cargo.lock").write_text(cargo_lock)
    (source / "LICENSE").write_text("license fixture")
    (source / "NOTICE").write_text("notice fixture")
    lock = {
        "revision": "test-revision",
        "version": "1.2.3",
        "rust_toolchain": "1.95.0",
        "cargo_lock_sha256": build_codex.sha256(workspace / "Cargo.lock"),
        "source_sha256": build_codex.source_digest(source),
    }
    lock_path = tmp_path / "source.json"
    lock_path.write_text(json.dumps(lock))
    monkeypatch.setattr(build_codex, "ROOT", tmp_path)
    monkeypatch.setattr(build_codex, "LOCK", lock_path)
    monkeypatch.setattr(build_codex.shutil, "which", lambda _: "/test/cargo")

    output = tmp_path / ".runtime/codex"
    output.mkdir(parents=True)
    name = "codex-app-server.exe" if os.name == "nt" else "codex-app-server"
    (output / name).write_text("previous executable")
    unrelated = tmp_path / ".runtime/unrelated.txt"
    unrelated.write_text("keep")

    def check_output(command, **kwargs):
        if command[0] == "cargo":
            return json.dumps(
                {"packages": [{"name": "codex-app-server", "version": "1.2.3"}]}
            )
        assert Path(command[0]).read_text() == "new executable"
        return "codex-app-server 1.2.3\n"

    monkeypatch.setattr(build_codex.subprocess, "check_output", check_output)

    def compile_source(command, *, cwd, env, check):
        assert check and "--locked" in command
        assert env["CARGO_INCREMENTAL"] == "0"
        assert 'version = "1.2.3"' in (cwd / "Cargo.lock").read_text()
        target = Path(env["CARGO_TARGET_DIR"]) / "dev-small"
        target.mkdir(parents=True)
        (target / name).write_text("new executable")
        (target / "intermediate.o").write_text("disposable")

    compiler = Mock(side_effect=compile_source)
    monkeypatch.setattr(build_codex.subprocess, "run", compiler)
    yield source, output, name, compiler
    assert (workspace / "Cargo.lock").read_text() == cargo_lock
    assert unrelated.read_text() == "keep"
    assert not list((tmp_path / ".runtime").glob("codex-build-*"))


def test_success_removes_intermediates_and_keeps_installed_runtime(build_fixture):
    source, output, name, compiler = build_fixture
    build_codex.build(source, output, "dev-small")
    compiler.assert_called_once()
    assert (output / name).read_text() == "new executable"
    manifest = json.loads((output / "build.json").read_text())
    assert manifest["binary_sha256"] == build_codex.sha256(output / name)
    assert (output / "LICENSE").read_text() == "license fixture"
    assert (output / "NOTICE").read_text() == "notice fixture"


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(101, ["cargo"]),
        OSError("No space left on device"),
        KeyboardInterrupt(),
    ],
    ids=["compile-failure", "disk-full", "ctrl-c"],
)
def test_failed_build_cleans_up_and_preserves_previous_runtime(build_fixture, error):
    source, output, name, compiler = build_fixture
    compile_source = compiler.side_effect

    def fail(*args, **kwargs):
        compile_source(*args, **kwargs)
        raise error

    compiler.side_effect = fail
    with pytest.raises(type(error)):
        build_codex.build(source, output, "dev-small")
    assert (output / name).read_text() == "previous executable"
    assert not (output / "build.json").exists()

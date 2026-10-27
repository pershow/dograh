"""Minimal configurations for isolated Dograh projects and internal jobs."""

import json
import os
import tempfile


def prepare_home(
    root,
    *,
    model,
    base_url,
    token,
    workspace=None,
    context_window=None,
    output_tokens=32768,
):
    home, workdir = root / "codex", workspace or root / "workspace"
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    config = {
        "model": model,
        "model_provider": "dograh",
        "approval_policy": "never",
        "sandbox_mode": "read-only",
        "web_search": "disabled",
    }
    if context_window:
        config["model_context_window"] = context_window
        config["model_auto_compact_token_limit"] = max(
            1024, min(context_window * 9 // 10, context_window - output_tokens - 8192)
        )
    text = "\n".join(f"{key} = {json.dumps(value)}" for key, value in config.items())
    text += """
[model_providers.dograh]
name = "Dograh model connection"
wire_api = "responses"
env_key = "DOGRAH_GATEWAY_TOKEN"
requires_openai_auth = false
supports_websockets = false
# The gateway owns bounded provider retries and reports them to the workbench.
# Do not multiply those retries in the transport and sampling layers.
request_max_retries = 0
stream_max_retries = 0
"""
    text += "base_url = " + json.dumps(base_url) + "\n"
    text += """
[features]
shell_tool = false
unified_exec = false
code_mode = false
code_mode_host = false
code_mode_prewarm = false
multi_agent = false
multi_agent_v2 = false
apps = false
connectors = false
plugins = false
remote_control = false
skip_host_skill_discovery = true
"""
    # Multiple sessions in one project can start simultaneously. Readers must
    # see a complete config; per-process gateway endpoints are thread overrides.
    with tempfile.NamedTemporaryFile(mode="w", dir=home, delete=False) as pending:
        pending.write(text)
    os.replace(pending.name, home / "config.toml")
    env = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "SYSTEMROOT", "SSL_CERT_FILE")
        if key in os.environ
    }
    env.update(
        {
            "CODEX_HOME": str(home),
            "DOGRAH_GATEWAY_TOKEN": token,
            "RUST_LOG": "error",
        }
    )
    return workdir, env

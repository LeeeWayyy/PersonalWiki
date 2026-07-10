"""Backend server entrypoint.

This is the app-callable replacement for the runtime behavior that used to live
in backend/run.sh. It prepares machine-local configuration before Uvicorn imports
app.main, because settings and ingest paths are read at import time.
"""
from __future__ import annotations

import os
import sys
from collections.abc import MutableMapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import app_config  # noqa: E402


class ServeConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeConfig:
    backend_dir: Path
    content_dir: Path
    host: str
    port: int
    messages: list[str] = field(default_factory=list)


def configure_environment(
    root: Path = ROOT,
    environ: MutableMapping[str, str] | None = None,
    validate_content: bool = True,
) -> RuntimeConfig:
    root = root.resolve()
    backend_dir = root / "backend"
    env = os.environ if environ is None else environ

    try:
        content_dir, messages = app_config.bootstrap_local_env(
            root,
            env,
            validate_content=validate_content,
            error_cls=ServeConfigError,
            content_hint=(
                "Set PW_CONTENT_DIR=/abs/path/to/wiki in backend/.env, "
                "or run python3 scripts/vendor_content.py."
            ),
        )
    except OSError as exc:
        raise ServeConfigError(f"backend: failed to prepare backend/.env: {exc}") from exc

    env.setdefault("PW_LLM_CMD_BASE_DIR", str(backend_dir))

    host = env.get("PW_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = app_config.parse_port(
        "PW_PORT",
        env.get("PW_PORT", "8787"),
        error_cls=ServeConfigError,
        invalid_message="backend: invalid {name}: {value}",
        range_message="backend: {name} must be between 1 and 65535: {port}",
    )

    return RuntimeConfig(
        backend_dir=backend_dir,
        content_dir=content_dir,
        host=host,
        port=port,
        messages=messages,
    )


def exec_uvicorn(config: RuntimeConfig, uvicorn_args: Sequence[str]) -> None:
    os.chdir(config.backend_dir)
    print(f"backend listening on http://{config.host}:{config.port}", flush=True)
    argv = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        config.host,
        "--port",
        str(config.port),
        *uvicorn_args,
    ]
    os.execvpe(sys.executable, argv, os.environ)


def main(argv: Sequence[str] | None = None) -> int:
    uvicorn_args = list(sys.argv[1:] if argv is None else argv)
    try:
        config = configure_environment()
    except ServeConfigError as exc:
        print(exc, file=sys.stderr)
        return 1

    for message in config.messages:
        print(message, flush=True)
    exec_uvicorn(config, uvicorn_args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

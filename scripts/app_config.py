#!/usr/bin/env python3
"""Machine-local Personal Wiki configuration helpers.

Safe env-file parsing, local wiki folder resolution, and backend auth-token
bootstrap.
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
from collections.abc import MutableMapping
from pathlib import Path

KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
BOOTSTRAPPED_ENV_KEY = "PW_LOCAL_ENV_BOOTSTRAPPED"


class AppConfigError(RuntimeError):
    pass


def parse_env_value(raw: str) -> str:
    value = raw.rstrip("\r").strip()
    double = re.match(r'^"(.*)"\s*(?:#.*)?$', value)
    if double:
        return double.group(1)
    single = re.match(r"^'(.*)'\s*(?:#.*)?$", value)
    if single:
        return single.group(1)
    return re.split(r"\s#", value, maxsplit=1)[0].rstrip()


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if re.match(r"^\s*#", line):
            continue
        match = KEY_RE.match(line)
        if not match:
            continue
        key, raw = match.groups()
        out[key] = parse_env_value(raw)
    return out


def local_env_updates(root: Path, environ: dict[str, str] | None = None) -> dict[str, str]:
    """Return env-file values to export.

    Explicit process environment wins. For keys not present in the original
    process environment, `backend/.env` overrides root `.env`.
    """
    root = root.resolve()
    environ = os.environ if environ is None else environ
    explicit = set(environ)
    updates: dict[str, str] = {}
    for path in (root / ".env", root / "backend" / ".env"):
        for key, value in parse_env_file(path).items():
            if key not in explicit:
                updates[key] = value
    return updates


def abs_path(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else root / path


def content_dir(root: Path, env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    value = env.get("PW_CONTENT_DIR") or str(root / "content")
    return abs_path(root, value)


def parse_port(
    name: str,
    value: str,
    *,
    error_cls: type[Exception] = AppConfigError,
    invalid_message: str | None = None,
    range_message: str | None = None,
) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        message = invalid_message or "{name} must be a number: {value}"
        raise error_cls(message.format(name=name, value=value)) from exc
    if port < 1 or port > 65535:
        message = range_message or "{name} must be between 1 and 65535: {port}"
        raise error_cls(message.format(name=name, value=value, port=port))
    return port


def validate_content_dir(
    path: Path,
    *,
    error_cls: type[Exception] = AppConfigError,
    hint: str = (
        "Set PW_CONTENT_DIR=/abs/path/to/wiki in backend/.env, or vendor once with "
        "python3 scripts/vendor_content.py."
    ),
) -> None:
    if not path.is_dir():
        raise error_cls(f"wiki folder not found at {path}\n{hint}")
    try:
        next(path.iterdir())
    except StopIteration as exc:
        raise error_cls(f"wiki folder is empty at {path}\n{hint}") from exc
    except OSError as exc:
        raise error_cls(f"cannot read wiki folder at {path}: {exc}") from exc


def bootstrap_local_env(
    root: Path,
    environ: MutableMapping[str, str] | None = None,
    *,
    validate_content: bool = True,
    error_cls: type[Exception] = AppConfigError,
    content_hint: str = (
        "Set PW_CONTENT_DIR=/abs/path/to/wiki in backend/.env, or vendor once with "
        "python3 scripts/vendor_content.py."
    ),
) -> tuple[Path, list[str]]:
    """Prepare local env-file config and return the resolved wiki folder.

    When app_start launches the backend it passes this prepared environment along.
    The marker avoids re-reading .env files and regenerating backend/.env in the
    child process while still validating the resolved content directory.
    """
    root = root.resolve()
    env = os.environ if environ is None else environ
    messages: list[str] = []
    if env.get(BOOTSTRAPPED_ENV_KEY) != "1":
        messages = ensure_backend_env(root, env)
        env.update(local_env_updates(root, env))
        env[BOOTSTRAPPED_ENV_KEY] = "1"

    resolved = content_dir(root, env).expanduser().resolve(strict=False)
    env["PW_CONTENT_DIR"] = str(resolved)
    if validate_content:
        validate_content_dir(
            resolved,
            error_cls=error_cls,
            hint=content_hint,
        )
    return resolved, messages


def generate_auth_token() -> str:
    return secrets.token_urlsafe(24)


def backend_env_token(path: Path) -> str:
    return parse_env_file(path).get("PW_AUTH_TOKEN", "")


def write_backend_auth_token(path: Path, token: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out: list[str] = []
    replaced = False
    for line in lines:
        if not replaced and KEY_RE.match(line) and KEY_RE.match(line).group(1) == "PW_AUTH_TOKEN":
            out.append(f"PW_AUTH_TOKEN={token}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        if out:
            out.append("")
        out.append(f"PW_AUTH_TOKEN={token}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def ensure_backend_env(root: Path, environ: dict[str, str] | None = None) -> list[str]:
    root = root.resolve()
    environ = os.environ if environ is None else environ
    env_file = root / "backend" / ".env"
    example = root / "backend" / ".env.example"
    messages: list[str] = []

    if not env_file.exists():
        shutil.copyfile(example, env_file)
        messages.append("created backend/.env")

    if environ.get("PW_AUTH_TOKEN"):
        return messages

    if backend_env_token(env_file):
        return messages

    write_backend_auth_token(env_file, generate_auth_token())
    try:
        env_file.chmod(0o600)
    except OSError:
        pass
    messages.append("generated backend auth token in backend/.env")
    return messages

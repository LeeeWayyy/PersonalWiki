#!/usr/bin/env python3
"""Start the local Personal Wiki app.

This replaces the process-management logic that used to live in top-level
run.sh. Shell remains as a compatibility wrapper; this module owns config
loading, dependency checks, port cleanup, backend launch, site build/serve, and
shutdown.
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from collections.abc import Callable, MutableMapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import app_config

ROOT = Path(__file__).resolve().parent.parent
MIN_NODE = (22, 12)
TRUTHY = {"1", "true", "yes", "on"}
FALSY = {"0", "false", "no", "off"}


class AppStartError(RuntimeError):
    pass


@dataclass(frozen=True)
class StartConfig:
    root: Path
    mode: str
    site_host: str
    site_port: int
    backend_host: str
    backend_port: int
    content_dir: Path
    open_ui: bool
    kill_ports: bool
    env: dict[str, str]
    messages: list[str] = field(default_factory=list)


def _log_default(message: str) -> None:
    print(message, flush=True)


def _truthy(value: str | None) -> bool:
    return bool(value and value.strip().lower() in TRUTHY)


def _falsy(value: str | None) -> bool:
    return bool(value and value.strip().lower() in FALSY)


def parse_node_version(value: str) -> tuple[int, int]:
    parts = value.strip().lstrip("v").split(".")
    try:
        return int(parts[0]), int(parts[1])
    except (IndexError, ValueError) as exc:
        raise AppStartError(f"could not parse Node version: {value}") from exc


def node_version_ok(version: tuple[int, int]) -> bool:
    return version >= MIN_NODE


def build_config(
    argv: Sequence[str],
    root: Path = ROOT,
    environ: MutableMapping[str, str] | None = None,
) -> StartConfig:
    parser = argparse.ArgumentParser(description="Start the local Personal Wiki app")
    parser.add_argument("--dev", action="store_true", help="run Astro dev server instead of build+static serve")
    parser.add_argument("--open", action="store_true", help="open the site URL after startup")
    parser.add_argument("--no-open", action="store_true", help="do not open the site URL")
    parser.add_argument(
        "--no-kill-ports",
        action="store_true",
        help="fail on busy ports instead of stopping existing listeners",
    )
    args = parser.parse_args(list(argv))

    root = root.resolve()
    env = dict(os.environ if environ is None else environ)
    try:
        content_dir, messages = app_config.bootstrap_local_env(root, env, error_cls=AppStartError)
    except OSError as exc:
        raise AppStartError(f"failed to prepare backend/.env: {exc}") from exc

    site_host = env.get("SITE_HOST", "127.0.0.1").strip() or "127.0.0.1"
    backend_host = env.get("PW_HOST", "127.0.0.1").strip() or "127.0.0.1"
    site_port = app_config.parse_port("SITE_PORT", env.get("SITE_PORT", "4321"), error_cls=AppStartError)
    backend_port = app_config.parse_port("PW_PORT", env.get("PW_PORT", "8787"), error_cls=AppStartError)
    env["SITE_PORT"] = str(site_port)
    env["PW_PORT"] = str(backend_port)
    env["PW_HOST"] = backend_host

    open_ui = (args.open or _truthy(env.get("PW_OPEN_UI"))) and not args.no_open
    kill_ports = not args.no_kill_ports and not _falsy(env.get("PW_KILL_PORTS"))

    return StartConfig(
        root=root,
        mode="dev" if args.dev else "preview",
        site_host=site_host,
        site_port=site_port,
        backend_host=backend_host,
        backend_port=backend_port,
        content_dir=content_dir,
        open_ui=open_ui,
        kill_ports=kill_ports,
        env=env,
        messages=messages,
    )


def _run_checked(cmd: Sequence[str], cwd: Path, env: dict[str, str], label: str) -> None:
    try:
        subprocess.run(list(cmd), cwd=cwd, env=env, check=True)
    except FileNotFoundError as exc:
        raise AppStartError(f"{label} failed: command not found: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise AppStartError(f"{label} failed with exit code {exc.returncode}") from exc


def ensure_node(env: dict[str, str]) -> tuple[str, str]:
    node = shutil.which("node")
    if not node:
        raise AppStartError("Node 22.12+ is required, but node is not installed")
    npm = shutil.which("npm")
    if not npm:
        raise AppStartError("npm is required, but npm is not installed")
    res = subprocess.run([node, "--version"], text=True, capture_output=True, env=env, check=False)
    version_text = (res.stdout or res.stderr).strip()
    if res.returncode != 0 or not node_version_ok(parse_node_version(version_text)):
        raise AppStartError(f"Node 22.12+ is required; found {version_text or 'unknown'}")
    return node, npm


def ensure_site_deps(config: StartConfig, npm: str, log: Callable[[str], None] = _log_default) -> None:
    if (config.root / "node_modules").is_dir():
        return
    log("installing site dependencies")
    _run_checked([npm, "install"], config.root, config.env, "npm install")


def _venv_python(root: Path) -> Path:
    if sys.platform == "win32":
        return root / "backend" / ".venv" / "Scripts" / "python.exe"
    return root / "backend" / ".venv" / "bin" / "python"


def ensure_backend_deps(config: StartConfig, log: Callable[[str], None] = _log_default) -> Path:
    backend_dir = config.root / "backend"
    venv_dir = backend_dir / ".venv"
    python = _venv_python(config.root)
    if not python.exists():
        log("creating backend virtualenv")
        _run_checked([sys.executable, "-m", "venv", str(venv_dir)], backend_dir, config.env, "backend venv setup")

    probe = subprocess.run(
        [str(python), "-c", "import uvicorn, fastapi, multipart"],
        cwd=backend_dir,
        env=config.env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if probe.returncode == 0:
        return python

    log("installing backend dependencies")
    _run_checked(
        [str(python), "-m", "pip", "install", "-q", "--upgrade", "pip"],
        backend_dir,
        config.env,
        "backend pip upgrade",
    )
    _run_checked(
        [str(python), "-m", "pip", "install", "-q", "-r", "requirements.txt"],
        backend_dir,
        config.env,
        "backend dependency install",
    )
    return python


def _port_accepts_connections(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) == 0


def listening_pids(port: int) -> list[int]:
    lsof = shutil.which("lsof")
    if not lsof:
        return []
    res = subprocess.run(
        [lsof, "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
        text=True,
        capture_output=True,
        check=False,
    )
    pids: list[int] = []
    for line in res.stdout.splitlines():
        try:
            pids.append(int(line.strip()))
        except ValueError:
            continue
    return pids


def free_port(
    host: str,
    port: int,
    label: str,
    kill_ports: bool,
    log: Callable[[str], None] = _log_default,
) -> None:
    pids = listening_pids(port)
    if not pids:
        if _port_accepts_connections(host, port):
            raise AppStartError(
                f"{label} port {port} is busy, but no listener PID could be found. "
                "Stop the process manually or install lsof."
            )
        return
    if not kill_ports:
        raise AppStartError(f"{label} port {port} is busy (pid {', '.join(map(str, pids))})")

    log(f"{label} port {port} busy (pid {', '.join(map(str, pids))}); stopping existing listener")
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not listening_pids(port):
            return
        time.sleep(0.2)

    remaining = listening_pids(port)
    if remaining:
        log(f"{label} port {port} still busy; force-stopping pid {', '.join(map(str, remaining))}")
    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    time.sleep(0.2)
    if listening_pids(port):
        raise AppStartError(f"{label} port {port} is still busy after cleanup")


def start_process(name: str, cmd: Sequence[str], cwd: Path, env: dict[str, str]) -> subprocess.Popen:
    try:
        return subprocess.Popen(list(cmd), cwd=cwd, env=env)
    except FileNotFoundError as exc:
        raise AppStartError(f"failed to start {name}: command not found: {cmd[0]}") from exc


def wait_for_health(url: str, proc: subprocess.Popen, timeout_s: float = 30) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AppStartError(f"backend process exited before /health was ready (exit {proc.returncode})")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.5)
    raise AppStartError(f"backend did not answer /health within {int(timeout_s)}s")


def stop_processes(processes: Sequence[subprocess.Popen], log: Callable[[str], None] = _log_default) -> None:
    live = [proc for proc in processes if proc.poll() is None]
    if live:
        log("stopping")
    for proc in live:
        proc.terminate()
    deadline = time.monotonic() + 4.0
    for proc in live:
        remaining = max(0.1, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            proc.kill()
    for proc in live:
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass


def supervise(processes: Sequence[tuple[str, subprocess.Popen]]) -> int:
    while True:
        for name, proc in processes:
            code = proc.poll()
            if code is not None:
                print(f"{name} exited with code {code}", flush=True)
                return code or 0
        time.sleep(0.5)


def _terminate_on_sigterm(*_: object) -> None:
    # Turn SIGTERM into the same clean-shutdown path as Ctrl-C (SIGINT), matching
    # the old run.sh `trap cleanup INT TERM` so `kill` never orphans the children.
    raise KeyboardInterrupt


def start_app(config: StartConfig, log: Callable[[str], None] = _log_default) -> int:
    for message in config.messages:
        log(message)

    try:
        signal.signal(signal.SIGTERM, _terminate_on_sigterm)
    except ValueError:
        pass  # not on the main thread (e.g. a test harness) — nothing to install

    node, npm = ensure_node(config.env)
    ensure_site_deps(config, npm, log)
    backend_python = ensure_backend_deps(config, log)

    free_port(config.backend_host, config.backend_port, "backend", config.kill_ports, log)
    free_port(config.site_host, config.site_port, "site", config.kill_ports, log)

    log(f"Personal Wiki starting ({config.mode} mode)")
    processes: list[tuple[str, subprocess.Popen]] = []
    try:
        backend = start_process(
            "backend",
            [str(backend_python), "-m", "app.serve"],
            config.root / "backend",
            config.env,
        )
        processes.append(("backend", backend))
        wait_for_health(f"http://{config.backend_host}:{config.backend_port}/health", backend)
        log(f"backend is up (http://localhost:{config.backend_port})")

        if config.mode == "dev":
            site = start_process(
                "site",
                [npm, "run", "dev", "--", "--host", config.site_host, "--port", str(config.site_port)],
                config.root,
                config.env,
            )
        else:
            log("building site (sync + astro + pagefind)")
            _run_checked([npm, "run", "build"], config.root, config.env, "site build")
            site = start_process(
                "site",
                [node, "scripts/serve.mjs", "--host", config.site_host, "--port", str(config.site_port)],
                config.root,
                config.env,
            )
        processes.append(("site", site))

        site_url = f"http://localhost:{config.site_port}"
        log("")
        log(f"site     {site_url}")
        log(f"backend  http://localhost:{config.backend_port}")
        log("Ctrl-C to stop both")
        if config.open_ui:
            webbrowser.open(site_url)
        return supervise(processes)
    except KeyboardInterrupt:
        return 0
    finally:
        stop_processes([proc for _, proc in processes], log)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        config = build_config(sys.argv[1:] if argv is None else argv)
        return start_app(config)
    except AppStartError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

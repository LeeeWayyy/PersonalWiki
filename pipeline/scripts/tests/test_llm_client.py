import json
import importlib.util
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("llm_client", ROOT / "scripts" / "llm_client.py")
llm_client = importlib.util.module_from_spec(SPEC)
sys.modules["llm_client"] = llm_client
SPEC.loader.exec_module(llm_client)

DERIVED_SPEC = importlib.util.spec_from_file_location("derived_lib", ROOT / "scripts" / "derived_lib.py")
derived_lib = importlib.util.module_from_spec(DERIVED_SPEC)
sys.modules["derived_lib"] = derived_lib
DERIVED_SPEC.loader.exec_module(derived_lib)

INGEST_SPEC = importlib.util.spec_from_file_location("ingest_mod", ROOT / "ingest.py")
ingest_mod = importlib.util.module_from_spec(INGEST_SPEC)
sys.modules["ingest_mod"] = ingest_mod
INGEST_SPEC.loader.exec_module(ingest_mod)


def write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _fake_codex(emit_events: bool = False) -> str:
    """A stand-in `codex` that mirrors the real --json/-o contract: the final
    message (what we treat as the diff) is written to the `-o <file>` path, and
    with emit_events it streams a couple of JSONL events to stdout for the
    progress reader. The written payload echoes argv + cwd so tests can assert on
    the flags/working dir codex was launched with."""
    events = ""
    if emit_events:
        events = (
            "print(json.dumps({'type':'token_count',"
            "'info':{'last_token_usage':{'input_tokens':84000}}}), flush=True)\n"
            "print(json.dumps({'type':'message','role':'assistant',"
            "'content':[{'type':'output_text','text':'writing pages'}]}), flush=True)\n"
        )
    return (
        "#!/usr/bin/env python3\n"
        "import os, sys, json\n"
        "argv = sys.argv[1:]\n"
        "stdin = sys.stdin.read()\n"
        "out = argv[argv.index('-o') + 1] if '-o' in argv else None\n"
        f"{events}"
        "payload = ' '.join(argv) + '\\nSTDIN=' + stdin + '\\nCWD=' + os.getcwd()\n"
        "open(out, 'w').write(payload) if out else sys.stdout.write(payload)\n"
    )


def _env_echo_codex() -> str:
    return (
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "argv = sys.argv[1:]\n"
        "out = argv[argv.index('-o') + 1]\n"
        "keys = ('PW_AUTH_TOKEN', 'TRANSCRIPT_REMOTE_TOKEN', 'PW_LLM_API_KEY', "
        "'OPENAI_API_KEY', 'CODEX_HOME')\n"
        "open(out, 'w').write(json.dumps({key: os.environ.get(key) for key in keys}))\n"
    )


def _fake_plain_cli() -> str:
    return (
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "keys = ('PW_AUTH_TOKEN', 'TRANSCRIPT_REMOTE_TOKEN', 'PW_LLM_API_KEY', "
        "'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'GEMINI_API_KEY')\n"
        "print(json.dumps({'argv': sys.argv[1:], 'stdin': sys.stdin.read(), "
        "'cwd': os.getcwd(), 'env': {key: os.environ.get(key) for key in keys}}))\n"
    )


def _silent_codex() -> str:
    return "#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n"


def _slow_quiet_codex() -> str:
    return (
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "argv = sys.argv[1:]\n"
        "out = argv[argv.index('-o') + 1]\n"
        "time.sleep(0.35)\n"
        "open(out, 'w').write('ok')\n"
    )


class _FakeApiResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


class LlmClientTests(unittest.TestCase):
    def test_command_override_honors_base_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "cmd"
            cwd = Path(tmp) / "cwd"
            base.mkdir()
            cwd.mkdir()
            write_executable(base / "stub.sh", "#!/usr/bin/env bash\ncat >/dev/null\nprintf ok\n")
            env = {
                "LLM_CMD": "./stub.sh",
                "PW_LLM_CMD_BASE_DIR": str(base),
                "PATH": os.environ.get("PATH", ""),
            }
            with patch.dict(os.environ, env, clear=True), patch("os.getcwd", return_value=str(cwd)):
                self.assertEqual(llm_client.complete_command("prompt", timeout=5), "ok")

    def test_execution_identity_captures_non_secret_provider_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            write_executable(bin_dir / "codex", _fake_codex())
            env = {
                "PW_LLM_PROVIDER": "codex",
                "PW_CODEX_REASONING_EFFORT": "high",
                "PW_CODEX_VERBOSITY": "medium",
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            }
            with patch.dict(os.environ, env, clear=True):
                identity = llm_client.execution_identity("analyzer-model")
            self.assertEqual(identity["provider"], "codex")
            self.assertEqual(identity["model"], "analyzer-model")
            self.assertEqual(identity["reasoning"], "high")
            self.assertEqual(identity["verbosity"], "medium")
            self.assertRegex(identity["codex_binary_fingerprint"], r"^[0-9a-f]{64}$")
            self.assertIsNone(identity["command_fingerprint"])

    def test_ignored_user_config_cannot_leak_model_or_xhigh_reasoning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            codex_home = root / "codex-home"
            bin_dir.mkdir()
            codex_home.mkdir()
            write_executable(bin_dir / "codex", _fake_codex())
            (codex_home / "config.toml").write_text(
                'model = "personal-model"\n'
                'model_reasoning_effort = "xhigh"\n',
                encoding="utf-8",
            )
            env = {
                "PW_LLM_PROVIDER": "codex",
                "CODEX_HOME": str(codex_home),
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            }
            with patch.dict(os.environ, env, clear=True):
                identity = llm_client.execution_identity()
                out = llm_client.complete_command("hello", timeout=5)

            self.assertIsNone(identity["model"])
            self.assertEqual(identity["reasoning"], "medium")
            self.assertIsNone(identity["codex_config_fingerprint"])
            self.assertNotIn("personal-model", out)
            self.assertNotIn("xhigh", out)
            self.assertIn('model_reasoning_effort="medium"', out)

    def test_codex_config_fingerprint_ignores_secrets_and_honors_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            codex_home = root / "codex-home"
            bin_dir.mkdir()
            codex_home.mkdir()
            write_executable(bin_dir / "codex", _fake_codex())
            config = codex_home / "config.toml"
            config.write_text(
                'model = "config-model"\napi_key = "secret-one"\n',
                encoding="utf-8",
            )
            env = {
                "PW_LLM_PROVIDER": "codex",
                "PW_LLM_MODEL": "env-model",
                "PW_CODEX_REASONING_EFFORT": "low",
                "PW_CODEX_VERBOSITY": "low",
                "CODEX_HOME": str(codex_home),
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            }
            with patch.dict(os.environ, env, clear=True):
                first = llm_client.execution_identity()
                config.write_text(
                    'model = "changed-config-model"\napi_key = "secret-two"\n',
                    encoding="utf-8",
                )
                second = llm_client.execution_identity()

            self.assertEqual(first["model"], "env-model")
            self.assertEqual(second["model"], "env-model")
            self.assertIsNone(first["codex_config_fingerprint"])
            self.assertEqual(first, second)
            self.assertNotIn("secret", json.dumps(first))

    def test_api_identity_redacts_credentials_and_query(self):
        env = {
            "PW_LLM_PROVIDER": "api",
            "PW_LLM_API_KEY": "secret",
            "PW_LLM_BASE_URL": "https://user:pass@example.test:8443/v1?token=secret",
        }
        with patch.dict(os.environ, env, clear=True):
            identity = llm_client.execution_identity("api-analyzer")
        self.assertEqual(identity["api_base_url"], "https://example.test:8443/v1")
        self.assertNotIn("secret", json.dumps(identity))

    def test_command_identity_changes_when_command_file_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "stub.sh"
            write_executable(script, "#!/usr/bin/env bash\nprintf one\n")
            env = {"LLM_CMD": str(script), "PATH": os.environ.get("PATH", "")}
            with patch.dict(os.environ, env, clear=True):
                first = llm_client.execution_identity("model")
                time.sleep(0.002)
                write_executable(script, "#!/usr/bin/env bash\nprintf changed\n")
                second = llm_client.execution_identity("model")
            self.assertNotEqual(
                first["command_fingerprint"], second["command_fingerprint"]
            )

    def test_command_model_override_matches_execution_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "stub.sh"
            write_executable(
                script,
                '#!/usr/bin/env bash\ncat >/dev/null\nprintf "%s" "$PW_LLM_MODEL"\n',
            )
            env = {
                "LLM_CMD": str(script),
                "PW_LLM_MODEL": "main-model",
                "PATH": os.environ.get("PATH", ""),
            }
            with patch.dict(os.environ, env, clear=True):
                identity = llm_client.execution_identity("analyzer-model")
                output = llm_client.complete_command(
                    "prompt", timeout=5, model="analyzer-model"
                )
            self.assertEqual(identity["model"], "analyzer-model")
            self.assertEqual(output, "analyzer-model")

    def test_codex_automation_flags_change_execution_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            write_executable(bin_dir / "codex", _fake_codex())
            base = {
                "PW_LLM_PROVIDER": "codex",
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            }
            with patch.dict(os.environ, base, clear=True):
                first = llm_client.execution_identity("model")
            changed = {
                **base,
                "PW_CODEX_IGNORE_RULES": "0",
                "PW_CODEX_DISABLE_SHELL": "0",
            }
            with patch.dict(os.environ, changed, clear=True):
                second = llm_client.execution_identity("model")
            self.assertNotEqual(
                first["codex_automation_fingerprint"],
                second["codex_automation_fingerprint"],
            )

    def test_enabled_codex_rules_are_fingerprinted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            codex_home = root / "codex-home"
            rules = codex_home / "rules"
            bin_dir.mkdir()
            rules.mkdir(parents=True)
            write_executable(bin_dir / "codex", _fake_codex())
            rule = rules / "default.rules"
            rule.write_text("allow one\n", encoding="utf-8")
            env = {
                "PW_LLM_PROVIDER": "codex",
                "PW_CODEX_IGNORE_RULES": "0",
                "CODEX_HOME": str(codex_home),
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            }
            with patch.dict(os.environ, env, clear=True):
                first = llm_client.execution_identity("model")
                rule.write_text("allow different behavior\n", encoding="utf-8")
                second = llm_client.execution_identity("model")
            self.assertNotEqual(
                first["codex_automation_fingerprint"],
                second["codex_automation_fingerprint"],
            )

    def test_workspace_instructions_are_always_fingerprinted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            workdir = root / "work"
            bin_dir.mkdir()
            workdir.mkdir()
            write_executable(bin_dir / "codex", _fake_codex())
            instructions = workdir / "AGENTS.md"
            instructions.write_text("first instruction\n", encoding="utf-8")
            env = {
                "PW_LLM_PROVIDER": "codex",
                "PW_CODEX_IGNORE_RULES": "1",
                "PW_CODEX_WORKDIR": str(workdir),
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            }
            with patch.dict(os.environ, env, clear=True):
                first = llm_client.execution_identity("model")
                instructions.write_text("changed instruction\n", encoding="utf-8")
                second = llm_client.execution_identity("model")
            self.assertNotEqual(
                first["codex_automation_fingerprint"],
                second["codex_automation_fingerprint"],
            )

    def test_unseeded_scratch_ancestor_instructions_are_fingerprinted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            scratch_root = root / "scratch"
            bin_dir.mkdir()
            scratch_root.mkdir()
            write_executable(bin_dir / "codex", _fake_codex())
            instructions = scratch_root / "AGENTS.md"
            instructions.write_text("first instruction\n", encoding="utf-8")
            env = {
                "PW_LLM_PROVIDER": "codex",
                "PW_CODEX_WORKDIR": str(root / "missing-workdir"),
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            }
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(llm_client.tempfile, "gettempdir", return_value=str(scratch_root)),
            ):
                first = llm_client.execution_identity("model")
                instructions.write_text("changed instruction\n", encoding="utf-8")
                second = llm_client.execution_identity("model")
            self.assertNotEqual(
                first["codex_automation_fingerprint"],
                second["codex_automation_fingerprint"],
            )

    def test_derived_lib_call_llm_honors_base_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "cmd"
            cwd = Path(tmp) / "cwd"
            base.mkdir()
            cwd.mkdir()
            write_executable(base / "stub.sh", "#!/usr/bin/env bash\ncat >/dev/null\nprintf ok\n")
            env = {
                "LLM_CMD": "./stub.sh",
                "PW_LLM_CMD_BASE_DIR": str(base),
                "PATH": os.environ.get("PATH", ""),
            }
            with patch.dict(os.environ, env, clear=True), patch("os.getcwd", return_value=str(cwd)):
                self.assertEqual(derived_lib.call_llm("prompt", 5), "ok")

    def test_ingest_llm_honors_base_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "cmd"
            cwd = Path(tmp) / "cwd"
            base.mkdir()
            cwd.mkdir()
            write_executable(base / "stub.sh", "#!/usr/bin/env bash\ncat >/dev/null\nprintf ok\n")
            env = {
                "LLM_CMD": "./stub.sh",
                "PW_LLM_CMD_BASE_DIR": str(base),
                "PATH": os.environ.get("PATH", ""),
            }
            with patch.dict(os.environ, env, clear=True), patch("os.getcwd", return_value=str(cwd)):
                self.assertEqual(ingest_mod.llm("prompt", soft=False), "ok")

    def test_ingest_renderer_disables_codex_shell_unless_overridden(self):
        for configured, expected in ((None, "1"), ("0", "0")):
            env = {"PW_LLM_PROVIDER": "codex"}
            if configured is not None:
                env["PW_CODEX_DISABLE_SHELL"] = configured
            seen = []

            def complete(*_args, **_kwargs):
                seen.append(os.environ.get("PW_CODEX_DISABLE_SHELL"))
                return "ok"

            with patch.dict(os.environ, env, clear=True), patch.object(
                ingest_mod.llm_client, "complete", side_effect=complete
            ):
                self.assertEqual(ingest_mod.llm("prompt", soft=False), "ok")
            self.assertEqual(seen, [expected])

    def test_codex_provider_uses_direct_argv_without_shell_bridge(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            write_executable(bin_dir / "codex", _fake_codex())
            env = {
                "PW_LLM_PROVIDER": "codex",
                "CODEX_HOME": str(Path(tmp) / "empty-codex-home"),
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            }
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(llm_client.provider(), "codex")
                out = llm_client.complete_command("hello", timeout=5)
            self.assertIn("--ignore-user-config", out)
            self.assertIn("--ignore-rules", out)
            self.assertIn("--disable shell_tool", out)
            self.assertIn('model_reasoning_effort="medium"', out)
            self.assertIn('model_verbosity="low"', out)
            self.assertNotIn(" -m ", out)
            self.assertIn("-o ", out)
            self.assertIn(" -", out)
            self.assertIn("STDIN=hello", out)

    def test_codex_subprocess_only_receives_provider_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            write_executable(bin_dir / "codex", _env_echo_codex())
            env = {
                "PW_LLM_PROVIDER": "codex",
                "PW_AUTH_TOKEN": "app-secret",
                "TRANSCRIPT_REMOTE_TOKEN": "transcript-secret",
                "PW_LLM_API_KEY": "fallback-secret",
                "OPENAI_API_KEY": "provider-secret",
                "CODEX_HOME": str(Path(tmp) / "codex-home"),
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            }
            with patch.dict(os.environ, env, clear=True):
                seen = json.loads(llm_client.complete_command("hello", timeout=5))
            self.assertIsNone(seen["PW_AUTH_TOKEN"])
            self.assertIsNone(seen["TRANSCRIPT_REMOTE_TOKEN"])
            self.assertIsNone(seen["PW_LLM_API_KEY"])
            self.assertEqual(seen["OPENAI_API_KEY"], "provider-secret")
            self.assertEqual(seen["CODEX_HOME"], env["CODEX_HOME"])

    def test_claude_cli_alias_uses_safe_plain_text_argv_and_scrubbed_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            workdir = root / "workset"
            bin_dir.mkdir()
            workdir.mkdir()
            write_executable(bin_dir / "claude", _fake_plain_cli())
            env = {
                "PW_LLM_PROVIDER": "claude",
                "PW_LLM_MODEL": "sonnet",
                "PW_LLM_WORKDIR": str(workdir),
                "PW_AUTH_TOKEN": "app-secret",
                "TRANSCRIPT_REMOTE_TOKEN": "transcript-secret",
                "PW_LLM_API_KEY": "fallback-secret",
                "ANTHROPIC_API_KEY": "claude-secret",
                "ANTHROPIC_AUTH_TOKEN": "claude-token",
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            }
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(llm_client.provider(), "claude-cli")
                identity = llm_client.execution_identity()
                seen = json.loads(llm_client.complete_command("hello", timeout=5))
            self.assertEqual(seen["stdin"], "hello")
            self.assertEqual(os.path.realpath(seen["cwd"]), os.path.realpath(workdir))
            self.assertEqual(seen["argv"], [
                "--safe-mode", "--print", "--output-format", "text",
                "--no-session-persistence", "--permission-mode", "plan",
                "--tools", "", "--model", "sonnet",
            ])
            self.assertIsNone(seen["env"]["PW_AUTH_TOKEN"])
            self.assertIsNone(seen["env"]["TRANSCRIPT_REMOTE_TOKEN"])
            self.assertIsNone(seen["env"]["PW_LLM_API_KEY"])
            self.assertEqual(seen["env"]["ANTHROPIC_API_KEY"], "claude-secret")
            self.assertEqual(seen["env"]["ANTHROPIC_AUTH_TOKEN"], "claude-token")
            self.assertRegex(identity["binary_fingerprint"], r"^[0-9a-f]{64}$")

    def test_agy_cli_alias_honors_override_timeout_and_legacy_workdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            workdir = root / "legacy-workset"
            bin_dir.mkdir()
            workdir.mkdir()
            write_executable(bin_dir / "agy", _fake_plain_cli())
            env = {
                "PW_LLM_PROVIDER": "agy-cli",
                "PW_CODEX_WORKDIR": str(workdir),
                "GEMINI_API_KEY": "agy-secret",
                "PW_AUTH_TOKEN": "app-secret",
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            }
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(llm_client.provider(), "agy-cli")
                seen = json.loads(llm_client.complete_command(
                    "hello", timeout=7, model="flash"
                ))
            self.assertEqual(seen["stdin"], "hello")
            self.assertEqual(os.path.realpath(seen["cwd"]), os.path.realpath(workdir))
            self.assertEqual(seen["argv"], [
                "--print", "--mode", "plan", "--sandbox",
                "--print-timeout", "7s", "--model", "flash",
            ])
            self.assertIsNone(seen["env"]["PW_AUTH_TOKEN"])
            self.assertEqual(seen["env"]["GEMINI_API_KEY"], "agy-secret")

    def test_codex_uses_pw_llm_model_when_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            write_executable(bin_dir / "codex", _fake_codex())
            env = {
                "PW_LLM_PROVIDER": "codex",
                "PW_LLM_MODEL": "gpt-5-codex",
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            }
            with patch.dict(os.environ, env, clear=True):
                out = llm_client.complete_command("hello", timeout=5)
            self.assertIn("-m gpt-5-codex", out)  # model passed through
            self.assertIn("STDIN=hello", out)     # prompt passed through stdin

    def test_codex_call_model_override_takes_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            write_executable(bin_dir / "codex", _fake_codex())
            env = {
                "PW_LLM_PROVIDER": "codex",
                "PW_LLM_MODEL": "expensive-ingest-model",
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            }
            with patch.dict(os.environ, env, clear=True):
                out = llm_client.complete_command("keywords", timeout=5, model="cheap-keyword-model")
            self.assertIn("-m cheap-keyword-model", out)
            self.assertNotIn("expensive-ingest-model", out)

    def test_codex_automation_profile_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            write_executable(bin_dir / "codex", _fake_codex())
            env = {
                "PW_LLM_PROVIDER": "codex",
                "PW_CODEX_IGNORE_RULES": "0",
                "PW_CODEX_DISABLE_SHELL": "1",
                "PW_CODEX_REASONING_EFFORT": "low",
                "PW_CODEX_VERBOSITY": "medium",
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            }
            with patch.dict(os.environ, env, clear=True):
                out = llm_client.complete_command("hello", timeout=5)
            self.assertIn("--ignore-user-config", out)
            self.assertNotIn("--ignore-rules", out)
            self.assertIn("--disable shell_tool", out)
            self.assertIn('model_reasoning_effort="low"', out)
            self.assertIn('model_verbosity="medium"', out)

    def test_codex_reuses_seeded_workdir_without_deleting_it(self):
        # When ingest seeds a workdir (candidate pages), codex must run there and
        # the dir must SURVIVE the call (ingest owns cleanup) — the modify case.
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            seeded = Path(tmp) / "workset"
            (seeded / "wiki" / "entities").mkdir(parents=True)
            write_executable(bin_dir / "codex", _fake_codex())
            env = {
                "PW_LLM_PROVIDER": "codex",
                "PW_CODEX_WORKDIR": str(seeded),
                "PW_CODEX_PROGRESS": "0",
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            }
            with patch.dict(os.environ, env, clear=True):
                out = llm_client.complete_command("hello", timeout=5)
            self.assertIn(f"-C {seeded}", out)
            cwd = [line for line in out.splitlines() if line.startswith("CWD=")][0][4:]
            self.assertEqual(os.path.realpath(cwd), os.path.realpath(seeded))  # ran in seeded dir
            self.assertTrue(seeded.is_dir())       # NOT deleted by llm_client

    def test_codex_streams_json_events_as_progress_and_diff_from_output_file(self):
        # --json events on stdout become stderr heartbeats; the diff comes from -o.
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            write_executable(bin_dir / "codex", _fake_codex(emit_events=True))
            env = {
                "PW_LLM_PROVIDER": "codex",
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            }
            buf = io.StringIO()
            with patch.dict(os.environ, env, clear=True), contextlib.redirect_stderr(buf):
                out = llm_client.complete_command("hello", timeout=5)
            progress = buf.getvalue()
            self.assertIn("turn 1 · 84k", progress)   # token_count → heartbeat
            self.assertIn("writing pages", progress)   # assistant message → heartbeat
            self.assertIn("hello", out)                # diff still returned (from -o)

    def test_codex_heartbeats_while_provider_is_quiet(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            write_executable(bin_dir / "codex", _slow_quiet_codex())
            env = {
                "PW_LLM_PROVIDER": "codex",
                "PW_CODEX_HEARTBEAT_S": "0.1",
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            }
            buf = io.StringIO()
            with patch.dict(os.environ, env, clear=True), contextlib.redirect_stderr(buf):
                out = llm_client.complete_command("hello", timeout=5)
            self.assertEqual(out, "ok")
            self.assertIn("codex · still running", buf.getvalue())

    def test_codex_timeout_kills_silent_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            write_executable(bin_dir / "codex", _silent_codex())
            env = {
                "PW_LLM_PROVIDER": "codex",
                "PW_CODEX_PROGRESS": "0",
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            }
            start = time.monotonic()
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(RuntimeError, "LLM command timed out after 1s"):
                    llm_client.complete_command("hello", timeout=1)
            self.assertLess(time.monotonic() - start, 5)

    def test_codex_progress_line_reads_turn_ctx_message_and_tool(self):
        state = {}
        # window comes from task_started, then token_count reports per-turn ctx
        self.assertIsNone(llm_client._codex_progress_line(
            {"payload": {"type": "task_started", "model_context_window": 258400}}, state))
        line = llm_client._codex_progress_line(
            {"payload": {"type": "token_count",
                         "info": {"last_token_usage": {"input_tokens": 84000}}}}, state)
        self.assertEqual(line, "codex · turn 1 · 84k/258k ctx")
        # assistant narrative
        msg = llm_client._codex_progress_line(
            {"payload": {"type": "message", "role": "assistant",
                         "content": [{"type": "output_text",
                                      "text": "creating  真核细胞.md\nlinking"}]}}, state)
        self.assertIn("creating 真核细胞.md linking", msg)
        self.assertIn("84k/258k ctx", msg)  # carries last-known ctx
        agent_msg = llm_client._codex_progress_line(
            {"payload": {"type": "agent_message", "message": "checking final diff"}}, state)
        self.assertEqual(agent_msg, "codex · 84k/258k ctx · checking final diff")
        # tool call: apply_patch add-file gets summarized
        tool = llm_client._codex_progress_line(
            {"payload": {"type": "custom_tool_call", "name": "apply_patch",
                         "arguments": "*** Begin Patch\n*** Add File: wiki/entities/x.md\n+..."}}, state)
        self.assertIn("apply_patch Add wiki/entities/x.md", tool)
        # unknown event → no line
        self.assertIsNone(llm_client._codex_progress_line({"payload": {"type": "noise"}}, state))

    def test_codex_is_isolated_from_repo_with_writable_scratch(self):
        # The overflow fix: codex runs in an empty scratch working root (-C) with
        # a writable sandbox there, and NEVER read-only, so it can't burn context
        # exploring the content repo or thrash on blocked scratch writes.
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            write_executable(bin_dir / "codex", _fake_codex())
            env = {
                "PW_LLM_PROVIDER": "codex",
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            }
            with patch.dict(os.environ, env, clear=True):
                out = llm_client.complete_command("hello", timeout=5)
            self.assertIn("--sandbox workspace-write", out)
            self.assertNotIn("read-only", out)
            self.assertIn("-C ", out)  # isolated working root passed
            # codex ran with cwd == the scratch dir, not the content repo.
            cwd = [ln for ln in out.splitlines() if ln.startswith("CWD=")][0][4:]
            self.assertIn("pw-codex-", cwd)

    def test_legacy_shell_bridge_is_treated_as_codex_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            write_executable(bin_dir / "codex", _fake_codex())
            env = {
                "LLM_CMD": "../pipeline/scripts/llm-codex.sh",
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            }
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(llm_client.command(), "")
                self.assertEqual(llm_client.provider(), "codex")
                self.assertIn("prompt", llm_client.complete_command("prompt", timeout=5))

    def test_explicit_api_provider_uses_chat_completion_without_enable_flag(self):
        seen = {}

        def fake_urlopen(req, timeout):
            seen["url"] = req.full_url
            seen["timeout"] = timeout
            seen["auth"] = req.get_header("Authorization")
            seen["body"] = json.loads(req.data.decode())
            return _FakeApiResponse({"choices": [{"message": {"content": " api answer \n"}}]})

        env = {
            "PW_LLM_PROVIDER": "openai",
            "PW_LLM_API_KEY": "sk-test",
            "PW_LLM_MODEL": "api-model",
            "LLM_CMD": "printf command-should-not-run",
        }
        with patch.dict(os.environ, env, clear=True), patch("urllib.request.urlopen", fake_urlopen):
            self.assertTrue(llm_client.configured())
            self.assertFalse(llm_client.command_configured())
            self.assertEqual(llm_client.provider(), "api")
            self.assertEqual(llm_client.model(), "api-model")
            self.assertEqual(llm_client.complete("hello", timeout=7, model="call-model"), "api answer")
        self.assertEqual(seen["url"], "https://api.openai.com/v1/chat/completions")
        self.assertEqual(seen["timeout"], 7)
        self.assertEqual(seen["auth"], "Bearer sk-test")
        self.assertEqual(seen["body"]["model"], "call-model")
        self.assertEqual(seen["body"]["messages"], [{"role": "user", "content": "hello"}])

    def test_explicit_api_provider_requires_key(self):
        with patch.dict(os.environ, {"PW_LLM_PROVIDER": "api"}, clear=True):
            self.assertFalse(llm_client.configured())
            self.assertEqual(llm_client.provider(), "api")
            with self.assertRaisesRegex(RuntimeError, "requires PW_LLM_API_KEY"):
                llm_client.complete("hello")

    def test_provider_failure_is_retried_once(self):
        with patch.dict(os.environ, {"LLM_CMD": "stub"}, clear=True), patch.object(
            llm_client, "complete_command", side_effect=[RuntimeError("transient"), "ok"]
        ) as complete:
            self.assertEqual(llm_client.complete("hello"), "ok")
        self.assertEqual(complete.call_count, 2)

    def test_failed_local_provider_does_not_silently_switch_identity(self):
        env = {
            "LLM_CMD": "stub",
            "PW_LLM_API_ENABLED": "1",
            "PW_LLM_API_KEY": "key",
        }
        with patch.dict(os.environ, env, clear=True), patch.object(
            llm_client, "complete_command", side_effect=RuntimeError("down")
        ) as local, patch.object(
            llm_client, "_complete_api", return_value="api answer"
        ) as api:
            with self.assertRaisesRegex(RuntimeError, "down"):
                llm_client.complete("hello")
        self.assertEqual(local.call_count, 2)
        api.assert_not_called()

    def test_empty_local_provider_does_not_silently_switch_identity(self):
        env = {
            "LLM_CMD": "stub",
            "PW_LLM_API_ENABLED": "1",
            "PW_LLM_API_KEY": "key",
        }
        with patch.dict(os.environ, env, clear=True), patch.object(
            llm_client, "complete_command", return_value=None
        ) as local, patch.object(
            llm_client, "_complete_api", return_value="api answer"
        ) as api:
            self.assertIsNone(llm_client.complete("hello"))
        self.assertEqual(local.call_count, 2)
        api.assert_not_called()

    def test_unknown_provider_errors(self):
        with patch.dict(os.environ, {"PW_LLM_PROVIDER": "banana"}, clear=True):
            self.assertFalse(llm_client.configured())
            self.assertIsNone(llm_client.provider())
            with self.assertRaisesRegex(RuntimeError, "unsupported PW_LLM_PROVIDER"):
                llm_client.complete("hello")

    def test_api_key_requires_explicit_enable(self):
        with patch.dict(os.environ, {"PW_LLM_API_KEY": "unused"}, clear=True):
            self.assertFalse(llm_client.configured())


if __name__ == "__main__":
    unittest.main()

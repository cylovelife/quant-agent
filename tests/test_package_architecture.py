"""Keep package boundaries and import-time behavior observable.

@author cylovelife
"""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src/quant_agent"


def test_all_runtime_code_lives_in_the_named_package():
    assert PACKAGE.is_dir()
    assert not list((ROOT / "src").glob("*.py"))
    for folder in {p.parent for p in PACKAGE.rglob("*.py")}:
        assert (folder / "__init__.py").is_file()


def test_domain_has_no_outward_dependencies():
    files = list((PACKAGE / "domain").rglob("*.py"))
    assert files
    forbidden = {"sqlite3", "requests", "curl_cffi", "akshare", "socket", "subprocess"}
    for path in files:
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert name.split(".")[0] not in forbidden, (path, name)
                if name.startswith("quant_agent."):
                    assert name.startswith("quant_agent.domain."), (path, name)
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    assert node.func.id != "open", path
                if isinstance(node.func, ast.Attribute):
                    assert node.func.attr not in {
                        "read_text",
                        "read_bytes",
                        "write_text",
                        "write_bytes",
                        "mkdir",
                        "connect",
                    }, path


def test_infrastructure_does_not_import_use_cases_or_cli():
    for path in (PACKAGE / "infrastructure").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = [x.name for x in node.names] if isinstance(node, ast.Import) else []
            if isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
            assert not any(
                name.startswith(("quant_agent.application.", "quant_agent.entrypoints."))
                for name in names
            ), path


def test_application_does_not_call_back_into_entrypoints():
    for path in (PACKAGE / "application").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = [x.name for x in node.names] if isinstance(node, ast.Import) else []
            if isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
            assert not any(name.startswith("quant_agent.entrypoints.") for name in names), path


def test_new_source_files_and_launcher_changes_invalidate_code_identity(tmp_path, monkeypatch):
    from quant_agent.runtime import code_identity

    package = tmp_path / "src/quant_agent"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('"""test package"""')
    launcher = tmp_path / "run.py"
    launcher.write_text("print('original')")
    monkeypatch.setattr(code_identity, "PACKAGE_ROOT", package)
    monkeypatch.setattr(code_identity, "SOURCE_ROOT", tmp_path)
    original = code_identity.code_signature()
    (package / "additional.py").write_text("value = 1")
    added = code_identity.code_signature()
    assert original != added
    launcher.write_text("print('changed')")
    assert code_identity.code_signature() != added


def test_installed_cli_works_outside_the_repository(tmp_path):
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["QUANT_AGENT_HOME"] = str(tmp_path / "workspace")
    result = subprocess.run(
        [sys.executable, "-m", "quant_agent", "--help"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "--simulate-order" in result.stdout and "--daily" in result.stdout
    assert not (tmp_path / "workspace").exists()


def test_research_cli_retains_runtime_timeout_and_exit_status(monkeypatch):
    import socket

    from quant_agent.application.investing import workflow
    from quant_agent.entrypoints import cli, research

    monkeypatch.setattr(workflow, "load_configs", lambda: ({}, {}))
    monkeypatch.setattr(workflow, "apply_runtime_config", lambda config: None)

    def execute(args, config, params, root):
        assert args.daily and args.offline
        assert socket.getdefaulttimeout() == 20
        return 7

    monkeypatch.setattr(research, "run_command", execute)
    previous = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(None)
        assert cli.main(["--daily", "--offline"]) == 7
    finally:
        socket.setdefaulttimeout(previous)


def test_importing_all_modules_does_not_create_workspace_or_connect(tmp_path):
    env = os.environ.copy()
    env.update(
        PYTHONPATH=str(ROOT / "src"),
        PYTHONDONTWRITEBYTECODE="1",
        QUANT_AGENT_HOME=str(tmp_path / "workspace"),
    )
    code = """
import importlib, json, pkgutil, socket
def reject(*args, **kwargs):
    raise AssertionError('network connection during import')
socket.socket.connect = reject
before = socket.getdefaulttimeout()
import quant_agent
modules = [m.name for m in pkgutil.walk_packages(quant_agent.__path__, 'quant_agent.')]
for name in modules:
    importlib.import_module(name)
assert socket.getdefaulttimeout() == before
assert not {'store', 'execution', 'data_quality', 'competition_data'} & set(__import__('sys').modules)
print(json.dumps({'modules': len(modules)}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["modules"] > 40
    assert not (tmp_path / "workspace").exists()

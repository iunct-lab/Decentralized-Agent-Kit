import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

spec = importlib.util.spec_from_file_location("ssm_exec", Path(__file__).parents[1] / "ssm_exec.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


@pytest.fixture
def fake_ssm(monkeypatch):
    import boto3
    calls = []
    creations = []
    reply = {"Type": "SecureString", "Value": "fake-test-key"}

    def get_parameter(**kw):
        calls.append(kw)
        return {"Parameter": reply}

    def client(*args, **kw):
        creations.append((args, kw))
        return SimpleNamespace(get_parameter=get_parameter)

    monkeypatch.setattr(boto3, "client", client)
    monkeypatch.setenv("ANTHROPIC_API_KEY_SSM_PARAMETER", "/switchboard/anthropic-api-key")
    return calls, creations, reply


def test_key_only_reaches_exec_environment(monkeypatch, capsys, fake_ssm):
    calls, creations, _ = fake_ssm
    executions = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stale")
    monkeypatch.setattr(launcher.os, "execvpe", lambda *args: executions.append(args))
    launcher.main(["uv", "run", "uvicorn", "dak_agent.server:app"])
    assert calls == [{"Name": "/switchboard/anthropic-api-key", "WithDecryption": True}]
    args, kw = creations[0]
    assert args == ("ssm",) and kw["region_name"] == "ap-northeast-1"
    assert kw["config"].retries["total_max_attempts"] == 1
    program, child_args, env = executions[0]
    assert program == "uv" and "fake-test-key" not in repr(child_args)
    assert env["ANTHROPIC_API_KEY"] == "fake-test-key"
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("value,type_", [("", "SecureString"), (None, "SecureString"), ("fake-test-key", "String")])
def test_invalid_parameter_never_starts_command(monkeypatch, capsys, fake_ssm, value, type_):
    calls, _, reply = fake_ssm
    reply.update(Value=value, Type=type_)
    monkeypatch.setattr(launcher.os, "execvpe", lambda *args: pytest.fail("must not exec"))
    assert launcher.main(["uv"]) == 1
    assert len(calls) == 1
    assert capsys.readouterr().err == "Anthropic SSM read failed; command was not started.\n"


def test_access_denied_is_not_retried_or_logged(monkeypatch, capsys):
    import boto3
    calls = []

    def get_parameter(**kw):
        calls.append(kw)
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "secret must not be echoed"}}, "GetParameter")

    monkeypatch.setenv("ANTHROPIC_API_KEY_SSM_PARAMETER", "/switchboard/anthropic-api-key")
    monkeypatch.setattr(boto3, "client", lambda *a, **kw: SimpleNamespace(get_parameter=get_parameter))
    monkeypatch.setattr(launcher.os, "execvpe", lambda *a: pytest.fail("must not exec"))
    assert launcher.main(["uv"]) == 1
    assert len(calls) == 1
    assert capsys.readouterr().err == "Anthropic SSM read failed; command was not started.\n"


def test_no_command_does_not_read_ssm(monkeypatch):
    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **kw: pytest.fail("must not read SSM"))
    assert launcher.main([]) == 2

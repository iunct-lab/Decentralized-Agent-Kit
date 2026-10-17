"""The properties of `deploy/aws/template.yaml` the cost and access limits rest on
(PBI #135): no automatic retry, bounded concurrency, Bedrock only for the listed
models, a budget notified twice, the key parameter as the only SSM read, and no key
value anywhere in `deploy/aws/`."""
import re
from pathlib import Path

import pytest
import yaml

DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "aws"


class _Loader(yaml.SafeLoader):
    pass


def _tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {suffix: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {suffix: loader.construct_sequence(node)}
    return {suffix: loader.construct_mapping(node)}


_Loader.add_multi_constructor("!", _tag)


@pytest.fixture(scope="module")
def resources():
    return yaml.load((DEPLOY / "template.yaml").read_text(), Loader=_Loader)["Resources"]


@pytest.fixture(scope="module")
def function(resources):
    return resources["DakFunction"]["Properties"]


def test_async_invocation_is_never_retried(function):
    config = function["EventInvokeConfig"]
    assert config["MaximumRetryAttempts"] == 0
    assert config["MaximumEventAgeInSeconds"] <= 60


def test_concurrency_is_reserved(function):
    assert function["ReservedConcurrentExecutions"] == {"Ref": "ReservedConcurrency"}


def _statements(function):
    """The policy statements, with the `Fn::If` wrappers opened: [(condition, statement)]."""
    out = []
    for s in function["Policies"][0]["Statement"]:
        if "If" in s:  # `!If [condition, statement, !Ref AWS::NoValue]`
            out.append((s["If"][0], s["If"][1]))
        else:
            out.append((None, s))
    return [(c, st) for c, st in out if "Action" in st]


def test_bedrock_is_limited_to_the_listed_models(function):
    bedrock = [(c, s) for c, s in _statements(function) if s["Action"] == "bedrock:InvokeModel"]
    assert len(bedrock) == 1
    condition, statement = bedrock[0]
    assert condition == "HasBedrock"
    assert statement["Resource"] == {"Split": [",", {"Ref": "BedrockModelArns"}]}


def test_api_key_parameter_is_the_only_ssm_read(function):
    ssm = [(c, s) for c, s in _statements(function) if str(s["Action"]).startswith("ssm:")]
    assert len(ssm) == 1
    condition, statement = ssm[0]
    assert condition == "HasApiKeyParameter" and statement["Action"] == "ssm:GetParameter"
    assert "${ApiKeyParameterName}" in statement["Resource"]["Sub"]
    assert "parameter${ApiKeyParameterName}" in statement["Resource"]["Sub"]


def test_table_access_is_only_get_put_update(function):
    actions = [a for _, s in _statements(function) if isinstance(s["Action"], list) for a in s["Action"]]
    assert sorted(actions) == ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]


def test_budget_notifies_at_50_and_100_percent(resources):
    notes = resources["Budget"]["Properties"]["NotificationsWithSubscribers"]
    assert sorted(n["Notification"]["Threshold"] for n in notes) == [50, 100]


def test_only_listed_roles_may_invoke(resources):
    permissions = [r for r in resources.values() if r["Type"] == "AWS::Lambda::Permission"]
    assert permissions and all(p["Properties"]["Action"] == "lambda:InvokeFunction" for p in permissions)
    assert all(str(p["Properties"]["Principal"]) != "*" for p in permissions)


def test_no_key_in_the_deploy_files():
    text = "\n".join(p.read_text() for p in DEPLOY.iterdir())
    # Names such as `ApiKeyParameterName` are expected; a key *value* is not.
    assert not re.search(r"AKIA[0-9A-Z]{16}|sk-ant-[A-Za-z0-9_-]{10,}", text)
    assert not re.search(r"(api_?key|secret|password)\s*[:=]\s*['\"]?[A-Za-z0-9/+_-]{16,}", text, re.I)

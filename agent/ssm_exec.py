"""Read the Anthropic SecureString inside the runtime, then exec the server.

Only the parameter name belongs in Docker configuration; never the API key.
"""
import os
import sys


def main(argv=None):
    command = sys.argv[1:] if argv is None else argv
    if not command:
        print("Usage: ssm_exec.py COMMAND [ARGS...]", file=sys.stderr)
        return 2
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError

    parameter_name = os.environ.get("ANTHROPIC_API_KEY_SSM_PARAMETER", "")
    if not parameter_name:
        print("ANTHROPIC_API_KEY_SSM_PARAMETER is required.", file=sys.stderr)
        return 2
    try:
        client = boto3.client("ssm", region_name="ap-northeast-1",
                              config=Config(connect_timeout=10, read_timeout=30,
                                            retries={"total_max_attempts": 1}))
        parameter = client.get_parameter(Name=parameter_name, WithDecryption=True)["Parameter"]
        key = parameter["Value"]
        if parameter["Type"] != "SecureString" or not isinstance(key, str) or not key.strip():
            raise ValueError("Invalid parameter")
    except (BotoCoreError, ClientError, ValueError, KeyError, TypeError):
        print("Anthropic SSM read failed; command was not started.", file=sys.stderr)
        return 1
    env = dict(os.environ)
    env["ANTHROPIC_API_KEY"] = key
    try:
        os.execvpe(command[0], command, env)
    except OSError:
        print("Could not start the requested command.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

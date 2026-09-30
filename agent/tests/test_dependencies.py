import os
import re
import tomllib
import unittest

_PYPROJECT = os.path.join(os.path.dirname(__file__), "..", "pyproject.toml")


class TestDirectDependencies(unittest.TestCase):
    def test_no_direct_openai_anthropic_pin(self):
        """dak_agent never imports openai/anthropic; models go through LiteLLM.

        LiteLLM requires openai itself and reaches anthropic/ models over HTTP
        without the anthropic SDK, so a direct pin only adds Dependabot noise.
        """
        with open(_PYPROJECT, "rb") as f:
            deps = tomllib.load(f)["project"]["dependencies"]
        names = {re.split(r"[\s\[<>=!~;]", dep, maxsplit=1)[0].lower() for dep in deps}
        self.assertEqual(names & {"openai", "anthropic"}, set())


if __name__ == "__main__":
    unittest.main()

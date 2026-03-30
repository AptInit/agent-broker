from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


class GenerateSkillTests(unittest.TestCase):
    def test_generate_skill_writes_allowlist_first_template(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            output_path = Path(tempdir) / "SKILL.md"
            result = subprocess.run(
                ["python3", "scripts/generate_skill.py", "--output", str(output_path)],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            rendered = output_path.read_text(encoding="utf-8")
            self.assertIn(
                "description: Use when messages from `echo`, `pwd`, `sleep` explicitly refer you to `agent-broker-user-diagnose` or `SKILL.md` for brokered CLI troubleshooting.",
                rendered,
            )
            self.assertIn(
                "## First sanity check: is the tool even allowlisted?",
                rendered,
            )
            self.assertIn(
                "The current configured allowlist from `agent_broker_v1/config.py` or `agent_broker_v1/config_local.py` is: `echo`, `pwd`, `sleep`.",
                rendered,
            )
            self.assertIn(
                "The current repo config is in `agent_broker_v1/config.py`, unless `agent_broker_v1/config_local.py` is present.",
                rendered,
            )
            self.assertIn(
                "Use this skill when you expected ordinary local CLI behavior but have signs that command execution is being brokered.",
                rendered,
            )
            self.assertIn(
                "a normal session usually will not expose it through a generated shim at all.",
                rendered,
            )
            self.assertIn(
                "This usually means something routed a non-allowlisted tool name through the broker.",
                rendered,
            )


if __name__ == "__main__":
    unittest.main()

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support import REPO_ROOT, assert_tmpdir_empty


class FreshTmpdirCleanupTests(unittest.TestCase):

    def test_representative_python_suite_leaves_no_temp_files_in_a_fresh_tmpdir(self):
        root = Path(tempfile.mkdtemp(prefix="idp-tmp-check-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)

        tmpdir = root / "tmp"
        tmpdir.mkdir()
        env = dict(os.environ, TMPDIR=str(tmpdir))

        for pattern in (
            "test_runner.py",
            "test_phase3.py",
            "test_handoff_hooks.py",
            "test_memory.py",
            "test_orca_backend.py",
        ):
            result = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", pattern],
                cwd=str(REPO_ROOT),
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                result.returncode,
                0,
                f"discover {pattern} under a fresh TMPDIR failed\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
            )

        assert_tmpdir_empty(tmpdir, label="fresh TMPDIR")

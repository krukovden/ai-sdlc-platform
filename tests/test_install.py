"""Installing must be one symlink, and refusing must be the default.

The value of installing rather than copying is that `git pull` in one checkout
updates every project. That only holds if the link points at the checkout, so
these tests pin the link — and, more importantly, pin what happens when
something is already sitting at the target. Overwriting a file the user put
there is data loss, not an upgrade, so the refusals matter more than the
happy path.
"""

import io
import contextlib
import os
import tempfile
import unittest
from pathlib import Path

from support import ScriptTestCase, load_script

install = load_script("install")


def output(func, *args, **kwargs):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        func(*args, **kwargs)
    return out.getvalue()


class QuietTestCase(ScriptTestCase):
    """These commands report to stdout by design; a test run is not their audience."""

    def setUp(self):
        redirect = contextlib.redirect_stdout(io.StringIO())
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)


class InstallTests(QuietTestCase):

    def setUp(self):
        super().setUp()
        self.target = Path(self.tmp.name) / "bin" / "idp"

    def test_links_the_command_at_the_checkout_rather_than_copying_it(self):
        install.install(self.target, force=False, dry_run=False)

        self.assertTrue(self.target.is_symlink())
        self.assertEqual(Path(os.readlink(self.target)), install.SOURCE)

    def test_creates_the_bin_directory_when_it_is_missing(self):
        install.install(self.target, force=False, dry_run=False)
        self.assertTrue(self.target.parent.is_dir())

    def test_a_dry_run_says_what_it_would_do_and_does_nothing(self):
        text = output(install.install, self.target, force=False, dry_run=True)

        self.assertIn("would link", text)
        self.assertFalse(self.target.exists())

    def test_installing_twice_is_not_an_error_and_changes_nothing(self):
        install.install(self.target, force=False, dry_run=False)
        text = output(install.install, self.target, force=False, dry_run=False)

        self.assertIn("already linked", text)
        self.assertTrue(self.target.is_symlink())

    def test_warns_when_the_directory_is_not_on_path(self):
        text = output(install.install, self.target, force=False, dry_run=True)
        self.assertIn("not on PATH", text)


class RefusalTests(QuietTestCase):
    """What is already there belongs to the user until they say otherwise."""

    def setUp(self):
        super().setUp()
        self.target = Path(self.tmp.name) / "idp"

    def test_exits_3_rather_than_overwriting_a_regular_file(self):
        self.target.write_text("#!/bin/sh\necho someone else's script\n")

        message = self.assert_exits(3, install.install, self.target,
                                    force=False, dry_run=False)

        self.assertIn("not a symlink", message)
        self.assertIn("someone else's script", self.target.read_text())

    def test_exits_3_rather_than_repointing_a_link_to_somewhere_else(self):
        self.target.symlink_to(Path(self.tmp.name) / "other-platform")

        message = self.assert_exits(3, install.install, self.target,
                                    force=False, dry_run=False)

        self.assertIn("other-platform", message)

    def test_force_repoints_a_link_that_belongs_to_another_checkout(self):
        self.target.symlink_to(Path(self.tmp.name) / "other-platform")
        install.install(self.target, force=True, dry_run=False)

        self.assertEqual(Path(os.readlink(self.target)), install.SOURCE)


class UninstallTests(QuietTestCase):

    def setUp(self):
        super().setUp()
        self.target = Path(self.tmp.name) / "idp"

    def test_removes_our_own_link(self):
        install.install(self.target, force=False, dry_run=False)
        install.uninstall(self.target, dry_run=False)

        self.assertFalse(self.target.exists() or self.target.is_symlink())

    def test_says_so_and_stops_when_nothing_is_installed(self):
        text = output(install.uninstall, self.target, dry_run=False)
        self.assertIn("nothing to do", text)

    def test_refuses_to_remove_a_link_that_points_elsewhere(self):
        self.target.symlink_to(Path(self.tmp.name) / "other-platform")
        message = self.assert_exits(3, install.uninstall, self.target, dry_run=False)

        self.assertIn("Refusing", message)
        self.assertTrue(self.target.is_symlink())

    def test_refuses_to_remove_a_regular_file(self):
        self.target.write_text("not ours")
        self.assert_exits(3, install.uninstall, self.target, dry_run=False)
        self.assertEqual(self.target.read_text(), "not ours")

    def test_a_dry_run_reports_without_removing(self):
        install.install(self.target, force=False, dry_run=False)
        text = output(install.uninstall, self.target, dry_run=True)

        self.assertIn("would remove", text)
        self.assertTrue(self.target.is_symlink())


class VersionTests(ScriptTestCase):

    def test_the_version_and_the_standard_are_both_stated(self):
        self.assertTrue(install.VERSION)
        self.assertTrue(install.STANDARD)


if __name__ == "__main__":
    unittest.main()

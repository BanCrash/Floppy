"""Selector cases for scripts/test.sh --affected."""

import os
import stat
import subprocess
import textwrap
from pathlib import Path

from django.test import SimpleTestCase

from config.affected_tests import (
    MODE_FULL,
    MODE_LABELS,
    MODE_NONE,
    MissingBaseError,
    changed_paths,
    select,
)

_REPO = Path(__file__).resolve().parents[3]


def _write(repo: Path, relative: str, source: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")


def _sample(repo: Path) -> None:
    _write(
        repo,
        "src/app/models/__init__.py",
        "from app.models.media import Movie\n",
    )
    _write(repo, "src/app/models/media.py", "class Movie:\n    pass\n")
    _write(repo, "src/app/models/music.py", "class Album:\n    pass\n")
    _write(repo, "src/app/services.py", "def run():\n    return None\n")
    _write(repo, "src/app/signals.py", "def ready():\n    return None\n")
    _write(
        repo,
        "src/app/tests/test_media.py",
        "from app.models import Movie\n",
    )
    _write(
        repo,
        "src/app/tests/test_services.py",
        "from app.services import run\n",
    )
    _write(
        repo,
        "src/app/tests/test_signals.py",
        "import app.signals\n",
    )
    _write(repo, "src/users/orphan.py", "VALUE = 1\n")


class AffectedTestsSelectionTests(SimpleTestCase):
    def setUp(self):
        self.repo = Path(self._tmpdir())
        _sample(self.repo)

    def _tmpdir(self) -> str:
        import tempfile

        self.addCleanup(lambda: None)
        path = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(path, ignore_errors=True))
        return path

    def _mode(self, *paths: str, hooks_dir: str | None = None):
        return select(list(paths), repo=self.repo, hooks_dir=hooks_dir)

    def test_changed_test_module_runs_that_module(self):
        mode, labels, _notes = self._mode("src/app/tests/test_media.py")
        self.assertEqual(mode, MODE_LABELS)
        self.assertEqual(labels, ["app.tests.test_media"])

    def test_reexported_submodule_selects_the_barrel_importer(self):
        mode, labels, _notes = self._mode("src/app/models/media.py")
        self.assertEqual(mode, MODE_LABELS)
        self.assertEqual(labels, ["app.tests.test_media"])

    def test_direct_import_selects_that_test(self):
        mode, labels, _notes = self._mode("src/app/services.py")
        self.assertEqual(mode, MODE_LABELS)
        self.assertEqual(labels, ["app.tests.test_services"])

    def test_unmapped_file_runs_the_app(self):
        mode, labels, notes = self._mode("src/app/models/music.py")
        self.assertEqual(mode, MODE_LABELS)
        self.assertEqual(labels, ["app"])
        self.assertIn("src/app/models/music.py", notes[0])

    def test_signal_module_runs_the_app(self):
        mode, labels, _notes = self._mode("src/app/signals.py")
        self.assertEqual(mode, MODE_LABELS)
        self.assertEqual(labels, ["app"])

    def test_template_runs_the_fast_suite(self):
        mode, _labels, _notes = self._mode("src/templates/base.html")
        self.assertEqual(mode, MODE_FULL)

    def test_lockfile_runs_the_fast_suite(self):
        mode, _labels, _notes = self._mode("uv.lock")
        self.assertEqual(mode, MODE_FULL)

    def test_docs_only_runs_nothing(self):
        mode, labels, _notes = self._mode("docs/plans/3-run-affected-tests.md")
        self.assertEqual(mode, MODE_NONE)
        self.assertEqual(labels, [])

    def test_deleted_test_module_is_omitted(self):
        mode, labels, _notes = self._mode("src/app/tests/test_gone.py")
        self.assertEqual(mode, MODE_NONE)
        self.assertEqual(labels, [])

    def test_untracked_test_module_is_included(self):
        _write(self.repo, "src/app/tests/test_new.py", "VALUE = 1\n")
        mode, labels, _notes = self._mode("src/app/tests/test_new.py")
        self.assertEqual(mode, MODE_LABELS)
        self.assertEqual(labels, ["app.tests.test_new"])

    def test_mcp_tests_do_not_escalate(self):
        mode, labels, notes = self._mode("mcp_server/tests/test_tool.py")
        self.assertEqual(mode, MODE_NONE)
        self.assertEqual(labels, [])
        self.assertIn("mcp_server/tests/test_tool.py", notes[0])

    def test_other_app_fallback_stays_on_that_app(self):
        mode, labels, _notes = self._mode("src/users/orphan.py")
        self.assertEqual(mode, MODE_LABELS)
        self.assertEqual(labels, ["users"])

    def test_hook_file_runs_the_app(self):
        _write(self.repo, "hooks/genre.py", "VALUE = 1\n")
        mode, labels, _notes = self._mode(
            "hooks/genre.py",
            hooks_dir=str(self.repo / "hooks"),
        )
        self.assertEqual(mode, MODE_LABELS)
        self.assertEqual(labels, ["app"])

    def test_app_fallback_covers_a_specific_label_in_that_app(self):
        mode, labels, _notes = self._mode(
            "src/app/models/music.py",
            "src/app/services.py",
        )
        self.assertEqual(mode, MODE_LABELS)
        self.assertEqual(labels, ["app"])

    def test_collector_returns_paths_when_base_exists(self):
        try:
            paths = changed_paths(_REPO)
        except MissingBaseError:
            self.skipTest("origin/latest is not available")
        self.assertIsInstance(paths, list)


class AffectedTestsScriptTests(SimpleTestCase):
    def _run(self, stdout: str) -> tuple[int, str]:
        bindir = Path(self._tmpdir())
        invoked = bindir / "invoked"
        uv = bindir / "uv"
        uv.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import sys
                args = sys.argv[1:]
                if any(arg.endswith("affected_tests.py") for arg in args):
                    sys.stdout.write({stdout!r})
                    raise SystemExit(0)
                open({str(invoked)!r}, "w").write(" ".join(args))
                raise SystemExit(0)
                """
            ),
            encoding="utf-8",
        )
        timeout = bindir / "timeout"
        timeout.write_text(
            textwrap.dedent(
                """\
                #!/bin/sh
                while [ $# -gt 0 ]; do
                  case "$1" in
                    --kill-after=*) shift ;;
                    [0-9]*) shift; break ;;
                    *) shift ;;
                  esac
                done
                exec "$@"
                """
            ),
            encoding="utf-8",
        )
        uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
        timeout.chmod(timeout.stat().st_mode | stat.S_IEXEC)
        env = os.environ.copy()
        env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
        result = subprocess.run(  # noqa: S603
            ["bash", str(_REPO / "scripts" / "test.sh"), "--affected"],  # noqa: S607
            cwd=_REPO,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        recorded = invoked.read_text(encoding="utf-8") if invoked.exists() else ""
        return result.returncode, recorded

    def _tmpdir(self) -> str:
        import tempfile

        path = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(path, ignore_errors=True))
        return path

    def test_none_does_not_start_django(self):
        code, recorded = self._run("none\n")
        self.assertEqual(code, 0)
        self.assertEqual(recorded, "")

    def test_labels_use_the_targeted_exclude(self):
        code, recorded = self._run("labels\napp.tests.test_media\n")
        self.assertEqual(code, 0)
        self.assertIn("app.tests.test_media", recorded)
        self.assertIn("--exclude-tag", recorded)
        self.assertIn("network", recorded)
        self.assertNotIn("slow", recorded)

    def test_full_uses_the_fast_suite_excludes(self):
        code, recorded = self._run("full\n")
        self.assertEqual(code, 0)
        self.assertIn("--exclude-tag", recorded)
        self.assertIn("slow", recorded)
        self.assertIn("network", recorded)
        self.assertIn("src/manage.py", recorded)

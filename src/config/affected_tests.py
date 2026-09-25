"""Turn a git diff into Django test labels.

The import walk is direct edges plus names re-exported from package
``__init__.py`` files. A transitive walk through ``app.models`` would select
almost every test. String imports and ``@patch`` targets are not parsed;
those misses fall back to the app label.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

APPS = ("app", "users", "integrations", "lists", "events", "api", "config")

MODE_NONE = "none"
MODE_FULL = "full"
MODE_LABELS = "labels"

# Lockfile, settings, and the runner. A change here runs the fast suite.
ESCAPE_HATCH = frozenset(
    {
        "uv.lock",
        "pyproject.toml",
        "src/manage.py",
        "scripts/test.sh",
        "src/config/settings.py",
        "src/config/test_settings.py",
        "src/config/__init__.py",
        "src/config/test_runner.py",
        "src/config/affected_tests.py",
    }
)

# Loaded by string from AppConfig.ready(), so a static importer set is not
# the set of tests that can fire them.
SIGNAL_MODULES = frozenset(
    {
        "app.signals",
        "app.signals_watch_state",
        "app.signals_music",
        "integrations.signals_state",
        "users.signals",
    }
)

_OUTSIDE_RUNNER = ("mcp_server/", "scripts/tests/")


class MissingBaseError(RuntimeError):
    """origin/latest is not in this clone."""


class GitFailedError(RuntimeError):
    """A git command exited non-zero."""


def changed_paths(repo: Path) -> list[str]:
    """Return branch commits plus staged, unstaged, and untracked paths.

    Raises:
        MissingBaseError: ``origin/latest`` cannot be resolved.
    """
    base = _git(repo, ["merge-base", "HEAD", "origin/latest"])
    if base is None:
        raise MissingBaseError
    sha = base[0] if base else ""
    if not sha:
        raise MissingBaseError
    names: set[str] = set()
    names.update(_git(repo, ["diff", "--name-only", f"{sha}...HEAD"]) or [])
    names.update(_git(repo, ["diff", "--name-only", "HEAD"]) or [])
    names.update(_git(repo, ["ls-files", "--others", "--exclude-standard"]) or [])
    return sorted(names)


def select(
    paths: list[str],
    *,
    repo: Path,
    hooks_dir: str | None = None,
) -> tuple[str, list[str], list[str]]:
    """Classify ``paths`` as ``none``, ``full``, or a sorted label list.

    Returns:
        Mode, labels (empty unless mode is ``labels``), and stderr notes.
    """
    notes: list[str] = []
    outside: list[str] = []
    force_full = False
    python_paths: list[str] = []
    hook_hit = False

    for path in paths:
        if path.startswith(_OUTSIDE_RUNNER):
            outside.append(path)
            continue
        if _is_hook(path, repo, hooks_dir):
            hook_hit = True
            continue
        if path in ESCAPE_HATCH or path.startswith(("src/templates/", "src/static/")):
            force_full = True
            continue
        if path.startswith("src/") and path.endswith(".py"):
            if _app_label(path) is None:
                force_full = True
            else:
                python_paths.append(path)
            continue

    if outside:
        notes.append("outside the Django runner: " + ", ".join(outside))
    if force_full:
        notes.append("cross-cutting or runner change: running the fast suite")
        return MODE_FULL, [], notes

    labels: set[str] = set()
    if hook_hit:
        labels.add("app")
    if python_paths:
        reverse = _reverse_index(repo)
        unmapped: list[str] = []
        for path in python_paths:
            module = _module_name(path)
            if module is None:
                continue
            if _is_test_module(path):
                if (repo / path).is_file():
                    labels.add(module)
                continue
            if module in SIGNAL_MODULES:
                label = _app_label(path)
                if label:
                    labels.add(label)
                continue
            importers = reverse.get(module, ())
            if importers:
                labels.update(importers)
                continue
            label = _app_label(path)
            if label:
                labels.add(label)
                unmapped.append(path)
        if unmapped:
            notes.append("no importing test for: " + ", ".join(unmapped))

    pruned = _prune(labels)
    if not pruned:
        return MODE_NONE, [], notes
    return MODE_LABELS, pruned, notes


def main() -> int:
    """Print ``none``, ``full``, or ``labels`` plus one label per line."""
    repo = Path.cwd()
    try:
        paths = changed_paths(repo)
    except MissingBaseError:
        sys.stderr.write("origin/latest is missing; fetch it and rerun\n")
        return 1
    except GitFailedError:
        return 1
    hooks = os.environ.get("MUSIC_HOOKS_DIR") or None
    mode, labels, notes = select(paths, repo=repo, hooks_dir=hooks)
    for note in notes:
        sys.stderr.write(note + "\n")
    sys.stdout.write(mode + "\n")
    if mode == MODE_LABELS:
        for label in labels:
            sys.stdout.write(label + "\n")
    return 0


def _git(repo: Path, args: list[str]) -> list[str] | None:
    """Run git and return stdout lines. ``None`` when merge-base fails."""
    result = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        if args[0] == "merge-base":
            return None
        if result.stderr:
            sys.stderr.write(result.stderr)
        raise GitFailedError
    return [line for line in result.stdout.splitlines() if line]


def _module_name(path: str) -> str | None:
    """Return the dotted module for ``src/**/*.py``."""
    if not path.startswith("src/") or not path.endswith(".py"):
        return None
    relative = path[len("src/") : -len(".py")]
    relative = relative.removesuffix("/__init__")
    return relative.replace("/", ".")


def _app_label(path: str) -> str | None:
    """Return the app name when ``path`` is under ``src/<app>/``."""
    prefix, _slash, rest = path.partition("/")
    app, sep, _tail = rest.partition("/")
    if prefix != "src" or not sep or app not in APPS:
        return None
    return app


def _is_test_module(path: str) -> bool:
    """Return whether ``path`` is a discoverable ``test_*.py`` module."""
    name = path.rsplit("/", 1)[-1]
    return (
        path.startswith("src/")
        and "/tests/" in path
        and name.startswith("test_")
        and name.endswith(".py")
    )


def _is_hook(path: str, repo: Path, hooks_dir: str | None) -> bool:
    """Return whether ``path`` is a music listen hook file."""
    if not hooks_dir:
        return False
    hooks = Path(hooks_dir)
    if not hooks.is_absolute():
        hooks = repo / hooks
    candidate = (repo / path).resolve()
    try:
        candidate.relative_to(hooks.resolve())
    except ValueError:
        return False
    return candidate.suffix == ".py" and not candidate.name.startswith("_")


def _prune(labels: set[str]) -> list[str]:
    """Drop module labels already covered by an app label in the same set."""
    apps = {label for label in labels if label in APPS}
    return [
        label
        for label in sorted(labels)
        if label in apps or label.split(".", 1)[0] not in apps
    ]


def _reverse_index(repo: Path) -> dict[str, set[str]]:
    """Map a production module to the test labels that import it."""
    src = repo / "src"
    if not src.is_dir():
        return {}
    known: set[str] = set()
    init_files: list[tuple[str, str]] = []
    test_files: list[tuple[str, str]] = []
    for file in src.rglob("*.py"):
        relative = file.relative_to(repo).as_posix()
        module = _module_name(relative)
        if module is None:
            continue
        known.add(module)
        if file.name == "__init__.py":
            init_files.append((relative, module))
        elif _is_test_module(relative):
            test_files.append((relative, module))

    reexports: dict[str, dict[str, str]] = {}
    for relative, module in init_files:
        reexports[module] = _reexport_names(
            (repo / relative).read_text(encoding="utf-8", errors="replace"),
            module,
            known,
        )

    reverse: dict[str, set[str]] = {}
    for relative, module in test_files:
        source = (repo / relative).read_text(encoding="utf-8", errors="replace")
        for imported in _direct_imports(source, module, reexports, known):
            reverse.setdefault(imported, set()).add(module)
    return reverse


def _reexport_names(source: str, package: str, known: set[str]) -> dict[str, str]:
    """Map names bound in a package ``__init__`` to the module they come from."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    names: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        base = _import_base(node, package, is_package=True)
        if not base:
            continue
        for alias in node.names:
            if alias.name == "*":
                continue
            child = f"{base}.{alias.name}"
            names[alias.name] = child if child in known else base
    return names


def _direct_imports(
    source: str,
    importer: str,
    reexports: dict[str, dict[str, str]],
    known: set[str],
) -> set[str]:
    """Return modules a test file imports, resolving re-exported names."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        base = _import_base(node, importer, is_package=False)
        if not base:
            continue
        found.add(base)
        exported = reexports.get(base, {})
        for alias in node.names:
            if alias.name == "*":
                found.update(exported.values())
                continue
            if alias.name in exported:
                found.add(exported[alias.name])
            child = f"{base}.{alias.name}"
            if child in known:
                found.add(child)
    return found


def _import_base(node: ast.ImportFrom, importer: str, *, is_package: bool) -> str:
    """Resolve the module an ``import from`` statement names."""
    if node.level:
        parts = importer.split(".")
        if not is_package:
            parts = parts[:-1]
        drop = node.level - 1
        if drop:
            parts = parts[:-drop] if drop <= len(parts) else []
        if node.module:
            parts.extend(node.module.split("."))
        return ".".join(parts)
    return node.module or ""


if __name__ == "__main__":
    raise SystemExit(main())

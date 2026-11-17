from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

INIT_EPISODE = ROOT / "scripts" / "init_episode.py"


def argparse_options(tree: ast.AST) -> set[str]:
    """Dest names declared with add_argument('--foo-bar') -> 'foo_bar'."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_argument"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                value = arg.value
                if value.startswith("--"):
                    names.add(value[2:].replace("-", "_"))
                elif value.startswith("-") and len(value) == 2:
                    names.add(value[1:])
    return names


def args_attributes(tree: ast.AST) -> set[str]:
    """Every ``args.<name>`` attribute read anywhere in the module."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "args":
                names.add(node.attr)
    return names


class InitEpisodeArgparseTests(unittest.TestCase):
    """Regression guard for the S01E12 init crash.

    Commit e55693b changed the shared-workspace call to
    ``shared_instance_root(ld.profile, ld.profile_path, args.episode)`` but
    ``init_episode.py`` never declared an ``--episode`` option, so every shared-mode
    run died with ``AttributeError: 'Namespace' object has no attribute 'episode'``.
    ``test_init_episode.py`` only imports the pure helpers and never calls ``main()``,
    so no existing test executed that branch.
    """

    def test_every_args_attribute_is_declared_by_add_argument(self):
        tree = ast.parse(INIT_EPISODE.read_text(encoding="utf-8"))
        defined = argparse_options(tree)
        used = args_attributes(tree)
        missing = sorted(used - defined)
        self.assertEqual(
            missing, [],
            f"init_episode.py reads args.{missing} but never declares it with "
            f"add_argument(); argparse will raise AttributeError at runtime. "
            f"declared={sorted(defined)} used={sorted(used)}",
        )

    def test_shared_workspace_call_does_not_reference_undeclared_episode_option(self):
        """Direct check of the exact expression that broke S01E12."""
        source = INIT_EPISODE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        defined = argparse_options(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Name) and func.id == "shared_instance_root"):
                continue
            for arg in node.args:
                if (isinstance(arg, ast.Attribute)
                        and isinstance(arg.value, ast.Name)
                        and arg.value.id == "args"):
                    self.assertIn(
                        arg.attr, defined,
                        f"shared_instance_root() is passed args.{arg.attr}, which is "
                        f"not an option of init_episode.py",
                    )

    def test_series_and_episode_identity_are_derived_before_workspace_resolution(self):
        """Workspace resolution uses the identity derived from the URL and selected series."""
        tree = ast.parse(INIT_EPISODE.read_text(encoding="utf-8"))
        main = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == "main"),
            None,
        )
        self.assertIsNotNone(main, "init_episode.py has no main()")
        identity_call = next((n for n in ast.walk(main)
                              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                              and n.func.id == "archive_episode_identity"), None)
        workspace_call = next((n for n in ast.walk(main)
                               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                               and n.func.id == "shared_instance_root"), None)
        self.assertIsNotNone(identity_call, "main() never resolves series/episode identity")
        self.assertIsNotNone(workspace_call, "main() never resolves an episode workspace")
        self.assertLess(identity_call.lineno, workspace_call.lineno,
                        "workspace must be selected only after URL/series identity is resolved")


if __name__ == "__main__":
    unittest.main()

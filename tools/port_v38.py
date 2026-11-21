#!/usr/bin/env python3
"""Port pure v38 modules and their tests mechanically (verbatim, provenance-stamped).

Usage:
  python tools/port_v38.py module <v38-tree> <scripts/x.py> <fstk/media/y.py> [--drop-main]
  python tools/port_v38.py tests  <v38-tree> <tests/test_x.py> <tests/characterization/test_v38_x.py>
                                  --from-module <v38 module name> --to <fstk module>
                                  [--only-uses name,name] [--skip-uses name,name]

``tests`` keeps each unittest method whose body references only the allowed names
(``--only-uses``) and none of ``--skip-uses``; imports are rewritten to the fstk module.
Ported bodies are not edited by hand, so a behaviour change is always an explicit diff.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path


def port_module(tree: Path, src_rel: str, dest: Path, drop_main: bool) -> None:
    src = (tree / src_rel).read_text(encoding="utf-8")
    src = re.sub(r"^#!.*\n", "", src)
    if drop_main:
        mod = ast.parse(src)
        cut = []
        for node in mod.body:
            if (isinstance(node, ast.FunctionDef) and node.name == "main") or (
                    isinstance(node, ast.If) and "__main__" in ast.get_source_segment(src, node)):
                cut.append((node.lineno, node.end_lineno))
        lines = src.splitlines()
        for start, end in sorted(cut, reverse=True):
            del lines[start - 1:end]
        src = "\n".join(lines).rstrip() + "\n"
    header = f"# Ported verbatim from v38 {src_rel} (tools/port_v38.py). Do not hand-edit ported logic\n" \
             "# without a characterization test that states the intended behaviour change.\n"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(header + src, encoding="utf-8")


def port_tests(tree: Path, src_rel: str, dest: Path, from_module: str, to: str,
               only_uses: set[str], skip_uses: set[str], extra_imports: list[str]) -> int:
    src = (tree / src_rel).read_text(encoding="utf-8")
    mod = ast.parse(src)
    helpers, classes = [], []
    kept = 0
    for node in mod.body:
        if isinstance(node, (ast.FunctionDef, ast.Assign)) and not (
                isinstance(node, ast.FunctionDef) and node.name.startswith("test")):
            seg = ast.get_source_segment(src, node)
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | \
                    {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
            if not (names & skip_uses):
                helpers.append(seg)
        if isinstance(node, ast.ClassDef):
            methods = []
            for item in node.body:
                if not isinstance(item, ast.FunctionDef):
                    continue
                seg = ast.get_source_segment(src, item)
                names = {n.id for n in ast.walk(item) if isinstance(n, ast.Name)} | \
                        {n.attr for n in ast.walk(item) if isinstance(n, ast.Attribute)}
                if names & skip_uses:
                    continue
                if only_uses and item.name.startswith("test") and not (names & only_uses):
                    continue
                methods.append("    " + seg.replace("\n", "\n"))
                kept += item.name.startswith("test")
            if methods:
                bases = ", ".join(ast.get_source_segment(src, b) for b in node.bases)
                classes.append(f"class {node.name}({bases}):\n" + "\n\n".join(methods))
    imports = sorted({ln for ln in src.splitlines()
                      if re.match(r"^(import|from) (json|math|re|io|tempfile|unittest|random|struct|wave|"
                                  r"itertools|hashlib|os|types|unittest\.mock|pathlib|collections|numpy)\b", ln)})
    names = ", ".join(sorted(only_uses)) if only_uses else "*"
    body = [f'"""Characterization: v38 {src_rel} ({kept} tests), ported by tools/port_v38.py."""',
            "from __future__ import annotations", "", *imports, *extra_imports,
            (f"from {to} import {names}  # noqa: F401" if only_uses else ""), "", "", *[h + "\n\n" for h in helpers],
            *[c + "\n\n\n" for c in classes]]
    text = "\n".join(body).rstrip() + "\n"
    text = text.replace(f"{from_module}.", f"{to.rsplit('.', 1)[-1]}.")
    dest.write_text(text, encoding="utf-8")
    return kept


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        print(__doc__)
        return 2
    kind, tree, src_rel, dest = argv[0], Path(argv[1]), argv[2], Path(argv[3])
    opts = argv[4:]

    def opt(name: str, default: str = "") -> str:
        return opts[opts.index(name) + 1] if name in opts else default

    if kind == "module":
        port_module(tree, src_rel, dest, "--drop-main" in opts)
        print(f"ported {src_rel} → {dest}")
    else:
        kept = port_tests(tree, src_rel, dest, opt("--from-module"), opt("--to"),
                          {s for s in opt("--only-uses").split(",") if s},
                          {s for s in opt("--skip-uses").split(",") if s},
                          [s for s in opt("--extra-imports").split(";") if s])
        print(f"ported {kept} tests from {src_rel} → {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

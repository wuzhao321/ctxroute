"""Static public-test selection. This file also runs inside the task container.

Only git-listed Python source at the allowed base and the task's working diff is
read. Repository modules are never imported. No grading data or issue-specific
test names enter the selection algorithm.
"""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


def module_name(path: str) -> str:
    return path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")


def definitions(tree: ast.AST, module: str) -> dict[str, ast.AST]:
    found: dict[str, ast.AST] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + "." + child.name
                found[name] = child
                visit(child, name)

    visit(tree, module)
    return found


def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = dotted(node.value)
        return prefix + "." + node.attr if prefix else ""
    return ""


def imports(tree: ast.AST, path: str) -> dict[str, str]:
    module = module_name(path)
    package = module if path.endswith("/__init__.py") else module.rpartition(".")[0]
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom):
            parts = package.split(".")
            base = ".".join(parts[: len(parts) - node.level + 1]) if node.level else ""
            origin = ".".join(p for p in (base, node.module or "") if p)
            for alias in node.names:
                aliases[alias.asname or alias.name] = origin + "." + alias.name
    return aliases


def select_tests(
    original: dict[str, str],
    current: dict[str, str],
    changed: list[str],
    *,
    caller_depth: int = 2,
    max_files: int = 8,
    target_contract: bool = False,
) -> dict[str, Any]:
    trees: dict[str, ast.AST] = {}
    nodes: dict[str, ast.AST] = {}
    paths: dict[str, str] = {}
    parse_errors: list[str] = []
    # Existing public tests remain anchored to base, so changing a test cannot
    # remove it from regression selection. Modified/new test functions are
    # separately proposed as target tests.
    sources = {**original, **{p: s for p, s in current.items() if not p.startswith("tests/")}}
    for path, source in sources.items():
        try:
            tree = ast.parse(source)
        except SyntaxError:
            parse_errors.append(path)
            continue
        trees[path] = tree
        for name, node in definitions(tree, module_name(path)).items():
            nodes[name], paths[name] = node, path
    seeds: set[str] = set()
    target: set[str] = set()
    modified_public_tests: list[str] = []
    for path in changed:
        try:
            before = definitions(ast.parse(original.get(path, "")), module_name(path))
            after = definitions(ast.parse(current.get(path, "")), module_name(path))
        except SyntaxError:
            parse_errors.append(path)
            continue
        changed_names = {
            name
            for name in before.keys() | after.keys()
            if name not in before
            or name not in after
            or ast.dump(before[name]) != ast.dump(after[name])
        }
        if path.startswith("tests/"):
            modified_public_tests.extend(
                name
                for name in changed_names
                if name in before
                and isinstance(before[name], (ast.FunctionDef, ast.AsyncFunctionDef))
            )
            for name in changed_names:
                if name in after and name.rsplit(".", 1)[-1].startswith("test"):
                    target.add(
                        path + "::" + name.removeprefix(module_name(path) + ".").replace(".", "::")
                    )
        else:
            seeds.update(changed_names or (name for name in nodes if paths[name] == path))
    reverse: dict[str, set[str]] = {}
    leaves: dict[str, list[str]] = {}
    for name in nodes:
        leaves.setdefault(name.rsplit(".", 1)[-1], []).append(name)
    for name, node in nodes.items():
        if isinstance(node, ast.ClassDef):
            # Constructing a class does not execute every method. Constructor
            # calls are represented by a single edge to __init__, if present.
            constructor = name + ".__init__"
            if constructor in nodes:
                reverse.setdefault(constructor, set()).add(name)
            continue
        path = paths[name]
        module = module_name(path)
        aliases = imports(trees[path], path)
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            called = dotted(call.func)
            if not called:
                continue
            first, _, tail = called.partition(".")
            resolved = aliases.get(first, first) + ("." + tail if tail else "")
            candidates = [resolved, module + "." + resolved]
            if first in {"self", "cls"}:
                candidates.insert(0, name.rpartition(".")[0] + "." + tail)
            matches = [candidate for candidate in candidates if candidate in nodes]
            if not matches and len(leaves.get(called.rsplit(".", 1)[-1], [])) == 1:
                matches = leaves[called.rsplit(".", 1)[-1]]
            for callee in matches:
                reverse.setdefault(callee, set()).add(name)
    reached = {name: [name] for name in seeds}
    frontier = set(seeds)
    # One edge reaches direct tests; the configured additional depth admits
    # public callers, context managers and their tests.
    for _ in range(caller_depth + 1):
        new: set[str] = set()
        for callee in sorted(frontier):
            for caller in sorted(reverse.get(callee, ())):
                if caller not in reached:
                    reached[caller] = reached[callee] + [caller]
                    new.add(caller)
        frontier = new
    selected: dict[str, list[str]] = {}
    for name, chain in sorted(reached.items()):
        path = paths.get(name, "")
        if path.startswith("tests/") and name.rsplit(".", 1)[-1].startswith("test"):
            selector = path + "::" + name.removeprefix(module_name(path) + ".").replace(".", "::")
            selected[selector] = chain
    # If a changed module's dynamic dispatch yields no static test edge, expose
    # public test modules importing it, with explicit weaker attribution.
    if seeds and not selected:
        affected = {module_name(p) for p in changed if not p.startswith("tests/")}
        for path, tree in trees.items():
            if path.startswith("tests/") and any(
                value == mod or value.startswith(mod + ".")
                for value in imports(tree, path).values()
                for mod in affected
            ):
                selected[path] = ["public_module_import_fallback"]
    files = {s.split("::")[0] for s in selected} | {s.split("::")[0] for s in target}
    relevant_errors = sorted(set(parse_errors) & set(changed))
    complete = (
        bool(selected)
        and len(files) <= max_files
        and not relevant_errors
        and not modified_public_tests
    )
    result: dict[str, Any] = {
        "changed_functions": sorted(seeds),
        "changed_files": changed,
        "target": sorted(target),
        "regression": sorted(selected) if complete else [],
        "reasons": selected,
        "selection_complete": complete,
        "selection_issue": "" if complete else "No reliable bounded public regression selection",
        "parse_errors": relevant_errors,
        "modified_existing_test_functions": sorted(modified_public_tests),
        "coverage": "static_selection_not_proof_of_behavioral_coverage",
    }
    if target_contract:
        target_files = sorted({s.split("::")[0] for s in target})
        asserted: list[str] = []
        for selector in sorted(target):
            file, _, qualified = selector.partition("::")
            tree = ast.parse(current.get(file, ""))
            node = definitions(tree, module_name(file)).get(
                module_name(file) + "." + qualified.replace("::", ".")
            )
            if node is not None and any(
                isinstance(n, ast.Assert) and not isinstance(n.test, ast.Constant)
                for n in ast.walk(node)
            ):
                asserted.append(selector)
        result["target_contract"] = {
            "asserted_selectors": asserted,
            "file_sha256": {
                p: hashlib.sha256(current.get(p, "").encode()).hexdigest() for p in target_files
            },
            "implementation_changed": any(not p.startswith("tests/") for p in changed),
            "test_only_valid": bool(target)
            and set(asserted) == target
            and not relevant_errors
            and not modified_public_tests
            and len(files) <= max_files,
        }
    return result


def inspect_repository(
    base: str, caller_depth: int, max_files: int, target_contract: bool = False
) -> dict[str, Any]:
    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], text=True, timeout=10)

    names = git("ls-tree", "-r", "--name-only", base).splitlines()
    changed = git("diff", "--name-only", base, "--").splitlines()
    names = sorted({p for p in names + changed if p.endswith(".py")})
    if len(names) > 12000:
        raise ValueError("Public Python inventory exceeds selector bound")
    original: dict[str, str] = {}
    current: dict[str, str] = {}
    total = 0
    root = Path.cwd().resolve()
    # One git archive-style batch avoids a process per file. Only base Python
    # blobs requested by ls-tree are read, not git history or grading assets.
    requested = [f"{base}:{p}" for p in names]
    data = subprocess.run(
        ["git", "cat-file", "--batch"],
        input=("\n".join(requested) + "\n").encode(),
        capture_output=True,
        check=True,
        timeout=20,
    ).stdout
    offset = 0
    for path in names:
        end = data.index(b"\n", offset)
        header = data[offset:end].split()
        offset = end + 1
        if header[-1] != b"missing":
            size = int(header[-1])
            original[path] = data[offset : offset + size].decode(errors="replace")
            offset += size + 1
            total += size
        file = root / path
        if file.is_file() and file.resolve().is_relative_to(root) and path in changed:
            if file.stat().st_size > 2_000_000:
                raise ValueError("Changed source exceeds selector bound")
            current[path] = file.read_text(errors="replace")
    if total > 40_000_000:
        raise ValueError("Public source exceeds selector bound")
    result = select_tests(
        original,
        current,
        [p for p in changed if p.endswith(".py")],
        caller_depth=caller_depth,
        max_files=max_files,
        target_contract=target_contract,
    )
    result["base_commit"] = base
    if target_contract:
        result["target_contract"]["implementation_changed"] = any(
            not p.startswith("tests/") for p in changed
        )
    return result


if __name__ == "__main__":
    import sys

    print(
        json.dumps(
            inspect_repository(
                sys.argv[1],
                int(sys.argv[2]),
                int(sys.argv[3]),
                len(sys.argv) > 4 and sys.argv[4] == "1",
            )
        )
    )

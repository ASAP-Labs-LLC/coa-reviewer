"""The updater's CLI surface must be documented, and stay documented.

`RELEASING.md` explains the *release* workflow narratively. It does not
enumerate the updater's commands and flags, so a flag added to `updater.py`
could ship with no reference anywhere. This test is the drift guard: it reads
the real argparse surface out of `updater.py` and fails when a command or a
flag is missing from `docs/UPDATER-FLAGS.md`.

It parses the source with `ast` rather than importing `updater`, so collecting
this test never runs the updater's module-level code.
"""
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UPDATER = REPO / "deploy" / "updater" / "updater.py"
FLAGS_DOC = REPO / "docs" / "UPDATER-FLAGS.md"


def _cli_surface() -> tuple[list[str], list[str]]:
    """(commands, flags) as argparse actually defines them."""
    tree = ast.parse(UPDATER.read_text(encoding="utf-8"))
    commands: list[str] = []
    flags: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_argument"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        name = node.args[0].value
        if not isinstance(name, str):
            continue
        if name.startswith("-"):
            flags.append(name)
            continue
        for kw in node.keywords:
            if kw.arg == "choices" and isinstance(kw.value, (ast.List, ast.Tuple)):
                commands.extend(
                    elt.value for elt in kw.value.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                )
    return sorted(set(commands)), sorted(set(flags))


def test_updater_exposes_a_cli_surface_we_can_read() -> None:
    """Guard the guard: if this returns nothing, the test below is vacuous."""
    commands, flags = _cli_surface()
    assert commands, "parsed no subcommands out of updater.py"
    assert flags, "parsed no flags out of updater.py"


def test_flags_doc_exists() -> None:
    assert FLAGS_DOC.is_file(), f"missing {FLAGS_DOC.relative_to(REPO)}"


def test_every_command_is_documented() -> None:
    commands, _ = _cli_surface()
    text = FLAGS_DOC.read_text(encoding="utf-8")
    missing = [c for c in commands if f"`{c}`" not in text]
    assert not missing, f"undocumented updater commands: {missing}"


def test_every_flag_is_documented() -> None:
    _, flags = _cli_surface()
    text = FLAGS_DOC.read_text(encoding="utf-8")
    missing = [f for f in flags if f"`{f}`" not in text]
    assert not missing, f"undocumented updater flags: {missing}"


def test_releasing_md_points_at_the_flags_doc() -> None:
    """A reference nobody can find is not documentation."""
    releasing = (REPO / "RELEASING.md").read_text(encoding="utf-8")
    assert "UPDATER-FLAGS.md" in releasing, (
        "RELEASING.md does not link to docs/UPDATER-FLAGS.md"
    )

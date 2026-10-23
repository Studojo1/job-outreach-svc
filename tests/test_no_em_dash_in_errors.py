"""Messages the API hands to students must not contain em or en dashes.

The discovery error card on 8 Oct 26 read "Your progress is saved — please try
again shortly." The frontend prints `detail` verbatim, so the only place to
keep dashes out is here. This scans every `detail` / `message` / `error` string
in the API and service code, so a new one fails the build.
"""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
DASHES = ("—", "–")
FIELDS = ("detail", "message", "error")
SKIP_DIRS = {"tests", "scripts", "migrations", "node_modules", ".venv", "venv"}


def _dashed_strings(tree):
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Call):
            targets += [k.value for k in node.keywords if k.arg in FIELDS]
        if isinstance(node, ast.Dict):
            targets += [
                v for k, v in zip(node.keys, node.values)
                if isinstance(k, ast.Constant) and k.value in FIELDS
            ]
        for target in targets:
            for sub in ast.walk(target):
                if (isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                        and any(d in sub.value for d in DASHES)):
                    yield sub.lineno, sub.value


def test_no_dashes_in_user_facing_messages():
    found = []
    for path in sorted(ROOT.rglob("*.py")):
        if SKIP_DIRS & set(path.relative_to(ROOT).parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno, value in _dashed_strings(tree):
            found.append(f"{path.relative_to(ROOT)}:{lineno}: {value[:80]}")
    assert not found, "em/en dash in a user-facing message:\n" + "\n".join(found)


def test_scanner_catches_a_dash():
    tree = ast.parse('raise HTTPException(status_code=503, detail="saved — retry")')
    assert list(_dashed_strings(tree))

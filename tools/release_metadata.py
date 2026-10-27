"""Check a release tag against the add-on version, or write a release into the repo metadata.

check X.Y.Z               fail if X.Y.Z is lower than the version in config.yaml
apply X.Y.Z [--notes F]   set X.Y.Z in config.yaml, pyproject.toml, uv.lock and
                          src/hub_model.py, and add a CHANGELOG.md entry from the
                          GitHub release notes in F unless the version has one
"""

import argparse, re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = Path("danfoss_link_cc_mqtt/config.yaml")
CHANGELOG = Path("danfoss_link_cc_mqtt/CHANGELOG.md")
VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
# (file, pattern whose first group is the version, expected match count)
FIELDS = (
    (CONFIG, r'^version: "([^"]+)"$', 1),
    (Path("pyproject.toml"), r'^version = "([^"]+)"$', 1),
    (Path("uv.lock"), r'^name = "danfoss-link-mqtt"\nversion = "([^"]+)"$', 1),
    (Path("src/hub_model.py"), r'^VERSION = "([^"]+)"$', 1),
)


def parse(version):
    match = VERSION.match(version)
    if not match:
        raise SystemExit(f"Not a release version: {version!r}")
    return tuple(int(part) for part in match.groups())


def current(root):
    match = re.search(FIELDS[0][1], (root / CONFIG).read_text(), re.M)
    return match.group(1)


def set_version(root, version):
    changed = []
    for path, pattern, count in FIELDS:
        text = (root / path).read_text()
        regex = re.compile(pattern, re.M)
        if len(regex.findall(text)) != count:
            raise SystemExit(f"{path}: version field not found exactly once")
        new = regex.sub(lambda m: m.group(0).replace(m.group(1), version), text)
        if new != text:
            (root / path).write_text(new)
            changed.append(str(path))
    return changed


def changelog_entry(version, notes):
    """GitHub release notes as a CHANGELOG section below the version heading.

    The notes open with "## What's Changed" and its "###" categories; other "##"
    sections such as "New Contributors" become categories too.
    """
    lines = []
    for line in notes.strip().splitlines():
        if line.startswith("<!--") or line.startswith("## What's Changed"):
            continue
        lines.append("#" + line if line.startswith("## ") else line)
    body = "\n".join(lines).strip()
    return f"## {version}\n\n{body or '- No user-facing changes.'}\n"


def add_changelog(root, version, notes):
    path = root / CHANGELOG
    text = path.read_text()
    if re.search(rf"^## {re.escape(version)}$", text, re.M):
        return False
    head, sep, rest = text.partition("\n## ")
    older = "\n## " + rest if sep else ""
    path.write_text(head.rstrip() + "\n\n" + changelog_entry(version, notes) + older)
    return True


def main(argv=None, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("check", "apply"))
    parser.add_argument("version")
    parser.add_argument("--notes", type=Path)
    args = parser.parse_args(argv)
    if parse(args.version) < parse(current(root)):
        raise SystemExit(f"{args.version} is lower than the add-on version {current(root)}")
    if args.command == "check":
        return 0
    changed = set_version(root, args.version)
    notes = args.notes.read_text() if args.notes else ""
    if add_changelog(root, args.version, notes):
        changed.append(str(CHANGELOG))
    print("\n".join(changed) if changed else "Metadata already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

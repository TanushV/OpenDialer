"""Generate a reproducible dependency/license inventory from installed locked artifacts."""

import hashlib
import importlib.metadata
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
entries = []
texts = {}


def record(ecosystem, name, version, license_name, directory, paths):
    refs = []
    for path in paths:
        try:
            text = path.read_text(errors="replace")
            if not text.strip() or len(text) > 200000:
                continue
            digest = hashlib.sha256(text.encode()).hexdigest()[:16]
            texts[digest] = text
            refs.append(digest)
        except OSError:
            continue
    entries.append((ecosystem, name, version, license_name or "See included license text", sorted(set(refs))))


for dist in importlib.metadata.distributions():
    paths = [
        Path(dist.locate_file(f))
        for f in dist.files or []
        if any(word in Path(str(f)).name.lower() for word in ["license", "copying", "copyright", "notice"])
        and not str(f).endswith(".py")
    ]
    record(
        "Python",
        dist.metadata["Name"],
        dist.version,
        dist.metadata.get("License-Expression") or dist.metadata.get("License", ""),
        None,
        paths,
    )
lock = json.loads((root / "apps/desktop/package-lock.json").read_text())
for rel, value in lock.get("packages", {}).items():
    if not rel:
        continue
    directory = root / "apps/desktop" / rel
    paths = [
        p
        for p in directory.glob("*")
        if p.is_file()
        and any(word in p.name.lower() for word in ["license", "copying", "copyright", "notice"])
    ]
    record(
        "npm", rel.split("node_modules/")[-1], value["version"], value.get("license", ""), directory, paths
    )
metadata = json.loads((root / "work/cargo-dependencies.json").read_text())
for value in metadata["packages"]:
    directory = Path(value["manifest_path"]).parent
    paths = [
        p
        for p in directory.glob("*")
        if p.is_file()
        and any(word in p.name.lower() for word in ["license", "copying", "copyright", "notice"])
    ]
    record("Cargo", value["name"], value["version"], value.get("license", ""), directory, paths)
lines = [
    "# Third-party notices",
    "",
    "This inventory includes locked application, build and test dependencies. Not every listed package is shipped in every platform binary. No external neural model weights are bundled. Platform SDK components retain their own licenses.",
    "",
    "| Ecosystem | Package | Version | License | Text IDs |",
    "|---|---|---|---|---|",
]
for ecosystem, name, version, license_name, refs in sorted(entries):
    label = license_name.replace("\n", " ").replace("|", " / ")[:180]
    lines.append(f"| {ecosystem} | {name} | {version} | {label} | " + ", ".join(refs) + " |")
for digest, text in sorted(texts.items()):
    lines.extend(["", f"## License text {digest}", "", "```text", text.replace("```", "~~~"), "```"])
(root / "THIRD_PARTY_NOTICES.md").write_text("\n".join(lines) + "\n")
print(f"Generated {len(entries)} dependency entries and {len(texts)} distinct notices.")

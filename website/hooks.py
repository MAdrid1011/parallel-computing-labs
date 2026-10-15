"""Keep operation pages and source downloads in sync with the independent labs."""

from io import BytesIO
import json
from pathlib import Path
import re
from zipfile import ZIP_DEFLATED, ZipFile

from mkdocs.structure.files import File

ROOT = Path(__file__).resolve().parent.parent
OMIT = {"build", "results", ".venv", "__pycache__", ".git"}
SOURCE_MARKER = re.compile(r"^\{\{SOURCE (\{[^\n]+\})\}\}$", re.MULTILINE)


def source_excerpt(match):
    """Read teaching excerpts from the same files included in the lab downloads."""
    spec = json.loads(match.group(1))
    path = (ROOT / spec["path"]).resolve()
    if not path.is_relative_to(ROOT) or not path.is_file():
        raise ValueError(f"Invalid source excerpt: {spec['path']}")
    lines = path.read_text(encoding="utf-8").splitlines()
    begin, end = 0, len(lines)
    if "start" in spec:
        candidates = [i for i, line in enumerate(lines) if spec["start"] in line]
        if len(candidates) != 1:
            raise ValueError(f"Source start must match once: {spec}")
        begin = candidates[0]
    if "end" in spec:
        candidates = [i for i in range(begin, len(lines)) if spec["end"] in lines[i]]
        if not candidates:
            raise ValueError(f"Source end not found: {spec}")
        end = candidates[0] + 1
    relative = path.relative_to(ROOT).as_posix()
    language = {".cpp": "cpp", ".hpp": "cpp", ".sv": "systemverilog", ".py": "python"}[path.suffix]
    url = f"https://github.com/MAdrid1011/parallel-computing-labs/blob/main/{relative}#L{begin + 1}-L{end}"
    label = f"源码：[{relative} · L{begin + 1}–L{end}]({url})（节选）"
    code = "\n".join(lines[begin:end])
    return f'{label}\n\n```{language} linenums="{begin + 1}"\n{code}\n```'


def on_page_markdown(markdown, page, config, files):
    for number in range(1, 6):
        marker = f"{{{{LAB{number}_README}}}}"
        if marker in markdown:
            readme = (ROOT / f"Lab{number}" / "README.md").read_text(encoding="utf-8")
            # The operation page supplies its own title and introductory guidance.
            markdown = markdown.replace(marker, readme.split("\n", 1)[1].lstrip())
    return SOURCE_MARKER.sub(source_excerpt, markdown)


def on_files(files, config):
    for number in range(1, 6):
        lab = ROOT / f"Lab{number}"
        build_dirs = {p.parent for p in lab.rglob("CMakeCache.txt")}
        archive = BytesIO()
        with ZipFile(archive, "w", ZIP_DEFLATED) as output:
            for path in sorted(lab.rglob("*")):
                relative = path.relative_to(lab)
                if (not path.is_file() or OMIT.intersection(relative.parts)
                        or any(part.startswith("build-") for part in relative.parts)
                        or any(parent in build_dirs for parent in path.parents)
                        or path.name == ".DS_Store" or path.suffix in {".pyc", ".vcd", ".fst"}):
                    continue
                output.write(path, str(Path(lab.name) / relative))
        files.append(File.generated(config, f"downloads/Lab{number}.zip", content=archive.getvalue()))
    return files

"""
GuardRAG v2 — corpus ingestion with parent-child chunking.

Supports three source types:
  - docs/guides   (.mdx)  — how-to guides
  - docs/tutorials (.ipynb) — Jupyter notebooks
  - docs/api/qiskit-ibm-runtime (.mdx) — API reference (filtered)

Parent-child chunking strategy:
  - CHILD chunk (~120 words): embedded for precise vector matching
  - PARENT chunk (~500 words): sent to the LLM for grounded generation

This separation reduces confabulation: the LLM receives enough surrounding
context to answer without inventing specific values not present in the excerpt.

Usage:
    python ingestion/parse_qiskit_docs_v2.py --out data/chunks_v2.jsonl

    Or specify individual dirs:
    python ingestion/parse_qiskit_docs_v2.py \
        --guides-dir  data/qiskit-docs/docs/guides \
        --tutorials-dir data/qiskit-docs/docs/tutorials \
        --api-dir data/qiskit-docs/docs/api/qiskit-ibm-runtime \
        --out data/chunks_v2.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# ──────────────────────────────────────────────────────────────────
# MDX cleaning (reused from v1, tightened)
# ──────────────────────────────────────────────────────────────────
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
COMMENT_RE = re.compile(r"\{/\*.*?\*/\}", re.DOTALL)
IMPORT_EXPORT_RE = re.compile(r"^(import|export)\s.*$\n?", re.MULTILINE)
SELF_CLOSING_RE = re.compile(r"<(\w+)([^>]*?)/>")
ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
ANY_TAG_RE = re.compile(r"</?[\w.]+(?:\s[^>]*)?>")
HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$", re.MULTILINE)
WHITESPACE_RE = re.compile(r"\n{3,}")

# API files to skip: fake backends (large but not useful for Q&A)
# and stubs < MIN_API_BYTES bytes
MIN_API_BYTES = 2_000
FAKE_BACKEND_RE = re.compile(r"fake-provider-fake-", re.IGNORECASE)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip().strip('"')
    return meta, text[match.end():]


def expand_self_closing(text: str) -> str:
    def replace(m: re.Match) -> str:
        attrs = dict(ATTR_RE.findall(m.group(2)))
        title = attrs.get("title")
        desc = attrs.get("description")
        if title and desc:
            return f"\n- {title}: {desc}\n"
        if title:
            return f"\n- {title}\n"
        return ""
    return SELF_CLOSING_RE.sub(replace, text)


def strip_mdx(text: str) -> str:
    text = COMMENT_RE.sub("", text)
    text = IMPORT_EXPORT_RE.sub("", text)
    text = expand_self_closing(text)
    text = ANY_TAG_RE.sub("", text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = WHITESPACE_RE.sub("\n\n", text)
    return text.strip()


# ──────────────────────────────────────────────────────────────────
# Chunking helpers
# ──────────────────────────────────────────────────────────────────

def split_into_sections(text: str, doc_title: str) -> list[tuple[str, str]]:
    """Split on H1/H2/H3 headings → list of (heading, body)."""
    matches = list(HEADING_RE.finditer(text))
    if not matches:
        return [(doc_title, text)]
    sections: list[tuple[str, str]] = []
    if matches[0].start() > 0:
        intro = text[: matches[0].start()].strip()
        if intro:
            sections.append((doc_title, intro))
    for i, m in enumerate(matches):
        heading = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:
            sections.append((heading, body))
    return sections


def words(text: str) -> list[str]:
    return text.split()


def make_parent_child_chunks(
    section_heading: str,
    section_body: str,
    doc_title: str,
    child_words: int = 120,
    parent_words: int = 500,
    overlap: int = 30,
) -> list[tuple[str, str]]:
    """
    Returns list of (child_text, parent_text) pairs.

    child_text  → embedded for retrieval (precise matching)
    parent_text → sent to the LLM (grounded generation, more context)

    If the section fits in one parent chunk, there's a single pair where
    child == first child_words of the section, parent == whole section.
    """
    section_words = words(section_body)

    if len(section_words) <= parent_words:
        # Small section: parent = full section, child = first child_words
        parent_text = section_body
        child_text = " ".join(section_words[:child_words]) if len(section_words) > child_words else section_body
        return [(child_text, parent_text)]

    # Large section: sliding parent windows, child is the first child_words of each parent
    pairs: list[tuple[str, str]] = []
    step = max(parent_words - overlap, 1)
    for start in range(0, len(section_words), step):
        parent_slice = section_words[start: start + parent_words]
        child_slice = parent_slice[:child_words]
        pairs.append((" ".join(child_slice), " ".join(parent_slice)))
        if start + parent_words >= len(section_words):
            break
    return pairs


def doc_url(relative_path: Path) -> str:
    parts = relative_path.parts
    if parts[0] == "docs":
        relative_path = Path(*parts[1:])
    slug = relative_path.with_suffix("").as_posix()
    return f"https://quantum.cloud.ibm.com/docs/{slug}"

# ──────────────────────────────────────────────────────────────────
# MDX file processor
# ──────────────────────────────────────────────────────────────────

def process_mdx(path: Path, docs_root: Path, source_type: str) -> list[dict]:
    raw = path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(raw)
    doc_title = meta.get("title", path.stem.replace("-", " ").title())
    cleaned = strip_mdx(body)
    relative_path = path.relative_to(docs_root)
    url = doc_url(relative_path)

    records = []
    for section_idx, (heading, section_body) in enumerate(
        split_into_sections(cleaned, doc_title)
    ):
        pairs = make_parent_child_chunks(heading, section_body, doc_title)
        for chunk_idx, (child_text, parent_text) in enumerate(pairs):
            if len(child_text.split()) < 10:
                continue  # skip near-empty chunks
            records.append({
                "id": f"{source_type}__{path.stem}_{section_idx}_{chunk_idx}",
                "text": child_text,          # embedded for retrieval
                "parent_text": parent_text,  # sent to the LLM
                "metadata": {
                    "doc_title": doc_title,
                    "section": heading,
                    "source_file": str(relative_path),
                    "source_type": source_type,
                    "url": url,
                },
            })
    return records


# ──────────────────────────────────────────────────────────────────
# Jupyter notebook processor
# ──────────────────────────────────────────────────────────────────

def process_notebook(path: Path, docs_root: Path) -> list[dict]:
    """
    Extract markdown cells from a Jupyter notebook.
    Code cells are included as fenced code blocks appended to the
    preceding markdown cell (so the LLM sees the code in context).
    """
    nb = json.loads(path.read_text(encoding="utf-8"))
    cells = nb.get("cells", [])

    # Extract frontmatter from first markdown cell if present
    first_md = next((c for c in cells if c["cell_type"] == "markdown"), None)
    doc_title = path.stem.replace("-", " ").title()
    if first_md:
        raw_first = "".join(first_md["source"])
        meta, _ = parse_frontmatter(raw_first + "\n")
        doc_title = meta.get("title", doc_title)

    relative_path = path.relative_to(docs_root)
    url = doc_url(relative_path)

    # Build sections: merge consecutive markdown cells and append following code cells
    sections: list[tuple[str, str]] = []  # (heading, content)
    current_heading = doc_title
    current_parts: list[str] = []

    for cell in cells:
        src = "".join(cell["source"]).strip()
        if not src:
            continue

        if cell["cell_type"] == "markdown":
            # Strip frontmatter if present (first cell often contains it)
            src_clean = FRONTMATTER_RE.sub("", src).strip()
            if not src_clean:
                continue
            # Check if this cell starts a new heading
            cleaned = strip_mdx(src_clean)
            heading_match = re.match(r"^#{1,3}\s+(.+)", cleaned)
            if heading_match:
                # Save accumulated content to previous section
                if current_parts:
                    sections.append((current_heading, "\n\n".join(current_parts)))
                current_heading = heading_match.group(1).strip()
                current_parts = [cleaned]
            else:
                current_parts.append(cleaned)

        elif cell["cell_type"] == "code" and src:
            # Append code to current section as fenced block
            current_parts.append(f"```python\n{src}\n```")

    if current_parts:
        sections.append((current_heading, "\n\n".join(current_parts)))

    records = []
    for section_idx, (heading, section_body) in enumerate(sections):
        pairs = make_parent_child_chunks(heading, section_body, doc_title)
        for chunk_idx, (child_text, parent_text) in enumerate(pairs):
            if len(child_text.split()) < 10:
                continue
            records.append({
                "id": f"tutorial__{path.stem}_{section_idx}_{chunk_idx}",
                "text": child_text,
                "parent_text": parent_text,
                "metadata": {
                    "doc_title": doc_title,
                    "section": heading,
                    "source_file": str(relative_path),
                    "source_type": "tutorial",
                    "url": url,
                },
            })
    return records


# ──────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--guides-dir", type=Path,
        default=Path("data/qiskit-docs/docs/guides"),
        help="Path to docs/guides (MDX files)",
    )
    parser.add_argument(
        "--tutorials-dir", type=Path,
        default=Path("data/qiskit-docs/docs/tutorials"),
        help="Path to docs/tutorials (Jupyter notebooks)",
    )
    parser.add_argument(
        "--api-dir", type=Path,
        default=Path("data/qiskit-docs/docs/api/qiskit-ibm-runtime"),
        help="Path to docs/api/qiskit-ibm-runtime (MDX files)",
    )
    parser.add_argument("--out", required=True, type=Path, help="Output JSONL file")
    args = parser.parse_args()

    all_records: list[dict] = []
    docs_root = args.guides_dir.parent.parent.parent  # data/qiskit-docs

    # 1. Guides
    if args.guides_dir.exists():
        guide_files = sorted(args.guides_dir.glob("*.mdx"))
        for path in guide_files:
            all_records.extend(process_mdx(path, docs_root, "guide"))
        print(f"Guides    : {len(guide_files)} files → {sum(1 for r in all_records if r['metadata']['source_type'] == 'guide')} chunks")
    else:
        print(f"Guides dir not found: {args.guides_dir}")

    before_tutorials = len(all_records)

    # 2. Tutorials (Jupyter notebooks)
    if args.tutorials_dir.exists():
        nb_files = sorted(args.tutorials_dir.glob("*.ipynb"))
        for path in nb_files:
            all_records.extend(process_notebook(path, docs_root))
        tutorial_chunks = len(all_records) - before_tutorials
        print(f"Tutorials : {len(nb_files)} notebooks → {tutorial_chunks} chunks")
    else:
        print(f"Tutorials dir not found: {args.tutorials_dir}")

    before_api = len(all_records)

    # 3. API reference (filtered)
    if args.api_dir.exists():
        api_files = sorted(args.api_dir.glob("*.mdx"))
        skipped = 0
        for path in api_files:
            # Skip: too small (stubs) or fake backends
            if path.stat().st_size < MIN_API_BYTES or FAKE_BACKEND_RE.search(path.name):
                skipped += 1
                continue
            all_records.extend(process_mdx(path, docs_root, "api"))
        api_chunks = len(all_records) - before_api
        used = len(api_files) - skipped
        print(f"API ref   : {len(api_files)} files, {skipped} skipped (stubs/fakes) → {used} used → {api_chunks} chunks")
    else:
        print(f"API dir not found: {args.api_dir}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for record in all_records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    avg_child = sum(len(r["text"].split()) for r in all_records) / max(len(all_records), 1)
    avg_parent = sum(len(r["parent_text"].split()) for r in all_records) / max(len(all_records), 1)
    print(f"\nTotal     : {len(all_records)} chunks → {args.out}")
    print(f"Avg child (embedded) : {avg_child:.0f} words")
    print(f"Avg parent (to LLM)  : {avg_parent:.0f} words")
    print(f"Corpus growth vs v1  : {len(all_records)} vs ~834 (+{len(all_records)-834} chunks)")


if __name__ == "__main__":
    main()

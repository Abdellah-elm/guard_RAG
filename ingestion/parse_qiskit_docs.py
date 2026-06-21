"""
Parse the official Qiskit / IBM Quantum documentation (MDX source) into
clean, section-aware, chunked text records ready for embedding.

Source corpus: https://github.com/Qiskit/documentation
License: dual-licensed — code under Apache 2.0, content (the docs themselves)
under CC BY-SA 4.0. Safe to index for a personal/portfolio RAG project.

This is a heuristic MDX -> plain text cleaner (regex-based, zero dependencies),
not a full MDX/JSX AST parser. It's good enough to produce clean chunks for
a v1 RAG pipeline; revisit if a quality pass on the output reveals artifacts
on specific pages.

Usage:
    git clone --depth 1 --filter=blob:none --sparse \
        https://github.com/Qiskit/documentation.git data/qiskit-docs
    cd data/qiskit-docs && git sparse-checkout set docs/guides && cd ../..

    python ingestion/parse_qiskit_docs.py \
        --docs-dir data/qiskit-docs/docs/guides \
        --out data/chunks.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
COMMENT_RE = re.compile(r"\{/\*.*?\*/\}", re.DOTALL)
IMPORT_EXPORT_RE = re.compile(r"^(import|export)\s.*$\n?", re.MULTILINE)
SELF_CLOSING_RE = re.compile(r"<(\w+)([^>]*?)/>")
ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
ANY_TAG_RE = re.compile(r"</?[\w.]+(?:\s[^>]*?)?>")
HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$", re.MULTILINE)
WHITESPACE_RE = re.compile(r"\n{3,}")


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Pull out the YAML-ish frontmatter block without pulling in a yaml dependency."""
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta, text[match.end():]


def expand_self_closing(text: str) -> str:
    """Self-closing components (mostly <Card .../>) carry their useful text in
    title/description props. Turn them into a plain bullet line instead of
    dropping the content."""

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
    """Strip MDX/JSX scaffolding while preserving prose. Container components
    like <Admonition> or <CardGroup> keep their inner text; only the tag
    markers are removed."""
    text = COMMENT_RE.sub("", text)
    text = IMPORT_EXPORT_RE.sub("", text)
    text = expand_self_closing(text)
    text = ANY_TAG_RE.sub("", text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = WHITESPACE_RE.sub("\n\n", text)
    return text.strip()


def split_into_sections(text: str, doc_title: str) -> list[tuple[str, str]]:
    """Split a cleaned doc body into (heading, body) sections on #/##/### headers.
    This is the main chunk boundary: sections are natural semantic units."""
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


def chunk_words(text: str, target_words: int = 350, overlap_words: int = 50) -> list[str]:
    """Word-count based chunking (~350 words ≈ 450-500 tokens) with overlap.
    Only kicks in for sections longer than the target — most doc sections
    are short enough to stay as a single chunk, which keeps retrieval units
    coherent."""
    words = text.split()
    if len(words) <= target_words:
        return [text]
    chunks = []
    step = max(target_words - overlap_words, 1)
    for start in range(0, len(words), step):
        chunk = words[start: start + target_words]
        chunks.append(" ".join(chunk))
        if start + target_words >= len(words):
            break
    return chunks


def doc_url(relative_path: Path) -> str:
    """Best-effort mapping from file path to the live docs URL. Spot-check a
    few against https://quantum.cloud.ibm.com/docs/ before trusting it for
    citations."""
    slug = relative_path.with_suffix("").as_posix()
    return f"https://quantum.cloud.ibm.com/docs/{slug}"


def process_file(path: Path, docs_root: Path) -> list[dict]:
    raw = path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(raw)
    doc_title = meta.get("title", path.stem.replace("-", " ").title())
    cleaned = strip_mdx(body)
    relative_path = path.relative_to(docs_root.parent)
    url = doc_url(relative_path)

    records = []
    for section_idx, (heading, section_text) in enumerate(split_into_sections(cleaned, doc_title)):
        for chunk_idx, chunk_text in enumerate(chunk_words(section_text)):
            records.append({
                "id": f"{path.stem}_{section_idx}_{chunk_idx}",
                "text": chunk_text,
                "metadata": {
                    "doc_title": doc_title,
                    "section": heading,
                    "source_file": str(relative_path),
                    "url": url,
                },
            })
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs-dir", required=True, type=Path, help="Path to the cloned docs/guides folder")
    parser.add_argument("--out", required=True, type=Path, help="Output JSONL file")
    args = parser.parse_args()

    files = sorted(args.docs_dir.glob("*.mdx"))
    if not files:
        raise SystemExit(f"No .mdx files found in {args.docs_dir}")

    all_records = []
    for path in files:
        all_records.extend(process_file(path, args.docs_dir))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for record in all_records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    avg_words = sum(len(r["text"].split()) for r in all_records) / len(all_records)
    print(f"Parsed {len(files)} files into {len(all_records)} chunks -> {args.out}")
    print(f"Average chunk length: {avg_words:.0f} words")


if __name__ == "__main__":
    main()

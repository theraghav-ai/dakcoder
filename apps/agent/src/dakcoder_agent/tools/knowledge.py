"""``search_docs`` and ``playbook``: retrieval over the knowledge base.

Part A section 14.3 specifies BM25 plus a vector index. This is the BM25 half,
and only the BM25 half. Two reasons, and the second is the real one.

An embedding index needs a model, a vector store and an offline story for both —
Part B section 4.3 has to vendor every dependency into a ``.vsix`` that installs
without a network, and a sentence-transformer is 90 MB of that budget. And the
corpus is thirteen documents whose *section headings are already the query terms*:
"repository pattern", "handler signature", "FX registration". Lexical retrieval
over a curated, small, well-titled corpus is not a compromise; it is the
technique that fits the shape of the data. The vector half earns its keep when
the corpus grows past what one person wrote deliberately.

Retrieval returns **sections, not documents**. A whole reference is up to 500
lines; the section under one heading is twenty. Part A section 6.2 caps a
``search_docs`` insertion at 3,000 tokens, and returning documents would spend
the whole cap on one hit.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dakcoder_shared.envelope import ToolResult

from .router import Invocation

__all__ = ["Corpus", "HANDLERS", "handlers_for", "load_playbooks"]

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: Words carrying no retrieval signal here. Deliberately short: an aggressive
#: stop-list would drop "get", "new" and "error", which are the most
#: discriminating terms in a Go codebase rather than the least.
_STOP = frozenset(
    """a an and are as at be by do does for from has have how i if in into is it its
    of on or should that the their there this to use used using was what when where
    which who why will with you your""".split()
)

#: BM25 constants. The defaults from the literature; the corpus is far too small
#: for tuning them to be anything but overfitting to today's thirteen files.
_K1 = 1.5
_B = 0.75


def _tokenise(text: str) -> list[str]:
    """Split into lowercase terms, also splitting identifiers.

    ``NewUserResponse`` has to match a query for "response", and ``dblib.Psql``
    a query for "psql". Without splitting, every Go identifier is a term that
    appears once in the corpus and matches nothing anyone would type.
    """
    out: list[str] = []
    for match in _WORD.finditer(text):
        word = match.group(0)
        out.append(word.lower())
        parts = re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+", word)
        if len(parts) > 1:
            out.extend(p.lower() for p in parts)
    return [w for w in out if w not in _STOP and len(w) > 1]


@dataclass(frozen=True, slots=True)
class Section:
    """One heading and its body."""

    document: str
    heading: str
    level: int
    body: str
    terms: Counter[str] = field(default_factory=Counter)

    @property
    def citation(self) -> str:
        return f"@skill:{self.document}§{self.heading}"

    @property
    def length(self) -> int:
        return sum(self.terms.values())


class Corpus:
    """The knowledge base, split into sections and indexed.

    Built once and cached. The whole corpus is about 2,000 lines, so this costs a
    few milliseconds and saves doing it on every call — which matters because
    ``search_docs`` is one of the few tools a model calls several times in a
    single turn.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.sections: list[Section] = []
        self._df: Counter[str] = Counter()
        self._avg_length = 1.0
        self._load()

    def _load(self) -> None:
        files = sorted(self.root.glob("*.md")) + sorted(self.root.glob("references/*.md"))
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            self.sections.extend(_split_sections(path.stem, text))

        for section in self.sections:
            self._df.update(set(section.terms))
        if self.sections:
            self._avg_length = sum(s.length for s in self.sections) / len(self.sections)

    def search(self, query: str, limit: int = 4) -> list[tuple[Section, float]]:
        terms = _tokenise(query)
        if not terms or not self.sections:
            return []

        total = len(self.sections)
        scored: list[tuple[Section, float]] = []
        for section in self.sections:
            score = 0.0
            for term in terms:
                frequency = section.terms.get(term, 0)
                if not frequency:
                    continue
                df = self._df[term]
                idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
                norm = 1 - _B + _B * section.length / self._avg_length
                score += idf * (frequency * (_K1 + 1)) / (frequency + _K1 * norm)

            # A heading match is worth more than a body match of the same
            # frequency: headings in this corpus are written as the questions
            # people ask, so a hit there means the section is *about* the query
            # rather than merely mentioning it.
            heading_terms = set(_tokenise(section.heading))
            overlap = len(heading_terms & set(terms))
            if overlap:
                score *= 1 + 0.6 * overlap

            if score > 0:
                scored.append((section, score))

        scored.sort(key=lambda pair: (-pair[1], pair[0].document, pair[0].heading))
        return scored[:limit]


def _split_sections(document: str, text: str) -> list[Section]:
    """Split markdown on headings, keeping fenced code intact.

    Splitting without tracking fences would cut a section at a ``#`` comment
    inside a shell block — which is how a retrieval index ends up returning half
    a code example with no context.
    """
    sections: list[Section] = []
    heading = document
    level = 1
    buffer: list[str] = []
    fenced = False

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body or heading != document:
            sections.append(
                Section(
                    document=document,
                    heading=heading,
                    level=level,
                    body=body,
                    terms=Counter(_tokenise(f"{heading}\n{body}")),
                )
            )

    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        match = None if fenced else re.match(r"^(#{1,4})\s+(.*)$", line)
        if match:
            flush()
            level = len(match.group(1))
            heading = match.group(2).strip()
            buffer = []
        else:
            buffer.append(line)
    flush()
    return [s for s in sections if s.body]


@lru_cache(maxsize=4)
def _corpus(root: str) -> Corpus:
    return Corpus(Path(root))


def _knowledge_root(explicit: Path | None = None) -> Path | None:
    if explicit and explicit.is_dir():
        return explicit
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "packages" / "knowledge"
        if candidate.is_dir():
            return candidate
    return None


# ── playbooks ───────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def load_playbooks(directory: str | None = None) -> dict[str, dict]:
    """Load the failure-class playbooks (Part A section 13.2)."""
    root = Path(directory) if directory else Path(__file__).resolve().parent.parent / "playbooks"
    out: dict[str, dict] = {}
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out[data.get("id", path.stem)] = data
    return out


def _render_playbook(entry: dict) -> str:
    lines = [f"# {entry['id']} — {entry.get('symptom', '')}"]
    if entry.get("cause"):
        lines.append(f"\n**Cause.** {entry['cause']}")
    if entry.get("steps"):
        lines.append("\n**Fix.**")
        lines += [f"{n}. {step}" for n, step in enumerate(entry["steps"], 1)]
    if entry.get("tool"):
        lines.append(f"\n**Preferred tool.** `{entry['tool']}`")
    if entry.get("watch_out"):
        lines.append(f"\n**Watch out.** {entry['watch_out']}")
    if entry.get("citation"):
        lines.append(f"\n_{entry['citation']}_")
    return "\n".join(lines)


# ── handlers ────────────────────────────────────────────────────────────────


def handlers_for(knowledge_root: Path | None = None, playbook_dir: Path | None = None) -> dict:
    root = _knowledge_root(knowledge_root)

    def search_docs(inv: Invocation) -> ToolResult:
        if root is None:
            return ToolResult.failure(
                "the knowledge base is not installed in this runtime.",
                fix="Use search_repo against the reference template instead.",
            )
        query = inv.arg("query", "")
        hits = _corpus(str(root)).search(query, limit=4)
        if not hits:
            return ToolResult.success(
                f"nothing in the knowledge base matches {query!r}.\n"
                "The contract may simply not cover it — in that case follow the "
                "reference template, and prefer the pattern already used nearby.",
            )
        chunks = [
            f"── {section.citation} ──\n{section.body}" for section, _score in hits
        ]
        return ToolResult.success(
            "\n\n".join(chunks),
            meta={"hits": [s.citation for s, _ in hits]},
        )

    def playbook(inv: Invocation) -> ToolResult:
        entries = load_playbooks(str(playbook_dir) if playbook_dir else None)
        if not entries:
            return ToolResult.failure(
                "no playbooks are installed in this runtime.",
                fix="Use search_docs for the contract rule behind the failure.",
            )

        wanted = (inv.arg("rule") or "").strip().lower()
        if not wanted:
            listing = "\n".join(
                f"- {key}: {entry.get('symptom', '')}" for key, entry in sorted(entries.items())
            )
            return ToolResult.success(f"playbooks available:\n{listing}")

        if wanted in entries:
            return ToolResult.success(_render_playbook(entries[wanted]))

        # Match on the failure text as well as the id. The model usually has a
        # compiler message, not a playbook name — asking it to know our
        # taxonomy before it can look anything up defeats the purpose.
        terms = set(_tokenise(wanted))
        ranked = sorted(
            entries.values(),
            key=lambda e: -len(terms & set(_tokenise(" ".join(str(v) for v in e.values())))),
        )
        best = ranked[0] if ranked else None
        overlap = len(terms & set(_tokenise(" ".join(str(v) for v in (best or {}).values()))))
        if best and overlap >= 2:
            return ToolResult.success(_render_playbook(best))

        return ToolResult.success(
            f"no playbook matches {wanted!r}. Available: {', '.join(sorted(entries))}.\n"
            "If this failure recurs it should get one — say so in your summary.",
        )

    return {"search_docs": search_docs, "playbook": playbook}


HANDLERS = handlers_for()

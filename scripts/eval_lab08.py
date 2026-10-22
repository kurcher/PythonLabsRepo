"""precision@5 for keyword (BM25) / semantic / hybrid on 10 + 5 paraphrase queries.

    uv run --extra semantic python scripts/eval_lab08.py build/index-pos.bin

The 10 queries and their relevance labels are Lab 3's. The 5 paraphrases describe a
book *without* its distinctive words (no "whale", "Scrooge", "Holmes", ...): exactly
where keyword search should struggle and embeddings should help.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from eval_lab03 import QUERIES

from findex import store
from findex.search import search_mode
from findex.semantic import SemanticIndex, embeddings_dir

PARAPHRASES: dict[str, list[str]] = {
    "giant sea creature hunted by an obsessed captain": [r"Moby Dick"],
    "a miser visited by spirits on a holiday night": [r"Christmas Carol"],
    "scientist builds a living being from dead body parts": [r"Frankenstein"],
    "girl falls into a strange fantasy world underground": [
        r"Alice's Adventures",
        r"Alice’s Adventures",
    ],
    "detective solves crimes by careful deduction": [
        r"Sherlock Holmes",
        r"Study in Scarlet",
        r"Hound of the Baskervilles",
    ],
}


def p_at_5(index, sem, query: str, patterns: list[str], mode: str) -> tuple[float, str]:
    _, results = search_mode(index, query, mode, k=5, semantic=sem)  # type: ignore[arg-type]
    rel = [any(re.search(p, r.title) for p in patterns) for r in results]
    return sum(rel) / 5, "".join("✓" if ok else "✗" for ok in rel) or "—"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    path = Path(sys.argv[1])
    index = store.load(path)
    sem = SemanticIndex.load(embeddings_dir(path))
    modes = ("keyword", "semantic", "hybrid")
    print("| Query | Kind | Keyword P@5 | Semantic P@5 | Hybrid P@5 |")
    print("|---|---|---|---|---|")
    sums = {kind: dict.fromkeys(modes, 0.0) for kind in ("original", "paraphrase")}
    for kind, qs in (("original", QUERIES), ("paraphrase", PARAPHRASES)):
        for query, patterns in qs.items():
            cells = []
            for mode in modes:
                p, marks = p_at_5(index, sem, query, patterns, mode)
                sums[kind][mode] += p
                cells.append(f"{p:.1f} {marks}")
            print(f"| `{query}` | {kind} | " + " | ".join(cells) + " |", flush=True)
    for kind, qs in (("original", QUERIES), ("paraphrase", PARAPHRASES)):
        means = " | ".join(f"**{sums[kind][m] / len(qs):.2f}**" for m in modes)
        print(f"| **mean, {kind}** ({len(qs)}) | | {means} |")
    allq = len(QUERIES) + len(PARAPHRASES)
    total = " | ".join(
        f"**{(sums['original'][m] + sums['paraphrase'][m]) / allq:.2f}**" for m in modes
    )
    print(f"| **mean, all 15** | | {total} |")


if __name__ == "__main__":
    main()

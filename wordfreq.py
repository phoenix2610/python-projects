#!/usr/bin/env python3
"""Rank words and phrases across a folder of documents, with a real relevance score.

    wordfreq.py ~/notes --top 25
    wordfreq.py ~/notes --ngram 2 --min-count 3
    wordfreq.py ~/notes --tfidf --per-file

Raw counts just tell you that "the" is popular. --tfidf scores each term against
how many documents it appears in, so terms that are frequent *here* and rare
elsewhere in the corpus float to the top — the difference between a word list and
a topic list.
"""
from __future__ import annotations

import argparse
import math
import os
import re
import sys
from collections import Counter, defaultdict

WORD = re.compile(r"[a-z][a-z'-]{1,}")
STOPWORDS = set("""a about above after again against all am an and any are aren't as at be because been
before being below between both but by can cannot could couldn't did didn't do does doesn't doing don't down
during each few for from further had hadn't has hasn't have haven't having he her here hers herself him himself
his how i if in into is isn't it its itself let's me more most mustn't my myself no nor not of off on once only
or other ought our ours ourselves out over own same shan't she should shouldn't so some such than that the their
theirs them themselves then there these they this those through to too under until up very was wasn't we were
weren't what when where which while who whom why with won't would wouldn't you your yours yourself yourselves
""".split())
CODE_FENCE = re.compile(r"```.*?```", re.S)
MARKUP = re.compile(r"<[^>]+>|https?://\S+|[*_`#>|]")


def tokenize(text: str, keep_stopwords: bool) -> list[str]:
    text = MARKUP.sub(" ", CODE_FENCE.sub(" ", text.lower()))
    words = WORD.findall(text)
    return words if keep_stopwords else [w for w in words if w not in STOPWORDS]


def ngrams(words: list[str], n: int) -> list[str]:
    return [" ".join(words[i:i + n]) for i in range(len(words) - n + 1)] if n > 1 else words


def read(path: str) -> str:
    try:
        return open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


def collect(root: str, exts: set[str]) -> list[str]:
    if os.path.isfile(root):
        return [root]
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith((".", "node_modules"))]
        for name in filenames:
            if not exts or os.path.splitext(name)[1].lower() in exts:
                found.append(os.path.join(dirpath, name))
    return found


def bar(value: float, peak: float, width: int = 24) -> str:
    return "█" * max(1, round(width * value / peak)) if peak else ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--ngram", type=int, default=1)
    ap.add_argument("--min-count", type=int, default=2)
    ap.add_argument("--tfidf", action="store_true", help="rank by tf-idf instead of raw count")
    ap.add_argument("--per-file", action="store_true", help="also print each file's top terms")
    ap.add_argument("--keep-stopwords", action="store_true")
    ap.add_argument("--ext", default=".md,.txt,.rst", help="comma-separated extensions, empty for all")
    args = ap.parse_args()

    exts = {e if e.startswith(".") else "." + e for e in args.ext.split(",") if e.strip()}
    files = collect(args.path, exts)
    if not files:
        print("no matching files", file=sys.stderr)
        return 1

    per_file: dict[str, Counter] = {}
    doc_freq: Counter = Counter()
    total = Counter()
    for path in files:
        terms = ngrams(tokenize(read(path), args.keep_stopwords), args.ngram)
        counts = Counter(terms)
        per_file[path] = counts
        total.update(counts)
        doc_freq.update(counts.keys())

    n_docs = len(files)
    if args.tfidf:
        scored = {
            term: (count / max(sum(total.values()), 1)) * math.log(n_docs / doc_freq[term] + 1)
            for term, count in total.items() if count >= args.min_count
        }
    else:
        scored = {t: float(c) for t, c in total.items() if c >= args.min_count}

    ranked = sorted(scored.items(), key=lambda kv: -kv[1])[: args.top]
    if not ranked:
        print(f"nothing appears at least {args.min_count} times", file=sys.stderr)
        return 1

    label = "tf-idf" if args.tfidf else "count"
    peak = ranked[0][1]
    width = max(len(t) for t, _ in ranked)
    print(f"{len(files)} files, {sum(total.values()):,} tokens, {len(total):,} distinct "
          f"{args.ngram}-grams\n")
    print(f"{'term'.ljust(width)}  {label:>8}  docs")
    for term, value in ranked:
        shown = f"{value:.4f}" if args.tfidf else f"{value:.0f}"
        print(f"{term.ljust(width)}  {shown:>8}  {doc_freq[term]:>4}  {bar(value, peak)}")

    if args.per_file:
        print()
        for path, counts in sorted(per_file.items()):
            if not counts:
                continue
            top = ", ".join(t for t, _ in counts.most_common(5))
            print(f"  {os.path.relpath(path, args.path)}: {top}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

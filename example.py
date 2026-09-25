"""快速演示：python3 example.py"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from spellcheck import SpellChecker, damerau_distance

if __name__ == "__main__":
    # (词, 词频)；重复词会自动合并，词频累加
    vocab = [
        ("the", 5_300_000), ("they", 2_100_000), ("there", 1_800_000),
        ("their", 1_200_000), ("then", 900_000), ("theme", 120_000),
        ("theory", 80_000), ("hello", 50_000), ("help", 30_000),
    ]
    checker = SpellChecker(vocab)

    print("distance('teh','the', transpositions=True)  =",
          damerau_distance("teh", "the", True))
    print("distance('teh','the', transpositions=False) =",
          damerau_distance("teh", "the", False))
    print()
    for q in ("teh", "ther", "theori"):
        print(f"suggest({q!r}, k=5, max_distance=2):")
        for s in checker.suggest(q, k=5, max_distance=2):
            print(f"    {s.word:<8} dist={s.distance} freq={s.frequency:,}")
        print()

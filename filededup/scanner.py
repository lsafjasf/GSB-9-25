"""File fingerprinting, chunk indexing and similarity detection.

Per-file work is a single streaming pass: the whole-file SHA-256 and the
content-defined chunks are computed together, so a file of any size is
processed with O(1) extra memory (read buffer + one chunk).

The cross-file index maps chunk-id -> files containing it.  It grows
with the amount of *distinct content* (~1 index entry per avg chunk,
i.e. roughly 0.1% of the scanned data), not with the size of any single
file.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

from .cdc import Chunker

CHUNK_DIGEST_SIZE = 16  # blake2b-128: collision-safe for chunk ids


def chunk_id(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=CHUNK_DIGEST_SIZE).digest()


@dataclass
class FileInfo:
    path: str
    size: int
    digest: str  # whole-file sha256 hex
    chunks: dict[bytes, tuple[int, int]]  # chunk_id -> (occurrences, length)
    n_chunks: int

    @property
    def repeated_chunks(self) -> dict[bytes, tuple[int, int]]:
        """Chunks occurring more than once inside this file."""
        return {c: v for c, v in self.chunks.items() if v[0] > 1}


def scan_file(path: str, chunker: Chunker) -> FileInfo:
    file_hash = hashlib.sha256()
    chunks: dict[bytes, tuple[int, int]] = {}
    n_chunks = 0
    size = 0
    with open(path, "rb") as fh:
        for _offset, chunk in chunker.iter_chunks(fh):
            file_hash.update(chunk)
            cid = chunk_id(chunk)
            if cid in chunks:
                count, length = chunks[cid]
                chunks[cid] = (count + 1, length)
            else:
                chunks[cid] = (1, len(chunk))
            n_chunks += 1
            size += len(chunk)
    return FileInfo(path, size, file_hash.hexdigest(), chunks, n_chunks)


@dataclass
class SimilarEdge:
    file_a: int
    file_b: int
    shared_chunks: int
    containment: float  # shared / min(chunks_a, chunks_b)
    jaccard: float


@dataclass
class SimilarGroup:
    members: list[int]  # indices into DedupIndex.files (all files, all hashes)
    edges: list[SimilarEdge]  # pair evidence, sorted by containment desc


class DedupIndex:
    def __init__(self, chunker: Chunker | None = None) -> None:
        self.chunker = chunker or Chunker()
        self.files: list[FileInfo] = []
        self.chunk_files: dict[bytes, list[int]] = {}
        self.bytes_scanned = 0

    def add_file(self, path: str) -> FileInfo:
        info = scan_file(path, self.chunker)
        idx = len(self.files)
        self.files.append(info)
        self.bytes_scanned += info.size
        for cid in info.chunks:
            self.chunk_files.setdefault(cid, []).append(idx)
        return info

    def add_paths(self, paths: list[str]) -> None:
        for path in iter_files(paths):
            self.add_file(path)

    # -- exact duplicates -------------------------------------------------

    def exact_groups(self) -> list[list[int]]:
        """Groups of file indices with identical whole-file SHA-256."""
        by_hash: dict[str, list[int]] = {}
        for i, f in enumerate(self.files):
            by_hash.setdefault(f.digest, []).append(i)
        return sorted(
            (g for g in by_hash.values() if len(g) > 1),
            key=lambda g: -self.files[g[0]].size,
        )

    # -- near duplicates --------------------------------------------------

    def similar_groups(self, threshold: float = 0.5) -> list[SimilarGroup]:
        """Groups of files sharing at least `threshold` of their chunks.

        Similarity of a pair = weighted shared-chunk containment:
        shared / min(chunks_a, chunks_b), where shared sums
        min(count_a, count_b) over common chunk ids.
        """
        # Collapse exact duplicates first: one representative per digest.
        rep_of: dict[str, int] = {}
        reps: list[int] = []
        for i, f in enumerate(self.files):
            if f.digest not in rep_of:
                rep_of[f.digest] = i
                reps.append(i)

        rep_chunk_files: dict[bytes, list[int]] = {}
        for r in reps:
            for cid in self.files[r].chunks:
                rep_chunk_files.setdefault(cid, []).append(r)

        shared: dict[tuple[int, int], int] = {}
        for cid, owners in rep_chunk_files.items():
            if len(owners) < 2:
                continue
            for x in range(len(owners)):
                for y in range(x + 1, len(owners)):
                    a, b = owners[x], owners[y]
                    key = (a, b) if a < b else (b, a)
                    shared[key] = shared.get(key, 0) + min(
                        self.files[a].chunks[cid][0], self.files[b].chunks[cid][0]
                    )

        parent = {r: r for r in reps}

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        edges_of: dict[int, list[SimilarEdge]] = {r: [] for r in reps}
        for (a, b), cnt in shared.items():
            na, nb = self.files[a].n_chunks, self.files[b].n_chunks
            if na == 0 or nb == 0:
                continue
            containment = cnt / min(na, nb)
            if containment < threshold:
                continue
            jaccard = cnt / (na + nb - cnt)
            edge = SimilarEdge(a, b, cnt, containment, jaccard)
            edges_of[a].append(edge)  # both endpoints land in one group
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        groups: dict[int, list[int]] = {}
        for r in reps:
            groups.setdefault(find(r), []).append(r)

        result: list[SimilarGroup] = []
        for group_reps in groups.values():
            if len(group_reps) < 2:
                continue
            edges: list[SimilarEdge] = []
            for r in group_reps:
                edges.extend(edges_of[r])
            edges.sort(key=lambda e: -e.containment)
            rep_set = set(group_reps)
            members = [i for i, f in enumerate(self.files) if rep_of[f.digest] in rep_set]
            result.append(SimilarGroup(members, edges))
        result.sort(key=lambda g: -max(e.containment for e in g.edges))
        return result

    # -- intra-file repeats -----------------------------------------------

    def internal_repeats(self) -> list[tuple[int, int, int]]:
        """(file_index, distinct_repeated_chunks, reclaimable_bytes)."""
        out = []
        for i, f in enumerate(self.files):
            reps = f.repeated_chunks
            if reps:
                saved = sum((count - 1) * length for count, length in reps.values())
                out.append((i, len(reps), saved))
        out.sort(key=lambda t: -t[2])
        return out


def iter_files(paths: list[str]):
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, names in os.walk(p):
                dirs.sort()
                for name in sorted(names):
                    yield os.path.join(root, name)
        else:
            yield p

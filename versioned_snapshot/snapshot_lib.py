"""versioned_snapshot: 基于路径复制写时拷贝（path-copying COW）的版本化快照库。

核心思想：
- 数据是不可变的嵌套 dict/list 结构。
- 每次修改只复制从根到被修改节点路径上的容器，未变化的子树通过引用共享。
- 因此创建快照是 O(1)（仅记录根引用），修改开销与变化量（路径长度）相关，
  与总数据量无关。
- 所有版本共享未变化节点；删除版本后，其独占节点因引用计数归零被自动回收。

仅使用 Python 标准库。
"""

from __future__ import annotations

import weakref
from typing import Any, Dict, Hashable, List, Tuple

Path = Tuple[Hashable, ...]

_MISSING = object()


class TrackedDict(dict):
    """可弱引用的 dict，用于验证节点回收。"""


class TrackedList(list):
    """可弱引用的 list，用于验证节点回收。"""


def _trackable(node: Any) -> bool:
    return isinstance(node, (TrackedDict, TrackedList))


class NodeRegistry:
    """以 id -> weakref 形式跟踪所有已创建的容器节点（list 子类不可哈希，
    无法直接用 WeakSet）。用于测试验证节点回收。"""

    def __init__(self) -> None:
        self._refs: Dict[int, weakref.ref] = {}

    def add(self, node: Any) -> None:
        self._refs[id(node)] = weakref.ref(node)

    def __len__(self) -> int:
        dead = [key for key, ref in self._refs.items() if ref() is None]
        for key in dead:
            del self._refs[key]
        return len(self._refs)


def _freeze(node: Any, registry: "weakref.WeakSet | None") -> Any:
    """把用户传入的普通嵌套结构转换为受跟踪的不可变节点。"""
    if isinstance(node, dict):
        new = TrackedDict()
        for key, value in node.items():
            new[key] = _freeze(value, registry)
        if registry is not None:
            registry.add(new)
        return new
    if isinstance(node, list):
        new = TrackedList(_freeze(item, registry) for item in node)
        if registry is not None:
            registry.add(new)
        return new
    return node


def _clone_container(node: Any, registry: "weakref.WeakSet | None") -> Any:
    """浅复制一个容器节点（路径复制时的单节点拷贝）。"""
    if isinstance(node, TrackedDict):
        new = TrackedDict(node)
    elif isinstance(node, TrackedList):
        new = TrackedList(node)
    else:
        raise TypeError(f"cannot clone node of type {type(node)!r}")
    if registry is not None:
        registry.add(new)
    return new


def _cow_set(node: Any, path: Path, value: Any, registry) -> Any:
    """写时拷贝地设置 path 处的值，返回新子树根；未涉及子树原样共享。"""
    if not path:
        return _freeze(value, registry)
    key = path[0]
    new = _clone_container(node, registry)
    if isinstance(node, TrackedDict):
        child = node.get(key)
    else:
        if key == len(node):  # 允许 index == len(list) 表示追加
            child = None
            new.append(None)
        else:
            child = node[key]
    new[key] = _cow_set(child, path[1:], value, registry)
    return new


def _cow_delete(node: Any, path: Path, registry) -> Any:
    """写时拷贝地删除 path 处的键/元素，返回新子树根。"""
    key = path[0]
    new = _clone_container(node, registry)
    if len(path) == 1:
        if isinstance(new, TrackedDict):
            if key not in new:
                raise KeyError(key)
            del new[key]
        else:
            del new[key]
        return new
    child = node[key]
    new[key] = _cow_delete(child, path[1:], registry)
    return new


def _lookup(node: Any, path: Path) -> Any:
    for key in path:
        node = node[key]
    return node


def _to_plain(node: Any) -> Any:
    """转换为普通 dict/list，便于断言与比较。"""
    if isinstance(node, TrackedDict):
        return {key: _to_plain(value) for key, value in node.items()}
    if isinstance(node, TrackedList):
        return [_to_plain(item) for item in node]
    return node


def count_nodes(root: Any) -> int:
    """统计从 root 可达的去重容器节点数（用于验证共享与回收）。"""
    seen: set[int] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        if not _trackable(node) or id(node) in seen:
            continue
        seen.add(id(node))
        stack.extend(node.values() if isinstance(node, TrackedDict) else node)
    return len(seen)


class Diff:
    """两个版本之间的差异。added/removed 映射 path -> 值；modified 映射 path -> (旧, 新)。"""

    def __init__(self) -> None:
        self.added: Dict[Path, Any] = {}
        self.removed: Dict[Path, Any] = {}
        self.modified: Dict[Path, Tuple[Any, Any]] = {}

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.modified)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"Diff(added={self.added}, removed={self.removed}, "
            f"modified={self.modified})"
        )


def _diff_nodes(old: Any, new: Any, path: Path, out: Diff) -> None:
    if old is new:
        return  # 共享子树，必然无差异 —— 这是增量 diff 快的原因
    if isinstance(old, TrackedDict) and isinstance(new, TrackedDict):
        for key in old.keys() - new.keys():
            out.removed[path + (key,)] = _to_plain(old[key])
        for key in new.keys() - old.keys():
            out.added[path + (key,)] = _to_plain(new[key])
        for key in old.keys() & new.keys():
            _diff_nodes(old[key], new[key], path + (key,), out)
        return
    if isinstance(old, TrackedList) and isinstance(new, TrackedList):
        common = min(len(old), len(new))
        for i in range(common):
            _diff_nodes(old[i], new[i], path + (i,), out)
        for i in range(common, len(old)):
            out.removed[path + (i,)] = _to_plain(old[i])
        for i in range(common, len(new)):
            out.added[path + (i,)] = _to_plain(new[i])
        return
    if old != new:
        out.modified[path] = (_to_plain(old), _to_plain(new))


class VersionedStore:
    """版本化键值存储。版本从 0 开始编号，0 为初始版本。"""

    def __init__(self, initial: Any = None, *, track_nodes: bool = True) -> None:
        self.registry: "NodeRegistry | None" = (
            NodeRegistry() if track_nodes else None
        )
        root = _freeze({} if initial is None else initial, self.registry)
        self._versions: Dict[int, Any] = {0: root}
        self._head_version = 0
        self._head = root  # 工作副本根（COW，永不原地修改）
        self._next_version = 1

    # ---- 修改（作用于当前工作副本） ----

    def set(self, path: Path, value: Any) -> None:
        if isinstance(path, (str, bytes)) or not isinstance(path, tuple):
            raise TypeError("path must be a tuple of keys/indices")
        self._head = _cow_set(self._head, path, value, self.registry)

    def delete(self, path: Path) -> None:
        self._head = _cow_delete(self._head, path, self.registry)

    def get(self, path: Path, default: Any = _MISSING) -> Any:
        try:
            return _lookup(self._head, path)
        except (KeyError, IndexError, TypeError):
            if default is _MISSING:
                raise
            return default

    # ---- 版本管理 ----

    def snapshot(self) -> int:
        """冻结当前工作副本为新版本，返回版本号。O(1)。"""
        version = self._next_version
        self._next_version += 1
        self._versions[version] = self._head
        self._head_version = version
        return version

    def checkout(self, version: int) -> None:
        """回退到指定版本；之后的修改通过 COW 进行，不会污染该版本。"""
        if version not in self._versions:
            raise KeyError(f"unknown version {version}")
        self._head_version = version
        self._head = self._versions[version]

    @property
    def head_version(self) -> int:
        return self._head_version

    def versions(self) -> List[int]:
        return sorted(self._versions)

    def view(self, version: int) -> Any:
        """返回指定版本的普通 dict/list 视图（深拷贝，只读用途）。"""
        if version not in self._versions:
            raise KeyError(f"unknown version {version}")
        return _to_plain(self._versions[version])

    def head_view(self) -> Any:
        return _to_plain(self._head)

    def delete_version(self, version: int) -> None:
        """删除版本并释放其引用；其独占节点由引用计数自动回收。"""
        if version not in self._versions:
            raise KeyError(f"unknown version {version}")
        del self._versions[version]

    def diff(self, v_old: int, v_new: int) -> Diff:
        if v_old not in self._versions or v_new not in self._versions:
            raise KeyError("unknown version")
        out = Diff()
        _diff_nodes(self._versions[v_old], self._versions[v_new], (), out)
        return out

    # ---- 观测/测试辅助 ----

    def live_tracked_nodes(self) -> int:
        """当前进程内仍存活的受跟踪容器节点总数。"""
        if self.registry is None:
            raise RuntimeError("node tracking disabled")
        return len(self.registry)

    def reachable_nodes(self) -> int:
        """所有现存版本 + 工作副本实际可达的去重节点数。"""
        seen: set[int] = set()
        stack = list(self._versions.values()) + [self._head]
        while stack:
            node = stack.pop()
            if not _trackable(node) or id(node) in seen:
                continue
            seen.add(id(node))
            stack.extend(
                node.values() if isinstance(node, TrackedDict) else node
            )
        return len(seen)

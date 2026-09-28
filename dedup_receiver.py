"""消息去重接收器（仅标准库）。

设计要点
--------
每条消息形如 ``{"id": str, "seq": int, "payload": ...}``：

* ``id`` 标识一条消息流（同一上游来源），``seq`` 为该流内单调递增的序号，
  序号空间为 ``2 ** seq_bits``，支持回绕（模运算比较）。
* 每个流维护一个滑动窗口：当前最大序号 ``max_seq`` 加上最近
  ``window_size`` 个 ``seq -> payload_hash`` 的记录。
* 判定规则（distance 均为模 2^seq_bits 的有向距离）：

  - ``seq`` 领先 ``max_seq``：新消息，处理并前移窗口；
    领先幅度 >= window_size 视为大幅跳跃，清空旧窗口（旧记录全部过期）。
  - ``seq`` 落后 ``max_seq`` 但距离 < window_size：窗口内，
    哈希一致判重复丢弃；哈希不一致判"同标识不同内容"冲突，丢弃并单独计数。
  - ``seq`` 落后距离 >= window_size：窗口外的迟到消息，无法区分
    "迟到重复"与"被延迟的新消息"，按保守策略**丢弃并计 expired**。
* ``id`` 缺失/非法或 ``seq`` 缺失/非法：无法判定是否重复，**不交给正常
  处理器**（否则同一封消息重投一次就会被处理两次，违背"同一消息只处理
  一次"），单独计 undecidable；若提供了 ``undecidable_handler`` 则路由
  到该隔离处理器，与正常处理完全分开。
* 内存有界：流数量上限 ``max_streams``（LRU 淘汰），每流窗口上限
  ``window_size``，总状态 <= max_streams * window_size 条小记录，
  与处理消息总量无关。

统计自洽不变式：received == processed + duplicate + expired + conflict
+ undecidable（jumps / evictions 为信息性计数）。
"""

from collections import OrderedDict

__all__ = ["DedupReceiver"]


def _default_hash(payload):
    # 进程内一致即可（仅用于同窗口内冲突检测），用内置 hash 保证速度。
    if isinstance(payload, (bytes, str)):
        return hash(payload)
    return hash(repr(payload))


class _StreamState:
    __slots__ = ("max_seq", "window")

    def __init__(self, seq, payload_hash):
        self.max_seq = seq
        self.window = {seq: payload_hash}  # seq -> payload hash


class DedupReceiver:
    def __init__(self, window_size=1024, max_streams=128, seq_bits=32,
                 handler=None, hash_fn=None, undecidable_handler=None):
        if window_size < 1:
            raise ValueError("window_size must be >= 1")
        if max_streams < 1:
            raise ValueError("max_streams must be >= 1")
        if not 2 <= seq_bits <= 63:
            raise ValueError("seq_bits must be in [2, 63]")
        if window_size > (1 << (seq_bits - 1)):
            raise ValueError("window_size must be <= 2^(seq_bits-1)")
        self.window_size = window_size
        self.max_streams = max_streams
        self.seq_bits = seq_bits
        self._mod = 1 << seq_bits
        self._half = 1 << (seq_bits - 1)
        self._mask = self._mod - 1
        self.handler = handler if handler is not None else (lambda msg: None)
        self.hash_fn = hash_fn if hash_fn is not None else _default_hash
        self.undecidable_handler = undecidable_handler
        self._streams = OrderedDict()  # id -> _StreamState, LRU 顺序
        self.stats = {
            "received": 0,    # 收到的消息总数
            "processed": 0,   # 实际交给 handler 处理的数量
            "duplicate": 0,   # 窗口内重复，丢弃
            "expired": 0,     # 窗口外迟到（重复或太旧的新消息），丢弃
            "conflict": 0,    # 同 id+seq 但内容不同，丢弃
            "undecidable": 0, # 标识/序号缺失或非法，无法去重，不处理，单独计数
            "jumps": 0,       # 序号大幅跳跃导致窗口清空的次数
            "evictions": 0,   # 流状态被 LRU 淘汰的次数
        }

    # -- 公共 API ---------------------------------------------------------

    def receive(self, msg):
        """接收一条消息，返回处置结果字符串。"""
        stats = self.stats
        stats["received"] += 1
        mid = msg.get("id")
        seq = msg.get("seq")
        payload = msg.get("payload")

        if not self._valid_id(mid) or not self._valid_seq(seq):
            stats["undecidable"] += 1
            if self.undecidable_handler is not None:
                self.undecidable_handler(msg)
            return "undecidable"

        state = self._streams.get(mid)
        if state is None:
            if len(self._streams) >= self.max_streams:
                self._streams.popitem(last=False)  # 淘汰最久未用的流
                stats["evictions"] += 1
            self._streams[mid] = _StreamState(seq, self.hash_fn(payload))
            return self._process(msg, "processed_new_stream")
        self._streams.move_to_end(mid)

        delta = (seq - state.max_seq) & self._mask
        if delta == 0:
            return self._check_window(msg, state, seq, payload)
        if delta < self._half:
            # seq 领先：新消息，前移窗口
            if delta >= self.window_size:
                stats["jumps"] += 1
                state.window.clear()
            else:
                self._prune(state, seq, delta)
            state.max_seq = seq
            state.window[seq] = self.hash_fn(payload)
            return self._process(msg, "processed")
        # seq 落后
        behind = (-delta) & self._mask
        if behind >= self.window_size:
            stats["expired"] += 1
            return "expired"
        return self._check_window(msg, state, seq, payload)

    def state_size(self):
        """返回 (流数量, 窗口记录总数)，用于验证内存有界。"""
        return (len(self._streams),
                sum(len(s.window) for s in self._streams.values()))

    def check_consistency(self):
        """校验统计自洽：
        received == processed + duplicate + expired + conflict + undecidable。"""
        s = self.stats
        assert s["received"] == (s["processed"] + s["duplicate"]
                                 + s["expired"] + s["conflict"]
                                 + s["undecidable"]), s
        return True

    # -- 内部 -------------------------------------------------------------

    def _valid_id(self, mid):
        return isinstance(mid, str) and len(mid) > 0

    def _valid_seq(self, seq):
        return (isinstance(seq, int) and not isinstance(seq, bool)
                and 0 <= seq < self._mod)

    def _process(self, msg, result):
        self.stats["processed"] += 1
        self.handler(msg)
        return result

    def _prune(self, state, new_max_seq, delta):
        # 增量淘汰：max_seq 每前进 1 位恰好有 1 个序号滑出窗口，
        # 前进 delta 位只需删除这 delta 个连续序号（均摊 O(1)），
        # 不再全表扫描整个窗口字典。
        window = state.window
        if not window:
            return
        mask = self._mask
        cutoff = new_max_seq - self.window_size  # 最后一个过期序号（模意义下）
        for old in range(cutoff - delta + 1, cutoff + 1):
            window.pop(old & mask, None)

    def _check_window(self, msg, state, seq, payload):
        stats = self.stats
        old_hash = state.window.get(seq)
        if old_hash is None:
            # 窗口内空隙：乱序到达的新消息（此前未见过该 seq），正常处理
            state.window[seq] = self.hash_fn(payload)
            return self._process(msg, "processed")
        if old_hash == self.hash_fn(payload):
            stats["duplicate"] += 1
            return "duplicate"
        stats["conflict"] += 1
        return "conflict"


if __name__ == "__main__":
    # 统计输出样例：构造覆盖所有处置类别的场景
    rx = DedupReceiver(window_size=8, max_streams=4, seq_bits=8)
    scenario = [
        {"id": "up-1", "seq": 1, "payload": "a"},      # 新流，处理
        {"id": "up-1", "seq": 2, "payload": "b"},      # 处理
        {"id": "up-1", "seq": 2, "payload": "b"},      # 窗口内重复 -> duplicate
        {"id": "up-1", "seq": 2, "payload": "B!"},     # 同 id+seq 内容不同 -> conflict
        {"id": "up-1", "seq": 3, "payload": "c"},      # 处理
        {"id": "up-1", "seq": 1, "payload": "a"},      # 窗口内乱序重复 -> duplicate
        {"id": "up-1", "seq": 100, "payload": "x"},    # 大幅跳跃 -> jumps，窗口清空
        {"id": "up-1", "seq": 3, "payload": "c"},      # 落后 97 >= 8 -> expired
        {"id": "up-1", "seq": 200, "payload": "u"},    # 再次跳跃 -> jumps
        {"id": "up-1", "seq": 255, "payload": "w"},    # 正常前进 -> 处理
        {"id": "up-1", "seq": 0, "payload": "y"},      # 序号回绕(255->0) -> 处理
        {"id": "up-1", "seq": 0, "payload": "y"},      # 回绕后重复 -> duplicate
        {"id": None, "seq": 5, "payload": "no-id"},    # 标识缺失 -> undecidable，不处理
        {"id": "up-2", "payload": "no-seq"},           # 序号缺失 -> undecidable，不处理
    ]
    for m in scenario:
        print(f"{str(m):58s} -> {rx.receive(m)}")
    print()
    print("统计输出:")
    for key, val in rx.stats.items():
        print(f"  {key:10s} = {val}")
    rx.check_consistency()
    print("\n自洽校验通过: received == processed + duplicate + expired + conflict + undecidable")

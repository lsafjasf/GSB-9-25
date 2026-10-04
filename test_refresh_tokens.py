"""test_refresh_tokens.py — 刷新令牌轮换自测（unittest，纯标准库）。

运行：
    python3 test_refresh_tokens.py -v          # 全部测试
    python3 test_refresh_tokens.py --demo      # 重放检测过程演示
"""

import sys
import threading
import unittest

from refresh_tokens import (
    AbsoluteLimitError,
    AccessTokenExpiredError,
    FamilyRevokedError,
    RefreshTokenExpiredError,
    RefreshTokenStore,
    ReplayDetectedError,
    UnknownTokenError,
)


class FakeClock:
    """可注入的虚拟时钟：advance() 前进，store 只读取它。"""

    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_store(clock: FakeClock, **overrides) -> RefreshTokenStore:
    params = dict(access_ttl=60, refresh_ttl=600, max_refreshes=3,
                  max_family_age=3600, time_func=clock)
    params.update(overrides)
    return RefreshTokenStore(**params)


class TestNormalRefresh(unittest.TestCase):
    """正常刷新：轮换出新令牌，旧刷新令牌立即作废。"""

    def setUp(self):
        self.clock = FakeClock()
        self.store = make_store(self.clock)
        self.pair0 = self.store.authenticate("alice")

    def test_rotate_issues_new_pair_and_invalidates_old(self):
        pair1 = self.store.refresh(self.pair0.refresh_token)
        self.assertEqual(pair1.family_id, self.pair0.family_id)
        self.assertEqual(pair1.refresh_seq, 1)
        self.assertNotEqual(pair1.refresh_token, self.pair0.refresh_token)
        self.assertNotEqual(pair1.access_token, self.pair0.access_token)
        self.assertEqual(self.store.validate_access(pair1.access_token), "alice")
        # 旧刷新令牌再用 -> 重放
        with self.assertRaises(ReplayDetectedError):
            self.store.refresh(self.pair0.refresh_token)

    def test_chain_of_refreshes(self):
        token = self.pair0.refresh_token
        for expected_seq in (1, 2, 3):
            pair = self.store.refresh(token)
            self.assertEqual(pair.refresh_seq, expected_seq)
            token = pair.refresh_token
        revoked, count, _ = self.store.family_status(self.pair0.family_id)
        self.assertFalse(revoked)
        self.assertEqual(count, 3)

    def test_access_token_expires(self):
        self.clock.advance(59)
        self.assertEqual(self.store.validate_access(self.pair0.access_token), "alice")
        self.clock.advance(2)  # t = 61 > access_ttl
        with self.assertRaises(AccessTokenExpiredError):
            self.store.validate_access(self.pair0.access_token)

    def test_refresh_token_expires_without_family_revocation(self):
        self.clock.advance(601)  # 超过 refresh_ttl=600
        with self.assertRaises(RefreshTokenExpiredError):
            self.store.refresh(self.pair0.refresh_token)
        revoked, _, _ = self.store.family_status(self.pair0.family_id)
        self.assertFalse(revoked)  # 合法过期不连坐家族

    def test_unknown_tokens_rejected(self):
        with self.assertRaises(UnknownTokenError):
            self.store.refresh("rt_nonexistent")
        with self.assertRaises(UnknownTokenError):
            self.store.validate_access("at_nonexistent")


class TestReplayAndFamilyRevocation(unittest.TestCase):
    """重放检测样例 + 族内全部令牌被拒的断言。"""

    def test_replay_revokes_entire_family(self):
        clock = FakeClock()
        store = make_store(clock)
        pair0 = store.authenticate("alice")
        pair1 = store.refresh(pair0.refresh_token)   # 合法用户轮换
        pair2 = store.refresh(pair1.refresh_token)   # 再轮换一次，族内已有 3 代令牌

        # 攻击者拿着窃取到的旧令牌 pair0.refresh_token 重放
        with self.assertRaises(ReplayDetectedError):
            store.refresh(pair0.refresh_token)

        # 族失效断言：家族状态为已作废，原因是 replay
        revoked, count, reason = store.family_status(pair0.family_id)
        self.assertTrue(revoked)
        self.assertEqual(reason, "replay")
        self.assertEqual(count, 2)

        # 族内全部刷新令牌（含从未使用过的最新令牌）一律被拒
        refresh_tokens, access_tokens = store.family_tokens(pair0.family_id)
        self.assertEqual(len(refresh_tokens), 3)
        for token in refresh_tokens:
            with self.assertRaises(FamilyRevokedError):
                store.refresh(token)
        # 族内全部访问令牌（含未过期的）一律被拒
        self.assertEqual(len(access_tokens), 3)
        for token in access_tokens:
            with self.assertRaises(FamilyRevokedError):
                store.validate_access(token)

        # 其他用户的家族不受影响
        other = store.authenticate("bob")
        self.assertEqual(store.validate_access(other.access_token), "bob")
        store.refresh(other.refresh_token)  # 正常轮换

    def test_stale_token_replay_after_several_rotations(self):
        clock = FakeClock()
        store = make_store(clock)
        pair = store.authenticate("alice")
        stolen = pair.refresh_token
        for _ in range(3):  # 合法用户连续轮换，窃取令牌早已作废
            pair = store.refresh(pair.refresh_token)
        with self.assertRaises(ReplayDetectedError):
            store.refresh(stolen)
        revoked, _, reason = store.family_status(pair.family_id)
        self.assertTrue(revoked)
        self.assertEqual(reason, "replay")


class TestAbsoluteLimit(unittest.TestCase):
    """绝对上限：超过上限必须重新认证，给出上限前后的边界结果。"""

    def test_max_refreshes_boundary(self):
        clock = FakeClock()
        store = make_store(clock, max_refreshes=3, max_family_age=None)
        pair = store.authenticate("alice")
        # 上限内：第 1..3 次刷新全部成功
        for _ in range(3):
            pair = store.refresh(pair.refresh_token)
        self.assertEqual(pair.refresh_seq, 3)
        # 第 4 次（越界）：必须重新认证，家族整体作废
        with self.assertRaises(AbsoluteLimitError) as ctx:
            store.refresh(pair.refresh_token)
        self.assertEqual(ctx.exception.limit, "max_refreshes")
        revoked, _, reason = store.family_status(pair.family_id)
        self.assertTrue(revoked)
        self.assertEqual(reason, "absolute_limit:max_refreshes")
        # 越界后族内任何令牌都不能再用
        with self.assertRaises(FamilyRevokedError):
            store.validate_access(pair.access_token)
        # 重新认证建立新家族后可正常使用
        fresh = store.authenticate("alice")
        self.assertNotEqual(fresh.family_id, pair.family_id)
        self.assertEqual(store.validate_access(fresh.access_token), "alice")

    def test_max_family_age_boundary(self):
        clock = FakeClock()
        store = make_store(clock, max_refreshes=None, max_family_age=3600,
                           refresh_ttl=10_000)
        pair = store.authenticate("alice")
        # 上限前：t = 3599（< 3600）仍可刷新
        clock.advance(3599)
        pair = store.refresh(pair.refresh_token)
        self.assertEqual(pair.refresh_seq, 1)
        # 越过上限：t = 3601（>= 3600）必须重新认证
        clock.advance(2)
        with self.assertRaises(AbsoluteLimitError) as ctx:
            store.refresh(pair.refresh_token)
        self.assertEqual(ctx.exception.limit, "max_family_age")
        revoked, _, _ = store.family_status(pair.family_id)
        self.assertTrue(revoked)


class TestConcurrentRefresh(unittest.TestCase):
    """并发刷新：同一刷新令牌被 N 个线程同时使用时，
    恰有一个成功，其余全部按重放处理，家族最终作废。"""

    def test_concurrent_refresh_same_token(self):
        clock = FakeClock()
        store = make_store(clock)
        pair0 = store.authenticate("alice")
        n_threads = 16
        barrier = threading.Barrier(n_threads)
        results, errors = [], []

        def worker():
            barrier.wait()  # 尽量同时发起
            try:
                results.append(store.refresh(pair0.refresh_token))
            except Exception as exc:  # noqa: BLE001 - 收集后统一断言
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 恰有一次成功
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].refresh_seq, 1)
        # 其余全部触发重放
        self.assertEqual(len(errors), n_threads - 1)
        for exc in errors:
            self.assertIsInstance(exc, ReplayDetectedError)
        # 家族已作废：赢家换到的新令牌也无法再使用
        revoked, _, reason = store.family_status(pair0.family_id)
        self.assertTrue(revoked)
        self.assertEqual(reason, "replay")
        with self.assertRaises(FamilyRevokedError):
            store.refresh(results[0].refresh_token)
        with self.assertRaises(FamilyRevokedError):
            store.validate_access(results[0].access_token)


def demo() -> None:
    """重放检测样例：打印一次完整的窃取-重放-族诛过程。"""
    clock = FakeClock()
    store = make_store(clock)
    print("== 重放检测演示 ==")
    pair0 = store.authenticate("alice")
    print(f"[t={clock.now:.0f}] 登录，签发家族 {pair0.family_id[:14]}... "
          f"rt0={pair0.refresh_token[:12]}...")
    pair1 = store.refresh(pair0.refresh_token)
    print(f"[t={clock.now:.0f}] 正常刷新：rt0 作废，签发 rt1={pair1.refresh_token[:12]}...")
    print(f"[t={clock.now:.0f}] 攻击者用窃取到的 rt0 重放 ...")
    try:
        store.refresh(pair0.refresh_token)
    except ReplayDetectedError as exc:
        print(f"[t={clock.now:.0f}] 判定重放 -> {exc}")
    revoked, _, reason = store.family_status(pair0.family_id)
    print(f"[t={clock.now:.0f}] 家族状态: revoked={revoked}, reason={reason}")
    try:
        store.refresh(pair1.refresh_token)
    except FamilyRevokedError as exc:
        print(f"[t={clock.now:.0f}] 合法用户持最新 rt1 也被拒 -> {exc}")
    print("结论：旧令牌重放导致整个令牌家族失效，双方都必须重新认证。")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        sys.argv.remove("--demo")
        demo()
    else:
        unittest.main(verbosity=2 if "-v" in sys.argv else 1)

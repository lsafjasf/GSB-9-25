"""fieldcrypt 自测套件：python3 test_fieldcrypt.py -v"""

import os
import struct
import unittest

import fieldcrypt as fc


def make_store(*versions):
    """生成密钥库，最后一个版本为活跃版本。"""
    store = fc.KeyStore()
    for v in versions:
        store.add(v, bytes([v]) * 32)
    return store


class TestChaCha20(unittest.TestCase):
    def test_rfc8439_vector(self):
        # RFC 8439 §2.4.2 官方测试向量
        key = bytes(range(32))
        nonce = bytes.fromhex("000000000000004a00000000")
        plaintext = (
            b"Ladies and Gentlemen of the class of '99: If I could offer "
            b"you only one tip for the future, sunscreen would be it.")
        expected = bytes.fromhex(
            "6e2e359a2568f98041ba0728dd0d6981e97e7aec1d4360c20a27afccfd9fae0b"
            "f91b65c5524733ab8f593dabcd62b3571639d624e65152ab8f530c359f0861d8"
            "07ca0dbf500d6a6156a38e088a22b65e52bc514d16ccf806818ce91ab7793736"
            "5af90bbf74a35be6b40b8eedf2785e42874d")
        self.assertEqual(fc._chacha20_xor(key, nonce, plaintext, 1), expected)


class TestRoundTrip(unittest.TestCase):
    def setUp(self):
        self.store = make_store(1)

    def test_basic(self):
        ct = fc.encrypt(b"hello world", self.store)
        self.assertEqual(fc.decrypt(ct, self.store), b"hello world")

    def test_empty_plaintext(self):
        ct = fc.encrypt(b"", self.store)
        self.assertEqual(fc.decrypt(ct, self.store), b"")

    def test_long_field(self):
        # 超长字段：2 MiB，跨 512 个块
        data = os.urandom(2 * 1024 * 1024)
        ct = fc.encrypt(data, self.store)
        self.assertEqual(fc.decrypt(ct, self.store), data)

    def test_various_lengths(self):
        for n in (1, 63, 64, 65, 4095, 4096, 4097, 10000):
            data = os.urandom(n)
            self.assertEqual(
                fc.decrypt(fc.encrypt(data, self.store), self.store), data)

    def test_nonce_randomized(self):
        c1 = fc.encrypt(b"same plaintext", self.store)
        c2 = fc.encrypt(b"same plaintext", self.store)
        self.assertNotEqual(c1, c2)  # 同一明文两次加密必须不同

    def test_header_contains_version_and_nonce(self):
        ct = fc.encrypt(b"x", self.store)
        info = fc.ciphertext_info(ct)
        self.assertEqual(info["key_version"], 1)
        self.assertEqual(len(ct), info["total_len"])
        self.assertEqual(fc.peek_key_version(ct), 1)

    def test_batch(self):
        items = [os.urandom(i % 300) for i in range(2000)]
        cts = fc.encrypt_many(items, self.store)
        self.assertEqual(fc.decrypt_many(cts, self.store), items)


class TestTamperDetection(unittest.TestCase):
    def setUp(self):
        self.store = make_store(1)
        self.plain = os.urandom(9000)  # 3 个密文块
        self.ct = fc.encrypt(self.plain, self.store)

    def _flip(self, offset):
        buf = bytearray(self.ct)
        buf[offset] ^= 0x01
        return bytes(buf)

    def test_tamper_header_reports_header(self):
        bad = self._flip(5)  # nonce 区域
        with self.assertRaises(fc.IntegrityError) as ctx:
            fc.decrypt(bad, self.store)
        self.assertEqual(ctx.exception.location, "header/tag")
        self.assertEqual(ctx.exception.offset, 0)

    def test_tamper_chunk_reports_chunk_and_offset(self):
        # 第 1 块（字节偏移 HEADER_SIZE + 4096 起）
        offset = fc.HEADER_SIZE + 4096 + 100
        bad = self._flip(offset)
        with self.assertRaises(fc.IntegrityError) as ctx:
            fc.decrypt(bad, self.store)
        self.assertEqual(ctx.exception.location, "chunk 1")
        self.assertEqual(ctx.exception.offset, fc.HEADER_SIZE + 4096)

    def test_tamper_tag_detected(self):
        bad = self._flip(len(self.ct) - 1)  # 最后一个标签字节
        with self.assertRaises(fc.IntegrityError):
            fc.decrypt(bad, self.store)

    def test_truncation(self):
        with self.assertRaises(fc.TruncatedCiphertextError) as ctx:
            fc.decrypt(self.ct[:10], self.store)  # 头部都不完整
        self.assertEqual(ctx.exception.section, "header")
        with self.assertRaises(fc.TruncatedCiphertextError) as ctx:
            fc.decrypt(self.ct[:-3], self.store)  # 尾部缺字节
        self.assertEqual(ctx.exception.section, "body")
        self.assertEqual(ctx.exception.expected, len(self.ct))
        self.assertEqual(ctx.exception.actual, len(self.ct) - 3)

    def test_length_mismatch_extra_bytes(self):
        with self.assertRaises(fc.LengthMismatchError) as ctx:
            fc.decrypt(self.ct + b"\x00", self.store)
        self.assertEqual(ctx.exception.expected, len(self.ct))
        self.assertEqual(ctx.exception.actual, len(self.ct) + 1)

    def test_bad_magic(self):
        bad = self._flip(0)
        with self.assertRaises(fc.FormatError):
            fc.decrypt(bad, self.store)


class TestAadBinding(unittest.TestCase):
    def setUp(self):
        self.store = make_store(1)

    def test_aad_roundtrip(self):
        ct = fc.encrypt(b"secret", self.store, aad="user:1001")
        self.assertEqual(fc.decrypt(ct, self.store, aad="user:1001"),
                         b"secret")

    def test_aad_mismatch_fails(self):
        ct = fc.encrypt(b"secret", self.store, aad="user:1001")
        with self.assertRaises(fc.AadMismatchError):
            fc.decrypt(ct, self.store, aad="user:1002")

    def test_cross_record_copy_detected(self):
        """跨记录复制密文：把 A 的密文贴到 B 的记录上必须解密失败。"""
        records = {}  # pk -> 密文
        for pk, secret in (("user:1001", b"alice-token"),
                           ("user:1002", b"bob-token")):
            records[pk] = fc.encrypt(secret, self.store, aad=pk)
        # 攻击者把 user:1001 的密文复制到 user:1002 的记录上
        records["user:1002"] = records["user:1001"]
        self.assertEqual(
            fc.decrypt(records["user:1001"], self.store, aad="user:1001"),
            b"alice-token")
        with self.assertRaises(fc.AadMismatchError):
            fc.decrypt(records["user:1002"], self.store, aad="user:1002")


class TestKeyErrors(unittest.TestCase):
    def test_key_missing_on_encrypt(self):
        with self.assertRaises(fc.KeyMissingError):
            fc.encrypt(b"x", fc.KeyStore())

    def test_key_missing_on_decrypt_empty_store(self):
        store = make_store(1)
        ct = fc.encrypt(b"x", store)
        with self.assertRaises(fc.KeyMissingError):
            fc.decrypt(ct, fc.KeyStore())

    def test_unknown_key_version(self):
        store = make_store(1)
        ct = bytearray(fc.encrypt(b"x", store))
        ct[3:5] = struct.pack(">H", 99)  # 篡改头部密钥版本
        with self.assertRaises(fc.UnknownKeyVersionError) as ctx:
            fc.decrypt(bytes(ct), store)
        self.assertEqual(ctx.exception.version, 99)

    def test_error_types_distinguishable(self):
        # 四类错误是互不相同的异常类型
        for cls in (fc.KeyMissingError, fc.UnknownKeyVersionError,
                    fc.TruncatedCiphertextError, fc.LengthMismatchError):
            self.assertTrue(issubclass(cls, fc.FieldCryptError))
        self.assertNotEqual(fc.KeyMissingError, fc.UnknownKeyVersionError)
        self.assertNotEqual(fc.TruncatedCiphertextError,
                            fc.LengthMismatchError)


class TestRotation(unittest.TestCase):
    def test_old_data_readable_new_writes_use_latest(self):
        store = make_store(1)
        old_ct = fc.encrypt(b"old data", store)
        store.add(2, bytes([2]) * 32)  # 轮换：v2 成为活跃版本
        # 旧数据仍可解密
        self.assertEqual(fc.decrypt(old_ct, store), b"old data")
        # 新写入使用最新版本
        new_ct = fc.encrypt(b"new data", store)
        self.assertEqual(fc.peek_key_version(new_ct), 2)
        self.assertEqual(fc.peek_key_version(old_ct), 1)

    def test_unknown_after_key_removed(self):
        store = make_store(1)
        ct = fc.encrypt(b"x", store)
        store.add(2, bytes([2]) * 32)
        store.remove(1)  # 确认全部重加密后才允许删除
        with self.assertRaises(fc.UnknownKeyVersionError):
            fc.decrypt(ct, store)

    def test_reencrypt_interruptible_and_reentrant(self):
        store = make_store(1)
        n = 500
        pks = [f"rec:{i}" for i in range(n)]
        plains = [os.urandom(40) for _ in range(n)]
        records = [fc.encrypt(p, store, aad=pk)
                   for p, pk in zip(plains, pks)]

        store.add(2, bytes([2]) * 32)  # 触发轮换

        # 第一次运行：处理到一半模拟崩溃（on_record 里抛异常）
        class Crash(Exception):
            pass

        done = []
        def watchdog(i):
            done.append(i)
            if len(done) == 200:
                raise Crash("模拟中断")

        with self.assertRaises(Crash):
            fc.reencrypt_batch(records, store,
                               aad_for=lambda i: pks[i], on_record=watchdog)

        # 中断点：前 200 条是新版本，其余仍是旧版本 —— 全部可解
        for i, rec in enumerate(records):
            self.assertEqual(fc.peek_key_version(rec), 2 if i < 200 else 1)
            self.assertEqual(fc.decrypt(rec, store, aad=pks[i]), plains[i])

        # 重入：从头再跑一遍，跳过已完成记录，剩余全部轮换
        rotated = fc.reencrypt_batch(records, store,
                                     aad_for=lambda i: pks[i])
        self.assertEqual(rotated, n - 200)
        for i, rec in enumerate(records):
            self.assertEqual(fc.peek_key_version(rec), 2)
            self.assertEqual(fc.decrypt(rec, store, aad=pks[i]), plains[i])

        # 幂等：再次运行不再改动任何记录
        self.assertEqual(fc.reencrypt_batch(records, store,
                                            aad_for=lambda i: pks[i]), 0)

    def test_reencrypt_resume_from_checkpoint(self):
        store = make_store(1)
        records = fc.encrypt_many([b"v" * 10] * 100, store)
        store.add(2, bytes([2]) * 32)
        first = fc.reencrypt_batch(records, store, start=0)
        # 手动从断点续跑（已全部完成则返回 0）
        self.assertEqual(first, 100)
        self.assertEqual(fc.reencrypt_batch(records, store, start=50), 0)


if __name__ == "__main__":
    unittest.main()

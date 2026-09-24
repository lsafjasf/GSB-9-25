"""field_crypto 自测：正确性、篡改定位、跨记录复制、轮换与可中断重加密。"""

import struct
import unittest

import field_crypto as fc


def make_store(*versions: int) -> fc.KeyStore:
    store = fc.KeyStore()
    for v in versions:
        store.add(v, fc.generate_key())
    return store


class RoundTripTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store(1)

    def test_basic_roundtrip(self):
        token = fc.encrypt(self.store, b"hello world")
        self.assertEqual(fc.decrypt(self.store, token), b"hello world")

    def test_empty_plaintext(self):
        token = fc.encrypt(self.store, b"")
        self.assertEqual(fc.decrypt(self.store, token), b"")

    def test_unicode(self):
        text = "敏感字段：张三，身份证 110101…"
        token = fc.encrypt(self.store, text.encode("utf-8"))
        self.assertEqual(fc.decrypt(self.store, token).decode("utf-8"), text)

    def test_oversized_field_multichunk(self):
        big = bytes(range(256)) * (5 * 1024 * 1024 // 256)  # 5 MiB，跨 80 个分块
        token = fc.encrypt(self.store, big)
        self.assertGreater(len(token), len(big))  # 每块附带 32 字节标签
        self.assertEqual(fc.decrypt(self.store, token), big)

    def test_same_plaintext_twice_gives_different_tokens(self):
        t1 = fc.encrypt(self.store, b"same")
        t2 = fc.encrypt(self.store, b"same")
        self.assertNotEqual(t1, t2)  # 随机 IV
        self.assertEqual(fc.decrypt(self.store, t1), fc.decrypt(self.store, t2))

    def test_batch_encrypt_decrypt(self):
        records = [
            {"id": i, "field": fc.encrypt(self.store, f"secret-{i}".encode(), aad=f"user:{i}".encode())}
            for i in range(1000)
        ]
        for rec in records:
            pt = fc.decrypt(self.store, rec["field"], aad=f"user:{rec['id']}".encode())
            self.assertEqual(pt, f"secret-{rec['id']}".encode())


class TamperTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store(1)
        self.plaintext = b"A" * (3 * fc.CHUNK_SIZE + 100)  # 4 个分块
        self.token = fc.encrypt(self.store, self.plaintext)

    def test_tamper_reports_position(self):
        # 翻转第 2 个分块（索引 2）密文的第一个字节
        offset = fc.HEADER_LEN + 2 * (fc.CHUNK_SIZE + fc.TAG_LEN)
        tampered = bytearray(self.token)
        tampered[offset] ^= 0x01
        with self.assertRaises(fc.IntegrityError) as ctx:
            fc.decrypt(self.store, bytes(tampered))
        self.assertEqual(ctx.exception.chunk_index, 2)
        self.assertEqual(ctx.exception.byte_offset, offset)

    def test_tamper_tag_byte(self):
        offset = fc.HEADER_LEN + fc.CHUNK_SIZE + 5  # 第 0 块的标签内
        tampered = bytearray(self.token)
        tampered[offset] ^= 0x80
        with self.assertRaises(fc.IntegrityError) as ctx:
            fc.decrypt(self.store, bytes(tampered))
        self.assertEqual(ctx.exception.chunk_index, 0)

    def test_truncation(self):
        with self.assertRaises(fc.TruncatedCiphertextError):
            fc.decrypt(self.store, self.token[:-1])
        with self.assertRaises(fc.TruncatedCiphertextError):
            fc.decrypt(self.store, self.token[:10])  # 连头部都不完整

    def test_trailing_bytes_length_mismatch(self):
        with self.assertRaises(fc.LengthMismatchError):
            fc.decrypt(self.store, self.token + b"\x00")

    def test_header_length_field_corrupted(self):
        # 把头部声明的明文长度改大 -> 长度与头部不符
        tampered = bytearray(self.token)
        struct.pack_into(">Q", tampered, 4 + 4 + 16 + 32, len(self.plaintext) + 1)
        with self.assertRaises((fc.LengthMismatchError, fc.TruncatedCiphertextError,
                                fc.IntegrityError, fc.AssociatedDataMismatchError)):
            fc.decrypt(self.store, bytes(tampered))

    def test_bad_magic(self):
        tampered = b"XXXX" + self.token[4:]
        with self.assertRaises(fc.InvalidFormatError):
            fc.decrypt(self.store, tampered)


class AssociatedDataTests(unittest.TestCase):
    def test_cross_record_copy_detected(self):
        """user:1 的密文被复制到 user:2 的记录上，必须解密失败。"""
        store = make_store(1)
        db = {}
        for uid in (1, 2):
            db[uid] = fc.encrypt(store, f"ssn-of-user-{uid}".encode(), aad=f"user:{uid}".encode())

        # 攻击者把 user:1 的密文覆盖到 user:2 的记录
        db[2] = db[1]
        with self.assertRaises(fc.AssociatedDataMismatchError):
            fc.decrypt(store, db[2], aad=b"user:2")
        # 原记录不受影响
        self.assertEqual(fc.decrypt(store, db[1], aad=b"user:1"), b"ssn-of-user-1")

    def test_aad_roundtrip(self):
        store = make_store(1)
        token = fc.encrypt(store, b"data", aad=b"pk:42")
        self.assertEqual(fc.decrypt(store, token, aad=b"pk:42"), b"data")
        with self.assertRaises(fc.AssociatedDataMismatchError):
            fc.decrypt(store, token, aad=b"pk:43")
        with self.assertRaises(fc.AssociatedDataMismatchError):
            fc.decrypt(store, token)  # 漏传 AAD 同样失败


class KeyErrorTests(unittest.TestCase):
    def test_encrypt_without_key(self):
        with self.assertRaises(fc.KeyMissingError):
            fc.encrypt(fc.KeyStore(), b"x")

    def test_unknown_key_version(self):
        writer = make_store(7)
        token = fc.encrypt(writer, b"data")
        reader = make_store(1, 2)  # 没有版本 7
        with self.assertRaises(fc.UnknownKeyVersionError) as ctx:
            fc.decrypt(reader, token)
        self.assertEqual(ctx.exception.version, 7)

    def test_error_types_are_distinguishable(self):
        self.assertTrue(issubclass(fc.KeyMissingError, fc.FieldEncryptionError))
        self.assertTrue(issubclass(fc.UnknownKeyVersionError, fc.FieldEncryptionError))
        self.assertTrue(issubclass(fc.TruncatedCiphertextError, fc.FieldEncryptionError))
        self.assertTrue(issubclass(fc.LengthMismatchError, fc.FieldEncryptionError))
        for cls in (fc.KeyMissingError, fc.UnknownKeyVersionError,
                    fc.TruncatedCiphertextError, fc.LengthMismatchError,
                    fc.IntegrityError, fc.AssociatedDataMismatchError):
            for other in (fc.KeyMissingError, fc.UnknownKeyVersionError,
                          fc.TruncatedCiphertextError, fc.LengthMismatchError,
                          fc.IntegrityError, fc.AssociatedDataMismatchError):
                if cls is not other:
                    self.assertFalse(issubclass(cls, other))


class RotationTests(unittest.TestCase):
    def test_rotation_keeps_old_data_readable(self):
        store = make_store(1)
        old_token = fc.encrypt(store, b"old-secret", aad=b"pk:1")
        store.add(2, fc.generate_key())
        store.set_active(2)

        # 旧密文仍可解密
        self.assertEqual(fc.decrypt(store, old_token, aad=b"pk:1"), b"old-secret")
        # 新写入用新版本
        new_token = fc.encrypt(store, b"new-secret", aad=b"pk:1")
        self.assertEqual(fc.peek_key_version(new_token), 2)
        # 重加密
        self.assertTrue(fc.needs_rotation(store, old_token))
        rotated = fc.rotate(store, old_token, aad=b"pk:1")
        self.assertEqual(fc.peek_key_version(rotated), 2)
        self.assertEqual(fc.decrypt(store, rotated, aad=b"pk:1"), b"old-secret")
        # 幂等：再次 rotate 原样返回
        self.assertIs(fc.rotate(store, rotated, aad=b"pk:1"), rotated)

    def test_batch_reencryption_interruptible_and_reentrant(self):
        store = make_store(1)
        records = [
            {"id": i, "field": fc.encrypt(store, f"v1-secret-{i}".encode(), aad=f"pk:{i}".encode())}
            for i in range(500)
        ]
        store.add(2, fc.generate_key())
        store.set_active(2)

        # 第一次运行处理 137 条后“崩溃”
        rotated, skipped = fc.reencrypt_batch(
            store, records, "field",
            aad_of=lambda r: f"pk:{r['id']}".encode(),
            should_stop=lambda n: n >= 137,
        )
        self.assertEqual(rotated, 137)
        # 中断点前后：每条记录要么是 v1 要么是 v2 密文，且全部可解
        for rec in records:
            self.assertIn(fc.peek_key_version(rec["field"]), (1, 2))
            pt = fc.decrypt(store, rec["field"], aad=f"pk:{rec['id']}".encode())
            self.assertEqual(pt, f"v1-secret-{rec['id']}".encode())

        # 重入：剩余的全部轮换完成
        rotated2, skipped2 = fc.reencrypt_batch(
            store, records, "field", aad_of=lambda r: f"pk:{r['id']}".encode()
        )
        self.assertEqual(rotated2, 500 - 137)
        self.assertEqual(skipped2, 137)
        for rec in records:
            self.assertEqual(fc.peek_key_version(rec["field"]), 2)

        # 第三次运行：全部跳过，无写入
        rotated3, skipped3 = fc.reencrypt_batch(
            store, records, "field", aad_of=lambda r: f"pk:{r['id']}".encode()
        )
        self.assertEqual((rotated3, skipped3), (0, 500))

    def test_rotation_with_wrong_aad_fails(self):
        store = make_store(1)
        token = fc.encrypt(store, b"x", aad=b"pk:1")
        store.add(2, fc.generate_key())
        store.set_active(2)
        with self.assertRaises(fc.AssociatedDataMismatchError):
            fc.rotate(store, token, aad=b"pk:2")


if __name__ == "__main__":
    unittest.main(verbosity=2)

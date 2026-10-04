# 刷新令牌轮换（Refresh Token Rotation）库

纯 Python 3 标准库实现，时间可注入，自带线程安全状态管理。

## 文件

- `refresh_tokens.py` — 库源码（签发 / 轮换 / 重放检测 / 族失效 / 绝对上限）
- `test_refresh_tokens.py` — 10 个自测用例 + 重放检测过程演示

## 运行方式

```bash
python3 test_refresh_tokens.py -v          # 全部 10 个测试
python3 test_refresh_tokens.py --demo      # 重放检测样例（打印完整过程）
```

## 设计

- **令牌**：不透明随机串（`secrets.token_urlsafe`），状态只存服务端；
  令牌泄露后即使被冒用，服务端状态判定即可拦截。
- **轮换**：每次 `refresh()` 成功后，旧刷新令牌立即标记为 `used`，
  签发一对全新的访问 / 刷新令牌。
- **重放检测（家族失效）**：`used` 令牌再次出现即判定为窃取重放，
  整个 family 立即作废，族内历史上全部刷新令牌、访问令牌（包括攻击者
  刚换到的新令牌和未过期的访问令牌）一律拒绝；其他用户家族不受影响。
- **绝对上限（强制重新认证）**：
  - `max_refreshes`：家族内最大成功刷新次数（边界 3 次成功、第 4 次拒绝）；
  - `max_family_age`：自登录起的家族绝对寿命（边界 3599s 可刷新、3601s 拒绝）。
  任一触顶即作废整个家族并抛 `AbsoluteLimitError`，必须重新 `authenticate()`。
- **时间注入**：`RefreshTokenStore(time_func=...)`，测试用 `FakeClock`，结果确定。
- **并发**：所有状态变更在同一把锁内；16 线程用同一刷新令牌并发刷新，
  恰 1 次成功，其余全部按重放处理，家族最终作废。
- **刷新令牌自身过期**（`refresh_ttl`）与攻击区分：合法过期只拒绝本次请求，
  不连坐家族；访问令牌过期同理，可拿当前刷新令牌续期。

## 异常层次

`TokenError`
├─ `UnknownTokenError`
├─ `FamilyRevokedError`（含 `family_id` / `reason`）
│  ├─ `ReplayDetectedError`（reason=replay）
│  └─ `AbsoluteLimitError`（reason=absolute_limit:*，附 `limit`）
├─ `RefreshTokenExpiredError`
└─ `AccessTokenExpiredError`

## 测试覆盖

| 用例 | 情形 |
| --- | --- |
| `test_rotate_issues_new_pair_and_invalidates_old` | 正常刷新 + 旧令牌立即重放 |
| `test_chain_of_refreshes` | 连续轮换链 |
| `test_access_token_expires` | 访问令牌过期边界（59s 有效 / 61s 拒绝） |
| `test_refresh_token_expires_without_family_revocation` | 刷新令牌 TTL 过期不连坐 |
| `test_unknown_tokens_rejected` | 未知令牌拒绝 |
| `test_replay_revokes_entire_family` | 重放样例 + 族内全部刷新/访问令牌被拒断言 |
| `test_stale_token_replay_after_several_rotations` | 跨多代的旧令牌重放 |
| `test_max_refreshes_boundary` | 次数上限前后（3 成功 / 第 4 拒绝 / 重新认证恢复） |
| `test_max_family_age_boundary` | 寿命上限前后（3599 成功 / 3601 拒绝） |
| `test_concurrent_refresh_same_token` | 16 线程并发，恰 1 成功，其余重放 |

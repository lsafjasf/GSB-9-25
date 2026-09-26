# 性能数据（实测）

环境：Python 3.12.3，Linux 6.18 WSL2 x86_64，临时目录位于 WSL ext4（4 KiB 块），
`python3 benchmark.py` 实测，每档多轮取 best（并附 avg）。

## 结果

| 快照大小 | 阶段 | best | avg | 吞吐 (best) |
|---:|---|---:|---:|---:|
| 10 MiB | sha256 纯校验 | 4.4 ms | 4.4 ms | 2281 MiB/s |
| 10 MiB | canonical JSON 序列化 | 4.7 ms | 6.4 ms | 2150 MiB/s |
| 10 MiB | **原子保存（写+fsync+目录fsync）** | 21.2 ms | 28.1 ms | 472 MiB/s |
| 10 MiB | **恢复（读+sha256+JSON 解析）** | 13.0 ms | 14.1 ms | 768 MiB/s |
| 50 MiB | sha256 纯校验 | 19.6 ms | 20.0 ms | 2547 MiB/s |
| 50 MiB | canonical JSON 序列化 | 43.0 ms | 45.6 ms | 1163 MiB/s |
| 50 MiB | **原子保存** | 151.4 ms | 167.5 ms | 330 MiB/s |
| 50 MiB | **恢复** | 106.0 ms | 108.3 ms | 472 MiB/s |
| 100 MiB | sha256 纯校验 | 39.2 ms | 39.3 ms | 2552 MiB/s |
| 100 MiB | canonical JSON 序列化 | 90.0 ms | 97.1 ms | 1111 MiB/s |
| 100 MiB | **原子保存** | 320.4 ms | 343.1 ms | 312 MiB/s |
| 100 MiB | **恢复** | 206.8 ms | 211.2 ms | 484 MiB/s |

## 校验开销说明

* sha256 本身约 2.3–2.5 GiB/s（标准库 OpenSSL 实现）：50 MiB 约 **20 ms**。
* 占端到端耗时比例（best/best）：保存约 **12–21%**，恢复约 **19–34%**；
  快照越大，占比越低（固定的 fsync 与 JSON 成本占主导）。
* 保存路径真正的大头是 `fsync`（文件 + 目录刷盘，约 300+ MiB/s 量级）；
  恢复路径的大头是 JSON 解析（约 1.1 GiB/s 有效速率）而非校验。
* 结论：sha256 校验不是瓶颈；它提供了对磁盘位腐烂/截断/篡改的可靠检测，
  且只对 body 做单遍哈希，额外内存为 O(1)（body 本身随 JSON 信封一次性读入）。

复现：`python3 benchmark.py 50 5`

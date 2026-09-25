"""对 sample_logs.txt 做模板抽取与聚类，打印报告。

用法: python3 demo.py [--sort-kv] [logfile]
"""
import sys

from logtpl import cluster_lines, render_report


def main() -> None:
    args = [a for a in sys.argv[1:]]
    sort_kv = "--sort-kv" in args
    path = next((a for a in args if not a.startswith("--")), "sample_logs.txt")
    with open(path, encoding="utf-8") as f:
        result = cluster_lines(f, sort_kv=sort_kv)
    print(render_report(result))


if __name__ == "__main__":
    main()

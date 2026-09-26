"""生成错误提示样例集 ERROR_SAMPLES.md。

用法: python3 examples/error_samples.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cliparse import ParseError, Parser


def build_parser():
    p = Parser(prog="tool", description="内部脚本统一参数解析示例",
               exit_on_error=False)
    p.add_option("--verbose", "-v", action="count", help="增加日志级别")
    p.add_option("--output", "-o", metavar="FILE", help="输出文件")
    p.add_option("--jobs", "-j", type=int, default=1, help="并发数")
    p.add_option("--mode", choices=["fast", "slow"], default="fast",
                 help="运行模式")
    p.add_option("--config", required=True, help="配置文件 (必选)")
    p.add_positional("src", help="源文件")
    run = p.add_subcommand("run", help="运行任务")
    run.add_option("--dry-run", "-n", action="store_true", help="试运行")
    run.add_positional("task", help="任务名")
    return p


CASES = [
    ("未知长选项", ["--frob"]),
    ("未知短选项", ["-z"]),
    ("缺少必需值 (选项在末尾)", ["--config", "c.yml", "src.txt", "--output"]),
    ("值位置出现另一个选项", ["--config", "c.yml", "src.txt",
                              "--output", "--verbose"]),
    ("值类型非法", ["--config", "c.yml", "src.txt", "--jobs", "abc"]),
    ("值不在候选集合中", ["--config", "c.yml", "src.txt", "--mode", "medium"]),
    ("必需位置参数缺失", ["--config", "c.yml"]),
    ("必需选项缺失", ["src.txt"]),
    ("多余的位置参数", ["--config", "c.yml", "a", "b", "c"]),
    ("布尔开关被赋值", ["--config", "c.yml", "src.txt", "--verbose=3"]),
    ("子命令的必需位置参数缺失", ["run"]),
    ("子命令中的未知选项", ["run", "build", "--frob"]),
]


def main():
    out = ["# 错误提示样例集",
           "",
           "由 `python3 examples/error_samples.py` 自动生成。",
           "所有错误均以退出码 2 结束 (exit_on_error=True 时), "
           "并附带对应作用域的用法提示。",
           ""]
    for title, argv in CASES:
        parser = build_parser()
        try:
            parser.parse(argv)
            rendered = "(未报错)"
        except ParseError as e:
            rendered = e.format()
        out.append("## %s" % title)
        out.append("")
        out.append("命令: `tool %s`" % " ".join(argv))
        out.append("")
        out.append("```")
        out.append(rendered)
        out.append("```")
        out.append("")

    # 帮助文本样例
    out.append("## 帮助文本 (-h/--help, 退出码 0)")
    out.append("")
    out.append("```")
    out.append(build_parser().format_help())
    out.append("```")
    out.append("")

    path = os.path.join(os.path.dirname(__file__), "..", "ERROR_SAMPLES.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print("written: %s" % os.path.abspath(path))


if __name__ == "__main__":
    main()

# 错误提示样例集

由 `python3 examples/error_samples.py` 自动生成。
所有错误均以退出码 2 结束 (exit_on_error=True 时), 并附带对应作用域的用法提示。

## 未知长选项

命令: `tool --frob`

```
usage: tool [--help] [--verbose] [--output FILE] [--jobs JOBS] [--mode MODE] --config CONFIG SRC {run} ...
tool: error: unrecognized option '--frob'
```

## 未知短选项

命令: `tool -z`

```
usage: tool [--help] [--verbose] [--output FILE] [--jobs JOBS] [--mode MODE] --config CONFIG SRC {run} ...
tool: error: unrecognized option '-z'
```

## 缺少必需值 (选项在末尾)

命令: `tool --config c.yml src.txt --output`

```
usage: tool [--help] [--verbose] [--output FILE] [--jobs JOBS] [--mode MODE] --config CONFIG SRC {run} ...
tool: error: option '--output' requires a value
```

## 值位置出现另一个选项

命令: `tool --config c.yml src.txt --output --verbose`

```
usage: tool [--help] [--verbose] [--output FILE] [--jobs JOBS] [--mode MODE] --config CONFIG SRC {run} ...
tool: error: option '--output' requires a value (got option '--verbose'; use '--output=<value>' if this is intended)
```

## 值类型非法

命令: `tool --config c.yml src.txt --jobs abc`

```
usage: tool [--help] [--verbose] [--output FILE] [--jobs JOBS] [--mode MODE] --config CONFIG SRC {run} ...
tool: error: --jobs: invalid int value: 'abc'
```

## 值不在候选集合中

命令: `tool --config c.yml src.txt --mode medium`

```
usage: tool [--help] [--verbose] [--output FILE] [--jobs JOBS] [--mode MODE] --config CONFIG SRC {run} ...
tool: error: --mode: invalid choice: 'medium' (choose from 'fast', 'slow')
```

## 必需位置参数缺失

命令: `tool --config c.yml`

```
usage: tool [--help] [--verbose] [--output FILE] [--jobs JOBS] [--mode MODE] --config CONFIG SRC {run} ...
tool: error: missing required argument 'src'
```

## 必需选项缺失

命令: `tool src.txt`

```
usage: tool [--help] [--verbose] [--output FILE] [--jobs JOBS] [--mode MODE] --config CONFIG SRC {run} ...
tool: error: missing required option '--config'
```

## 多余的位置参数

命令: `tool --config c.yml a b c`

```
usage: tool [--help] [--verbose] [--output FILE] [--jobs JOBS] [--mode MODE] --config CONFIG SRC {run} ...
tool: error: unexpected extra argument(s): b c
```

## 布尔开关被赋值

命令: `tool --config c.yml src.txt --verbose=3`

```
usage: tool [--help] [--verbose] [--output FILE] [--jobs JOBS] [--mode MODE] --config CONFIG SRC {run} ...
tool: error: option '--verbose' does not take a value
```

## 子命令的必需位置参数缺失

命令: `tool run`

```
usage: tool run [--help] [--dry-run] TASK
tool run: error: missing required argument 'task'
```

## 子命令中的未知选项

命令: `tool run build --frob`

```
usage: tool run [--help] [--dry-run] TASK
tool run: error: unrecognized option '--frob'
```

## 帮助文本 (-h/--help, 退出码 0)

```
usage: tool [--help] [--verbose] [--output FILE] [--jobs JOBS] [--mode MODE] --config CONFIG SRC {run} ...

内部脚本统一参数解析示例

positional arguments:
  SRC  源文件

options:
  -h, --help         显示本帮助并退出
  -v, --verbose      增加日志级别
  -o, --output FILE  输出文件
  -j, --jobs JOBS    并发数
  --mode MODE        运行模式
  --config CONFIG    配置文件 (必选)

subcommands:
  run  运行任务
```

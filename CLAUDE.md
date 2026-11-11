# CLAUDE.md

本仓库用于让 AI 代理操作 PX Logic 逻辑分析仪。安装、用法和数据格式见 @README.md。

## 操作约定

1. 先运行 `pxlogic-agent --json --scan`（未安装时用 `python3 px_logic.py --json --scan`），`ok` 为 true 才继续。
2. 只需要频率、占空比时，运行 `pxlogic-agent --json --rate <Hz> --time <ms>`，读 stdout 的 JSON，不要解析日志。
3. 需要解码时用 Python API，读 `measure()`、`summary()` 和解码函数的返回值，不要解析打印输出。
4. 测频和解码用的 `sample_rate` 必须等于 `nearest_rate()` 取整后的实际采样率。
5. 16 通道设备：命令行加 `--channels 16`，Python 里写 `LogicAnalyzer(channels=16)`。
6. 找不到设备时：在 WSL 里先确认 Windows 侧执行过 `usbipd attach --wsl --busid <BUSID>`，再用 `lsusb` 查找 `16c0:05dc` 或 `1a86:5237`。
7. 需要中止正在进行的采集（如 loop 模式）时，执行 `pxlobster --stop`。
8. 修改代码后运行 `python3 -m unittest discover -s tests -v`。

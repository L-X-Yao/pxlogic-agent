# CLAUDE.md

本仓库用于让 AI 代理操作 PX Logic 逻辑分析仪。安装、用法和数据格式见 @README.md。

## 操作约定

1. 采集前先运行 `pxlobster --scan`，确认设备在线。
2. 优先使用 `px_logic.py` 的 `LogicAnalyzer`；直接调用 CLI 时加 `--decode-cross`。
3. 测频和解码用的 `sample_rate` 必须等于 `nearest_rate()` 取整后的实际采样率。
4. 32 通道设备的采样是 uint32，16 通道是 uint16，bit N 对应通道 N。
5. `--scan` 没有输出时：在 WSL 里先确认 Windows 侧执行过 `usbipd attach --wsl --busid <BUSID>`，再用 `lsusb` 查找 `16c0:05dc` 或 `1a86:5237`。
6. 需要中止正在进行的采集（如 loop 模式）时，执行 `pxlobster --stop`。
7. 修改解码器后运行 `python3 -m unittest discover -s tests -v`。

# AGENTS.md

本仓库让 AI 代理操作 PX Logic 逻辑分析仪：采集、测频率和脉宽，以及解码 UART / SPI / I2C。安装和完整用法见 README.md。

## 操作约定

1. 先运行 `pxlogic-agent --json --scan`（未安装时用 `python3 px_logic.py --json --scan`），`ok` 为 true 才继续。
2. 只需要频率、占空比时，运行 `pxlogic-agent --json --rate <Hz> --time <ms>`，读 stdout 的 JSON，不要解析日志。
3. 需要等触发时，加 `--trigger "0=r"`（CH0 上升沿），并用 `--timeout <秒>` 限定等待时间；超时后工具会自动发送 `--stop`。
4. 需要解码时用 Python API，读 `measure()`、`summary()` 和解码函数的返回值，不要解析打印输出。需要每帧的时间，或 UART 的校验、停止位错误时，解码函数加 `detail=True`。
5. 测频和解码用的 `sample_rate` 必须等于 `nearest_rate()` 取整后的实际采样率。
6. 采样率至少取被测信号最高频率的 10 倍；解 UART 时至少取波特率的 10 倍。测脉宽（`high_us`）时分辨率是一个采样周期，舵机、电调 PWM 至少用 1 MHz。
7. 采样点数等于采样率乘时长，32 通道每点 4 字节；单次尽量不超过 5000 万点（约 200 MB）。
8. 16 通道设备：命令行加 `--channels 16`，Python 里写 `LogicAnalyzer(channels=16)`。
9. 找不到设备时：在 WSL 里先确认 Windows 侧执行过 `usbipd attach --wsl --busid <BUSID>`，再用 `lsusb` 查找 `16c0:05dc` 或 `1a86:5237`。
10. 需要中止正在进行的采集（如 loop 模式）时，执行 `pxlobster --stop`。
11. 修改代码后运行 `python3 -m unittest discover -s tests -v`；修改操作约定时，同步更新 `skills/pxlogic/SKILL.md`。

---
name: pxlogic
description: 用 PX Logic 逻辑分析仪采集和分析数字信号：测 PWM 频率、占空比、脉宽和抖动，抓 UART / SBUS / SPI / I2C 波形并解码。用户提到逻辑分析仪、PX Logic、pxlobster、抓波形、测频率、测舵机或电调 PWM 脉宽、看串口、SBUS 或 I2C / SPI 总线时使用。Capture and decode digital signals (PWM frequency, duty and pulse width, UART, SBUS, SPI, I2C) with a PX Logic logic analyzer.
---

# PX Logic 逻辑分析仪

通过 `pxlogic-agent` 命令和 `px_logic` Python 模块操作设备，安装方式：`pip install git+https://github.com/L-X-Yao/pxlogic-agent`。底层采集由 pxlobster 完成，它要在 PATH 里，或由环境变量 `PXLOBSTER` 指定。

## 步骤

1. 确认设备在线：`pxlogic-agent --json --scan`，`ok` 为 true 才继续。
2. 测频率、占空比、脉宽：`pxlogic-agent --json --rate <Hz> --time <ms>`，读 stdout 的 JSON 里的 `active_channels`（`high_us` 是高电平脉宽，单位 µs），不要解析日志。单次跳变加 `--min-edges 0`，否则默认只列边沿数大于 2 的通道。
3. 等触发：加 `--trigger "0=r"`（CH0 上升沿）和 `--timeout <秒>`；超时后会自动发送 `--stop`。
4. 解码优先用命令行，读 JSON 的 `frames`；采集时加 `--save`，之后可用 `--input` 离线分析：

```bash
pxlogic-agent --json --rate 20e6 --time 200 --decode uart --channel 5 --baud 115200
pxlogic-agent --json --input cap.bin --rate 20e6 --decode spi \
  --sclk 2 --mosi 3 --miso 4 --cs 5 --word-bits 16
```

复杂场景用 Python API：

```python
from px_logic import LogicAnalyzer, decode_uart, decode_i2c, decode_spi, load_capture

la = LogicAnalyzer()                     # 16 通道设备写 LogicAnalyzer(channels=16)
rate = la.nearest_rate(20e6)
data = la.capture(rate=rate, duration_ms=200)   # 空数组表示失败，原因在 la.last_error
# 离线：data = load_capture("cap.bin")；16 通道写 channels=16
print(bytes(decode_uart(data, channel=5, baudrate=115200, sample_rate=rate)))
frames = decode_uart(data, channel=6, baudrate=100_000, sample_rate=rate,    # SBUS
                     parity="even", stop_bits=2, invert=True, detail=True)   # 每帧带 time_s 和错误标志
txns = decode_i2c(data, sda_ch=0, scl_ch=1)     # [{"addr", "rw", "data", "acks"}, ...]
pairs = decode_spi(data, sclk_ch=2, mosi_ch=3, miso_ch=4, cs_ch=5, cpol=0, cpha=0,
                   word_bits=16, lsb_first=False)
```

## 规则

- 测频和解码用的 `sample_rate` 必须等于 `nearest_rate()` 取整后的值。
- 采样率至少取被测信号最高频率的 10 倍；解 UART 时至少取波特率的 10 倍。测脉宽时分辨率是一个采样周期，舵机、电调 PWM 至少用 1 MHz。
- 采样点数等于采样率乘时长，32 通道每点 4 字节；单次尽量不超过 5000 万点（约 200 MB）。
- UART 支持 5–9 位数据、无 / 奇 / 偶校验、1 或 2 个停止位和反相（`invert=True`，如 SBUS）；SPI 支持 1–32 位字、MSB 或 LSB 先发，片选释放时重新对齐，`detail=True` 时 `bits` 小于 `word_bits` 的是残字；I2C 只支持 7 位地址。
- 需要每帧的时间，或 UART 的校验、停止位错误时，解码函数加 `detail=True`；SPI、I2C 还要传 `sample_rate` 才有 `time_s`。
- 找不到设备时：WSL 里先确认 Windows 侧执行过 `usbipd attach --wsl --busid <BUSID>`，再用 `lsusb` 查找 `16c0:05dc` 或 `1a86:5237`。
- 需要中止正在进行的采集时，执行 `pxlobster --stop`。

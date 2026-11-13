# pxlogic-agent

[![tests](https://github.com/L-X-Yao/pxlogic-agent/actions/workflows/tests.yml/badge.svg)](https://github.com/L-X-Yao/pxlogic-agent/actions/workflows/tests.yml)

给 AI 代理用的 PX Logic 逻辑分析仪工具。Claude Code 等代理可以用它自己采集信号、测频，以及解码 UART / SPI / I2C，配合调试硬件。它是供代理调用的工具，本身不是代理。

A PX Logic logic-analyzer toolkit for AI agents: capture, frequency measurement and UART/SPI/I2C decoding on top of the pxlobster CLI. It is a tool that agents call, not an agent itself.

> 非官方项目，与 PX Logic 厂商无关。USB 通信、FPGA 位流加载和采集都由第三方开源 CLI [pxlobster](https://github.com/molqzone/pxlobster)（GPLv3）完成。本仓库只通过命令行调用它，不包含、也不分发它的二进制。

## 代理怎么用

1. 按下文“安装”一节装好 pxlobster 和本工具。
2. 在本仓库目录启动 Claude Code，它会自动读取 [CLAUDE.md](https://github.com/L-X-Yao/pxlogic-agent/blob/main/CLAUDE.md) 里的操作约定。
3. 直接描述需求，例如“CH0 接了 PWM，测一下频率和占空比”，或“CH5 是 115200 的串口，抓 200 ms 解出内容”。代理会调用 `pxlogic-agent --json` 或 Python API 完成采集和解码。

在其他项目里用：pip 安装后，把仓库里的 `skills/pxlogic` 目录复制到 `~/.claude/skills/`，Claude Code 在任何项目里遇到相关需求都会加载它。

Codex、Cursor 等代理读取 [AGENTS.md](https://github.com/L-X-Yao/pxlogic-agent/blob/main/AGENTS.md)。人也可以直接使用下文的命令行和 Python API。

## 已验证环境

| 项 | 值 |
|---|---|
| 设备 | PX-Logic U2 channel 32（USB ID `16c0:05dc`） |
| pxlobster | v0.2.4 |
| 系统 | Windows x86_64；WSL2 + usbipd-win |
| 实测 | 400 Hz PWM 在 5 / 20 / 100 / 250 MHz 采样率下都测得 400.0 Hz |
| CI | Ubuntu、Windows × Python 3.9、3.14，跑单元测试，不接硬件 |

16 通道型号没有实测过。pxlobster 对 16 通道设备输出 uint16 采样，使用时要写 `LogicAnalyzer(channels=16)`。

## 安装

### 1. 获取 pxlobster

本仓库在 v0.2.4 上测试。下面的 sha256 取自上游发布页的 `SHA256SUMS.txt`。

**Linux / WSL（x86_64）**

```bash
curl -fLO https://github.com/molqzone/pxlobster/releases/download/v0.2.4/pxlobster-linux-x86_64.tar.gz
echo "9cc6f381eb5f356938f1b636604214cdda5c1ac84b79ace0b4c573e68c05a6c8  pxlobster-linux-x86_64.tar.gz" | sha256sum -c
tar -xzf pxlobster-linux-x86_64.tar.gz
install -m 755 pxlobster-linux-x86_64 pxlobster   # 放进 PATH，或放在 px_logic.py 同目录
sudo apt install libusb-1.0-0
```

**Windows（x86_64）**

下载 [pxlobster-windows-x86_64.zip](https://github.com/molqzone/pxlobster/releases/download/v0.2.4/pxlobster-windows-x86_64.zip)，sha256 为 `25d0022b82ec037e8bf8d2587519b302453c28c29d08ecb95427323c7f13743d`。把解压出的 `pxlobster-windows-x86_64.exe` 改名为 `pxlobster.exe`，放进 PATH，或放在 `px_logic.py` 同目录。

pxlobster.exe 自带 libusb。设备需要 WinUSB 驱动，装过厂商的 PXView 就已经配好。

其他平台（Linux aarch64、macOS aarch64）的构建见上游发布页，未测试。

### 2. USB 权限（原生 Linux）

```bash
sudo curl -fsSL -o /etc/udev/rules.d/99-pxlobster.rules \
  https://raw.githubusercontent.com/molqzone/pxlobster/v0.2.4/packaging/udev/99-pxlobster.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

装完后重新插拔设备。

### 3. WSL：USB 透传

在 Windows PowerShell 里执行，其中 `bind` 需要管理员权限：

```powershell
winget install usbipd
usbipd list                          # 找到 PX Logic 的 BUSID
usbipd bind --busid <BUSID>          # 每台设备只需一次
usbipd attach --wsl --busid <BUSID>  # 每次重插或重启后都要执行
```

WSL 里一般不需要 udev 规则。如果 `pxlobster --scan` 没有输出，先用 `lsusb` 确认能看到 `16c0:05dc` 或 `1a86:5237`。

### 4. 安装本工具

```bash
pip install git+https://github.com/L-X-Yao/pxlogic-agent
```

这会装上 `px_logic` 模块和 `pxlogic-agent` 命令，依赖只有 numpy。不想安装的话，`pip install numpy` 后直接使用仓库里的 `px_logic.py` 也可以。

pxlobster 的查找顺序：环境变量 `PXLOBSTER`、`px_logic.py` 所在目录、PATH。用 pip 安装时，把 pxlobster 放进 PATH，或者设置 `PXLOBSTER`。

### 5. 验证

```bash
pxlogic-agent --scan                   # 列出设备
pxlogic-agent --rate 20e6 --time 200   # 采集 200 ms，打印各活动通道的频率和占空比
```

没有用 pip 安装时，把 `pxlogic-agent` 换成 `python3 px_logic.py`，Windows 上是 `python px_logic.py`。采集时 pxlobster 会自动加载 FPGA 位流，不需要先单独执行 `--prime`。

## 用法

### 命令行（JSON 输出）

`pxlogic-agent` 采集一段数据，汇总每个活动通道；加 `--decode` 时改为解码协议。加 `--json` 后，stdout 只输出一个 JSON 对象，日志全部转到 stderr。成功时退出码为 0，失败时为 1。

```bash
pxlogic-agent --json --rate 20e6 --time 200
```

输出示例（实际为一行）：

```json
{"ok": true, "sample_rate": 20000000, "duration_ms": 200, "samples": 4000000,
 "active_channels": [{"channel": 0, "freq_hz": 400.0, "duty_pct": 25.0, "jitter_pct": 0.01, "edges": 160,
                      "high_us": 625.0, "high_min_us": 624.95, "high_max_us": 625.05}]}
```

`high_us`、`high_min_us`、`high_max_us` 是高电平脉宽的平均、最小、最大值（µs），被采集边界截断的脉冲不计入，分辨率是一个采样周期。测舵机、电调 PWM 看这三项。

失败时输出 `{"ok": false, "error": "..."}`。

| 参数 | 说明 |
|---|---|
| `--scan` | 只列出设备，JSON 里是 `devices` 列表 |
| `--prime` | 只加载 FPGA 位流 |
| `--input FILE` | 不采集，直接分析 `--save` 生成的 `.bin`；不需要设备 |
| `--save FILE` | 采集成功后保存原始 `.bin` |
| `--rate` | 采样率（Hz），取最近的支持值，默认 20e6 |
| `--time` | 采集时长（ms），默认 200；配 `--input` 时忽略 |
| `--channels` | 设备通道数，16 或 32，默认 32 |
| `--trigger` | 触发条件，如 `0=r`（CH0 上升沿），语法同 pxlobster 的 `-t` |
| `--timeout` | 最长等待秒数，超时后自动发送 `--stop`，默认 max(30, 5 × 时长 + 10) |
| `--decode` | 解码 `uart`、`spi` 或 `i2c`，结果在 JSON 的 `frames` 里 |
| `--detail` | 解码结果带采样位置、`time_s` 和错误标志 |
| `--limit` | 最多返回多少条解码结果，0 为不限，默认 10000 |
| `--json` | stdout 只输出 JSON |

解码参数：

| 协议 | 参数 |
|---|---|
| UART | `--channel`、`--baud`、`--data-bits`、`--parity`、`--stop-bits`、`--invert` |
| SPI | `--sclk`、`--mosi`、`--miso`、`--cs`、`--cpol`、`--cpha`、`--word-bits`、`--lsb-first` |
| I2C | `--sda`、`--scl` |

```bash
# 采集 200 ms，解码 CH5 上的 115200 8N1 串口
pxlogic-agent --json --rate 20e6 --time 200 --decode uart --channel 5 --baud 115200

# 保存采集，之后离线解码，并保留每帧时间
pxlogic-agent --json --rate 20e6 --time 200 --save cap.bin
pxlogic-agent --json --input cap.bin --rate 20e6 --decode uart \
  --channel 5 --baud 115200 --detail

# 16 位 SPI，片选释放时重新对齐
pxlogic-agent --json --input cap.bin --rate 20e6 --decode spi \
  --sclk 2 --mosi 3 --miso 4 --cs 5 --word-bits 16
```

解码结果的 JSON 形如：

```json
{"ok": true, "protocol": "uart", "sample_rate": 20000000, "samples": 4000000,
 "duration_ms": 200.0, "count": 2, "truncated": false, "frames": [72, 105]}
```

`--detail` 时 `frames` 里是对象：UART 有 `value`、`start`、`end`、`time_s`、`framing_error`、`parity_error`；SPI 有 `mosi`、`miso`、`bits`、`start`、`end`、`time_s`；I2C 有 `addr`、`rw`、`data`、`acks`、`start`、`end`、`time_s`。`count` 是截断前的总数，`truncated` 表示是否被 `--limit` 截断。

### Python API

```python
from px_logic import LogicAnalyzer, decode_uart, decode_i2c, decode_spi, load_capture

la = LogicAnalyzer()          # 16 通道设备写 LogicAnalyzer(channels=16)
la.devices()                  # [{"usb_id": "16c0:05dc", "label": "PX-Logic U2 channel 32"}]

rate = la.nearest_rate(20e6)  # capture() 会取最近的支持采样率，分析时要用同一个值
data = la.capture(rate=rate, duration_ms=100, save_path="cap.bin")  # 可选：保存原始 .bin
# 离线分析不需要设备：data = load_capture("cap.bin")
# 16 通道文件写 load_capture("cap.bin", channels=16)
m = la.measure(data, channel=0, sample_rate=rate)   # 键同上文 JSON 里 active_channels 的元素
active = la.summary(data, sample_rate=rate)         # 打印并返回各活动通道的 measure() 结果

print(bytes(decode_uart(data, channel=5, baudrate=115200, sample_rate=rate)))

# SBUS：反相、100000 波特、8E2；detail=True 时每帧带采样序号、时间和错误标志
for f in decode_uart(data, channel=6, baudrate=100_000, sample_rate=rate,
                     parity="even", stop_bits=2, invert=True, detail=True):
    print(f"{f['time_s']:.6f} s  0x{f['value']:02X}  framing={f['framing_error']}  parity={f['parity_error']}")

for t in decode_i2c(data, sda_ch=0, scl_ch=1):
    print(f"I2C {t['rw']} 0x{t['addr']:02X}: {[hex(b) for b in t['data']]}")

# 16 位 SPI 字；默认 8 位、MSB 先发
pairs = decode_spi(data, sclk_ch=2, mosi_ch=3, miso_ch=4, cs_ch=5, cpol=0, cpha=0,
                   word_bits=16, lsb_first=False)  # [(mosi, miso), ...]
```

`capture()` 还接受 `mode`（`"buffer"` 或 `"stream"`；loop 模式会一直运行到被停止，`capture()` 直接报错，需要时请直接运行 pxlobster，再用 `--stop` 结束）、`triggers`（如 `"0=r"`，表示 CH0 上升沿）、`timeout_s` 和 `save_path`。等待超时会自动发送 `--stop`。采集失败或超时都返回空数组，原因在 `la.last_error`。`la.prime()` 可提前加载位流，只有设备报告加载成功时才返回 True。`measure_frequency()` 仍然可用，只返回频率。

三个解码器都接受 `detail=True`，每条结果变成字典，多出 `start`、`end`（采样序号）和 `time_s`（秒）。UART 还有 `framing_error`（停止位为低）和 `parity_error`；不加 `detail` 时不报告这两种错误，数据照常返回。SPI 还有 `bits`，小于 `word_bits` 表示片选释放时不满一个字。SPI、I2C 要同时传 `sample_rate` 才有 `time_s`，否则为 None。I2C 的 `end` 是 STOP 或重复起始的位置，采集结束前没等到时为 None。

### 直接调用 pxlobster

```bash
# 20 MHz 采集 200 ms，输出逐采样数据
./pxlobster -o capture.bin --format bin --time 200 --samplerate 20000000 --decode-cross

# CH0 上升沿触发
./pxlobster -o capture.bin --format bin --time 100 --samplerate 100000000 --decode-cross -t "0=r"

# sigrok 格式，可以用 PulseView 打开
./pxlobster -o capture.sr --format sr --time 200 --samplerate 20000000

# 从 stdout 读数据，测 CH0 频率
./pxlobster --stdout --format bin --time 100 --samplerate 20000000 --decode-cross | python3 -c "
import sys, numpy as np
d = np.frombuffer(sys.stdin.buffer.read(), dtype=np.uint32)
r = np.where(np.diff((d & 1).astype(np.int8)) == 1)[0]
print(f'{20e6 / np.mean(np.diff(r)):.1f} Hz' if len(r) > 1 else 'no signal')
"

# 停止正在进行的采集
./pxlobster --stop
```

## 数据格式

- 加 `--decode-cross`：每个采样点是一个小端字。32 通道设备为 uint32，16 通道为 uint16，bit N 对应通道 N。
- 不加：原始 cross-lane DMA 格式。每个条带长度为通道数 × 8 字节，每个通道占一个 u64，u64 的 bit k 是第 k 个采样，一个条带含 64 个采样。
- `--save` 和 `--input` 使用第一行的 packed `.bin`；`--input` 按 `--channels` 选择 uint32 或 uint16。

## 支持的采样率

1 GHz；800、500、400、250、200、125、100、50、25、20、10、5、4、2、1 MHz；500、400、200、100、50、40、20、10、5、2 kHz。

USB 2.0 连接下，高采样率受带宽限制。

## 协议解码器

| 函数 | 支持 | 限制 |
|---|---|---|
| `decode_uart` | 5–9 位数据，无 / 奇 / 偶校验，1 或 2 个停止位，LSB 先发；`invert=True` 用于空闲低电平的线路（如 SBUS）；起始位中点复核，滤掉毛刺 | 不支持 1.5 个停止位；错误标志只在 `detail=True` 时返回 |
| `decode_spi` | 模式 0–3，1–32 位字，MSB 或 LSB 先发，CS 低有效；每次片选释放都重新对齐，从字中间开始的采集只影响第一帧 | 不满 `word_bits` 的残字照常返回；不给 `cs_ch` 时无法重新对齐 |
| `decode_i2c` | 7 位地址，重复起始，记录 ACK/NACK；收到地址字节的段都会列出，包括只发了地址的段 | 不支持 10 位地址 |

单元测试用合成波形和一个模拟的 pxlobster，覆盖解码器和命令行，不需要硬件：

```bash
python3 -m unittest discover -s tests -v
```

## 致谢

- [molqzone/pxlobster](https://github.com/molqzone/pxlobster)：本项目依赖的采集 CLI（GPLv3）
- [PXLogic/PXView](https://github.com/PXLogic/PXView)：厂商的上位机。pxlobster 内置的 FPGA 位流与其中 `PXView/res/` 下的文件相同

## 许可证

本仓库代码采用 MIT 许可证，见 [LICENSE](https://github.com/L-X-Yao/pxlogic-agent/blob/main/LICENSE)。pxlobster 及其内置位流按各自的许可证分发，不包含在本仓库中。

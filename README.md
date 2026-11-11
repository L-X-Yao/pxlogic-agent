# pxlogic-agent

给 AI 代理用的 PX Logic 逻辑分析仪工具。Claude Code 等代理可以用它自己采集信号、测频，以及解码 UART / SPI / I2C，配合调试硬件。它是供代理调用的工具，本身不是代理。

A PX Logic logic-analyzer toolkit for AI agents: capture, frequency measurement and UART/SPI/I2C decoding on top of the pxlobster CLI. It is a tool that agents call, not an agent itself.

> 非官方项目，与 PX Logic 厂商无关。USB 通信、FPGA 位流加载和采集都由第三方开源 CLI [pxlobster](https://github.com/molqzone/pxlobster)（GPLv3）完成。本仓库只通过命令行调用它，不包含、也不分发它的二进制。

## 代理怎么用

1. 按下文“安装”一节装好 pxlobster 和 numpy。
2. 在本仓库目录启动 Claude Code，它会自动读取 [CLAUDE.md](CLAUDE.md) 里的操作约定。
3. 直接描述需求，例如“CH0 接了 PWM，测一下频率和占空比”，或“CH5 是 115200 的串口，抓 200 ms 解出内容”。代理会调用 `px_logic.py` 完成采集和解码。

用其他代理时，把 CLAUDE.md 和本 README 一起交给它。人也可以直接使用下文的 Python API 和 CLI。

## 已验证环境

| 项 | 值 |
|---|---|
| 设备 | PX-Logic U2 channel 32（USB ID `16c0:05dc`） |
| pxlobster | v0.2.4 |
| 系统 | Windows x86_64；WSL2 + usbipd-win |
| 实测 | 400 Hz PWM 在 5 / 20 / 100 / 250 MHz 采样率下都测得 400.0 Hz |

16 通道型号没有实测过。pxlobster 对 16 通道设备输出 uint16 采样，使用时要写 `LogicAnalyzer(channels=16)`。

## 安装

### 1. 获取 pxlobster

本仓库在 v0.2.4 上测试。下面的 sha256 取自上游发布页的 `SHA256SUMS.txt`。

**Linux / WSL（x86_64）**

```bash
curl -fLO https://github.com/molqzone/pxlobster/releases/download/v0.2.4/pxlobster-linux-x86_64.tar.gz
echo "9cc6f381eb5f356938f1b636604214cdda5c1ac84b79ace0b4c573e68c05a6c8  pxlobster-linux-x86_64.tar.gz" | sha256sum -c
tar -xzf pxlobster-linux-x86_64.tar.gz
install -m 755 pxlobster-linux-x86_64 pxlobster   # 放在 px_logic.py 同目录，或放进 PATH
sudo apt install libusb-1.0-0
```

**Windows（x86_64）**

下载 [pxlobster-windows-x86_64.zip](https://github.com/molqzone/pxlobster/releases/download/v0.2.4/pxlobster-windows-x86_64.zip)，sha256 为 `25d0022b82ec037e8bf8d2587519b302453c28c29d08ecb95427323c7f13743d`。把解压出的 `pxlobster-windows-x86_64.exe` 改名为 `pxlobster.exe`，放在 `px_logic.py` 同目录或 PATH 里。

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

### 4. Python 依赖

```bash
pip install numpy
```

### 5. 验证

```bash
./pxlobster --scan        # 列出设备
./pxlobster --prime-fw    # 提前加载 FPGA 位流；可选，采集时 pxlobster 也会自动加载
python3 px_logic.py --rate 20e6 --time 200   # 采集 200 ms，打印各活动通道的频率和占空比
```

Windows 上把 `./pxlobster` 换成 `.\pxlobster.exe`，`python3` 换成 `python`。

## 用法

### Python API

```python
from px_logic import LogicAnalyzer, decode_uart, decode_i2c, decode_spi

la = LogicAnalyzer()          # 16 通道设备写 LogicAnalyzer(channels=16)
la.prime()                    # 可选：提前加载 FPGA 位流

rate = la.nearest_rate(20e6)  # capture() 会取最近的支持采样率，分析时要用同一个值
data = la.capture(rate=rate, duration_ms=100)   # numpy uint32 数组，bit N = 通道 N
la.summary(data, sample_rate=rate)              # 各活动通道的频率、占空比、边沿数
freq = la.measure_frequency(data, channel=0, sample_rate=rate)

print(bytes(decode_uart(data, channel=5, baudrate=115200, sample_rate=rate)))

for t in decode_i2c(data, sda_ch=0, scl_ch=1):
    print(f"I2C {t['rw']} 0x{t['addr']:02X}: {[hex(b) for b in t['data']]}")

pairs = decode_spi(data, sclk_ch=2, mosi_ch=3, miso_ch=4, cs_ch=5, cpol=0, cpha=0)  # [(mosi, miso), ...]
```

`capture()` 还接受 `mode`（`"buffer"` / `"stream"` / `"loop"`）和 `triggers`（如 `"0=r"`，表示 CH0 上升沿）。采集失败时返回空数组，pxlobster 的报错打印到 stderr。

### 直接调用 CLI

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

## 支持的采样率

1 GHz；800、500、400、250、200、125、100、50、25、20、10、5、4、2、1 MHz；500、400、200、100、50、40、20、10、5、2 kHz。

USB 2.0 连接下，高采样率受带宽限制。

## 协议解码器

| 函数 | 支持 | 限制 |
|---|---|---|
| `decode_uart` | 8N1，LSB 先发，空闲高电平 | 不检查停止位，不支持校验位 |
| `decode_spi` | 模式 0–3，MSB 先发，CS 低有效 | 按 8 位分帧 |
| `decode_i2c` | 7 位地址，重复起始，记录 ACK/NACK | 不支持 10 位地址 |

解码器有基于合成波形的单元测试，不需要硬件：

```bash
python3 -m unittest discover -s tests -v
```

## 致谢

- [molqzone/pxlobster](https://github.com/molqzone/pxlobster)：本项目依赖的采集 CLI（GPLv3）
- [PXLogic/PXView](https://github.com/PXLogic/PXView)：厂商的上位机。pxlobster 内置的 FPGA 位流与其中 `PXView/res/` 下的文件相同

## 许可证

本仓库代码采用 MIT 许可证，见 [LICENSE](LICENSE)。pxlobster 及其内置位流按各自的许可证分发，不包含在本仓库中。

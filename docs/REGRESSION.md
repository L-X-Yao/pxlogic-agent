# 设备回归清单

设备接上后按顺序执行。目标是确认 0.1.0 在真实硬件上可用，不是做穷举测试。

## 准备

- PX Logic U2 16 或 32 通道，pxlobster v0.2.4 在 PATH。
- WSL 里先确认 Windows 侧执行过 `usbipd attach --wsl --busid <BUSID>`。
- 所有信号共地，杜邦线短接可靠。
- 16 通道设备所有命令加 `--channels 16`。

## 自动部分

```bash
python3 tools/device_check.py --channels 32 --pwm-channel 0
# 16 通道：
python3 tools/device_check.py --channels 16 --pwm-channel 0
```

脚本检查 scan、5/20/100/250 MHz 的 PWM 频率、save/load 往返。需要测触发超时时，确认 CH0 没有信号后加 `--trigger-test`。

## 手动部分

1. **PWM 脉宽**：舵机或电调 PWM 用 `--decode` 或 `--check`，把 `high_us` 和飞控设定值对照；分辨率是一个采样周期，至少用 1 MHz。
2. **UART**：USB 转串口 TX 接 CH5，115200 8N1 循环发已知字符串：
   ```bash
   pxlogic-agent --json --rate 20e6 --time 200 --decode uart --channel 5 --baud 115200
   ```
   解出的字节必须和发送内容一致。
3. **SBUS**（可选）：接收机接 CH6，100000 8E2 反相：
   ```bash
   pxlogic-agent --json --rate 20e6 --time 200 --decode uart --channel 6 \
     --baud 100000 --parity even --stop-bits 2 --invert
   ```
   首字节应为 `0x0F`。
4. **SPI**（可选）：不接触发直接采，确认第二帧起对齐；16 位器件加 `--word-bits 16`。
5. **识别**：接上已知 PWM/UART 后跑 `--check`，确认候选和参数正确。
6. **离线**：`--save` 保存一段，再用 `--input` 离线重跑，结果应一致。
7. **触发超时**：加 `--trigger "0=r" --timeout 3` 但不给信号，确认超时后自动 `--stop`，下一次采集正常。

## 通过标准

- `tools/device_check.py` 全部 PASS。
- UART 内容一致，SBUS 首字节 0x0F。
- `--check` 对已知信号的协议/参数正确。
- 保存和离线分析结果一致。
- 16 通道没有把 uint16 当成 uint32。

任何一项失败，记录命令、波形/帧和 `la.last_error`，先修再发 0.1.0。

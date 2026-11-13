---
name: pxlogic-diagnose
description: 诊断数字总线故障：先用 pxlogic-agent 识别协议和采集证据，再提出假设、决定下一次采集、对比好板，最后输出带证据的报告。用户描述“I2C 不通、串口没数据、SPI 没响应、PWM 不对、开机就死”等硬件问题时使用。Diagnose digital bus failures with evidence-backed captures from pxlogic-agent.
---

# PX Logic 总线诊断

先拿事实，再做推理。每个结论必须能引用通道、帧或时间；没有证据就继续采集，不要猜。

## 步骤

1. **确认输入**：有设备先 `pxlogic-agent --json --scan`；已有 capture 用 `--input <file> --channels 16|32`。`ok` 为 true 才继续。
2. **识别**：`pxlogic-agent --json --check`，读 `channels`、`buses` 的候选、`confidence` 和证据。不要直接把候选当结论。
3. **提假设**：根据症状和候选列出 1–3 个假设，例如“地址不对”“上拉缺失”“模式错”“波特率错”“片选没拉低”。
4. **决定下一次采集**：只抓能区分假设的通道和时间窗；需要等事件时加 `--trigger` 和 `--timeout`，需要保存时加 `--save`。
5. **确认或否定**：用 `--decode` 指定参数，读 `frames`、`framing_error`、`parity_error`、ACK/NACK、`time_s`。和好板或基线 capture 对比。
6. **报告**：按下面模板输出。最多 3 轮采集，仍无法区分就明确说“证据不足”，并给出下一步。

## 报告模板

```text
症状：
结论：<一句话，带置信度>
证据：
- CHx: <协议/参数>，帧/时间范围，错误计数
- 对比：<好板/基线差异>
假设状态：<确认/否定/未验证>
下一步：<一个动作>
限制：<没测到的部分>
```

## 规则

- 不用“可能/大概”代替证据；不确定就写“未验证”。
- 不把 `--check` 的候选当诊断结果；它只给事实。
- `--check` 的 UART 只试 8N1、8E1、SBUS；SPI 按 8 位字；数据线不唯一时不猜 MOSI/MISO。确认后再用 `--decode` 指定参数。
- 一次只改一个变量：先确认接线/地址/模式，再怀疑器件。
- 不直接操作电源、复位或烧录；需要时让用户执行。
- 不伪造波形或时间；每个数字来自 `measure()`、解码结果或 capture。

## 常见故障的下一步

| 症状 | 先看 | 再抓 |
|---|---|---|
| I2C 无响应 | `buses` 里是否有 I2C、ACK/NACK、地址 | SDA/SCL 上拉、换地址、对比好板 |
| UART 乱码/无数据 | `--check` 的波特率/校验/反相候选 | 已知内容串口、不同波特率、共地 |
| SPI 无响应 | SCLK/CS/数据线、mode | CS 是否拉低、mode 0–3、字长 |
| PWM 不对 | 频率、占空比、`high_us`、抖动 | 设定值对照、不同采样率 |
| 偶发故障 | 是否有长时采集 | 加触发/循环采集，保留故障前后 |

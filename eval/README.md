# eval

离线评测：用合成波形检查 `LogicAnalyzer.identify()` 是否找对协议、参数和总线。不需要硬件。

```bash
python3 eval/runner.py
python3 eval/runner.py --json
python3 eval/runner.py --case spi
```

每个 case 是一段 JSON，包含：

| 字段 | 说明 |
|---|---|
| `name` | 用例名 |
| `kind` | `pwm`、`uart`、`spi`、`i2c`、`idle`、`noise` |
| `sample_rate` | 采样率 |
| 波形参数 | 通道号、波特率、帧、地址等，见 `cases/smoke.json` |
| `expected` | 期望命中的通道/总线，以及置信度、帧数、ACK 等下限 |

跑分器会生成 capture，调用 `identify()`，逐项核对期望，输出通过数和失败原因。退出码为 0 表示全部通过。

当前 `cases/smoke.json` 有 15 个用例：PWM、PWM 抖动、舵机 PWM、UART、UART 帧错误、UART 校验错误、SBUS、I2C 写、I2C NACK、I2C 读、SPI mode 0/3、空闲、噪声。

加用例时优先加**合成故障**：错误波特率、错误 SPI 模式、I2C NACK、PWM 抖动、总线卡死。真实 capture 到位后再补，文件要小并脱敏。

这是作者自用的回归集，不是公开排行榜。

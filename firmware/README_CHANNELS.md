# CC2340R53 通道独立采样固件（XDS110 有线烧录）

项目名为 `phsense_wired`，基于用户原 `bluetooth_data_stream` 普通固件创建。
不需要 MCUboot、不需要 OAD、不需要先刷 v1。这次未修改旧 CCS 工程和 OTA 工程。

## 在 CCS 中烧录

1. `File > Import Project(s)` 选择本 `wired_firmware` 文件夹，导入 `phsense_wired`。
2. 使用 `targetConfigs/CC2340R53.ccxml`，目标是 CC2340R53、XDS110、目标板供电。
   使用你已经验证正常的 USB 调试器和 SWD 连接，目标板独立供电。
3. Build Project，然后 Flash Project；也可在已连接的 CCS 会话中用 Load Program
   加载 `Release/phsense_wired.out`。OUT 自带地址，不要按 OTA 的 `0x6000` 加载。
4. 退出调试，断开目标板上的 SWD 信号跳线，给目标板重新上电，再启动新版 Windows EXE 扫描 Data Stream。
   定制板 INA_GS0 使用 DIO16/SWDIO，A2 使用 DIO17/SWDCK；为保留烧录能力，
   固件只在无调试器会话启动时接管它们。带着 CCS 调试会话启动会使设置命令回复 `ERR,DEBUG`。
5. Windows 读取设备确认后，图表顶部的八个通道按钮和各通道增益下拉框会启用。
   上电和连接后均不自动采样；设置好参数后点击英文界面的 `Start` 开始采样。蓝牙断开会停止采样。
   `Clear` 清空全部图表和导出缓存，但不停止正在进行的采样，也不删除已记录的 CSV。
   图表与 `Export All Data` 均保留自上次 Clear 以来的全部采样点，不再使用60秒窗口。

## 采样规则

- T 输入 0.1–24 秒，最多三位小数；N 为已启用通道的数量。
- 按 CH0 到 CH7 的顺序跳过未启用通道；每个通道先设置 C2:B2:B1 及其 INA225 增益，再等待 T/N 秒，做一次 ADC 采样。
- 每次转换后立即发出该通道 BLE 通知，Windows 仅更新该通道的独立折线图，不等待整轮结束。
- 实际整轮时间为 T 加上 GPIO、ADC 和发送入队等少量开销。FreeRTOS tick 为 1ms；除不尽时分配 1ms 余数，总等待时间仍为 T。
- N=0 时暂停 ADC 采样。设置改变会中断当前等待，从最低已启用通道开始新一轮。
- 每通道独立保存增益状态：0/1/2/3 = 25/50/100/200×，GS0=(state>>1)、GS1=(state&1)。
  板上共用放大器的实际增益随当前通道切换。
- 上电默认全部 8 通道、T=1 秒、各通道 25×。设置保存在 RAM，重新上电恢复默认。
- 保留 B3 高、A4 低、独立运行时 A2 高，以及原有 RF/时钟/ADC 引脚配置。

## 协议 v2（最大通知 15 字节，兼容默认 ATT MTU）

控制特征和通知特征仍为原 Data Stream UUID。

- 查询：ASCII `Q`。
- 原子配置：`C3 02 mask T_lo T_hi g0 g1 g2 g3 g4 g5 g6 g7`，共13字节；mask bit0对应CH0。
- 设置确认：同配置结构，首字节改为 `C4`，末尾增加 running 字节（0=等待Start，1=已开始），共14字节。
- 运行控制：ASCII `R=1` 开始、`R=0` 停止，返回上述14字节状态。配置命令本身不会开始采样。
- 采样通知：`A5 gain seq8 02 channel mv_lo mv_hi time_ms[4] cycle[4]`，共15字节。
  所有多字节字段均小端；`0xffff` 表示 ADC 错误；时间为设备启动后的采样时刻，32位毫秒。
- 非法配置不生效，返回 ASCII `ERR,PERIOD` / `ERR,GAIN` / `ERR,FORMAT`；调试引脚未释放返回 `ERR,DEBUG`。
- 旧 Android 应用的整轮数据解析不支持新格式；本次交付针对新版 Windows EXE。

## 构建与验证

2026-10-14 蓝牙稳定性修正：本项目使用本地 `common/Services/data_stream/data_stream_server.c`，
不再链接 SDK 原文件。通知发送或缓冲分配失败会立即返回，避免占住蓝牙任务；待发送采样回调最多8个。
请求连接间隔改为15–30ms（由电脑决定最终值）。资源不足仍可能丢样，Windows 的序号跳变提示可识别。
`vsenSentSamples`、`vsenDroppedSamples`、`vsenLastSendStatus` 为诊断计数。该修正需实际板验证。

在项目目录运行 `powershell -ExecutionPolicy Bypass -File build.ps1`。
使用 TI Arm LLVM 5.1.1、SimpleLink F3 SDK 9.21.00.36 LTS、SysConfig 1.26.3。
CLI 脚本会显式检查 CCFG CRC 补齐和 HEX 生成是否成功。

固件已在本机编译并完成 CCFG CRC 补齐；Windows 协议及模拟 BLE 的界面/CSV 检查通过。
未在真实定制 PCB 上验证采样间隔、放大器稳定时间和无线数据丢包情况。

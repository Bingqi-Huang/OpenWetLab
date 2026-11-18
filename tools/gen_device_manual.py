"""Generate docs/spec/02_GW001设备手册.md from captured data.

Inputs (all under captures/):
  20261118_GW_001_devices_after_change.json   latest GetDevices result (built-in)
  20261118_GW_001_drivers.json                 latest GetDrivers result (functions, properties, config parameters)
  *.jsonl               raw MQTT recordings (telemetry values, vendor commands, progress messages)
Hand-written test findings live in NOTES below. Re-run after any new capture:
  uv run python tools/gen_device_manual.py
"""
from collections import OrderedDict, defaultdict
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from wetlab.lab.devices import BY_CODE, TAUGHT_POINTS, gateway  # noqa: E402
from wetlab.lab.devices.base import READ  # noqa: E402

CAP = os.path.join(ROOT, "captures")
OUT = os.path.join(ROOT, "docs", "spec", "02_GW001设备手册.md")
POINTS_DIR = os.path.join(ROOT, "island", "GW_001", "vendor")  # newest Position_<date>.json is the current one
POINTS = max(glob.glob(os.path.join(POINTS_DIR, "Position_*.json")), default="")

ORDER = ["PCR002", "T002001", "TYPE_ROBOT_ARM001", "STEERING_PLATFORM001", "T006001", "T009001", "T007001"]

STATUS = {
    "PCR002": "开关门、抬热盖、自定义程序、热盖加热、暂停/继续/停止、只读查询均已实测（2026-11-18）",
    "T002001": "夹紧、定时/连续振荡、停止、急停、温控已实测（2026-11-18）",
    "TYPE_ROBOT_ARM001": "只读查询已实测；没有发过运动指令",
    "STEERING_PLATFORM001": "转动已实测：会报失败却照样转动（2026-11-18）",
    "T006001": "逻辑设备，无传感器；只读查询已实测",
    "T009001": "华大 P8。只从网关看到登记信息和驱动功能；我们没有给它发过任何东西",
    "T007001": "已停用（串口被占用）",
}

TESTED = {  # (device, function) -> short on-site result
    ("PCR002", "OpenDoor"): "4.1～4.3 秒返回，2026-11-17、18 共 5 次成功",
    ("PCR002", "CloseDoor"): "4.1～4.2 秒；必须传 WaitLid=false；有板时关门后约 3.6 秒热盖自动压下",
    ("PCR002", "SetLidMotor"): "Lift 0.2 秒即返回（不等动作完成），热盖 4～6 秒后到位",
    ("PCR002", "ListFiles"): "实测",
    ("PCR002", "GetDoor"): "实测", ("PCR002", "GetPcrStatus"): "实测", ("PCR002", "GetFaults"): "实测",
    ("PCR002", "GetHeaters"): "实测", ("PCR002", "GetFinalHold"): "实测",
    ("PCR002", "GetRunControlState"): "实测", ("PCR002", "ReadAttribute"): "实测；0x03C0 的温度解码是错的（见上）",
    ("PCR002", "GetTemperatures"): "实测；唯一能读到 A/B/C 区实际温度的办法",
    ("PCR002", "GetDeviceInfo"): "2026-11-18 发过 1 次，正好碰上网关停顿，没有收到结果；未再试",
    ("PCR002", "RunProgram"): "实测 5 次：0.22～0.25 秒返回，立即 Running，网关不被占用",
    ("PCR002", "PauseRun"): "0.37 秒返回，2 秒内 Paused，剩余时间冻结",
    ("PCR002", "StartRun"): "从暂停继续：0.36 秒返回，接着倒计时",
    ("PCR002", "StopRun"): "0.53 秒返回，下一帧 Idle",
    ("PCR002", "SetHeater"): "Lid 开/关各 0.2 秒返回；28→104℃ 约 100 秒",
    ("PCR002", "SetPlateCapacity"): "0.1 秒；遥测里的 PlateCapacity 不准（一直是 0）",
    ("PCR002", "SetTubeMode"): "Block，0.1 秒",
    ("T002001", "GetStatus"): "实测", ("T002001", "GetErrors"): "实测",
    ("T002001", "SetClamp"): "夹紧 0.6 秒、松开 1.1 秒", ("T002001", "SetTargetSpeed"): "0.03 秒",
    ("T002001", "StartShakeTimed"): "5 秒振荡，8.6 秒返回（2026-11-17）",
    ("T002001", "StartShake"): "0.07 秒返回，约 6 秒到 300 rpm",
    ("T002001", "StopShake"): "2.3 秒返回，已回零、已松夹",
    ("T002001", "EmergencyStop"): "0.03 秒返回；滑停约 5 秒，不回零、不松夹，还会关温控",
    ("T002001", "Home"): "0.02 秒返回", ("T002001", "SetTemperatureControl"): "0.04 秒",
    ("T002001", "SetTargetTemperature"): "0.04 秒；37℃、53℃ 实测见上",
    ("TYPE_ROBOT_ARM001", "RobotMode"): "实测", ("TYPE_ROBOT_ARM001", "GetPose"): "实测",
    ("TYPE_ROBOT_ARM001", "GetAngle"): "实测", ("TYPE_ROBOT_ARM001", "GripperStatus"): "实测",
    ("TYPE_ROBOT_ARM001", "GripperGetPosition"): "实测",
    ("STEERING_PLATFORM001", "SingleLoop"): "实测 6 次：90→0 全成功；0→90 两次报“无应答”但实际转了",
    ("T006001", "GetStatus"): "实测",
}

NOTES = {
    "PCR002": """\
- **返回格式**：真实结果看 `deviceData.success`。网关外层 `Data.success` 恒为 true。所有返回 0.03～0.3 秒，开关门除外。
- **开门 OpenDoor / 关门 CloseDoor**：驱动每 2 秒查一次门，到位才返回，实测 4.1～4.3 秒，期间网关被占用。关门必须传隐藏参数 `WaitLid=false`，否则驱动再等热盖下压最多 20 秒，没放板时报 `success:false`。
- **热盖自动下压**：板在里面时关门，热盖约 3.6 秒（2026-11-18）到 4～9 秒（2026-11-17）后自动压下；没放板不压。开门前必须先抬热盖。
- **抬热盖 SetLidMotor**：`Action` 缺省是 `Seal`（下压→封膜→抬起），必须显式写 `Lift` 或 `Press`。**它不等动作完成**：0.2 秒就返回，返回里的 `lidMotorState` 常是 `MotorFault`（0xFF）——这是电机刚启动那一刻的读数，不是真故障；遥测里先是 `Moving`，4～6 秒后 `Lifted`。2026-11-17 那次“通信超时”原因不明。所以要靠遥测等 `Lifted`，不能看返回。
- **自定义程序 RunProgram（2026-11-18 实测 5 次）**：0.22～0.25 秒返回，立即 `Running`，网关不被占用（运行中其他查询照常返回）。遥测上报 `PcrCurrentStep`、`PcrTotalSteps`、`PcrStepCurrentCycle`、`PcrStepTotalCycles`、`PcrRemainingSeconds`、`PcrTotalSeconds`、`PcrStepRemainingSeconds`。结束后回到 `Idle`，进度属性归零。
- **温度编码（重要，推翻了 2026-11-17 的结论）**：设备实际按 **原始值 = 温度 × 100** 执行：驱动把 37℃ 写成 0x0E74（3700），实测块温到 37.0℃。官方文档《属性定义 Rev 5》0x03C0 写的“10400～19900 对应 4～99℃”（即 (温度+100)×100）与实机不符；驱动 `ReadAttribute 0x03C0` 的解码也按文档公式，所以把 37℃ 显示成 −63℃。我们的代码只经驱动的 `RunProgram` 下发、不自己编码，不受影响；以后读回程序要按“读数 ÷ 100”解。PCR 里 2026-11-17 读到的“−2/−8/−30/−28℃”实际是 98/92/70/72℃，是别人写的正常程序。热盖和 A/B/C 区加热器目标（0x011A 等）用的确实是 (温度+100)×100，与文档一致。
- **循环次数**：`Cycles=n` 是**一共跑 n 遍**（2026-11-18：两步程序第 2 步设 Cycles=2、NextStep=1，实际 1→2→1→2）。
- **保持时间从到温后才开始计**，升降温另算。驱动返回的 `estimatedSeconds` 只是保持时间之和；遥测 `PcrTotalSeconds` 会在运行中不断更新，更接近实际（例：保持共 40 秒的 95℃→4℃ 程序实际约 200 秒）。
- **实际温度**：遥测里 `LiquidTemperatureA/B/C` 一直是“未读取”，只能用 `GetTemperatures` 读（返回 `liquidA/B/C`、`lid`、`ambient`、`heatsink`）。37℃ 约 8 秒到温，控温 ±0.2℃。
- **升降温速率（2026-11-18，热盖 105℃，Block 模式）**：升温平均约 2℃/秒（17→95℃ 约 39 秒，中段最快约 3.5℃/秒，接近目标时变慢）。降温近似按温度成比例：约 0.029 × 当前温度 ℃/秒（90℃ 时约 2.6℃/秒，50℃ 时约 1.4，20℃ 时约 0.55，8℃ 时约 0.25）；95→4℃ 约 107 秒。**能降到 4℃ 并稳住（3.9～4.2℃）**，但程序结束后不再制冷，温度立即回升（结束保温 FinalHold 当前关闭）。
- **热盖加热**：`SetHeater {Zone:Lid, Enabled:true, TargetCelsius:105}` 0.2 秒返回，热盖 28→104℃ 约 100 秒，加热期间网关不被占用。关闭时不带 `TargetCelsius` 会返回 `success:true` 加一条“回读目标温度与下发值 25 不一致”的警告，无害。
- **程序运行时热盖由 PCR 自己控制**：热盖已开着时下发程序，设备先把它关掉约 10 秒再打开；进入 4℃ 那一步时又把它关掉，之后一直关着，程序结束也不再打开。只开着热盖跑 37℃（我们没开热盖时）它始终关着。程序开始前还有约 20 秒“第 0 步”准备阶段（这次块温先降到约 17℃）。程序会不会等冷热盖到温才开始，没有测到（这次热盖已经是热的）。
- **暂停 / 继续 / 停止**：`PauseRun` 0.37 秒返回、2 秒内 `Paused`、剩余时间冻结；`StartRun` 0.36 秒从冻结处继续；`StopRun` 0.53 秒返回、下一帧 `Idle`。都不占住网关。
- **板容量**：遥测里的 `PlateCapacity` 一直是 0，与真实值不符（`ReadAttribute 0x03C7` 读到 100、50）。不能拿遥测核对它。2026-11-18 测试后已恢复为 100 µL。
- **GetDeviceInfo**：2026-11-18 11:11:38 发过一次，没收到结果；网关当时从 11:11:30 起已经停顿（有人在重启），不能判断是不是它的问题。没有再发。
- **官方状态定义**：仓门 0x014A：0 关、1 开、2 运动中、0xFE 自检中、0xFF 电机异常。热盖 0x014B：读 0 抬起、1 下压、2 封膜中、3 运动中、0xFE 自检中、0xFF 电机异常；写 0 抬起、1 下压、2 封膜。
- **接口锁 0x0204**：0 未锁定；1 被触摸屏锁定；2 被 485 锁定、可被触摸屏解锁。互锁要求为 0。
- **故障码 0x0200**：官方定义 bit0 ADC、bit1～3 A/B/C 区温度、bit4 热盖温度、bit5 散热底座过温、bit6/7 环境温度过高/过低、bit8 水泵、bit9 仓门电机、bit10 热盖电机、bit31 异常断电未完成程序。bit30（0x40000000）不在官方定义里，随门/热盖状态出现和消失，暂不当作故障。
- **封膜**：这台是“自动化热封”PCR，`SetLidMotor` 有 `Seal`（封膜）。我们禁止使用；task.md 的高温循环需不需要封膜、机器里有没有膜，待问。
- 普通热循环仪，没有荧光等测量输出。`GetProperty` 不是内置动作，会报“未知功能”。驱动配置 `AutoStartOnProgramWrite=true`、`MaxPcrSteps=30`、`AllowRawCommand=false`。""",
    "T002001": """\
- **返回格式**：真实结果看 `deviceData.ok`（不是 success）。`GetStatus` 直接返回状态字段，没有 ok。
- **状态值**：ElmState 1=夹紧、2=解锁；ShakeState 0=振荡运行中、1=停止并锁定在零位、**2=停止但未锁定（急停后）、4=加速/减速中**（2、4 是 2026-11-18 新看到的）。TempControllerState 0=空闲（温控关）、2=制热。
- **连续振荡**：`StartShake` 0.07 秒返回，返回里 `shakeState=4`；约 6 秒后遥测到 `ShakeState=0`、300 rpm。`StopShake` 2.3 秒返回（期间网关被占用），返回里已回零、已松夹（`homed:true`、`clampReleased:true`）。
- **停止后的第一帧状态是旧的**：`StopShake` 返回后约 1.3 秒的那一帧仍是 46 rpm、ElmState=1（夹紧），再下一帧才是 0 rpm、ElmState=2。状态是驱动定时去查后再发的，结果返回后的第一帧不一定反映结果。
- **急停 EmergencyStop 不要用于正常停止**：0.03 秒返回，但电机滑停约 5 秒，停下后 ShakeState=2（不回零）、夹爪仍夹紧，并且**把温控也关掉了**。恢复：`Home {lockAfter:true}` → `SetClamp {clamp:false}` → `SetTemperatureControl {on:true}`。
- **定时振荡**（2026-11-17）：`SetClamp {clamp:true}` 0.6 秒到位 → `SetTargetSpeed {rpm:300}` → `StartShakeTimed {seconds:5}`：转速 294～301 rpm，8.6 秒后返回；结束时自动停稳、回零锁定、松开夹紧。`StartShakeTimed` 阻塞到结束，长振荡要用 `StartShake` + `StopShake`。
- **温控（2026-11-18）**：加热块 30→37℃ 约 15 秒，最高冲到 38.3℃，约 70 秒后稳定在 37.0℃；37→53℃ 约 40 秒，最高冲到 57.9℃，约 100 秒后稳定在 53.0℃。以上是加热块温度，孔内液体会慢一些。所以它可以做 37℃、53℃ 的恒温，但每次换温度会冲高 1～5℃。厂商原来设的是 30℃ 制热，测试后已恢复。
- **串口**：配置在 COM11，与灯带 T007001 冲突，所以灯带连不上。
- 驱动配置：速度 200～3000 rpm，温度 −10～120℃，`ReleaseClampOnConnect=true`、`ReleaseClampAfterShake=true`、`HomeOnShakeStop=true`。""",
    "TYPE_ROBOT_ARM001": """\
- **我们只发过只读查询**（2026-11-18：RobotMode、GetPose、GetAngle、GripperStatus、GripperGetPosition，均 0.02～0.3 秒返回，结果在 `deviceData.success`）。当时机械臂停在原位点位 `2120809646631186433`（关节角与点位表一致），`RobotMode` 返回 `StandBy`（遥测里是“使能空闲”），夹爪位置 350（窄开）、`hasObject:false`。`GripperStatus` 有 `hasObject` 字段，搬运后可用来核对夹爪里有没有板。
- **搬运方式**：厂商调度发 `MovMulti`（设备 RPC），网关转成网关级 `ExecuteMotion` 执行。`Tasks` 里是平台点位编号（`PositionId`）加夹爪动作（`ClawSwitch` 0=夹取、1=放开）；`Material` 里有 `Index`（料架槽位，如 "1-3"）、`Angle`、`Speed`（如 "低速"）、`GrabHeight`、`Thickness`。完整报文见下方“实际搬运报文”。
- **搬运接口不在机械臂驱动里，在网关的运动插件里**（MQTT 接口文档 11.9、13 章）：往机械臂设备主题发 `MovMulti`，网关转成自己的 `ExecuteMotion` 执行；也可以直接发到网关主题 `lab/v1/gateway/GW_001/motion/request/{id}`。最终结果在 `lab/v1/gateway/GW_001/motion/response/{id}`（`code` 为 "0" 成功），每步进度在 `motion/progress/{id}`。
- **2026-11-18 现场录制的搬运**（`captures/20261118_arm_141121.jsonl`）：料架底层 → PCR 共 14 步、58～65 秒：转向台转到 0° → 反向夹爪 → 前往料架 → 夹取 → 合并移动到转向台 → 放开 → 抬升 → 转向台转到 90° → 夹爪调整（窄夹换宽夹，夹爪读数 0 → 800）→ 下降 → 夹取 → 合并移动到 PCR → 放开 → 返回。PCR → 料架 16 步。**转向台由运动插件按两端点位的姿态（料架 0、PCR 90）自动使用，不需要我们控制。** 姿态相同的两点之间（料架 → 洗板机）6 步、约 41 秒，不用转向台。
- **`Material.Index` 是料架的层**（2026-11-18 用户确认）：`1-1` 最高层（未示教）、`1-2` 中间层（点位 `2120761428308422658`）、`1-3` 底层（点位 `2118246861233086466`）。厂商调度每次都填料架那一端的编号，去程回程相同。实测“底层点位 + `1-3`”时抓取高度 Z 365～367，与示教点位的 367.0 一致：Index 没有改变抓取位置。
- **网关报成功不代表板被搬了**：2026-11-18 现场两次没夹到板，网关都报“ExecuteMotion success”。夹爪读数（0、350、800、1000）是设定的开合位置，看不出有没有夹到东西。
- **2026-11-18 下午现场重新示教**：转向台交接位 (−194.3, 343.2, 195.6) → (−190.8, 346.2, 194.8)；料架 `1-3` 取放位低了 2 mm；PCR 的 Y 移了 −1.7 mm；新增 P8 的点位。下表来自 2026-11-18 的点位文件。
- **一次搬运的过程**（料架→洗板机，约 41 秒，2026-11-17）：反向夹爪 → 前往料架 → 夹取 → 合并移动 料架→洗板机 → 放开 → 返回 → “V1运动执行完成”。
- **点位表**：接口查不到，由平台通过 SyncLocation 下发给网关。用户已提供网关的点位文件 `docs/Position.json`（7 个点位）。`transfer` 技能暂未开放，等 Material 各字段含义确认并现场陪同完成首次搬运后再开放。
- **观察到的异常**（2026-11-17）：厂商搬运中进入急停、夹爪通信断开；HRIF_MoveL/MoveJ 错误码 20018；断电、上电、重连后恢复。
- **不允许的功能**：MovL、MovJ、JointMovJ、RelMovL、RelMovJ、Mov、MovRunScript、MovCombinedScript（直接坐标运动），以及上下电、夹爪单独开合、速度设置。
- **停止**：搬运进行中 `Stop`、`EmergencyStop` 会排队，不能当急停用。急停只能靠现场物理按钮。
- 连接：TCP 192.168.10.10:10003。夹爪开口配置：窄抓 0 / 窄开 350 / 宽抓 800 / 宽开 1000，力 50。遥测 `OperationalMode` 为“远程”。""",
    "STEERING_PLATFORM001": """\
- **会报失败却照样转动（2026-11-18，用户现场目视确认）。** `SingleLoop` 实测 6 次：90°→0° 3 次都成功，0.7～1.2 秒返回，返回里的 `angle` 是 0 或 1；0°→90° 3 次里 2 次在约 5 秒后报 `success:false`“转向命令未收到电机应答（串口无响应），已放弃执行……确认没有其他程序（如 LK motor tool）占用串口”，**但转向台实际转到了 90°**，遥测在报错后约 3～6 秒变成 "90"；另 1 次成功，返回 `angle:89`。所以这台设备“报失败”不等于“没动”。
- **返回格式**：结果在 `deviceData.success`；失败时 `deviceData.error` 是中文原因。失败后遥测 `WorkingStatus=2`、`error` 为报错文本；它没有只读功能，下一条成功的指令或网关重启后才清掉。
- **转动很快**：90° 约 1～2 秒。返回之后的第一帧遥测有时仍是旧角度，第二帧才对。
- `Angles` 是字符串（"0"、"90"）。遥测只有 `Angles` 和 `States`（"Enabled"）。
- 2026-11-17 17:42～17:43 看到角度在 0 和 90 之间变化而没有发给它的 MQTT 指令——可能是厂商搬运时运动调度在用它（见机械臂一节）。设备状态以遥测为准。
- 2026-11-18 11:22、11:35 网关上短暂出现过一台编码为 `4`、名为“转向台”的设备（只有上线消息和一帧 `WorkingStatus`），之后的设备清单里没有它，应是现场改配置时的临时登记。
- 互锁：机械臂运动时禁止旋转；角度只允许 0/90/180/270。""",
    "T006001": """\
- **逻辑占位设备，不通信、没有传感器**，状态永远是“正常”。哪个槽位有板，系统不知道，必须由我们自己记录（或人工告知）。
- **返回格式**：`GetStatus` 0.01 秒返回，`deviceData` 只有 `rackState`、`rackStateText`、`errorCode`、`errorText`，**没有 success 或 ok 字段**；只能看外层 `Data.success`。
- 料架槽位用 `Material.Index`（如 "1-3"，推测为行-列）配合机械臂点位表示。""",
    "T009001": """\
- **2026-11-18 01:36 登记到 GW_001**（此前驱动只装在 GW_005 上）。设备名“MGIP8移液工作站”，驱动“MGI PrepALL P8 工作站驱动 v1.0.0”，经 http 访问 `http://192.168.1.21:10090`（P8 自己的 IController 接口）。
- 驱动对外 42 个功能（见下表），包括跑脚本、暂停/继续/停止、温控、振荡、PCR 方法、门锁、灯、拍照、切换仿真、透传任意接口（`ApiCall`）。我们的允许清单里一个都没有：摸底（04 第 7.1 节）之前一律不发。
- 遥测每 3 秒一帧，有 `Status`/`StatusCode`（200=空闲）、`IsIdle`、`IsRunning`、`IsPaused`、`IsBlocked`、`IsError`、`IsInitialized`、`Temperature`、`TempHeating`、`Shaking`、`LightOn`、`DoorOpen`、`LastError`。
- 2026-11-18 上午现场在调试 P8，期间 GW_001 多次整体停顿 10～155 秒（心跳和所有设备遥测一起停）。11:41 之后，它的两项配置被改成 `PollTemperature=false`、`DefaultRunWait=false`，此后网关稳定。推测原因：驱动每 2 秒经网络查询 P8、单次最多等 15 秒，P8 不回应时拖住了整个网关。未证实。""",
    "T007001": """\
- 在 GW_001 上已停用（IsEnabled=false）。驱动报错“串口 COM11 被其他程序占用”，因为振荡模组也配置在 COM11。
- 厂商调度会给它发 `DeviceController {color: "red"}` 之类的指令，网关返回“ERROR: device not found”，外层仍报成功。""",
}


def load_json(name):
    with open(os.path.join(CAP, name), encoding="utf-8") as f:
        return json.load(f)


def telemetry_values():
    vals = defaultdict(lambda: OrderedDict())
    vendor_requests, progress = [], defaultdict(list)
    for path in sorted(glob.glob(os.path.join(CAP, "*.jsonl"))):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    m = json.loads(line)
                    payload = json.loads(m["payload"])
                except (ValueError, KeyError, TypeError):
                    continue  # not an MQTT recording line (e.g. our own command logs in captures/)
                if not isinstance(payload, dict):
                    continue
                if payload.get("GatewayCode") != "GW_001":
                    continue
                t = m["topic"]
                if t.startswith("lab/v1/devices/") and t.endswith("/telemetry"):
                    dev = t.split("/")[3]
                    for kv in payload["Body"].get("Body") or []:
                        v = json.dumps(kv["Value"], ensure_ascii=False)
                        lst = vals[dev].setdefault(kv["Code"], [])
                        if v not in lst:
                            lst.append(v)
                elif "/rpc/request/" in t and "/WL-" not in t and "TYPE_ROBOT_ARM001" in t:
                    vendor_requests.append((m["tst"], payload))
                elif "/motion/progress/" in t:
                    b = payload["Body"]
                    if b["msg"] not in progress[b["requestId"]]:
                        progress[b["requestId"]].append(b["msg"])
    return vals, vendor_requests, progress


def esc(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


def fmt_param(p):
    bits = [p.get("DataType") or "?"]
    if p.get("Required"):
        bits.append("必填")
    if p.get("DefaultValue") not in (None, ""):
        bits.append(f"默认 {p['DefaultValue']}")
    if p.get("Unit"):
        bits.append(p["Unit"])
    if p.get("ReferenceValue"):
        bits.append(f"参考 {p['ReferenceValue']}")
    desc = f"：{p['Description']}" if p.get("Description") and p["Description"] != p["Name"] else ""
    return f"`{p['Name']}` ({', '.join(bits)}){desc}"


def our_class(code, fn):
    dev = BY_CODE.get(code)
    if dev is None:
        return "不允许"
    act = dev.spec.ACTIONS.get(fn) or gateway.spec.ACTIONS.get(fn)
    if act is None:
        return "不允许"
    return "只读" if act.kind == READ else "动作"


def blocking_text(code, fn):
    dev = BY_CODE.get(code)
    act = dev.spec.ACTIONS.get(fn) if dev else None
    if act is None:
        return ""
    if act.until_done:
        return "直到结束"
    return f"约 {act.seconds} 秒" if act.seconds else ""


def main():
    devices = load_json("20261118_GW_001_devices_after_change.json")
    drivers = {d["Name"]: d for d in load_json("20261118_GW_001_drivers.json")}
    vals, vendor_requests, progress = telemetry_values()
    by_code = {}
    for d in devices:
        cfg = {c["Key"]: c["Value"] for c in d.get("Configs", [])}
        by_code[cfg.get("Code")] = (d, cfg)

    out = []
    w = out.append
    w("# GW_001 设备手册\n")
    w("> 由 `tools/gen_device_manual.py` 根据 `captures/` 里的实采数据生成，手写的实测结论在脚本的 NOTES 里。"
      "数据采集于 2026-11-17、2026-11-18。重新采集后运行 `python3 tools/gen_device_manual.py` 更新本文。\n")
    w("功能分类“只读/动作/不允许”来自 `wetlab/lab/devices/*/spec.py` 的允许清单：不在清单里的功能，我们的系统一律不发。\n")
    w("## 总览\n")
    w("| 设备编码 | 名称 | 驱动 | 通信 | 当前状态 | 我们的测试情况 |")
    w("|---|---|---|---|---|---|")
    for code in ORDER:
        d, cfg = by_code[code]
        drv = drivers.get(d["DriverName"], {})
        conn = cfg.get("PortName") or cfg.get("BaseUrl") or (f"{cfg.get('IP')}:{cfg.get('Port')}" if cfg.get("IP") else "无")
        enabled = "启用" if d.get("IsEnabled") else "停用"
        w(f"| `{code}` | {d['Name']} | {d['DriverName']} v{drv.get('Version', '?')} | "
          f"{drv.get('Protocol', '?')} / {conn} | {enabled} | {STATUS.get(code, '')} |")
    w("")

    for code in ORDER:
        d, cfg = by_code[code]
        drv = drivers.get(d["DriverName"], {})
        w(f"## {code}：{d['Name']}\n")
        w(f"- 驱动：{drv.get('Name')} v{drv.get('Version')}（`{drv.get('Code')}`，{drv.get('DriverFile')}）")
        w(f"- 厂商/型号：{drv.get('Manufacturer')} / {drv.get('DeviceBrandName')} {drv.get('DeviceModelName')}")
        w(f"- 协议：{drv.get('Protocol')}（{drv.get('ProtocolName')}）")
        w(f"- 驱动说明：{esc(drv.get('Description', ''))}")
        w(f"- 网关设备 ID：{d.get('Id')}；平台设备 ID：{cfg.get('PlatformDeviceId')}；"
          f"启用：{d.get('IsEnabled')}；仿真：{d.get('IsSimulation')}\n")
        w("### 实测结论与注意事项\n")
        w(NOTES.get(code, "（暂无）") + "\n")

        w("### 当前配置\n")
        labels = {c["Key"]: c.get("Label", "") for c in drv.get("ConfigParameters", [])}
        w("| 配置项 | 含义 | 当前值 |")
        w("|---|---|---|")
        for k, v in cfg.items():
            if k in ("Code", "PlatformDeviceId"):
                continue
            w(f"| `{k}` | {esc(labels.get(k, ''))} | {esc(v)} |")
        w("")

        fns = drv.get("Functions", [])
        w(f"### 功能清单（共 {len(fns)} 个）\n")
        w("| 功能 | 说明 | 参数 | 我们的分类 | 阻塞网关 | 实测 |")
        w("|---|---|---|---|---|---|")
        for f in fns:
            params = "<br>".join(esc(fmt_param(p)) for p in f.get("Parameters", [])) or "—"
            blk = blocking_text(code, f["Name"])
            w(f"| `{f['Name']}` | {esc(f.get('Description', ''))} | {params} | {our_class(code, f['Name'])} | "
              f"{blk} | {TESTED.get((code, f['Name']), '')} |")
        w("")

        props = drv.get("Properties", [])
        seen = vals.get(code, {})
        w("### 状态属性\n")
        w("驱动声明的属性，以及遥测中实际出现过的取值（最多列 6 个）。\n")
        w("| 属性 | 驱动说明 | 实际出现过的值 |")
        w("|---|---|---|")
        listed = set()
        for p in props:
            name = p.get("Name")
            lower = name[:1].lower() + name[1:]
            actual = lower if name not in seen and lower in seen else name
            listed.add(name)
            listed.add(actual)
            obs = seen.get(actual, [])
            w(f"| `{actual}` | {esc(p.get('Description', ''))} | {esc(' / '.join(x[:50] for x in obs[:6]))} |")
        for name, obs in seen.items():
            if name in listed or (name[:1].upper() + name[1:]) in listed:
                continue
            w(f"| `{name}` | （驱动未声明，遥测中出现） | {esc(' / '.join(x[:50] for x in obs[:6]))} |")
        w("")

        if code == "TYPE_ROBOT_ARM001" and os.path.exists(POINTS):
            with open(POINTS, encoding="utf-8-sig") as f:
                pts = json.load(f)
            w(f"### 示教点位表（`island/GW_001/vendor/{os.path.basename(POINTS)}`，共 {len(pts)} 个）\n")
            w("这是网关本地的点位文件，和平台通过 SyncLocation 下发的格式相同。每个点位是一条轨迹："
              "“前往”按 Seq 顺序走到最后一个点（取放位），“返回”倒序走回第一个点；"
              "两点之间搬运时网关会把“返回 A”和“前往 B”合并执行。J=关节坐标（度），T=末端坐标（mm, mm, mm, 度, 度, 度）。\n")
            w("| 点位编号 | 设备 | 姿态 | 轨迹点数 | 取放位末端坐标 X, Y, Z, Rx, Ry, Rz | 我们的用法 |")
            w("|---|---|---|---|---|---|")
            for p in pts:
                traj = json.loads(p["positionData"])
                final = ", ".join(traj[-1]["Pos"]) if traj[-1]["Type"] == "T" else "（关节点）" + ", ".join(traj[-1]["Pos"])
                pos = TAUGHT_POINTS.get(str(p["id"]))
                use = pos.label if pos else {"洗板机": "不使用（洗板机属于 GW_005）", "MGIP8移液工作站": "暂不使用（P8 摸底之前）"}.get(p["deviceName"], "不使用")
                w(f"| `{p['id']}` | {p['deviceName']} | {p['posture']} | {len(traj)} | {final} | {use} |")
            w("")
            w("姿态：PCR 仪是 90，其余是 0。结合夹爪配置（窄抓/窄开对应 0/180，宽抓/宽开对应 90/270），"
              "推测 PCR 处要换成宽方向夹板；料架和 PCR 之间是否需要先经转向台转向，待示教人员确认。\n")

        if code == "TYPE_ROBOT_ARM001" and vendor_requests:
            w("### 实际搬运报文（厂商调度，2026-11-17 录制）\n")
            ts, req = next(((t, r) for t, r in vendor_requests if r.get("Type") == "MovMulti"), vendor_requests[0])
            w(f"录制时间 {ts[11:19]}，发往 `lab/v1/devices/TYPE_ROBOT_ARM001/rpc/request/{req['Body']['requestId']}`：\n")
            w("```json\n" + json.dumps(req, ensure_ascii=False, indent=1) + "\n```\n")
            msgs = progress.get(req["Body"]["requestId"], [])
            if msgs:
                w("网关在 `lab/v1/gateway/GW_001/motion/progress/<requestId>` 上发出的进度：\n")
                for msg in msgs:
                    w(f"- {msg}")
                w("")
            others = sorted({r.get("Type") for _, r in vendor_requests})
            w(f"录到的厂商发给机械臂的指令类型：{', '.join(others)}。\n")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print("wrote", OUT, sum(len(x) for x in out), "chars")


if __name__ == "__main__":
    main()

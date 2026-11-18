# LightsOut.IoT MQTT 第三方接口对接文档

> 文档版本：V1.0  
> 适用基线：当前 `LightsOut.交付源码/Src` 实现  
> 默认协议根：`lab/v1`  
> 适用对象：实验室平台、调度系统、上位系统及其他需要与 LightsOut 网关对接的项目

本文按当前源码实际行为整理，供第三方项目直接对接。旧资料中的 `v1/devices/me/...` 等 ThingsBoard 风格 Topic 不属于本文基线。

## 1. 阅读前必看

### 1.1 实现状态标记

| 标记 | 含义 |
| --- | --- |
| 已实现 | 当前网关已有订阅、发布及业务处理代码，可据此联调 |
| 部分实现 | 有入口或协议，但功能不完整，不能按名称推断行为已完成 |
| 预留 | 有 Topic 或平台协议定义，但当前网关未找到对应发布实现 |
| 待补齐 | 当前源码存在引用缺失或版本不一致，暂不能直接联调 |

### 1.2 关键注意事项

- ACK 只表示网关已接收请求，**不表示设备动作执行成功**。
- 普通设备调用的最终结果 `Data.success=true` 也**不一定代表驱动业务成功**，还需检查驱动原始返回内容。
- 所有下行指令都应使用非 retained 消息，避免重连后重复执行。
- 驱动列表和能力虽使用 retained，但所有驱动能力共用一个 Topic，晚订阅者通常只能看到最后一个驱动的 retained 报文。
- 当前设备状态由网关主动发布，代码未配置 Broker 遗嘱；异常断网时不能依赖一定收到 `offline`。
- 示例中的编码、点位、驱动名称、版本及运动参数均需替换为现场真实值，运动示例不能直接作为现场轨迹执行。

## 2. 通信模型与连接

### 2.1 角色

| 角色 | 职责 |
| --- | --- |
| 第三方平台/调度系统 | 下发 RPC、设备注册、运动及点位同步；消费网关上行消息 |
| LightsOut 网关 | 管理驱动与设备会话，路由指令，发布心跳、遥测和回执 |
| MQTT Broker | 负责消息传输与 Topic 路由 |
| 设备驱动 | 执行具体设备能力，产生属性值与业务结果 |

本文中的“上行”指网关到第三方，“下行”指第三方到网关。

### 2.2 网关连接配置

配置位于 `LightsOut.交付源码/Src/LightsOut.IoT.Gateway/appsettings.json`：

```json
{
  "MQTT": {
    "Host": "<broker-host>",
    "Port": 1883,
    "Username": "<username>",
    "Password": "<password>",
    "ClientId": "gateway-001",
    "TopicRoot": "lab/v1",
    "GatewayCode": "GW000001"
  }
}
```

| 配置 | 说明 |
| --- | --- |
| `Host` / `Port` | Broker TCP 地址与端口 |
| `Username` / `Password` | Broker 认证信息 |
| `ClientId` | 网关 MQTT 客户端 ID，应保证唯一 |
| `TopicRoot` | Topic 根，默认 `lab/v1` |
| `GatewayCode` | 网关业务编码，用于 Topic 和报文 |

当前客户端使用 TCP 连接，连接和单次发布超时为 3 秒。断线后自动重连并重新订阅。第三方客户端也应在重连后恢复自己的订阅。

## 3. 通用协议约定

### 3.1 Topic 参数

| 占位符 | 说明 |
| --- | --- |
| `{root}` | `MQTT:TopicRoot`，本文示例使用 `lab/v1` |
| `{gatewayCode}` | 网关编码，例如 `GW000001` |
| `{deviceCode}` | 设备业务编码，例如 `DV000151` |
| `{requestId}` | 一次请求的唯一标识，用于关联 ACK、进度和最终结果 |

建议：

- `requestId` 在平台侧全局唯一，同一请求的重试保持原值。
- 不要在编码和请求 ID 中使用 `/`、空格或 MQTT 通配符。
- 下行请求不要使用 retained 消息，避免网关重启后重复执行旧指令。

### 3.2 通用外层包 `MqttBasePacket`

网关心跳、设备遥测、设备状态、设备 RPC、设备注册和运动接口使用该外层结构：

```json
{
  "MsgId": "msg-202611170001",
  "Timestamp": 1787480054748,
  "GatewayCode": "GW000001",
  "DeviceCode": "DV000151",
  "Type": "telemetry",
  "Body": {}
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `MsgId` | string | 是 | 消息 ID；平台可将其作为幂等键 |
| `Timestamp` | long | 是 | Unix 毫秒时间戳 |
| `GatewayCode` | string/null | 是 | 网关编码 |
| `DeviceCode` | string/null | 条件必填 | 设备消息填设备编码；网关级消息为 `null` |
| `Type` | string | 是 | 消息类型或业务动作 |
| `Body` | object/array | 是 | 业务内容 |

字段名存在 PascalCase 与 camelCase 两种风格。虽然平台部分解析逻辑可能兼容别名，**发送到网关时仍应严格使用本文示例的字段名和大小写**，不要依赖大小写兼容行为。

### 3.3 通用校验规则

- 设备 RPC：Topic 中的 `deviceCode` 必须与外层 `DeviceCode` 一致。
- 设备 RPC：Topic 中的 `requestId` 必须与 `Body.requestId` 一致。
- 设备注册：外层 `GatewayCode` 必须等于当前网关编码，外层、Body 和注册参数中的设备编码必须一致。
- 网关运动：外层 `GatewayCode`、Topic/Body 的 `requestId`、外层 `Type` 与 `Body.action` 必须一致。
- JSON 无法解析或校验失败时，部分接口只记录日志，不返回失败回执；调用方必须设置超时。
- `MsgId` 用于消息幂等，`requestId` 用于请求关联，两者不要混用。

## 4. Topic 总览

下表中的 QoS 是网关实际订阅或发布使用的 QoS。下行消息的 retain 为第三方发布建议，上行消息的 retain 为网关实际行为。

| 功能 | 方向 | Topic | QoS | Retain | 状态 |
| --- | --- | --- | ---: | --- | --- |
| 网关心跳 | 上行 | `{root}/gateway/{gatewayCode}/telemetry` | 0 | false | 已实现 |
| 驱动列表 | 上行 | `{root}/gateway/{gatewayCode}/driver/list` | 0 | true | 已实现 |
| 驱动能力 | 上行 | `{root}/gateway/{gatewayCode}/driver/capability` | 0 | true | 已实现 |
| 驱动安装 | 下行 | `{root}/gateway/{gatewayCode}/driver/install` | 1 | false | 部分实现 |
| 设备注册 | 下行 | `{root}/gateway/{gatewayCode}/device/install` | 1 | false | 已实现 |
| 设备注册 ACK | 上行 | `{root}/gateway/{gatewayCode}/device/install/response/{requestId}` | 1 | false | 已实现 |
| 设备注册结果 | 上行 | `{root}/gateway/{gatewayCode}/device/install/result/{requestId}` | 1 | false | 已实现 |
| 网关运动请求 | 下行 | `{root}/gateway/{gatewayCode}/motion/request/{requestId}` | 1 | false | 已实现 |
| 网关运动 ACK/结果 | 上行 | `{root}/gateway/{gatewayCode}/motion/response/{requestId}` | 1 | false | 已实现 |
| 网关运动进度 | 上行 | `{root}/gateway/{gatewayCode}/motion/progress/{requestId}` | 0 | false | 已实现 |
| 点位同步 | 下行 | `{root}/gateway/{gatewayCode}/location` | 1 | false | 已实现 |
| 设备遥测 | 上行 | `{root}/devices/{deviceCode}/telemetry` | 0 | false | 已实现 |
| 设备状态 | 上行 | `{root}/devices/{deviceCode}/status` | 1 | false | 已实现 |
| 设备事件 | 上行 | `{root}/devices/{deviceCode}/event` | 1 | false | 协议预留 |
| 设备属性 | 上行 | `{root}/devices/{deviceCode}/attributes` | 1 | false | 协议预留 |
| 设备 RPC 请求 | 下行 | `{root}/devices/{deviceCode}/rpc/request/{requestId}` | 1 | false | 已实现 |
| 设备 RPC ACK/结果 | 上行 | `{root}/devices/{deviceCode}/rpc/response/{requestId}` | 1 | false | 已实现 |
| 设备 RPC 进度 | 上行 | `{root}/devices/{deviceCode}/rpc/progress/{requestId}` | 0 | false | 已实现 |
| 下机文件上报 | 上行 | 设计为 `{root}/gateway/{gatewayCode}/datafile/report` | 1 | false | 待补齐 |

注意：设备 Topic 中没有 `gatewayCode`，平台应通过报文外层 `GatewayCode` 和设备档案确定设备归属。

## 5. 网关心跳

### 5.1 接口定义

```text
方向：网关 -> 第三方平台
Topic：lab/v1/gateway/{gatewayCode}/telemetry
QoS：0
Retain：false
外层 Type：heartbeat
```

网关启动后每 3 秒发布一次。当前配置中的 `Gateway:HeartbeatIntervalSeconds` 不改变该代码内的 3 秒定时器。

### 5.2 Body 字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `GatewayName` | string | 当前实现填网关编码 |
| `ClientId` | string | MQTT 客户端 ID |
| `DeviceCount` | int | 当前运行时已加载设备会话数 |
| `OnlineCount` | int | 当前已连接设备会话数 |

### 5.3 示例

```json
{
  "MsgId": "msg-gateway-heartbeat-001",
  "Timestamp": 1787480054748,
  "GatewayCode": "GW000001",
  "DeviceCode": null,
  "Type": "heartbeat",
  "Body": {
    "GatewayName": "GW000001",
    "ClientId": "gateway-001",
    "DeviceCount": 8,
    "OnlineCount": 6
  }
}
```

平台可用该消息更新网关在线状态和最后心跳时间。不要仅根据 `DeviceCount` 推断数据库中的设备总数。

## 6. 驱动列表上报

### 6.1 接口定义

```text
方向：网关 -> 第三方平台
Topic：lab/v1/gateway/{gatewayCode}/driver/list
QoS：0
Retain：true
```

驱动列表使用独立 camelCase 报文，不套 `MqttBasePacket`。

### 6.2 顶层字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `msgId` | string | 是 | 消息 ID |
| `timestamp` | long | 是 | 毫秒时间戳 |
| `gatewayCode` | string | 是 | 网关编码 |
| `type` | string | 是 | 固定 `driver_list` |
| `drivers` | array | 是 | 可用驱动列表，可为空数组 |

### 6.3 drivers[] 字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `driverName` | string | 驱动名称 |
| `driverVersion` | string | 驱动版本 |
| `driverFile` | string | 驱动文件名 |
| `deviceTypeName` | string | 设备类型名称 |
| `deviceBrandName` | string | 品牌名称 |
| `deviceModelName` | string | 型号名称 |
| `protocol` | string | 驱动主协议标识 |

### 6.4 示例

```json
{
  "msgId": "gateway-driver-list-1787480054748-1",
  "timestamp": 1787480054748,
  "gatewayCode": "GW000001",
  "type": "driver_list",
  "drivers": [
    {
      "driverName": "CodeScan 条码扫描器驱动",
      "driverVersion": "1.0.1",
      "driverFile": "Driver.Scanner.CodeScan.dll",
      "deviceTypeName": "条码扫描器",
      "deviceBrandName": "CodeScan",
      "deviceModelName": "CS-2D",
      "protocol": "mqtt"
    }
  ]
}
```

## 7. 驱动能力上报

### 7.1 接口定义

```text
方向：网关 -> 第三方平台
Topic：lab/v1/gateway/{gatewayCode}/driver/capability
QoS：0
Retain：true
```

每个驱动发送一条独立能力报文。启动时先发送驱动列表，约 5 秒后逐个发送能力报文；重扫驱动并加载到新驱动时也会重新上报。

**Retain 注意事项：**所有驱动能力共用同一个 Topic。Broker 通常只为该 Topic 保存最后一条 retained 消息，因此后订阅者不能依靠 retained 消息恢复全部驱动能力。平台应在网关启动或重扫期间实时订阅，并通过本地持久化保存完整能力集合。

### 7.2 顶层字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `msgId` | string | 消息 ID |
| `timestamp` | long | 毫秒时间戳 |
| `gatewayCode` | string | 网关编码 |
| `type` | string | 固定 `driver_capability` |
| `driverName` / `driverVersion` | string | 驱动名称和版本 |
| `deviceTypeName` / `deviceBrandName` / `deviceModelName` | string | 适配设备信息 |
| `functionList` | array | 可调用功能列表 |
| `propertyList` | array | 设备属性定义 |
| `protocolList` | array | 协议及连接配置定义 |
| `positionList` | array | 位置定义；当前映射器固定返回空数组 |

### 7.3 functionList[]

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `code` | string | 功能编码，当前取驱动功能名 |
| `name` | string | 功能显示名称 |
| `type` | int | 当前固定 `0`，表示设备能力 |
| `commandAction` | string | 下行 RPC 的动作名，当前等于 `code` |
| `inParams` | array | 入参定义，无参数时为 `[]` |
| `timeoutSeconds` | int | 建议执行超时，来自驱动超时配置，缺省 30 秒 |
| `needDuration` | boolean | 当前固定 `false` |
| `description` | string | 功能描述 |
| `sort` | int | 显示顺序 |

`inParams[]`：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `code` | string | 参数编码，RPC 参数对象中的键 |
| `name` | string | 参数显示名 |
| `dataType` | string | 当前映射为 `int`、`float`、`boolean` 或 `string` |
| `referenceValue` | string | 参考值或取值说明 |
| `description` | string | 参数描述 |
| `required` | boolean | 是否必填 |
| `options` | array | 可选项；当前驱动映射器通常返回 `[]` |

选项结构为 `{ "label": "显示文本", "value": "实际值" }`。下发时必须使用 `value`，不能使用 `label`。

### 7.4 propertyList[]

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `code` | string | 属性编码，对应遥测 `Body.Body[].Code` |
| `name` | string | 属性显示名 |
| `propType` | number/string | 当前 `WorkingStatus` 为 `1`，其他驱动属性通常为 `string` |
| `propValue` | any | 默认值或参考值 |
| `unit` | string | 单位 |
| `description` | string | 描述 |
| `readOnly` | boolean | 是否只读 |
| `sort` | int | 显示顺序 |
| `options` | array | 可选项；`WorkingStatus` 会带状态选项 |

`WorkingStatus` 常用值：`0` 空闲、`1` 忙、`2` 故障、`3` 维护、`4` 暂停、`5` 调试中。

### 7.5 protocolList[]

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | string | 协议显示名 |
| `isDefault` | boolean | 当前固定 `true` |
| `configList` | array | 连接参数默认值 |
| `description` | string | 描述 |
| `sort` | int | 显示顺序 |

`configList[]` 包含 `name` 和 `value`，分别表示参数显示名与默认值。设备注册时，`connectConfig[].config` 的参数键可按这里的名称或本地驱动配置键进行映射。

### 7.6 positionList[]

当前代码模型包含 `name`、`availablePostures`、`occupiedDuringTransfer`、`coordinate`、`heightLimit`、`description`、`slotIndex`、`capacity`、`sort`，但当前映射器始终返回空数组，第三方不应依赖该列表获取现场点位。运动点位应使用点位同步接口。

### 7.7 示例

```json
{
  "msgId": "gateway-driver-capability-1787480054748-2",
  "timestamp": 1787480054748,
  "gatewayCode": "GW000001",
  "type": "driver_capability",
  "driverName": "CodeScan 条码扫描器驱动",
  "driverVersion": "1.0.1",
  "deviceTypeName": "条码扫描器",
  "deviceBrandName": "CodeScan",
  "deviceModelName": "CS-2D",
  "functionList": [
    {
      "code": "ScanBarcode",
      "name": "扫码",
      "type": 0,
      "commandAction": "ScanBarcode",
      "inParams": [
        {
          "code": "timeout",
          "name": "超时时间",
          "dataType": "int",
          "referenceValue": "30",
          "description": "扫码超时时间",
          "required": true,
          "options": []
        }
      ],
      "timeoutSeconds": 30,
      "needDuration": false,
      "description": "扫描条码",
      "sort": 1
    }
  ],
  "propertyList": [
    {
      "code": "WorkingStatus",
      "name": "工作状态",
      "propValue": 0,
      "propType": 1,
      "unit": "",
      "description": "工作状态",
      "readOnly": true,
      "sort": 1,
      "options": [
        { "label": "空闲", "value": "0" },
        { "label": "忙", "value": "1" },
        { "label": "故障", "value": "2" }
      ]
    }
  ],
  "protocolList": [
    {
      "name": "MQTT",
      "isDefault": true,
      "configList": [
        { "name": "MQTT地址", "value": "127.0.0.1" },
        { "name": "MQTT端口", "value": "1883" }
      ],
      "description": "",
      "sort": 1
    }
  ],
  "positionList": []
}
```

## 8. 设备遥测

### 8.1 接口定义

```text
方向：网关 -> 第三方平台
Topic：lab/v1/devices/{deviceCode}/telemetry
QoS：0
Retain：false
外层 Type：telemetry
```

网关每 3 秒批量发布一次有变化的属性。同一设备同一批次中，相同属性只保留最新值。

### 8.2 Body 字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `ClientId` | string | 网关 MQTT 客户端 ID |
| `Timestamp` | long | 毫秒时间戳 |
| `DeviceCode` | string | 设备编码 |
| `DeviceName` | string | 本地设备名称 |
| `Body` | array | 属性值数组 |

内层 `Body[]`：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `Code` | string | 属性编码，应与驱动能力 `propertyList[].code` 对应 |
| `Value` | any | 属性值，类型由驱动决定 |

### 8.3 示例

```json
{
  "MsgId": "msg-device-telemetry-001",
  "Timestamp": 1787480054800,
  "GatewayCode": "GW000001",
  "DeviceCode": "DV000151",
  "Type": "telemetry",
  "Body": {
    "ClientId": "gateway-001",
    "Timestamp": 1787480054800,
    "DeviceCode": "DV000151",
    "DeviceName": "入口条码扫描器",
    "Body": [
      { "Code": "Status", "Value": true },
      { "Code": "WorkingStatus", "Value": 0 },
      { "Code": "OperationalMode", "Value": "auto" },
      { "Code": "LastBarcode", "Value": "PL20260928000001" }
    ]
  }
}
```

第三方应按 `DeviceCode + Code` 更新最新属性，不要假定所有属性都会在每一帧出现，也不要假定 `Value` 永远是字符串。

## 9. 设备上下线状态

### 9.1 接口定义

```text
方向：网关 -> 第三方平台
Topic：lab/v1/devices/{deviceCode}/status
QoS：1
Retain：false
外层 Type：online 或 offline
```

设备会话首次连接成功时发布 `online`；显式停止设备或停止网关时发布 `offline`。

### 9.2 Body 字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `msgId` | string | 与外层 `MsgId` 相同 |
| `ts` | long | 毫秒时间戳 |
| `deviceCode` | string | 设备编码 |
| `deviceName` | string | 设备名称 |
| `type` | string | `online` 或 `offline` |
| `code` | string | `ONLINE` 或 `OFFLINE` |
| `msg` | string | 状态描述 |

### 9.3 离线示例

```json
{
  "MsgId": "msg-device-status-001",
  "Timestamp": 1787480054900,
  "GatewayCode": "GW000001",
  "DeviceCode": "DV000151",
  "Type": "offline",
  "Body": {
    "msgId": "msg-device-status-001",
    "ts": 1787480054900,
    "deviceCode": "DV000151",
    "deviceName": "入口条码扫描器",
    "type": "offline",
    "code": "OFFLINE",
    "msg": "设备连接断开"
  }
}
```

当前 MQTT 客户端没有配置 Broker 遗嘱消息。异常断网时不能保证一定收到 `offline`，平台还应根据遥测/心跳超时进行离线判断。

## 10. 设备事件与属性上报

### 10.1 当前状态

以下 Topic 已在协议中定义，但当前网关源码没有对应发布调用：

```text
lab/v1/devices/{deviceCode}/event
lab/v1/devices/{deviceCode}/attributes
```

第三方平台可以预留订阅，但不能把它们作为当前网关的必到数据通道。设备状态和属性应优先使用 `telemetry` 与 `status`。

### 10.2 事件报文预留格式

若后续启用事件上报，推荐外层 `Type` 使用 `alarm` 或具体业务事件类型，Body 字段如下：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `msgId` | string | 是 | 事件幂等键 |
| `requestId` | string | 否 | 关联请求 |
| `ts` | long | 建议 | 毫秒时间戳 |
| `deviceCode` | string | 建议 | 设备编码 |
| `type` | string | 建议 | 事件类型 |
| `code` | string | 建议 | 事件码 |
| `msg` | string | 否 | 描述 |
| `stage` | int | 否 | 阶段 |
| `progress` | int | 否 | 进度 0～100 |
| `plateBarcode` | string | 否 | 板条码 |
| `data` | object | 否 | 附加数据 |

## 11. 设备 RPC

### 11.1 Topic 与消息时序

```text
请求：第三方平台 -> 网关
lab/v1/devices/{deviceCode}/rpc/request/{requestId}

ACK/最终结果：网关 -> 第三方平台
lab/v1/devices/{deviceCode}/rpc/response/{requestId}

进度：网关 -> 第三方平台
lab/v1/devices/{deviceCode}/rpc/progress/{requestId}
```

正常时序：

1. 第三方订阅响应和进度 Topic。
2. 第三方发布请求，QoS 1，非 retained。
3. 网关校验通过后立即发布 `rpc-response` ACK。
4. 网关执行设备功能或系统动作。
5. 执行结束后，在同一 response Topic 发布 `rpc-result`。
6. 运动类任务可能在 progress Topic 发布多条 `rpc-progress`。

**ACK 只表示请求已通过初步校验并被接收，不表示设备执行成功。**

### 11.2 请求字段

| 字段 | 位置 | 必填 | 说明 |
| --- | --- | --- | --- |
| `MsgId` | 外层 | 是 | 请求消息 ID |
| `Timestamp` | 外层 | 是 | 毫秒时间戳 |
| `GatewayCode` | 外层 | 建议 | 网关编码 |
| `DeviceCode` | 外层 | 是 | 必须与 Topic 一致 |
| `Type` | 外层 | 是 | 动作名，应与 `Body.action` 一致 |
| `msgId` | Body | 建议 | 业务消息 ID |
| `requestId` | Body | 是 | 必须与 Topic 最后一段一致 |
| `ts` | Body | 建议 | 毫秒时间戳 |
| `tenantId` | Body | 可选 | 租户标识，网关路由不依赖该字段 |
| `deviceCode` | Body | 建议 | 设备编码 |
| `action` | Body | 是 | 动作名 |
| `timeout` | Body | 建议 | 业务超时时间，单位秒 |
| `context` | Body | 可选 | 业务上下文 |
| `{action}` | Body | 条件必填 | 参数对象，键名等于动作名 |

`context` 可携带 `batchNo`、`plateBarcode`、`locationCode`、`seqNo` 等业务信息。普通设备驱动是否使用这些字段取决于驱动实现。

### 11.3 通用设备功能请求示例

```json
{
  "MsgId": "msg-rpc-request-001",
  "Timestamp": 1787480055000,
  "GatewayCode": "GW000001",
  "DeviceCode": "DV000151",
  "Type": "ScanBarcode",
  "Body": {
    "msgId": "msg-rpc-request-001",
    "requestId": "C-202611170001",
    "ts": 1787480055000,
    "tenantId": "000000",
    "deviceCode": "DV000151",
    "action": "ScanBarcode",
    "timeout": 30,
    "context": {
      "batchNo": "B202611170001",
      "plateBarcode": "PL202611170001",
      "locationCode": "LOC-001",
      "seqNo": 1
    },
    "ScanBarcode": {
      "scanMode": "barcode",
      "timeout": 30
    }
  }
}
```

对应 Topic：

```text
lab/v1/devices/DV000151/rpc/request/C-202611170001
```

普通动作会转成 `DeviceControl`，实际执行名为 `Type`/`action`，参数优先取 `Body[action]`。若不存在该对象，网关会移除信封字段后，将 Body 中其余字段作为参数。

### 11.4 ACK

外层 `Type=rpc-response`：

```json
{
  "MsgId": "msg-rpc-ack-001",
  "Timestamp": 1787480055005,
  "GatewayCode": "GW000001",
  "DeviceCode": "DV000151",
  "Type": "rpc-response",
  "Body": {
    "requestId": "C-202611170001",
    "success": true,
    "ts": 1787480055005
  }
}
```

### 11.5 正常最终结果

外层 `Type=rpc-result`，Body 使用兼容字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `DeviceId` | string | 设备编码 |
| `Method` | string | 实际动作名 |
| `ResponseId` | string | 原请求 ID |
| `ClientId` | string | 网关客户端 ID |
| `Timestamp` | long | 毫秒时间戳 |
| `Data.success` | boolean | 网关处理结果 |
| `Data.description` | string | 成功时为原始返回内容，失败时为错误描述 |
| `Data.deviceData` | any | 对原始返回内容尝试 JSON 解析后的值；解析失败时为字符串 |

成功示例：

```json
{
  "MsgId": "msg-rpc-result-001",
  "Timestamp": 1787480055100,
  "GatewayCode": "GW000001",
  "DeviceCode": "DV000151",
  "Type": "rpc-result",
  "Body": {
    "DeviceId": "DV000151",
    "Method": "ScanBarcode",
    "ResponseId": "C-202611170001",
    "ClientId": "gateway-001",
    "Timestamp": 1787480055100,
    "Data": {
      "success": true,
      "description": "{\"barcode\":\"PL202611170001\"}",
      "deviceData": {
        "barcode": "PL202611170001"
      }
    }
  }
}
```

失败示例：

```json
{
  "MsgId": "msg-rpc-result-002",
  "Timestamp": 1787480055100,
  "GatewayCode": "GW000001",
  "DeviceCode": "DV000151",
  "Type": "rpc-result",
  "Body": {
    "DeviceId": "DV000151",
    "Method": "ScanBarcode",
    "ResponseId": "C-202611170001",
    "ClientId": "gateway-001",
    "Timestamp": 1787480055100,
    "Data": {
      "success": false,
      "description": "扫码超时",
      "deviceData": null
    }
  }
}
```

**重要限制：**普通 `DeviceControl` 分发在驱动调用没有抛出异常时，会将 `Data.success` 设为 `true`，即使驱动原始返回字符串表示失败。因此第三方还必须按具体驱动约定检查 `Data.deviceData` 或 `Data.description`，不能只看外层成功字段。

### 11.6 网关异常结果

请求处理过程中发生未捕获异常时，可能返回另一种错误 Body：

```json
{
  "MsgId": "msg-rpc-exception-001",
  "Timestamp": 1787480055100,
  "GatewayCode": "GW000001",
  "DeviceCode": "DV000151",
  "Type": "rpc-result",
  "Body": {
    "msgId": "msg-rpc-exception-001",
    "requestId": "C-202611170001",
    "ts": 1787480055100,
    "deviceCode": "DV000151",
    "type": "result",
    "code": "1",
    "msg": "设备不存在",
    "data": null
  }
}
```

第三方消费者应同时兼容：

- 正常分支：`Body.Data.success`。
- 异常分支：`Body.code`，其中 `"0"` 表示成功，非 `"0"` 表示失败。

### 11.7 内置动作与参数

| 动作 | 参数对象/行为 | 说明 |
| --- | --- | --- |
| 其他驱动动作 | `Body[action]` | 转为 `DeviceControl`，调用对应驱动功能 |
| `GetProperty` | 无 | 返回网关缓存的最新属性，不会即时读取设备 |
| `GatewayInfoRequest` | 无 | 返回版本、运行中设备数和在线数 |
| `GetDrivers` | 无 | 返回本地驱动列表 |
| `GetDevices` | 无 | 返回本地设备列表 |
| `RescanDrivers` | 无 | 重扫驱动；加载到新驱动时重新上报列表和能力 |
| `SetProperty` | `{ "property": "属性名", "value": "属性值" }` | 设置设备属性 |
| `MoveJoint` | `{ "joints": [0, -90, 90, 0, 90, 0] }` | 关节运动，需运动插件 |
| `MoveCartesian` | `{ "position": [400, 0, 300, 0, 90, 0] }` | 笛卡尔运动，需运动插件 |
| `TeachingGrasp` | 无 | 示教夹取 |
| `TeachingRelease` | 无 | 示教放开 |
| `PlanAndExecute` | `{ "deviceId": "机械臂名称", "start": "起点", "waypoints": ["途经点"], "end": "终点" }` | 按示教点位规划执行 |
| `ExecuteMotion` | `Tasks` 和 `Material` | 组合运动，建议优先使用第 13 章的网关级接口 |
| `MovMulti` | `Tasks` 和 `Material` | 特殊设备 RPC，内部转发为网关级 `ExecuteMotion` |

以下动作当前仅返回成功确认，没有完成名称所暗示的实际操作：

- `UninstallDriver`
- `SetDeviceConfig`
- `gateway_device_connect`
- `gateway_device_disconnect`
- `gateway_device_deleted`
- `gateway_device_renamed`

不要将这些 ACK/结果作为本地状态已经变更的依据。

### 11.8 RPC 进度

运动执行过程中的进度示例：

```json
{
  "MsgId": "msg-rpc-progress-001",
  "Timestamp": 1787480055050,
  "GatewayCode": "GW000001",
  "DeviceCode": "DV000151",
  "Type": "rpc-progress",
  "Body": {
    "msgId": "msg-rpc-progress-001",
    "requestId": "C-202611170001",
    "ts": 1787480055050,
    "gatewayCode": null,
    "deviceCode": "DV000151",
    "type": "progress",
    "code": "0",
    "msg": "开始执行运动步骤"
  }
}
```

当前运动进度不一定包含数值百分比。`code=0` 的进度消息也可能只表示某个步骤开始或完成，最终完成状态必须以 `rpc-result` 为准。

### 11.9 MovMulti 特殊流程

设备 RPC 的 `Type/action=MovMulti` 时：

1. `Body.requestId` 必须与 Topic 一致。
2. `Body.MovMulti` 必须存在，内容使用 `ExecuteMotion` 的 `Tasks/Material` 结构。
3. 网关将其转发到本网关的 `motion/request/{requestId}`。
4. 原设备请求先收到设备 ACK；运动执行完成后，原设备 response Topic 收到设备级 `rpc-result`。
5. 平台如果同时订阅运动 response Topic，还可能看到这次内部转发产生的运动 ACK/结果，应按 Topic 和请求来源区分。

## 12. 平台设备注册

### 12.1 Topic

```text
请求：lab/v1/gateway/{gatewayCode}/device/install
ACK：lab/v1/gateway/{gatewayCode}/device/install/response/{requestId}
结果：lab/v1/gateway/{gatewayCode}/device/install/result/{requestId}
```

请求 Topic 不包含 `requestId`，该值只从 `Body.requestId` 获取。外层 `Type` 和 `Body.action` 固定为 `RegisterDevice`。

### 12.2 RegisterDevice 字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `deviceId` | string | 否 | 平台设备 ID，优先作为幂等匹配键 |
| `deviceCode` | string | 是 | 必须与外层 `DeviceCode` 一致 |
| `deviceName` | string | 否 | 本地设备名称，缺省使用 `deviceCode` |
| `driver` | object | 是 | 驱动匹配信息 |
| `driver.name` | string | 是 | 驱动名称 |
| `driver.version` | string | 是 | 驱动版本 |
| `driver.code` | string | 否 | 驱动编码 |
| `protocol` | string | 否 | 选择连接协议；缺省取 `connectConfig` 第一项 |
| `connectConfig` | array | 是 | 协议连接配置 |
| `deviceType` | object | 条件必填 | 名称/版本及驱动编码未直接命中时，用于元数据匹配 |
| `deviceBrand` | object | 条件必填 | 同上 |
| `deviceModel` | object | 条件必填 | 同上 |

`deviceType`、`deviceBrand`、`deviceModel` 均可包含 `code` 和 `name`。

`connectConfig[]`：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `protocol` | string | 是 | 协议名，与声明协议匹配 |
| `config` | object | 是 | 连接参数；键可使用驱动参数显示名或本地配置键 |

`config` 中的 `Simulation` 或 `仿真` 用于设置模拟设备标记，值应为布尔值或可解析为布尔值的字符串。网关会自动写入本地保留配置 `Code=deviceCode` 和 `PlatformDeviceId=deviceId`，调用方无需重复传入。

### 12.3 请求示例

```json
{
  "MsgId": "msg-register-request-001",
  "Timestamp": 1787480056000,
  "GatewayCode": "GW000001",
  "DeviceCode": "DV000151",
  "Type": "RegisterDevice",
  "Body": {
    "msgId": "msg-register-request-001",
    "requestId": "G-202611170001",
    "ts": 1787480056000,
    "tenantId": "000000",
    "deviceCode": "DV000151",
    "action": "RegisterDevice",
    "timeout": 30,
    "RegisterDevice": {
      "deviceId": "platform-device-001",
      "deviceCode": "DV000151",
      "deviceName": "入口条码扫描器",
      "driver": {
        "name": "CodeScan 条码扫描器驱动",
        "version": "1.0.1",
        "code": "CodeScan"
      },
      "deviceType": {
        "code": "Scanner",
        "name": "条码扫描器"
      },
      "deviceBrand": {
        "code": "CodeScan",
        "name": "CodeScan"
      },
      "deviceModel": {
        "code": "CS-2D",
        "name": "CS-2D"
      },
      "protocol": "mqtt",
      "connectConfig": [
        {
          "protocol": "mqtt",
          "config": {
            "MQTT地址": "127.0.0.1",
            "MQTT端口": "1883",
            "Simulation": true
          }
        }
      ]
    }
  }
}
```

示例中的驱动名称、版本、编码和连接参数必须替换为实际驱动列表/能力中的值。

### 12.4 驱动匹配与幂等规则

驱动匹配顺序：

1. 在支持所选协议的可用驱动中，按名称和版本匹配。
2. 未命中时按 `driver.code` 匹配。
3. 仍未命中时按设备类型、品牌、型号匹配。
4. 无候选或存在多个候选都会失败。

设备落库规则：

- 优先按 `deviceId` 查找已有设备，其次按 `deviceCode` 查找。
- 两个标识同时存在但指向不同本地设备时注册失败。
- 已存在且内容变化时更新设备，不重复创建。
- 注册成功后，网关按设备启用状态启动或重建本地设备会话。

### 12.5 ACK 与结果

ACK 外层 `Type=rpc-response`，Body 为：

```json
{
  "requestId": "G-202611170001",
  "success": true,
  "ts": 1787480056005
}
```

成功结果：

```json
{
  "MsgId": "msg-register-result-001",
  "Timestamp": 1787480056100,
  "GatewayCode": "GW000001",
  "DeviceCode": "DV000151",
  "Type": "rpc-result",
  "Body": {
    "requestId": "G-202611170001",
    "ts": 1787480056100,
    "deviceCode": "DV000151",
    "type": "result",
    "code": "0",
    "msg": "RegisterDevice success",
    "data": {
      "deviceId": 3,
      "platformDeviceId": "platform-device-001",
      "deviceCode": "DV000151",
      "driverId": 5,
      "driverName": "CodeScan 条码扫描器驱动",
      "deviceName": "入口条码扫描器",
      "created": true,
      "updated": false,
      "started": true
    }
  }
}
```

结果字段：

| 字段 | 说明 |
| --- | --- |
| `data.deviceId` | 网关本地数据库设备 ID |
| `data.platformDeviceId` | 平台设备 ID |
| `data.deviceCode` | 设备编码 |
| `data.driverId` / `driverName` | 匹配到的本地驱动 |
| `data.deviceName` | 本地设备名称 |
| `data.created` / `updated` | 本次是新增还是更新 |
| `data.started` | 设备会话是否已启动或已存在 |

失败时 `code="1"`、`msg` 为错误原因、`data=null`。结构校验失败时可能没有 ACK 或结果，调用方必须设置超时。

## 13. 网关级运动执行

### 13.1 Topic

```text
请求：lab/v1/gateway/{gatewayCode}/motion/request/{requestId}
ACK/结果：lab/v1/gateway/{gatewayCode}/motion/response/{requestId}
进度：lab/v1/gateway/{gatewayCode}/motion/progress/{requestId}
```

外层 `Type` 与 `Body.action` 必须为 `ExecuteMotion`，`DeviceCode` 应为 `null`。该接口要求网关已安装运动插件；没有插件时返回“运动控制功能未安装”。

### 13.2 ExecuteMotion 参数

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `Tasks` | array | 是 | 运动任务列表，不能为空 |
| `Material` | object | 否 | 物料和运动参数 |

`Tasks[]`：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `PositionId` | string | 点位 ID，对应点位记录 `id` 的字符串形式 |
| `ClawSwitch` | string | `"0"` 夹取、`"1"` 放开、`"-1"` 不操作 |
| `Posture` | number | 姿态；大于 0 时覆盖点位默认姿态 |
| `PostureType` | int | 姿态类型扩展字段 |
| `PauseTime` | int | 暂停时间扩展字段 |
| `PlateNo` | string | 板位/序号，用于特定高度修正逻辑 |
| `RowHeight` | string | 行高修正参数 |
| `ColWidth` | string | 列宽扩展参数 |

`Material`：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `GrabHeight` | number | 抓取高度修正 |
| `MoveSpeed` | number | 运动速度 |
| `Thickness` | number | 物料厚度 |
| `StackHeight` | number | 堆叠高度 |

示例只演示报文结构。实际点位、姿态、速度及高度必须经过现场安全确认，不能直接用于真实设备。

### 13.3 请求示例

```json
{
  "MsgId": "msg-motion-request-001",
  "Timestamp": 1787480057000,
  "GatewayCode": "GW000001",
  "DeviceCode": null,
  "Type": "ExecuteMotion",
  "Body": {
    "msgId": "msg-motion-request-001",
    "requestId": "M-202611170001",
    "ts": 1787480057000,
    "gatewayCode": "GW000001",
    "action": "ExecuteMotion",
    "timeout": 120,
    "ExecuteMotion": {
      "Tasks": [
        {
          "PositionId": "1",
          "ClawSwitch": "-1",
          "Posture": 0,
          "PostureType": 0,
          "PauseTime": 0,
          "PlateNo": "",
          "RowHeight": "",
          "ColWidth": ""
        },
        {
          "PositionId": "2",
          "ClawSwitch": "0",
          "Posture": 0,
          "PostureType": 0,
          "PauseTime": 0,
          "PlateNo": "",
          "RowHeight": "",
          "ColWidth": ""
        }
      ],
      "Material": {
        "GrabHeight": 0,
        "MoveSpeed": 20,
        "Thickness": 0,
        "StackHeight": 0
      }
    }
  }
}
```

### 13.4 回执与进度

ACK 发往 motion response Topic，外层 `Type=rpc-response`，Body 包含 `requestId`、`success=true` 和 `ts`。

最终成功结果：

```json
{
  "MsgId": "msg-motion-result-001",
  "Timestamp": 1787480057100,
  "GatewayCode": "GW000001",
  "DeviceCode": null,
  "Type": "rpc-result",
  "Body": {
    "msgId": "msg-motion-result-001",
    "requestId": "M-202611170001",
    "ts": 1787480057100,
    "gatewayCode": "GW000001",
    "type": "result",
    "action": "ExecuteMotion",
    "code": "0",
    "msg": "ExecuteMotion success",
    "data": null
  }
}
```

失败时 `code="1"`，`msg` 为失败原因。运动过程中的阶段消息发往 motion progress Topic，外层 `Type=rpc-progress`，Body 使用 `code` 和 `msg` 表示阶段状态。最终结果必须与 `requestId` 关联，不能用进度消息代替。

## 14. 点位同步

### 14.1 接口定义

```text
方向：第三方平台 -> 网关
Topic：lab/v1/gateway/{gatewayCode}/location
QoS：1
外层 Type：SyncLocation
```

该接口没有 ACK 或结果 Topic。网关收到有效数据后，整批替换本地点位文件及内存缓存。

### 14.2 Body 点位字段

`Body` 必须是非空数组，每项字段如下：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | long | 是 | 点位 ID，必须大于 0 |
| `posture` | string | 否 | 默认姿态 |
| `deviceName` | string | 是 | 点位关联设备名称 |
| `robotarmName` | string | 是 | 执行机械臂的本地设备名称 |
| `positionData` | string | 是 | 位置轨迹 JSON 数组的字符串，不是直接嵌入的数组 |

`positionData` 解析后的每项：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `Seq` | int | 轨迹顺序 |
| `Type` | string | 点位类型，不能为空；常见为 `T`/`J`，具体由驱动解释 |
| `Pos` | string[] | 位置值数组，不能为空 |

### 14.3 示例

```json
{
  "MsgId": "msg-location-sync-001",
  "Timestamp": 1787480058000,
  "GatewayCode": "GW000001",
  "DeviceCode": null,
  "Type": "SyncLocation",
  "Body": [
    {
      "id": 1,
      "posture": "0",
      "deviceName": "入口条码扫描器",
      "robotarmName": "自动化机械臂A",
      "positionData": "[{\"Seq\":1,\"Type\":\"T\",\"Pos\":[\"400\",\"0\",\"300\",\"0\",\"90\",\"0\"]}]"
    }
  ]
}
```

以下情况只会记录错误，不返回回执：

- `Body` 为空或不是数组。
- 任一点位 `id<=0`，或缺少 `deviceName`/`robotarmName`。
- `positionData` 不是合法 JSON、不是非空数组，或轨迹项缺少 `Type`/`Pos`。
- 外层 `GatewayCode` 与当前网关不一致，或 `Type` 不是 `SyncLocation`。

第三方发送后应通过后续运动结果、网关日志或独立查询手段确认同步效果。

## 15. 驱动安装下发

### 15.1 当前实现限制

```text
方向：第三方平台 -> 网关
Topic：lab/v1/gateway/{gatewayCode}/driver/install
状态：部分实现，仅订阅并记录日志
```

当前网关不会下载、校验或安装驱动，也没有实现 ACK/结果回执。第三方不能将该 Topic 当作已可用的远程安装接口。

### 15.2 协议预留报文

外层 `Type` 和 `Body.action` 均为 `InstallDriver`：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `Body.requestId` | string | 请求 ID |
| `Body.timeout` | int | 协议建议 300 秒 |
| `InstallDriver.fileUrl` | string | 驱动包 URL |
| `InstallDriver.fileName` | string | 文件名 |
| `InstallDriver.fileSize` | long | 文件字节数 |
| `InstallDriver.fileSha256` | string | SHA-256 |

```json
{
  "MsgId": "msg-driver-install-001",
  "Timestamp": 1787480059000,
  "GatewayCode": "GW000001",
  "DeviceCode": null,
  "Type": "InstallDriver",
  "Body": {
    "msgId": "msg-driver-install-001",
    "requestId": "I-202611170001",
    "ts": 1787480059000,
    "tenantId": "000000",
    "action": "InstallDriver",
    "timeout": 300,
    "InstallDriver": {
      "fileUrl": "https://example.com/drivers/Driver.Scanner.CodeScan-1.0.1.zip",
      "fileName": "Driver.Scanner.CodeScan-1.0.1.zip",
      "fileSize": 1048576,
      "fileSha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    }
  }
}
```

## 16. 下机文件上报

### 16.1 当前状态

网关已有文件上传成功后组装 MQTT 报文的服务，设计 Topic 为：

```text
lab/v1/gateway/{gatewayCode}/datafile/report
```

设计 QoS 为 1、retain 为 false。但当前源码中缺少 `MqttTopics.DataFileReport` 和 `MqttMessageTypes.DataFileReport` 的实际定义，因此该接口属于**待补齐项**。第三方可提前按以下结构设计消费者，但联调前必须确认部署版本已经补齐并启用该能力。

### 16.2 报文字段

该报文使用 camelCase 独立结构，不套 `MqttBasePacket`。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `msgId` | string | 消息 ID |
| `timestamp` | long | 上报时间 |
| `gatewayCode` | string | 网关编码 |
| `deviceCode` / `deviceName` | string | 设备身份 |
| `type` | string | 设计为 `datafile-report` |
| `batchNo` | string | 订单号/批次号 |
| `fileName` | string | 文件名 |
| `objectKey` | string | 对象存储唯一路径 |
| `bucket` | string/null | 对象存储桶 |
| `url` | string/null | 带时效的预签名下载地址 |
| `size` | long | 文件字节数 |
| `sha256` | string/null | 文件 SHA-256 |
| `contentType` | string/null | 内容类型 |
| `uploadedAt` | long | 上传完成时间 |
| `source` | string | 数据来源 |

```json
{
  "msgId": "msg-datafile-001",
  "timestamp": 1787480060000,
  "gatewayCode": "GW000001",
  "deviceCode": "DV000151",
  "deviceName": "酶标仪A",
  "type": "datafile-report",
  "batchNo": "B202611170001",
  "fileName": "B202611170001_result.csv",
  "objectKey": "GW000001/3/B202611170001/B202611170001_result.csv",
  "bucket": "lightsout-data",
  "url": "https://storage.example.com/presigned-file-url",
  "size": 20480,
  "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "contentType": "text/csv",
  "uploadedAt": 1787480059990,
  "source": "LocalFile"
}
```

`url` 生成失败时仍可能只上报 `objectKey`。第三方应优先持久化 `objectKey`，并把 `url` 当作临时地址而不是永久资源标识。

## 17. 第三方接入流程

### 17.1 推荐顺序

1. 获取 Broker 地址、认证信息、`TopicRoot` 和 `GatewayCode`。
2. 建立连接，订阅网关心跳、驱动列表、驱动能力、设备遥测、设备状态和 RPC 回执。
3. 等待网关心跳，确认网关在线。
4. 接收并持久化驱动列表及每个驱动能力，建立设备功能与参数定义。
5. 如需注册设备，调用 `RegisterDevice` 并等待最终 result。
6. 接收设备遥测，确认设备编码、属性编码和值类型。
7. 先订阅对应 response/progress，再下发 RPC。
8. 按 `requestId` 关联 ACK、进度和结果；只有最终结果才结束本次请求。
9. 运动任务先同步点位，再下发 `ExecuteMotion`，并使用安全测试参数联调。

### 17.2 订阅示例

以下命令使用 Mosquitto 客户端，仅演示 Topic：

```bash
mosquitto_sub -h <broker-host> -p 1883 -u <username> -P <password> -q 1 -t 'lab/v1/devices/+/rpc/response/+'
mosquitto_sub -h <broker-host> -p 1883 -u <username> -P <password> -q 0 -t 'lab/v1/devices/+/telemetry'
mosquitto_sub -h <broker-host> -p 1883 -u <username> -P <password> -q 0 -t 'lab/v1/gateway/GW000001/driver/#'
mosquitto_sub -h <broker-host> -p 1883 -u <username> -P <password> -q 1 -t 'lab/v1/gateway/GW000001/motion/response/+'
```

多平台实例消费同一消息时，可根据 Broker 能力使用共享订阅，例如 `$share/<group>/lab/v1/devices/+/telemetry`。共享订阅是平台部署选择，不改变报文结构。

### 17.3 超时、重试与幂等

- 请求超时应综合 `Body.timeout`、驱动 `timeoutSeconds` 和网络耗时设置，不要只等待固定的 ACK 超时。
- 收到 ACK 后继续等待最终结果；超时未收到任何消息时，先检查请求是否通过结构校验。
- 重试请求保持同一 `requestId`，并确保业务动作可安全重试。
- 重发同一事件时保持同一 `MsgId`；新消息不要复用旧 `MsgId`。
- MQTT QoS 1 可能产生重复投递，消费者必须支持幂等。
- 网关 `MqttBrokerClient` 在断线时不会为普通发布提供持久缓存；不要假定遥测或 RPC 结果一定会在恢复后补发。
- retained 只用于驱动档案的辅助获取，不应用来触发一次性设备动作。

## 18. 常见错误排查

| 现象 | 可能原因 | 处理建议 |
| --- | --- | --- |
| 发布 RPC 后完全没有回执 | Topic 与外层设备编码不一致；Topic 与 Body 请求 ID 不一致；缺少 `Type/action`；JSON 无效 | 按第 3 章和第 11 章逐项核对，并查看网关日志 |
| 收到 ACK 后任务仍未完成 | ACK 只表示接收 | 继续等待 `rpc-result`，按 `requestId` 关联 |
| `Data.success=true` 但设备失败 | 普通 DeviceControl 外层成功不等于驱动业务成功 | 检查 `deviceData` 和 `description` 的驱动原始结果 |
| 枚举参数被设备拒绝 | 下发了 `options.label` | 改为 `options.value` |
| 设备在线但工作状态不更新 | 遥测缺少 `Code=WorkingStatus`，或属性编码与能力定义不一致 | 使用内层 `Body[]` 的 `Code/Value` 格式 |
| 收不到全部驱动能力 | 只读取了 retained 消息 | 在启动/重扫期间实时订阅，并持久化每条能力报文 |
| 设备注册失败 | 驱动名称/版本不匹配；协议无可用驱动；缺少必填连接配置；幂等标识冲突 | 先核对驱动列表与能力，再检查注册结果 `msg` |
| 运动返回无点位或无机械臂 | 点位未同步、PositionId 不存在、设备名称不匹配或运动插件未安装 | 先核对点位和本地设备名称，再检查插件与结果消息 |
| 点位同步没有效果 | `positionData` 误传为数组而非 JSON 字符串，或没有回执导致失败未被发现 | 检查字符串转义和网关日志 |
| 异常断网后没有 offline | 当前未配置 Broker 遗嘱 | 使用心跳/遥测超时做兜底离线判断 |
| 使用了旧协议没有消息 | 使用了 `v1/devices/me/...`、旧 heartbeat/cmd/state Topic | 改用本文 `lab/v1` 协议 |

## 19. 当前版本对接限制汇总

- 驱动安装只有订阅和日志，不能远程安装。
- `UninstallDriver`、`SetDeviceConfig` 和部分网关设备通知动作是空实现。
- 设备事件和属性 Topic 当前没有网关发布实现。
- 普通设备 RPC 的外层成功字段不能完全代表驱动业务成功。
- 设备状态没有 Broker 遗嘱保障。
- 下机文件 Topic 和消息类型常量在当前源码中缺失，需先核对部署版本。
- 驱动能力 retained Topic 只能保留最后一条能力消息。
- 运动进度是文本阶段消息，不保证提供数值百分比。
- 旧交接资料中的 ThingsBoard 风格 Topic、旧版点位字段和旧版心跳格式不属于本文当前对接基线。

## 20. 源码核对位置

| 内容 | 文件 |
| --- | --- |
| Topic 定义 | `LightsOut.交付源码/Src/LightsOut.IoT.Service/Messaging/Protocol/MqttTopics.cs` |
| 通用外层包与消息类型 | `LightsOut.交付源码/Src/LightsOut.IoT.Service/Messaging/Protocol/MqttBasePacket.cs` |
| 连接、订阅、发布、校验与 ACK | `LightsOut.交付源码/Src/LightsOut.IoT.Service/Messaging/MqttBrokerClient.cs` |
| RPC 路由、心跳、遥测、状态 | `LightsOut.交付源码/Src/LightsOut.IoT.Service/GatewayService.cs` |
| RPC 动作分发 | `LightsOut.交付源码/Src/LightsOut.IoT.Service/Commanding/CommandDispatcher.cs` |
| 驱动列表/能力发布 | `LightsOut.交付源码/Src/LightsOut.IoT.Service/Messaging/DriverReportService.cs` |
| 驱动能力字段映射 | `LightsOut.交付源码/Src/LightsOut.IoT.Service/Messaging/DriverReportMapper.cs` |
| 设备注册 | `LightsOut.交付源码/Src/LightsOut.IoT.Service/DeviceManagement/PlatformDeviceRegistrationService.cs` |
| 运动与点位同步 | `LightsOut.交付源码/Src/LightsOut.IoT.Motion.Core/MotionService.cs` |
| 下机文件报文 | `LightsOut.交付源码/Src/LightsOut.IoT.Service/Messaging/Protocol/MqttDataFileReport.cs` |
| 平台侧协议参考 | `device-mqtt-protocol.md` |

本文描述的是当前源码协议，不代表所有历史部署包均已具备相同能力。正式联调前，应确认目标环境的版本和上述“当前版本对接限制”。

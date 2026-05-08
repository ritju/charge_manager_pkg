# bt_watcher - BLE Charger Manager

通过 BLE 与充电器通信，并通过 MQTT 桥接数据与控制命令。

---

## 安装

### 1. 安装 MQTT Broker

```bash
sudo apt install mosquitto
```

修改配置，关闭持久化：

```ini
# /etc/mosquitto/mosquitto.conf
persistence false
autosave_interval 0
```

重启 Mosquitto：

```bash
sudo service mosquitto restart
```

检查日志配置（可选）：

```bash
/etc/logrotate.d/mosquitto
```

### 2. 安装 bt_watcher 系统服务

#### 方式一：从源码直接安装

```bash
cd /home/sherlock/codes/charge_manager_ws/src/bt_watcher
sudo ./install_service.sh
```

#### 方式二：从打包文件安装（部署到其他设备）

在开发机上打包：

```bash
cd /home/sherlock/codes/charge_manager_ws/src/bt_watcher
./pack.sh
```

生成的压缩包位于 `dist/bt_watcher-0.1.0.tar.gz`，拷贝到目标设备后：

```bash
tar xzf bt_watcher-0.1.0.tar.gz
cd bt_watcher-0.1.0
pip3 install -r requirements.txt
sudo ./install_service.sh
```

安装脚本会：
- 以当前登录用户（非 root）运行服务，使用用户安装的 Python 依赖
- 自动检测并安装缺失的 Python 包（`aiomqtt`, `bleak`, `crcmod`）
- 创建日志目录 `~/.local/share/bt_watcher/logs/`
- 注册 systemd 服务并设置为开机自启

### 3. 服务管理

```bash
# 查看状态
systemctl status bt_watcher

# 启动 / 停止 / 重启
sudo systemctl start bt_watcher
sudo systemctl stop bt_watcher
sudo systemctl restart bt_watcher

# 查看服务日志（journal）
journalctl -u bt_watcher -f

# 查看应用日志文件
tail -f ~/.local/share/bt_watcher/logs/bt_watcher.log

# 卸载服务
sudo systemctl stop bt_watcher && sudo systemctl disable bt_watcher && sudo rm /etc/systemd/system/bt_watcher.service && sudo systemctl daemon-reload
```

---

## 日志

- 日志文件：`~/.local/share/bt_watcher/logs/bt_watcher.log`
- 单文件最大 20MB，最多保留 5 个历史文件（自动轮转）
- 同时输出到 systemd journal，可通过 `journalctl -u bt_watcher -f` 查看

---

## 接口定义总览

### 1. bt_watcher（宿主机）的 MQTT 职责

| 操作 | Topic | 动作 |
|------|-------|------|
| 发布 BLE 解析数据 | `charger/ble/data` | 每次 `notify_data` 回调时发布 |
| 发布充电状态 | `charger/ble/state` | 数据解析后发布（映射 ChargeState2） |
| 发布连接状态 | `charger/ble/status` | 心跳周期 2Hz 发布 |
| 发布请求响应 | `charger/ble/response` | 对 command/connect/disconnect 请求返回执行结果 |
| 订阅控制命令 | `charger/ble/command` | 收到后执行充电/加水启停，并发布响应到 response |
| 订阅连接请求 | `charger/ble/connect` | 收到后执行 BLE 连接，并发布响应到 response |
| 订阅断开请求 | `charger/ble/disconnect` | 收到后执行 BLE 断开，并发布响应到 response |

### 2. charge_service_bluetooth.py（容器内）的桥接逻辑

| ROS 2 端 | → | MQTT 端 |
|----------|---|---------|
| 订阅 `/bluetooth_command` | → | 发布 `charger/ble/command` |
| 订阅 `/connect_bluetooth` (service) | → | 发布 `charger/ble/connect` |
| 订阅 `/disconnect_bluetooth` (service) | → | 发布 `charger/ble/disconnect` |
| 订阅 `charger/ble/state` (MQTT) | → | 发布 `/charger/state2` (ROS 2) |

---

## 消息格式（JSON）

> 所有请求消息需携带 `id` 字段用于标识请求，bt_watcher 会在 `charger/ble/response` 中通过 `request_id` 和 `topic` 返回对应结果。

### charger/ble/status（连接状态，2Hz 心跳）

```json
{
  "id": 1001,
  "connected": true,
  "mac": "AA:BB:CC:DD:EE:FF",
  "last_data_received": 1715000000.123
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 自增消息 ID |
| `connected` | bool | BLE 是否已连接 |
| `mac` | string | 已连接设备的 MAC 地址 |
| `last_data_received` | float | 最后一次收到 BLE 数据的时间戳 |

### charger/ble/state（对应 ChargeState2）

```json
{
  "id": 2001,
  "pid": "AA:BB:CC:DD:EE:FF",
  "has_contact": true,
  "is_charging": false,
  "is_waterflooding": false,
  "water_mode": "manual",
  "timestamp": 1715000000.123
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 自增消息 ID |
| `pid` | string | 设备 MAC 地址 |
| `has_contact` | bool | 是否接触 |
| `is_charging` | bool | 是否充电中 |
| `is_waterflooding` | bool | 是否加水中 |
| `water_mode` | string | 加水模式：`manual` / `auto` |
| `timestamp` | float | 时间戳 |

### charger/ble/data（BLE 原始数据）

```json
{
  "id": 3001,
  "raw_hex": ["6b", "00", "00", "21", "09", "00", ...],
  "crc_valid": true,
  "timestamp": 1715000000.123
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 自增消息 ID |
| `raw_hex` | string[] | BLE 原始字节（十六进制字符串数组） |
| `crc_valid` | bool | CRC 校验是否通过 |
| `timestamp` | float | 时间戳 |

### charger/ble/command（控制命令，请求）

```json
{
  "id": 1,
  "command": 0
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 请求 ID，用于响应匹配 |
| `command` | int | `0`=CHARGER_START, `1`=CHARGER_STOP, `2`=WATER_START, `3`=WATER_STOP |

### charger/ble/connect（连接请求）

```json
{
  "id": 2,
  "mac": "AA:BB:CC:DD:EE:FF"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 请求 ID |
| `mac` | string | 要连接的 BLE 设备 MAC 地址 |

### charger/ble/disconnect（断开请求）

```json
{
  "id": 3
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 请求 ID |

### charger/ble/response（响应）

bt_watcher 对所有请求消息返回执行结果。

```json
{
  "request_id": 1,
  "topic": "charger/ble/command",
  "code": "ok",
  "msg": "command_executed",
  "timestamp": 1715000000.456
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `request_id` | int | 对应请求的 `id` |
| `topic` | string | 来源 topic：`charger/ble/command` / `charger/ble/connect` / `charger/ble/disconnect` |
| `code` | string | 结果码，见下表 |
| `msg` | string | 结果描述 |
| `timestamp` | float | 响应时间戳 |

**code 值说明：**

| code | 含义 |
|------|------|
| `ok` | 请求执行成功 |
| `already_connected` | 已连接其他设备（connect 请求特有） |
| `invalid_command` | 未知命令（command 请求特有） |
| `invalid_request` | 请求格式错误 |

**响应示例 — 已连接其他设备：**

```json
{
  "request_id": 2,
  "topic": "charger/ble/connect",
  "code": "already_connected",
  "msg": "Already connected to AA:BB:CC:DD:EE:FF",
  "current_mac": "AA:BB:CC:DD:EE:FF",
  "timestamp": 1715000000.456
}
```
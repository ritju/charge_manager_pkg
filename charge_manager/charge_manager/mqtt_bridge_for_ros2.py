#!/usr/bin/env python3
"""
Bluetooth Charge Server - 容器内运行的 ROS 2 节点
通过 MQTT 与容器外的 bt_watcher 通信，桥接 ROS 2 与 BLE 充电桩。
"""

import rclpy
from rclpy.node import Node
import time
import os
import json
import threading
import paho.mqtt.client as mqtt
from charge_manager_msgs.srv import ConnectBluetooth, DisconnectBluetooth
from charge_manager_msgs.msg import ChargeState2
from charge_manager_msgs.msg import BluetoothCommand
from rclpy.qos import DurabilityPolicy, ReliabilityPolicy, QoSProfile, HistoryPolicy
from rclpy.callback_groups import ReentrantCallbackGroup, MutuallyExclusiveCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from typing import Optional


class BluetoothChargeServer(Node):
    """ROS 2 节点，运行在容器内，通过 MQTT 与容器外的 bt_watcher 通信。

    职责：
    - 将 ROS 2 Service (/connect_bluetooth, /disconnect_bluetooth) 桥接到 MQTT
    - 将 ROS 2 Subscription (/bluetooth_command) 桥接到 MQTT
    - 订阅 MQTT 的充电状态并发布到 ROS 2 (/charger/state2)
    """

    # MQTT Topics
    TOPIC_CONNECT = "charger/ble/connect"
    TOPIC_DISCONNECT = "charger/ble/disconnect"
    TOPIC_COMMAND = "charger/ble/command"
    TOPIC_STATE = "charger/ble/state"
    TOPIC_STATUS = "charger/ble/status"
    TOPIC_RESPONSE = "charger/ble/response"

    def __init__(self, name, mqtt_host: str = "host.docker.internal", mqtt_port: int = 1883):
        super().__init__(name)

        env_var = os.environ.get('DOCK_USE_BLUETOOTH_RESTORE_SERVICE', 'False')
        self.declare_parameter("use_bluetooth_restore_service", env_var)
        self.use_bluetooth_restore_service = self.get_parameter(
            "use_bluetooth_restore_service").get_parameter_value().string_value.strip().lower()
        if self.use_bluetooth_restore_service in ('true', 'yes', 'on', '1', 't', 'y', 'enabled'):
            self.get_logger().info('use_bluetooth_restore_service: True')
            self.use_bluetooth_restore_service = True
        else:
            self.get_logger().info('use_bluetooth_restore_service: False')
            self.use_bluetooth_restore_service = False

        # MQTT 配置
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.mqtt_client: Optional[mqtt.Client] = None

        # 请求 ID 计数器
        self._request_id_counter = 0
        self._request_id_lock = threading.Lock()

        # 响应等待标志（按 request_id 区分）
        self._pending_requests: dict[int, threading.Event] = {}
        self._pending_results: dict[int, dict] = {}
        self._pending_lock = threading.Lock()

        # 当前连接状态（来自 MQTT status）
        self._mqtt_connected = False
        self._mqtt_mac = ""
        self._mqtt_last_data_received = 0.0

        # 控制发布线程退出的标志
        self._shutdown_event = threading.Event()

        # ROS 2 服务
        self.bluetooth_concact_server = self.create_service(
            ConnectBluetooth, '/connect_bluetooth', self.connect_bluetooth,
            callback_group=ReentrantCallbackGroup())
        self.bluetooth_disconnect_server = self.create_service(
            DisconnectBluetooth, '/disconnect_bluetooth', self.disconnect_bluetooth_callback,
            callback_group=MutuallyExclusiveCallbackGroup())

        # ROS 2 发布器
        charger_state_qos = QoSProfile(depth=1)
        charger_state_qos.reliability = ReliabilityPolicy.BEST_EFFORT
        charger_state_qos.history = HistoryPolicy.KEEP_LAST
        charger_state_qos.durability = DurabilityPolicy.VOLATILE

        # 初始化 ChargeState2
        self.charge_state = ChargeState2()
        self.charge_state.pid = ""
        self.charge_state.has_contact = False
        self.charge_state.is_charging = False
        self.charge_state.is_waterflooding = False
        self.contact_state_last_ = False

        self.charge_state_publisher = self.create_publisher(
            ChargeState2, '/charger/state2', charger_state_qos,
            callback_group=ReentrantCallbackGroup())
        self.publish_rate = self.create_rate(20)  # 20Hz 发布频率

        # ROS 2 订阅
        self.start_stop_charge_server = self.create_subscription(
            BluetoothCommand, '/bluetooth_command', self.start_stop_charge_callback, 5,
            callback_group=ReentrantCallbackGroup())

        # 启动 MQTT
        self._start_mqtt()

        # 启动状态发布线程
        self.charge_state_publish_thread = threading.Thread(
            target=self.charge_state_pub, daemon=True)
        self.charge_state_publish_thread.start()

        self.get_logger().info("BluetoothChargeServer (MQTT bridge mode) initialized")

    # ==================== MQTT 初始化与回调 ====================

    def _start_mqtt(self):
        """启动 MQTT 客户端并订阅所需主题。"""
        self.mqtt_client = mqtt.Client()
        self.mqtt_client.on_connect = self._on_mqtt_connect
        self.mqtt_client.on_message = self._on_mqtt_message
        self.mqtt_client.on_disconnect = self._on_mqtt_disconnect

        try:
            self.get_logger().info(f"Connecting to MQTT broker: {self.mqtt_host}:{self.mqtt_port}")
            self.mqtt_client.connect(self.mqtt_host, self.mqtt_port, 60)
            self.mqtt_client.loop_start()
        except Exception as e:
            self.get_logger().error(f"MQTT connection failed: {e}")

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.get_logger().info("MQTT connected")
            self.mqtt_client.subscribe(self.TOPIC_STATE)
            self.mqtt_client.subscribe(self.TOPIC_STATUS)
            self.mqtt_client.subscribe(self.TOPIC_RESPONSE)
            self.get_logger().info(
                f"Subscribed to: {self.TOPIC_STATE}, {self.TOPIC_STATUS}, {self.TOPIC_RESPONSE}")
        else:
            self.get_logger().error(f"MQTT connection failed, rc: {rc}")

    def _on_mqtt_disconnect(self, client, userdata, rc):
        self.get_logger().warn(f"MQTT disconnected, rc: {rc}")
        self._mqtt_connected = False

    def _on_mqtt_message(self, client, userdata, msg):
        topic = msg.topic
        try:
            payload = json.loads(msg.payload.decode('utf-8'))
        except json.JSONDecodeError as e:
            self.get_logger().error(f"JSON decode error on {topic}: {e}")
            return
        except Exception as e:
            self.get_logger().error(f"Error processing MQTT message on {topic}: {e}")
            return

        if topic == self.TOPIC_STATE:
            self._handle_charge_state(payload)
        elif topic == self.TOPIC_STATUS:
            self._handle_connection_status(payload)
        elif topic == self.TOPIC_RESPONSE:
            self._handle_response(payload)
        else:
            self.get_logger().debug(f"Unhandled topic: {topic}")

    # ==================== MQTT 消息处理 ====================

    def _handle_charge_state(self, payload: dict):
        """处理充电状态，更新本地 ChargeState2。"""
        self.charge_state.pid = payload.get("pid", "")
        self.charge_state.has_contact = payload.get("has_contact", False)
        self.charge_state.is_charging = payload.get("is_charging", False)
        self.charge_state.is_waterflooding = payload.get("is_waterflooding", False)
        # 如果有其他字段也可以在这里更新
        if "water_mode" in payload:
            self.charge_state.water_mode = payload.get("water_mode", "auto")
        self.get_logger().debug(
            f"Charge state updated: pid={self.charge_state.pid}, "
            f"has_contact={self.charge_state.has_contact}, "
            f"is_charging={self.charge_state.is_charging}")

    def _handle_connection_status(self, payload: dict):
        """处理 BLE 连接状态（心跳，2Hz）。"""
        self._mqtt_connected = payload.get("connected", False)
        self._mqtt_mac = payload.get("mac", "")
        self._mqtt_last_data_received = payload.get("last_data_received", 0.0)
        
        # 更新数据接收时间，用于超时检测
        if self._mqtt_last_data_received > 0:
            self.data_received_time = self._mqtt_last_data_received
            
        self.get_logger().debug(
            f"BLE status: connected={self._mqtt_connected}, mac={self._mqtt_mac}")

    def _handle_response(self, payload: dict):
        """处理 bt_watcher 对请求的响应。"""
        request_id = payload.get("request_id")
        if request_id is None:
            self.get_logger().warn(f"Response missing request_id: {payload}")
            return

        with self._pending_lock:
            if request_id in self._pending_requests:
                self._pending_results[request_id] = payload
                self._pending_requests[request_id].set()
                self.get_logger().debug(
                    f"Received response for request_id={request_id}, "
                    f"code={payload.get('code')}")
            else:
                self.get_logger().debug(
                    f"Received response for unknown request_id={request_id}")

    # ==================== 状态发布线程 ====================

    def charge_state_pub(self):
        """定期发布充电状态到 /charger/state2，20Hz。"""
        self.get_logger().info(
            f'charger_state_pub thread => Process: {os.getpid()}, Thread: {threading.get_ident()}')
        
        while not self._shutdown_event.is_set():
            if not rclpy.ok():
                self.get_logger().info('rclpy\'s context is invalid, exiting...')
                break

            # 如果没有 BLE 连接，清空 pid
            if not self._mqtt_connected:
                self.charge_state.pid = ''

            # 发布状态
            self.charge_state_publisher.publish(self.charge_state)

            # 检测接触状态变化并记录日志
            if self.contact_state_last_ != self.charge_state.has_contact:
                self.get_logger().info(
                    f"bluetooth => contact state change from {str(self.contact_state_last_)} "
                    f"to {str(self.charge_state.has_contact)}")
                self.contact_state_last_ = self.charge_state.has_contact

            # 检测数据超时（超过 20 秒未收到数据）
            # 注意：这里需要从 MQTT status 消息中获取 last_data_received
            self.publish_rate.sleep()

    # ==================== ROS 2 Service: /connect_bluetooth ====================

    def _get_next_request_id(self) -> int:
        with self._request_id_lock:
            self._request_id_counter += 1
            return self._request_id_counter

    def connect_bluetooth(self, request, response):
        self.get_logger().info(f"Received /connect_bluetooth request, mac: {request.mac}")

        # 如果已经连接到目标 MAC，直接返回成功
        if self._mqtt_connected and self._mqtt_mac == request.mac:
            self.get_logger().info(f"Already connected to {request.mac}, skipping.")
            response.success = True
            response.connection_time = 0.0
            response.result = f"Already connected to {request.mac}"
            return response

        # 等待 bt_watcher 就绪（模拟原 restore 逻辑）
        if self.use_bluetooth_restore_service:
            if not self._wait_for_restore():
                self.get_logger().warn("Bluetooth restore service not ready, proceeding anyway.")

        start_time = time.time()
        req_id = self._get_next_request_id()
        event = threading.Event()

        with self._pending_lock:
            self._pending_requests[req_id] = event
            self._pending_results.pop(req_id, None)

        # 发布连接请求
        connect_msg = {"id": req_id, "mac": request.mac}
        self.mqtt_client.publish(self.TOPIC_CONNECT, json.dumps(connect_msg))
        self.get_logger().info(f"Published connect request (id={req_id}, mac={request.mac})")

        # 等待响应
        timeout = 30.0
        got_event = event.wait(timeout=timeout)

        with self._pending_lock:
            result = self._pending_results.pop(req_id, None)
            self._pending_requests.pop(req_id, None)

        elapsed = round(time.time() - start_time, 1)

        if not got_event:
            response.success = False
            response.connection_time = elapsed
            response.result = f"Connect request timeout ({timeout}s)"
            self.get_logger().warn(f"Connect timeout for mac={request.mac}")
            return response

        # 解析响应
        code = result.get("code", "")
        msg_text = result.get("msg", "")
        response.success = (code == "ok")
        response.connection_time = elapsed
        response.result = f"{code}: {msg_text}"
        self.get_logger().info(f"Connect response: success={response.success}, result={response.result}")
        return response

    def _wait_for_restore(self, timeout: float = 45.0) -> bool:
        """等待蓝牙恢复服务就绪。简化实现：仅轮询连接状态。"""
        start = time.time()
        while time.time() - start < timeout:
            # 如果 bt_watcher 已连接，认为恢复完成
            if self._mqtt_connected:
                return True
            time.sleep(1.0)
        return False

    # ==================== ROS 2 Service: /disconnect_bluetooth ====================

    def disconnect_bluetooth_callback(self, request, response):
        start_time = time.time()
        self.get_logger().info('Received /disconnect_bluetooth request')

        req_id = self._get_next_request_id()
        event = threading.Event()

        with self._pending_lock:
            self._pending_requests[req_id] = event
            self._pending_results.pop(req_id, None)

        # 发布断开请求
        disconnect_msg = {"id": req_id}
        self.mqtt_client.publish(self.TOPIC_DISCONNECT, json.dumps(disconnect_msg))
        self.get_logger().info(f"Published disconnect request (id={req_id})")

        # 等待响应
        timeout = 10.0
        got_event = event.wait(timeout=timeout)

        with self._pending_lock:
            result = self._pending_results.pop(req_id, None)
            self._pending_requests.pop(req_id, None)

        elapsed = round(time.time() - start_time, 1)

        if not got_event:
            response.success = False
            response.cost_time = elapsed
            response.infos = f"Disconnect request timeout ({timeout}s)"
            return response

        code = result.get("code", "")
        msg_text = result.get("msg", "")
        response.success = (code == "ok")
        response.cost_time = elapsed
        response.infos = f"{code}: {msg_text}"
        self.get_logger().info(f"Disconnect response: success={response.success}, infos={response.infos}")
        return response

    # ==================== ROS 2 Subscription: /bluetooth_command ====================

    def start_stop_charge_callback(self, msg):
        """将 ROS 2 BluetoothCommand 转换为 MQTT command 并发布。"""
        command_map = {
            BluetoothCommand.CHARGER_START: 0,
            BluetoothCommand.CHARGER_STOP: 1,
            BluetoothCommand.WATER_START: 2,
            BluetoothCommand.WATER_STOP: 3,
        }

        if self._mqtt_mac == "":
            self.get_logger().warn('No BLE connection, cannot execute command.')
            return

        command_value = command_map.get(msg.command)
        if command_value is None:
            self.get_logger().warn(f'Unknown BluetoothCommand: {msg.command}')
            return

        req_id = self._get_next_request_id()
        event = threading.Event()

        with self._pending_lock:
            self._pending_requests[req_id] = event
            self._pending_results.pop(req_id, None)

        command_msg = {"id": req_id, "command": command_value}
        self.mqtt_client.publish(self.TOPIC_COMMAND, json.dumps(command_msg))
        self.get_logger().info(f"Published command (id={req_id}, command={command_value})")

        # 等待执行结果
        timeout = 15.0
        got_event = event.wait(timeout=timeout)

        with self._pending_lock:
            result = self._pending_results.pop(req_id, None)
            self._pending_requests.pop(req_id, None)

        if got_event:
            code = result.get("code", "")
            msg_text = result.get("msg", "")
            self.get_logger().info(f"Command response: code={code}, msg={msg_text}")
        else:
            self.get_logger().warn(f"Command response timeout ({timeout}s)")

    # ==================== 生命周期管理 ====================

    def destroy_node(self):
        self._shutdown_event.set()
        if self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
        super().destroy_node()


def main(args=None):
    import argparse

    parser = argparse.ArgumentParser(description='Bluetooth Charge Server (MQTT bridge to bt_watcher)')
    parser.add_argument('--mqtt-host', default='localhost',
                        help='MQTT broker host (default: localhost)')
    parser.add_argument('--mqtt-port', type=int, default=1883,
                        help='MQTT broker port (default: 1883)')
    cmd_args, unknown = parser.parse_known_args()

    rclpy.init(args=args)
    node = BluetoothChargeServer('bluetooth_charge_server',
                                 mqtt_host=cmd_args.mqtt_host,
                                 mqtt_port=cmd_args.mqtt_port)
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        node.get_logger().info("Received shutdown signal")
    finally:
        executor.shutdown()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
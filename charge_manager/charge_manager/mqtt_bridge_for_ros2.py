#!/usr/bin/env python3
"""
MQTT to ROS2 Bridge - 在容器内运行，将 MQTT 消息桥接到 ROS2 话题
"""

import json
import time
import threading
import signal
import sys
import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, ReliabilityPolicy, QoSProfile, HistoryPolicy
from rclpy.callback_groups import ReentrantCallbackGroup, MutuallyExclusiveCallbackGroup

import paho.mqtt.client as mqtt

from charge_manager_msgs.srv import ConnectBluetooth, DisconnectBluetooth
from charge_manager_msgs.msg import ChargeState2, BluetoothCommand


class MQTTROS2Bridge(Node):
    def __init__(self, mqtt_host: str = "localhost", mqtt_port: int = 1883):
        super().__init__('mqtt_ros2_bridge')
        
        # MQTT 配置
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.mqtt_client: Optional[mqtt.Client] = None
        
        # 等待响应的标志
        self.connect_response_received = False
        self.connect_response_success = False
        self.connect_response_result = ""
        self.connect_response_mac = ""
        self.connect_response_lock = threading.Lock()
        
        self.disconnect_response_received = False
        self.disconnect_response_success = False
        self.disconnect_response_infos = ""
        self.disconnect_response_lock = threading.Lock()
        
        # ROS2 发布器
        charger_state_qos = QoSProfile(depth=1)
        charger_state_qos.reliability = ReliabilityPolicy.BEST_EFFORT
        charger_state_qos.history = HistoryPolicy.KEEP_LAST
        charger_state_qos.durability = DurabilityPolicy.VOLATILE
        
        self.charge_state_publisher = self.create_publisher(
            ChargeState2, '/charger/state2', charger_state_qos, 
            callback_group=ReentrantCallbackGroup()
        )
        
        # 启动 MQTT
        self.start_mqtt()
        
        self.get_logger().info("MQTT-ROS2 Bridge 初始化完成")
    
    # ==================== MQTT 回调 ====================
    
    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.get_logger().info(f"MQTT 连接成功，使用 {self.mqtt_host}:{self.mqtt_port}")
            # 订阅来自宿主机的状态主题
            client.subscribe("charger/ble/state")
            client.subscribe("charger/ble/status")
            client.subscribe("charger/ble/connect_response")
            client.subscribe("charger/ble/disconnect_response")
            self.get_logger().info("已订阅: charger/ble/state, charger/ble/status, charger/ble/connect_response, charger/ble/disconnect_response")
        else:
            self.get_logger().error(f"MQTT 连接失败，返回码: {rc}")
    
    def on_message(self, client, userdata, msg):
        topic = msg.topic
        try:
            payload = json.loads(msg.payload.decode('utf-8'))
            
            if topic == "charger/ble/state":
                self._handle_charge_state(payload)
            elif topic == "charger/ble/status":
                self._handle_connection_status(payload)
            elif topic == "charger/ble/connect_response":
                self._handle_connect_response(payload)
            elif topic == "charger/ble/disconnect_response":
                self._handle_disconnect_response(payload)
        
        except json.JSONDecodeError as e:
            self.get_logger().error(f"JSON 解析错误: {e}")
        except Exception as e:
            self.get_logger().error(f"处理 MQTT 消息时出错: {e}")
    
    def _handle_charge_state(self, payload: dict):
        """处理充电状态，发布到 /charger/state2"""
        msg = ChargeState2()
        msg.pid = payload.get("pid", "")
        msg.has_contact = payload.get("has_contact", False)
        msg.is_charging = payload.get("is_charging", False)
        msg.is_waterflooding = payload.get("is_waterflooding", False)
        
        # 注意：ChargeState2 可能没有 water_mode 字段，如果有则忽略或映射
        # 如果需要可以扩展，这里忽略 water_mode
        
        self.charge_state_publisher.publish(msg)
        self.get_logger().debug(f"发布充电状态: pid={msg.pid}, has_contact={msg.has_contact}, is_charging={msg.is_charging}")
    
    def _handle_connection_status(self, payload: dict):
        """处理连接状态（可选，用于日志）"""
        connected = payload.get("connected", False)
        mac = payload.get("mac", "")
        self.get_logger().debug(f"蓝牙连接状态: connected={connected}, mac={mac}")
    
    def _handle_connect_response(self, payload: dict):
        """处理连接响应"""
        with self.connect_response_lock:
            self.connect_response_success = payload.get("success", False)
            self.connect_response_result = payload.get("result", "")
            self.connect_response_mac = payload.get("mac", "")
            self.connect_response_received = True
        self.get_logger().info(f"收到连接响应: success={self.connect_response_success}, result={self.connect_response_result}")
    
    def _handle_disconnect_response(self, payload: dict):
        """处理断开连接响应"""
        with self.disconnect_response_lock:
            self.disconnect_response_success = payload.get("success", False)
            self.disconnect_response_infos = payload.get("infos", "")
            self.disconnect_response_received = True
        self.get_logger().info(f"收到断开响应: success={self.disconnect_response_success}, infos={self.disconnect_response_infos}")
    
    # ==================== ROS2 服务处理 ====================
    
    def _call_mqtt_connect(self, mac: str, timeout: float = 30.0) -> tuple:
        """通过 MQTT 调用连接服务"""
        with self.connect_response_lock:
            self.connect_response_received = False
            self.connect_response_success = False
        
        # 发布连接请求
        request = {"mac": mac}
        self.mqtt_client.publish("charger/ble/connect", json.dumps(request))
        self.get_logger().info(f"已发布连接请求: {mac}")
        
        # 等待响应
        start_time = time.time()
        while time.time() - start_time < timeout:
            with self.connect_response_lock:
                if self.connect_response_received:
                    return self.connect_response_success, self.connect_response_result
            time.sleep(0.1)
        
        return False, f"连接请求超时 ({timeout}s)"
    
    def _call_mqtt_disconnect(self, timeout: float = 10.0) -> tuple:
        """通过 MQTT 调用断开连接服务"""
        with self.disconnect_response_lock:
            self.disconnect_response_received = False
            self.disconnect_response_success = False
        
        # 发布断开请求
        self.mqtt_client.publish("charger/ble/disconnect", json.dumps({}))
        self.get_logger().info("已发布断开请求")
        
        # 等待响应
        start_time = time.time()
        while time.time() - start_time < timeout:
            with self.disconnect_response_lock:
                if self.disconnect_response_received:
                    return self.disconnect_response_success, self.disconnect_response_infos
            time.sleep(0.1)
        
        return False, f"断开请求超时 ({timeout}s)"
    
    def _call_mqtt_command(self, command: int):
        """通过 MQTT 发送控制命令"""
        request = {"command": command}
        self.mqtt_client.publish("charger/ble/command", json.dumps(request))
        self.get_logger().info(f"已发布命令: {command}")
    
    # ==================== ROS2 服务回调 ====================
    
    def handle_connect_bluetooth(self, request, response):
        """处理 /connect_bluetooth 服务请求"""
        self.get_logger().info(f"收到 ROS2 连接请求，MAC: {request.mac}")
        
        start_time = time.time()
        success, result = self._call_mqtt_connect(request.mac)
        
        response.success = success
        response.result = result
        response.connection_time = round(time.time() - start_time, 1)
        
        self.get_logger().info(f"连接响应: success={success}, result={result}")
        return response
    
    def handle_disconnect_bluetooth(self, request, response):
        """处理 /disconnect_bluetooth 服务请求"""
        self.get_logger().info("收到 ROS2 断开请求")
        
        start_time = time.time()
        success, infos = self._call_mqtt_disconnect()
        
        response.success = success
        response.infos = infos
        response.cost_time = round(time.time() - start_time, 1)
        
        self.get_logger().info(f"断开响应: success={success}, infos={infos}")
        return response
    
    # ==================== ROS2 订阅回调 ====================
    
    def handle_bluetooth_command(self, msg: BluetoothCommand):
        """处理 /bluetooth_command 订阅消息，转换为 MQTT 命令"""
        # 映射 ROS2 命令到 MQTT 命令值
        # 假设映射关系：CHARGER_START=0, CHARGER_STOP=1, WATER_START=2, WATER_STOP=3
        command_map = {
            BluetoothCommand.CHARGER_START: 0,
            BluetoothCommand.CHARGER_STOP: 1,
            BluetoothCommand.WATER_START: 2,
            BluetoothCommand.WATER_STOP: 3
        }
        
        command_value = command_map.get(msg.command)
        if command_value is not None:
            self._call_mqtt_command(command_value)
            self.get_logger().info(f"已转发命令: {msg.command} -> {command_value}")
        else:
            self.get_logger().warn(f"未知命令: {msg.command}")
    
    # ==================== MQTT 连接和启动 ====================
    
    def start_mqtt(self) -> bool:
        """启动 MQTT 客户端"""
        self.mqtt_client = mqtt.Client()
        self.mqtt_client.on_connect = self.on_connect
        self.mqtt_client.on_message = self.on_message
        
        try:
            self.mqtt_client.connect(self.mqtt_host, self.mqtt_port, 60)
            self.mqtt_client.loop_start()
            return True
        except Exception as e:
            self.get_logger().error(f"MQTT 连接失败: {e}")
            return False
    
    def stop_mqtt(self):
        """停止 MQTT 客户端"""
        if self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
    
    def destroy_node(self):
        """销毁节点"""
        self.stop_mqtt()
        super().destroy_node()


def main(args=None):
    import argparse
    
    parser = argparse.ArgumentParser(description='MQTT to ROS2 Bridge')
    parser.add_argument('--host', default='localhost', help='MQTT broker 主机地址')
    parser.add_argument('--port', type=int, default=1883, help='MQTT broker 端口号')
    cmd_args, unknown = parser.parse_known_args()
    
    rclpy.init(args=args)
    
    node = MQTTROS2Bridge(mqtt_host=cmd_args.host, mqtt_port=cmd_args.port)
    
    # 创建服务
    connect_service = node.create_service(
        ConnectBluetooth, 
        '/connect_bluetooth', 
        node.handle_connect_bluetooth,
        callback_group=ReentrantCallbackGroup()
    )
    disconnect_service = node.create_service(
        DisconnectBluetooth, 
        '/disconnect_bluetooth', 
        node.handle_disconnect_bluetooth,
        callback_group=MutuallyExclusiveCallbackGroup()
    )
    
    # 创建订阅
    command_sub = node.create_subscription(
        BluetoothCommand,
        '/bluetooth_command',
        node.handle_bluetooth_command,
        5,
        callback_group=ReentrantCallbackGroup()
    )
    
    node.get_logger().info("MQTT-ROS2 Bridge 服务已启动")
    node.get_logger().info(f"MQTT Broker: {cmd_args.host}:{cmd_args.port}")
    node.get_logger().info("ROS2 服务: /connect_bluetooth, /disconnect_bluetooth")
    node.get_logger().info("ROS2 订阅: /bluetooth_command")
    node.get_logger().info("ROS2 发布: /charger/state2")
    
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        node.get_logger().info("收到中断信号")
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
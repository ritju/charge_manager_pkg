import rclpy
from rclpy.node import Node
import time
import os
import threading
import crcmod.predefined
from charge_manager_msgs.srv import ConnectBluetooth, DisconnectBluetooth, ChargeCommand
from charge_manager_msgs.msg import ChargeState2
from charge_manager_msgs.msg import BluetoothCommand
from rclpy.qos import DurabilityPolicy,ReliabilityPolicy,QoSProfile,HistoryPolicy
import asyncio
from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError
import subprocess
import psutil
from signal import SIGINT, SIGTERM
from rclpy.callback_groups import ReentrantCallbackGroup, MutuallyExclusiveCallbackGroup
from rclpy.executors import MultiThreadedExecutor
import fcntl

import re

from charge_manager.utils import parse_fault, calculate_dis

class BluetoothChargeServer(Node):
    def __init__(self, name):
        # 定义外部映射表
        self.fault_map = {
            0x01: "无法关闭加水电磁阀",
            0x02: "一直处于手动加水状态",
            0x04: "无法关闭充电",
            0x08: "左距离传感器不在线（有延时）",
            0x10: "右距离传感器不在线（有延时）",
            0x20: "距离传感器到位，行程开关不到位，可能存在行程开关故障/不在线",
            0x40: "行程开关到位，但距离传感器没到位"
        }
        self.switch_stu_map = {
            0x00: "行程到位",
            0x01: "行程未到位",
        }
        
        super().__init__(name)
        env_var = os.environ.get('DOCK_USE_BLUETOOTH_RESTORE_SERVICE', 'False')
        self.declare_parameter("use_bluetooth_restore_service", env_var)
        self.use_bluetooth_restore_service = self.get_parameter("use_bluetooth_restore_service").get_parameter_value().string_value.strip().lower()
        if self.use_bluetooth_restore_service in ('true', 'yes', 'on', '1', 't', 'y', 'enabled'):
            self.get_logger().info('use_bluetooth_restore_service: True')
            self.use_bluetooth_restore_service = True
        else:
            self.get_logger().info('use_bluetooth_restore_service: False')
            self.use_bluetooth_restore_service = False
        
        env_var = os.environ.get('DOCK_USE_BLUETOOTH_PROTOCOL_NEW', 'False')
        self.declare_parameter("use_bluetooth_protocol_new", env_var)
        self.use_bluetooth_protocol_new = self.get_parameter("use_bluetooth_protocol_new").get_parameter_value().string_value.strip().lower()
        if self.use_bluetooth_protocol_new in ('true', 'yes', 'on', '1', 't', 'y', 'enabled'):
            self.get_logger().info('use_bluetooth_protocol_new: True')
            self.use_bluetooth_protocol_new = True
        else:
            self.get_logger().info('use_bluetooth_protocol_new: False')
            self.use_bluetooth_protocol_new = False

        env_var = os.environ.get('CHARACTER_FILTER', None)
        self.declare_parameter("character_filter", env_var)
        self.character_filter = self.get_parameter("character_filter").get_parameter_value().string_value.strip().lower()

        self.bluetooth_connected = False
        self.uuid_notify = None
        self.uuid_write = None
        self.send_data = None
        self.send_heartbeat_data = ['6b', '00', '00', '00', '00', '6b', '00', '00', '00', '21', '09', '00']
        self.heartbeat_time = 0
        self.data_received_time = 0
        self.disconnect_bluetooth = False
        self.bluetooth_found = False        

        self.bluetooth_concact_server = self.create_service(ConnectBluetooth, '/connect_bluetooth', self.connect_bluetooth, callback_group=ReentrantCallbackGroup())
        self.bluetooth_disconnect_server = self.create_service(DisconnectBluetooth, '/disconnect_bluetooth', self.disconnect_bluetooth_callback, callback_group=MutuallyExclusiveCallbackGroup())
        self.charge_command_server = self.create_service(ChargeCommand, '/charge_command', self.charge_command_callback, callback_group=ReentrantCallbackGroup())
        
        charger_state_qos = QoSProfile(depth=1)
        charger_state_qos.reliability = ReliabilityPolicy.BEST_EFFORT
        charger_state_qos.history = HistoryPolicy.KEEP_LAST
        charger_state_qos.durability = DurabilityPolicy.VOLATILE

        self.charge_state = ChargeState2()
        self.data_fields_last = []
        self.charge_state.pid = ""
        self.charge_state.has_contact = False
        self.charge_state.is_charging = False
        self.charge_state.is_waterflooding = False
        self.charge_state.water_mode = "unknown"
        self.charge_state.manual_enable_stu = False
        self.charge_state.fault_stu = ""
        self.charge_state.left_dis_sensor = -1
        self.charge_state.right_dis_sensor = -1
        self.charge_state.switch_stu = ""
        self.contact_state_last_ = False

        self.charge_state_publisher = self.create_publisher(ChargeState2, '/charger/state2', charger_state_qos, callback_group=ReentrantCallbackGroup())
        self.publish_rate = self.create_rate(5)
        # /charge_command service replaces the old /bluetooth_command topic subscription
        self.udp_data = None

        # 并发控制
        self._connect_lock = threading.Lock()
        self._ble_task = None
        self._client = None
        self._client_lock = threading.Lock()
        self._shutdown_event = threading.Event()

        # 单一事件循环
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self._run_event_loop, daemon=True)
        self.loop_thread.start()

        self.charge_state_publish_thread = threading.Thread(target=self.charge_state_pub, daemon=True)
        self.charge_state_publish_thread.start()

        self.bluetooth_adapter = "hci0"

        self.get_logger().info("Bluetooth charge Server starting")

    def _run_event_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def disconnect_bluetooth_callback(self, request, response):
        start_time = time.time()
        self.get_logger().info('received a request for /disconnect_bluetooth')

        if not self._connect_lock.acquire(blocking=False):
            response.success = False
            response.infos = "Another operation in progress"
            response.cost_time = round(time.time() - start_time, 1)
            self.get_logger().info("when disconnecting from Bluetooth, another operation is in progress")
            return response
        
        try:
            # 1. 设置标志，防止协程继续发送数据
            self.disconnect_bluetooth = True
            
            # 2. 主动断开正在运行的 BLE 任务
            with self._client_lock:
                client = self._client
            if client is not None and client.is_connected:
                try:
                    # 在事件循环中执行断开
                    future = asyncio.run_coroutine_threadsafe(client.disconnect(), self.loop)
                    future.result(timeout=3.0)  # 等待断开完成
                except Exception as e:
                    self.get_logger().info(f'主动断开异常: {e}')
            
            # 3. 取消正在进行的连接/通信协程
            if self._ble_task and not self._ble_task.done():
                self._ble_task.cancel()
                try:
                    self._ble_task.result(timeout=2.0)
                except Exception as e:
                    self.get_logger().info(f'取消正在进行的连接/通信协程异常: {e}')
            
            # 4. 强制清空状态（避免残留）
            self.charge_state.pid = ''
            self.bluetooth_connected = False
            self.heartbeat_time = 0
            
            response.success = True
            response.infos = '断开蓝牙连接成功。'
            response.cost_time = round(time.time() - start_time, 1)
            self.get_logger().info(f'断开蓝牙连接成功。耗时: {response.cost_time}s')
            return response
        finally:
            self._connect_lock.release()

    def terminate(self, proc: subprocess.Popen):
        parent_pid = proc.pid 
        parent = psutil.Process(parent_pid)
        index = 1
        self.get_logger().info(f'parent\'childeren num: {len(parent.children(recursive=True))}')
        for child in parent.children(recursive=True):
            self.get_logger().info(f'child_{index}\'s children num: {len(child.children(recursive=True))}')
            self.get_logger().info(f'Terminating child {index}, pid: {child.pid} ......')
            child.send_signal(SIGINT)
            rt_code = child.wait(2)
            if rt_code is None:
                self.get_logger().info(f'Terminate child {index} (pid: {child.pid}) failed.')
                cmd = f'/usr/bin/kill -9 {child.pid}'
                self.get_logger().info(f'execute "{cmd}" for kill child process.')
                os.system(cmd)
            else:
                self.get_logger().info(f'Terminate child {index} (pid: {child.pid}) success. rt_code: {rt_code}')            
            index += 1

        parent.send_signal(SIGINT)
        rt_code = parent.wait(2)
        if rt_code is None:
            self.get_logger().info(f'Terminate parent (pid: {parent.pid}) failed.')
        else:
            self.get_logger().info(f'Terminate parent (pid: {parent.pid}) success. rt_code: {rt_code}')

    def charge_state_pub(self):
        self.get_logger().info(f'charger_state_pub thread => Process: {os.getpid()}, Thread: {threading.get_ident()}')
        while not self._shutdown_event.is_set():
            if not rclpy.ok():
                self.get_logger().info('rclpy\'s context is invalid, exiting...')
                break

            with self._client_lock:
                client = self._client
            
            if self.data_fields_last:
                if client is None or not client.is_connected:
                    self.get_logger().info('BLE client is not connected, resetting charge state...')
                    if client is None:
                        self.get_logger().info('BLE client is None')
                    elif not client.is_connected:
                        self.get_logger().info('BLE client is not connected')
                        
                    self.data_fields_last = []
                    self.charge_state.pid = ''
                    self.charge_state.has_contact = False
                    self.charge_state.is_charging = False
                    self.charge_state.is_waterflooding = False
                    self.charge_state.water_mode = "unknown"
                    self.charge_state.manual_enable_stu = False
                    self.charge_state.fault_stu = ""
                    self.charge_state.left_dis_sensor = -1
                    self.charge_state.right_dis_sensor = -1
                    self.charge_state.switch_stu = ""

            self.charge_state_publisher.publish(self.charge_state)

            if self.contact_state_last_ != self.charge_state.has_contact:
                self.get_logger().info(f"bluetooth => contact state change from {str(self.contact_state_last_)} to {str(self.charge_state.has_contact)}")
                self.contact_state_last_ = self.charge_state.has_contact

            if (self.bluetooth_connected and self.data_received_time > 0 
                    and time.time() - self.data_received_time > 20):
                self.get_logger().info("No data received more than 20 seconds.")
                self.data_fields_last = []
                self.charge_state.pid = ''
                self.charge_state.has_contact = False
                self.charge_state.is_charging = False
                self.charge_state.is_waterflooding = False
                self.charge_state.water_mode = "unknown"
                self.charge_state.manual_enable_stu = False
                self.charge_state.fault_stu = ""
                self.charge_state.left_dis_sensor = -1
                self.charge_state.right_dis_sensor = -1
                self.charge_state.switch_stu = ""
                self.disconnect_bluetooth = True
                self.bluetooth_connected = False
                self.heartbeat_time = 0

            self.publish_rate.sleep()

    def charge_command_callback(self, request, response):
        """Service callback for /charge_command - returns structured error codes."""
        try:
            if request.command == BluetoothCommand.CHARGER_START:
                # Pre-check: bluetooth not connected
                if self.charge_state.pid == '':
                    self.get_logger().info('/charge_command CHARGER_START: bluetooth not found (pid is empty)')
                    response.success = False
                    response.code = 30
                    response.message = 'bluetooth not found'
                    return response
                # Pre-check: already charging
                if self.charge_state.is_charging:
                    self.get_logger().info('/charge_command CHARGER_START: already charging')
                    response.success = True
                    response.code = 0
                    response.message = 'success'
                    return response
                # Pre-check: no contact and not charging
                if not self.charge_state.has_contact:
                    self.get_logger().info('/charge_command CHARGER_START: bluetooth no data (no contact)')
                    response.success = False
                    response.code = 32
                    response.message = 'bluetooth no data'
                    return response
                # Send BLE charge start command (reuse existing logic)
                time.sleep(0.5)
                self.get_logger().info('/charge_command CHARGER_START: sending BLE command')
                send_d = self.send_heartbeat_data.copy()
                if self.use_bluetooth_protocol_new:
                    send_d[8] = '80'
                    send_d[9] = '00'
                    send_d[10] = '03'
                    send_d[11] = '00'
                    send_d.append('01')  # 开启充电
                    if self.charge_state.is_waterflooding:
                        send_d.append('01')
                    else:
                        send_d.append('00')
                    if self.charge_state.manual_enable_stu:
                        send_d.append('01')
                    else:
                        send_d.append('00')
                    send_d.append(self.crc8(send_d))
                    send_d.append('16')
                else:
                    send_d[8] = '80'
                    send_d[9] = '00'
                    send_d[10] = '02'
                    send_d[11] = '00'
                    send_d.append('02')  # 开启充电
                    send_d.append('00')
                    send_d.append(self.crc8(send_d))
                    send_d.append('16')
                self.send_data = bytes.fromhex(''.join(send_d))
                # Wait for confirmation with timeout
                t1 = time.time()
                while True:
                    if self.charge_state.is_charging:
                        self.get_logger().info('/charge_command CHARGER_START: success')
                        response.success = True
                        response.code = 0
                        response.message = 'success'
                        return response
                    elif time.time() - t1 > 10:
                        self.get_logger().info('/charge_command CHARGER_START: timeout response')
                        response.success = False
                        response.code = 13
                        response.message = 'timeout response'
                        return response
                    else:
                        time.sleep(1)

            elif request.command == BluetoothCommand.CHARGER_STOP:
                # Pre-check: bluetooth not connected
                if self.charge_state.pid == '':
                    self.get_logger().info('/charge_command CHARGER_STOP: bluetooth not found')
                    response.success = False
                    response.code = 30
                    response.message = 'bluetooth not found'
                    return response
                # Pre-check: not charging
                if not self.charge_state.is_charging:
                    self.get_logger().info('/charge_command CHARGER_STOP: not charging, already stopped')
                    response.success = True
                    response.code = 0
                    response.message = 'success'
                    return response
                # Send BLE charge stop command
                self.get_logger().info('/charge_command CHARGER_STOP: sending BLE command')
                send_d = self.send_heartbeat_data.copy()
                if self.use_bluetooth_protocol_new:
                    send_d[8] = '80'
                    send_d[9] = '00'
                    send_d[10] = '03'
                    send_d[11] = '00'
                    send_d.append('00')  # 关闭充电
                    if self.charge_state.is_waterflooding:
                        send_d.append('01')
                    else:
                        send_d.append('00')
                    if self.charge_state.manual_enable_stu:
                        send_d.append('01')
                    else:
                        send_d.append('00')
                    send_d.append(self.crc8(send_d))
                    send_d.append('16')
                else:
                    send_d[8] = '80'
                    send_d[9] = '00'
                    send_d[10] = '02'
                    send_d[11] = '00'
                    send_d.append('01')  # 关闭充电
                    send_d.append('00')
                    send_d.append(self.crc8(send_d))
                    send_d.append('16')
                self.send_data = bytes.fromhex(''.join(send_d))
                t1 = time.time()
                while True:
                    if not self.charge_state.is_charging:
                        self.get_logger().info('/charge_command CHARGER_STOP: success')
                        response.success = True
                        response.code = 0
                        response.message = 'success'
                        return response
                    elif time.time() - t1 > 10:
                        self.get_logger().info('/charge_command CHARGER_STOP: timeout response')
                        response.success = False
                        response.code = 13
                        response.message = 'timeout response'
                        return response
                    else:
                        time.sleep(1)
            elif request.command == BluetoothCommand.WATER_START:
                if self.charge_state.pid == '':
                    response.success = False
                    response.code = 30
                    response.message = 'bluetooth not found'
                    return response
                if not self.charge_state.has_contact:
                    response.success = False
                    response.code = 32
                    response.message = 'bluetooth no data'
                    return response
                if self.charge_state.is_waterflooding:
                    response.success = True
                    response.code = 0
                    response.message = 'success'
                    return response
                send_d = self.send_heartbeat_data.copy()
                if self.use_bluetooth_protocol_new:
                    send_d[8] = '80'; send_d[9] = '00'; send_d[10] = '03'; send_d[11] = '00'
                    send_d.append('01' if self.charge_state.is_charging else '00')
                    send_d.append('01')
                    send_d.append('01' if self.charge_state.manual_enable_stu else '00')
                    send_d.append(self.crc8(send_d)); send_d.append('16')
                else:
                    send_d[8] = '80'; send_d[9] = '00'; send_d[10] = '02'; send_d[11] = '00'
                    send_d.append('00'); send_d.append('01')
                    send_d.append(self.crc8(send_d)); send_d.append('16')
                self.send_data = bytes.fromhex(''.join(send_d))
                t1 = time.time()
                while True:
                    if self.charge_state.is_waterflooding:
                        response.success = True; response.code = 0; response.message = 'success'
                        return response
                    elif time.time() - t1 > 10:
                        response.success = False; response.code = 13; response.message = 'timeout response'
                        return response
                    else: time.sleep(1)

            elif request.command == BluetoothCommand.WATER_STOP:
                if self.charge_state.pid == '':
                    response.success = False; response.code = 30; response.message = 'bluetooth not found'
                    return response
                if not self.charge_state.is_waterflooding:
                    response.success = True; response.code = 0; response.message = 'success'
                    return response
                send_d = self.send_heartbeat_data.copy()
                if self.use_bluetooth_protocol_new:
                    send_d[8] = '80'; send_d[9] = '00'; send_d[10] = '03'; send_d[11] = '00'
                    send_d.append('01' if self.charge_state.is_charging else '00')
                    send_d.append('00')
                    send_d.append('01' if self.charge_state.manual_enable_stu else '00')
                    send_d.append(self.crc8(send_d)); send_d.append('16')
                else:
                    send_d[8] = '80'; send_d[9] = '00'; send_d[10] = '02'; send_d[11] = '00'
                    send_d.append('00'); send_d.append('02')
                    send_d.append(self.crc8(send_d)); send_d.append('16')
                self.send_data = bytes.fromhex(''.join(send_d))
                t1 = time.time()
                while True:
                    if not self.charge_state.is_waterflooding:
                        response.success = True; response.code = 0; response.message = 'success'
                        return response
                    elif time.time() - t1 > 10:
                        response.success = False; response.code = 13; response.message = 'timeout response'
                        return response
                    else: time.sleep(1)

            elif request.command == BluetoothCommand.ENABLE_MANUAL_ADD_WATER:
                if self.charge_state.pid == '':
                    response.success = False; response.code = 30; response.message = 'bluetooth not found'
                    return response
                if not self.charge_state.has_contact:
                    response.success = False; response.code = 32; response.message = 'bluetooth no data'
                    return response
                if self.charge_state.manual_enable_stu:
                    response.success = True; response.code = 0; response.message = 'success'
                    return response
                if not self.use_bluetooth_protocol_new:
                    response.success = False; response.code = 40; response.message = 'old protocol not supported'
                    return response
                send_d = self.send_heartbeat_data.copy()
                send_d[8] = '80'; send_d[9] = '00'; send_d[10] = '03'; send_d[11] = '00'
                send_d.append('01' if self.charge_state.is_charging else '00')
                send_d.append('01' if self.charge_state.is_waterflooding else '00')
                send_d.append('01')
                send_d.append(self.crc8(send_d)); send_d.append('16')
                self.send_data = bytes.fromhex(''.join(send_d))
                t1 = time.time()
                while True:
                    if self.charge_state.manual_enable_stu:
                        response.success = True; response.code = 0; response.message = 'success'
                        return response
                    elif time.time() - t1 > 10:
                        response.success = False; response.code = 13; response.message = 'timeout response'
                        return response
                    else: time.sleep(1)

            elif request.command == BluetoothCommand.DISABLE_MANUAL_ADD_WATER:
                if self.charge_state.pid == '':
                    response.success = False; response.code = 30; response.message = 'bluetooth not found'
                    return response
                if not self.charge_state.manual_enable_stu:
                    response.success = True; response.code = 0; response.message = 'success'
                    return response
                if not self.use_bluetooth_protocol_new:
                    response.success = False; response.code = 40; response.message = 'old protocol not supported'
                    return response
                send_d = self.send_heartbeat_data.copy()
                send_d[8] = '80'; send_d[9] = '00'; send_d[10] = '03'; send_d[11] = '00'
                send_d.append('01' if self.charge_state.is_charging else '00')
                send_d.append('01' if self.charge_state.is_waterflooding else '00')
                send_d.append('00')
                send_d.append(self.crc8(send_d)); send_d.append('16')
                self.send_data = bytes.fromhex(''.join(send_d))
                t1 = time.time()
                while True:
                    if not self.charge_state.manual_enable_stu:
                        response.success = True; response.code = 0; response.message = 'success'
                        return response
                    elif time.time() - t1 > 10:
                        response.success = False; response.code = 13; response.message = 'timeout response'
                        return response
                    else: time.sleep(1)

            else:
                self.get_logger().info(f'/charge_command: unsupported command {request.command}')
                response.success = False
                response.code = 40
                response.message = f'unsupported command: {request.command}'
                return response
        except Exception as e:
            self.get_logger().info(f'/charge_command: exception {e}')
            response.success = False
            response.code = 40
            response.message = f'unknown error: {e}'
            return response

    def wait_and_read(self, file_path, max_attempts=10, interval=1):
        attempts = 0
        while attempts < max_attempts:
            try:
                with open(file_path, 'r') as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    content = f.read()
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    self.get_logger().info(f'content of {file_path}: {content}')
                    if content == "":
                        self.get_logger().info(f'content is empty')
                        time.sleep(interval)
                        attempts += 1
                    else:
                        return content
            except (IOError, BlockingIOError):
                time.sleep(interval)
                attempts += 1
                self.get_logger().info(f'file {file_path} is busing, just wait {interval} second ......')
        return None

    def connect_bluetooth(self, request, response):
        self.get_logger().info(f"Received a request for connect bluetooth, mac: {request.mac}")

        if not self._connect_lock.acquire(blocking=False):
            response.success = False
            response.result = "Another operation in progress"
            self.get_logger().info("When connecting to Bluetooth, another operation is in progress")
            response.connection_time = 0.0
            return response

        try:
            if self.charge_state.pid == request.mac and self.bluetooth_connected:
                self.get_logger().info(f'Already connected to {request.mac}, skip reconnection.')
                response.success = True
                response.connection_time = 0.0
                response.result = f"Already connected to {request.mac}"
                self.get_logger().info(f"Already connected to {request.mac}")
                return response

            if self._ble_task is not None and not self._ble_task.done():
                self.get_logger().info('Another BLE task is still in progress, cancelling it...')
                self._ble_task.cancel()
                try:
                    self._ble_task.result(timeout=2.0)
                except:
                    pass
                with self._client_lock:
                    if self._client is not None:
                        self.get_logger().info('Force disconnecting previous client...')
                        try:
                            future_disconnect = asyncio.run_coroutine_threadsafe(
                                self._client.disconnect(), self.loop
                            )
                            future_disconnect.result(timeout=2.0)
                        except Exception as e:
                            self.get_logger().info(f'Force disconnect error: {e}')
                        finally:
                            self._client = None
                self.bluetooth_connected = False
                self.charge_state.pid = ''
                self.disconnect_bluetooth = False
                time.sleep(1)

            if self.charge_state.pid != '' and self.charge_state.pid != request.mac:
                self.get_logger().info(f'Disconnecting from {self.charge_state.pid} before connecting to {request.mac}')
                self.disconnect_bluetooth = True
                wait_start = time.time()
                while self.charge_state.pid != '' and (time.time() - wait_start) < 5.0:
                    time.sleep(0.1)
                if self.charge_state.pid != '':
                    self.get_logger().warn(f'Failed to disconnect, force clearing state')
                    self.charge_state.pid = ''
                    self.bluetooth_connected = False

            restore = 0
            if self.use_bluetooth_restore_service:
                content = self.wait_and_read('/map/bluetooth_restore.txt')
                if content:
                    try:
                        restore = int(content.strip())
                    except:
                        pass
            time_wait = time.time()
            while self.use_bluetooth_restore_service and restore and (time.time() - time_wait) < 45.0:
                self.get_logger().info("Waiting for bluetooth restoring ......")
                time.sleep(2)
                content = self.wait_and_read('/map/bluetooth_restore.txt')
                if content:
                    try:
                        restore = int(content.strip())
                    except:
                        pass

            self.get_logger().info("正在重连蓝牙...")
            self.heartbeat_time = 0
            self.data_received_time = 0
            self.connect_start_time = time.time()
            self.connect_exception = ""
            self.data_fields_last = []
            self.charge_state.pid = ''
            self.charge_state.has_contact = False
            self.charge_state.is_charging = False
            self.charge_state.is_waterflooding = False
            self.charge_state.water_mode = "unknown"
            self.charge_state.manual_enable_stu = False
            self.charge_state.fault_stu = ""
            self.charge_state.left_dis_sensor = -1
            self.charge_state.right_dis_sensor = -1
            self.charge_state.switch_stu = ""
            self.bluetooth_connected = None
            self.disconnect_bluetooth = False

            future = asyncio.run_coroutine_threadsafe(
                self.create_bleakclient(request.mac),
                self.loop
            )
            self._ble_task = future

            def done_callback(fut):
                try:
                    fut.result()
                except Exception as e:
                    self.get_logger().info(f'BLE协程异常: {e}')
            future.add_done_callback(done_callback)

            start_time = time.time()
            while True:
                if self.bluetooth_connected is not None:
                    break
                elif time.time() - start_time > 35:
                    self.get_logger().info(f"连接蓝牙超时: {request.mac} ......")
                    self.bluetooth_connected = False
                    self.disconnect_bluetooth = True
                    break
                else:
                    self.get_logger().info(f"等待蓝牙连接: {request.mac} ......", throttle_duration_sec=1)
                    time.sleep(0.1)

            if self.bluetooth_connected:
                self.get_logger().info('蓝牙连接成功.')
                response.success = True
                response.connection_time = round(time.time() - self.connect_start_time, 1)
                response.result = f"蓝牙连接成功 {self.connect_exception}"
                self.data_received_time = time.time()
                self._write_restore_file('0')
            else:
                self.get_logger().info('蓝牙连接失败.')
                response.success = False
                response.connection_time = round(time.time() - self.connect_start_time, 1)
                response.result = f"蓝牙连接失败  {self.connect_exception}"
                self._write_restore_file('1')
            return response
        finally:
            self._connect_lock.release()

    def get_bluetooth_adapter_simple(self):
        """简单获取第一个 hci 设备"""
        try:
            result = subprocess.run(
                ['hciconfig'], 
                capture_output=True, 
                text=True
            )
            # 匹配 hci0: 或 hci1: 等
            match = re.search(r'(hci\d+):', result.stdout)
            if match:
                adapter = match.group(1)
                self.get_logger().info(f"检测到蓝牙适配器: {adapter}")
                return adapter
        except Exception as e:
            self.get_logger().info(f"获取适配器失败: {e}")
        return "hci0"

    async def create_bleakclient(self, address):
        client = None

        self.get_logger().info("获取蓝牙设备名字")
        self.bluetooth_adapter = self.get_bluetooth_adapter_simple()

        try:
            self.get_logger().info("搜索附近的蓝牙......")
            devices = await BleakScanner(scanning_mode='active').discover(return_adv=True, timeout=5.0)
            devices_num = len(devices)
            self.get_logger().info(f'共搜索到 {devices_num} 个蓝牙信号。')
            self.bluetooth_found = False
            ble_device = None
            if devices_num > 0:
                self.get_logger().info('--------Mac-------- | --------Name-------')
                for key in devices:
                    self.get_logger().info(f'{key}   | {devices[key][1].local_name}')
                    if key == address:
                        self.bluetooth_found = True
                        ble_device = devices[key][0]

            if self.bluetooth_found:
                self.get_logger().info(f'搜索到mac: {address}')
                self.get_logger().info(f'address: {ble_device.address}')
                self.get_logger().info(f'name: {ble_device.name}')
                self.get_logger().info(f'rssi: {devices[address][1].rssi}')
                client = BleakClient(ble_device, adapter=self.bluetooth_adapter)
            else:
                self.get_logger().info(f'未搜索到mac: {address}，尝试直接连接')
                client = BleakClient(address, adapter=self.bluetooth_adapter)

            with self._client_lock:
                self._client = client

            await client.connect()

            self.uuid_write = None
            self.uuid_notify = None
            services = client.services
            for service in services:
                for character in service.characteristics:
                    # 获取发送数据的蓝牙服务uuid
                    if character.properties == ['write-without-response', 'write']:
                        if self.character_filter:
                            if character.uuid.startswith(self.character_filter) and self.uuid_write is None:
                                self.uuid_write = character.uuid
                                self.get_logger().info(f"uuid_write: {self.uuid_write}, properties: {character.properties}")
                        else:
                            self.uuid_write = character.uuid
                            self.get_logger().info(f"uuid_write: {self.uuid_write}, properties: {character.properties}")
                    # 获取接收数据的蓝牙服务uuid
                    elif character.properties == ['read', 'notify']:
                        if self.character_filter:
                            if character.uuid.startswith(self.character_filter) and self.uuid_notify is None:
                                self.uuid_notify = character.uuid 
                                self.get_logger().info(f"uuid_notify: {self.uuid_notify}, properties: {character.properties}")
                        else:
                            self.uuid_notify = character.uuid 
                            self.get_logger().info(f"uuid_notify: {self.uuid_notify}, properties: {character.properties}")
                    else:
                        continue

            if self.uuid_write is None or self.uuid_notify is None:
                raise Exception("未找到需要的 write 或 notify 特征")

            # await client.start_notify(self.uuid_notify, self.notify_data)
            await client.start_notify(self.uuid_notify, self.notify_data)
            self.get_logger().info("start_notify")

            # 连接成功并启动通知后再标记状态
            self.charge_state.pid = address
            self.bluetooth_connected = True

            while True:
                if not rclpy.ok():
                    self.get_logger().info('rclpy context invalid, exiting BLE loop')
                    break
                if not client.is_connected:
                    self.get_logger().info('BLE disconnected unexpectedly')
                    break
                if self.disconnect_bluetooth:
                    self.get_logger().info('收到主动断开请求')
                    break

                if self.send_data is not None:
                    await client.write_gatt_char(self.uuid_write, self.send_data, response=False)
                    self.send_data = None

                current_time = time.time()
                if current_time - self.heartbeat_time > 0.5:
                    send_d = self.send_heartbeat_data.copy()
                    send_d[8] = '80'
                    send_d[9] = '21'
                    send_d[10] = '01'
                    send_d[11] = '00'
                    send_d.append('00')
                    send_d.append(self.crc8(send_d))
                    send_d.append('16')
                    heart_bytes = bytes.fromhex(''.join(send_d))
                    await client.write_gatt_char(self.uuid_write, heart_bytes, response=False)
                    self.udp_data = None
                    self.heartbeat_time = current_time

                await asyncio.sleep(0.5)

        except Exception as e:
            self.get_logger().info(f'BLE 连接/通信异常: {str(e)}')
            self.connect_exception = str(e)
        finally:
            self.bluetooth_connected = False
            self.charge_state.pid = ""
            with self._client_lock:
                self._client = None
            if client is not None:
                try:
                    # if client.is_connected:
                    await client.disconnect()
                except Exception as e:
                    self.get_logger().info(f'断开连接时异常: {e}')
            self.get_logger().info('BLE 连接已关闭')

    def notify_data(self, sender, data):
        self.data_received_time = time.time()
        data_list = ['{:02x}'.format(x) for x in data]
        self.get_logger().info(f'解析后的数据为： {data_list}', throttle_duration_sec=10)
        if len(data_list) < 10:
            self.get_logger().info(f'data is too short: {data_list}')
            return
        crc8_ = self.crc8(data_list[:-2])
        if crc8_ == data_list[-2].upper():
            self.udp_data = data_list
            if data_list[8:10] == ['00', '21']:
                try:
                    data_length = calculate_dis(data_list[10], data_list[11])
                    data_fields = data_list[12:12+data_length]
                    data_fields_save = data_list[12:21]
                    if data_fields_save != self.data_fields_last:
                        self.get_logger().info(f'数据域发生变化')
                        self.get_logger().info(f'上一次数据域内容是: {self.data_fields_last}')
                        self.get_logger().info(f'这一次数据域内容是: {data_fields_save}')
                        self.data_fields_last = data_fields_save
                    if self.use_bluetooth_protocol_new:                        
                        self.charge_state.is_charging = (data_fields[0] == '01')
                        self.charge_state.has_contact = (data_fields[5] == '01')
                        # 00 未加水， 01 自动加水， 02 手动加水
                        self.charge_state.is_waterflooding = ((data_fields[6] == '01') or (data_fields[6] == '02'))
                        if data_fields[6] == '01':
                            self.charge_state.water_mode = "auto"
                        elif data_fields[6] == '02':
                            self.charge_state.water_mode = "manual"
                        elif data_fields[6] == '00':
                            self.charge_state.water_mode = "idle"
                        else:
                            self.charge_state.water_mode = "unknown"
                        self.charge_state.manual_enable_stu = (data_fields[7] == '01')
                        self.charge_state.fault_stu = parse_fault(data_fields[8], self.fault_map, "无故障")
                        self.charge_state.left_dis_sensor = calculate_dis(data_fields[9], data_fields[10])
                        self.charge_state.right_dis_sensor = calculate_dis(data_fields[11], data_fields[12])
                        switch_stu_value = int(data_fields[13], 16)                         
                        self.charge_state.switch_stu = self.switch_stu_map.get(switch_stu_value, "未知错误")
                    else:
                        self.charge_state.is_charging = (data_list[12] == '01')
                        self.charge_state.has_contact = (data_list[17] == '01')
                        self.charge_state.is_waterflooding = (data_list[19] == '01')
                        self.charge_state.water_mode = "manual" if data_list[18] == '01' else "auto"
                except IndexError:
                    pass
        else:
            self.get_logger().debug('CRC 校验失败')

    def crc8(self, data):
        crc8 = crcmod.predefined.Crc('crc-8-maxim')
        hex_str = ' '.join(data)
        crc8.update(bytes.fromhex(hex_str))
        crc8_value = hex(~crc8.crcValue & 0xff)[2:].upper()
        return crc8_value.zfill(2)

    def _write_restore_file(self, value):
        try:
            with open('/map/bluetooth_restore.txt', 'w') as f:
                f.write(value + '\n')
        except Exception as e:
            self.get_logger().info(f'写入 restore 文件失败: {e}')

    def __del__(self):
        self._shutdown_event.set()
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)


def main(args=None):
    rclpy.init(args=args)
    node = BluetoothChargeServer('bluetooth_charge_server')
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    try:
        executor.spin()
    finally:
        executor.shutdown()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
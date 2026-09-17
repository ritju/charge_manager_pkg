import bleak
from bleak import BleakClient, BleakScanner

import rclpy
from rclpy.node import Node
from rclpy.action import ActionServer, GoalResponse, CancelResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.action import ActionClient
from rclpy.executors import MultiThreadedExecutor
from rclpy.qos import DurabilityPolicy,ReliabilityPolicy,QoSProfile,HistoryPolicy

import time
import threading
import signal

from charge_manager_msgs.srv import ConnectBluetooth, DisconnectBluetooth, StartBluetooth, StopBluetooth
from charge_manager_msgs.action import Charge
from capella_ros_dock_msgs.action import Dock
from capella_ros_dock_msgs.msg import ChargeErrorCode
from capella_ros_dock_msgs.msg import ChargeErrorInfo
from capella_ros_msg.msg import Battery
from capella_ros_service_interfaces.msg import ChargeState, RgbCameraResolution
from std_srvs.srv import Empty as EmptyForSrv
from std_msgs.msg import Bool
from sensor_msgs.msg import Image
from geometry_msgs.msg import Twist
from capella_ros_msg.msg import Velocities
from capella_ros_msg.srv import TurnoffPcPower
from capella_ros_service_interfaces.srv import StartDetectApriltag, StopDetectApriltag, SwitchResolution

import collections
import threading

import glob
import re
from typing import List, Optional
import os
import subprocess

class ChargeActionState():
    idle = 'idle'
    stop_bluetooth_node = 'stop_bluetooth_node'
    start_bluetooth_node = 'start_bluetooth_node'
    connectbluetooth = 'connecting_bluetooth'
    disconnectbluetooth = 'disconnectbluetooth'
    docking = 'docking'
    start_charging = 'start_charging'
    charging = 'charging'

# "94:C9:60:43:BE:FD"

class ChargeAction(Node):
    
    def __init__(self):
        super().__init__('charge_action_server')
        self.get_logger().info('*** charge action ***     started.')
        self.battery_ = 0.0
        self.bluetooth_setup = False
        self.bluetooth_reboot_requested = True
        self.charger_position_bool = False
        self.bluetooth_state_stored = False
        self.core_monitor_state_stored = False
        
        self.msg_state_pub = Bool()
        self.msg_state_pub.data = False

        # 定义 callback_group 类型
        self.cb_group = ReentrantCallbackGroup()

        # 初始化 zero_cmd_vel_publisher
        self.zero_cmd_vel_publisher = self.create_publisher(Twist, '/cmd_vel', 1, callback_group=self.cb_group)
        self.msg_zero_cmd = Twist()
        self.msg_zero_cmd.linear.x = 0.0
        self.msg_zero_cmd.angular.z = 0.0

        # sub for is_undocking_state
        self.is_undocking_state = False
        self.is_undocking_state_last_time = time.time()
        self.is_undocking_state_sub_ = self.create_subscription(Bool, 'is_undocking_state', self.is_undocking_state_sub_callback, 1, callback_group=self.cb_group)


        # sub for battery
        self.battery_sub_ = self.create_subscription(Battery, 'battery', self.battery_sub_callback, 10, callback_group=self.cb_group)

        # sub for /charger_position_bool        
        charger_position_bool_qos = QoSProfile(depth=1)
        charger_position_bool_qos.reliability = ReliabilityPolicy.BEST_EFFORT
        charger_position_bool_qos.history = HistoryPolicy.KEEP_LAST
        charger_position_bool_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
        self.charger_position_bool_sub_ = self.create_subscription(Bool, '/charger_position_bool', self.charger_position_bool_sub_callback, charger_position_bool_qos, callback_group=self.cb_group)

        # sub for /charger/state
        charger_state_qos = QoSProfile(depth=1)
        charger_state_qos.reliability = ReliabilityPolicy.BEST_EFFORT
        charger_state_qos.history = HistoryPolicy.KEEP_LAST
        charger_state_qos.durability = DurabilityPolicy.VOLATILE
        self.charger_state_sub = self.create_subscription(ChargeState, '/charger/state', self.charger_state_sub_callback, charger_state_qos, callback_group=self.cb_group)

        # sub for /raw_vel ； 如果机器人充电状态下，轮子检测到速度就停止充电状态
        self.raw_vel_sub = self.create_subscription(Velocities, 'raw_vel', self.raw_vel_sub_callback, 5, callback_group=self.cb_group)

        # pub for /is_docking_state
        self.is_docking_state_pub = self.create_publisher(Bool, 'is_docking_state', 1, callback_group=self.cb_group)
        
        # 创建连接蓝牙的客户端
        self.connect_bluetooth_client_ = self.create_client(ConnectBluetooth, 'connect_bluetooth',callback_group=self.cb_group)

        # 创建断开蓝牙连接的客户端
        self.disconnect_bluetooth_client_ = self.create_client(DisconnectBluetooth, 'disconnect_bluetooth', callback_group=self.cb_group)

        # 创建启动apriltag检测的客户端
        self.start_apriltag_client_ = self.create_client(StartDetectApriltag, 'start_detect_apriltag', callback_group=self.cb_group)

        # 创建停止apriltag检测的客户端
        self.stop_apriltag_client_ = self.create_client(StopDetectApriltag, 'stop_detect_apriltag', callback_group=self.cb_group)
        
        # 创建切换 rgb_camera_back resolution 客户端
        self.switch_resolution_client_ = self.create_client(SwitchResolution, '/rgb_camera_manager_server/switch_resolution', callback_group=self.cb_group)
        
        # 创建重新上电服务客户端
        self.power_off_on_client_ = self.create_client(TurnoffPcPower, '/off_pc_power', callback_group=self.cb_group)
        
        # 创建对接充电桩的客户端
        self.dock_client_ = ActionClient(self, Dock, "dock", callback_group=self.cb_group)

        # /charger/start client
        self.charger_start_client_ = self.create_client(EmptyForSrv, '/charger/start', callback_group=self.cb_group)
        self.charger_start_client_last_request_time = time.time()

        # bluetooth start/stop client
        # self.bluetooth_start_client_ = self.create_client(StartBluetooth, '/bluetooth/start', callback_group=self.cb_group)
        # self.bluetooth_stop_client_ = self.create_client(StopBluetooth, '/bluetooth/stop', callback_group=self.cb_group)
        
        # 创建 charge action 服务端
        self.charge_action_server_ = ActionServer(self, Charge, 'charge', 
                                                  execute_callback=self.charge_action_execute_callback, 
                                                  callback_group= self.cb_group,
                                                  goal_callback=self.charge_action_goal_callback,
                                                  handle_accepted_callback=self.charge_action_handle_accepted_callback,
                                                  cancel_callback=self.charge_action_cancel_callback,
                                                  result_timeout=3600000
                                                  )

        self.charge_type = ''
        self.goal_handle = None

        # /charge/error_info: 仅在判定到不可重试错误时发布, 每个流程最多一条
        # session 由 Charge.Goal 透传(start_docking2 生成), 非 start_docking2 触发时为空串
        charge_error_qos = QoSProfile(depth=1)
        charge_error_qos.reliability = ReliabilityPolicy.RELIABLE
        charge_error_qos.history = HistoryPolicy.KEEP_LAST
        charge_error_qos.durability = DurabilityPolicy.VOLATILE
        self.charge_error_info_pub_ = self.create_publisher(ChargeErrorInfo, '/charge/error_info', charge_error_qos, callback_group=self.cb_group)

        self.init_params() 
        
        env = os.environ.get('CHARGE_ACTION_ALLOW_POWER_OFF_ON', 'False')
        env_bool = env.lower() == 'true'
        self.declare_parameter("charge_action_allow_power_off_on", env_bool)
        self.charge_action_allow_power_off_on = self.get_parameter("charge_action_allow_power_off_on").get_parameter_value().bool_value
        self.get_logger().info(f"charge_action_allow_power_off_on: {'True' if self.charge_action_allow_power_off_on else 'False'}")
        
        # 添加断电重启冷却时间相关变量
        env_interval = os.environ.get('CHARGE_ACTION_POWER_OFF_ON_INTERVAL', '1800')  # 默认1800秒
        self.declare_parameter("power_off_on_interval", float(env_interval))
        self.power_off_on_interval = self.get_parameter("power_off_on_interval").get_parameter_value().double_value
        self.last_power_off_on_time = 0.0
        self.get_logger().info(f"power_off_on_interval: {self.power_off_on_interval}")
    
        # 相机数据检查参数(可配置): 切换 HIGH 分辨率前需确认相机有数据, 否则上报 20 camera not ready
        env_camera_topic = os.environ.get('CHARGE_ACTION_CAMERA_IMAGE_TOPIC', '/rgb_camera_back/image_raw')
        self.declare_parameter("camera_image_topic", env_camera_topic)
        self.camera_image_topic = self.get_parameter("camera_image_topic").get_parameter_value().string_value
        env_camera_timeout = os.environ.get('CHARGE_ACTION_CAMERA_DATA_TIMEOUT', '5.0')
        self.declare_parameter("camera_data_timeout", float(env_camera_timeout))
        self.camera_data_timeout = self.get_parameter("camera_data_timeout").get_parameter_value().double_value
        env_camera_check = os.environ.get('CHARGE_ACTION_ENABLE_CAMERA_CHECK', 'true')
        self.declare_parameter("enable_camera_check", env_camera_check.lower() == 'true')
        self.enable_camera_check = self.get_parameter("enable_camera_check").get_parameter_value().bool_value
        self.get_logger().info(
            f'camera check: enable={self.enable_camera_check}, topic={self.camera_image_topic}, '
            f'data_timeout={self.camera_data_timeout}s')
        self.camera_last_data_time = 0.0
        camera_image_qos = QoSProfile(depth=1)
        camera_image_qos.reliability = ReliabilityPolicy.BEST_EFFORT
        camera_image_qos.history = HistoryPolicy.KEEP_LAST
        camera_image_qos.durability = DurabilityPolicy.VOLATILE
        self.camera_image_sub_ = self.create_subscription(
            Image, self.camera_image_topic, self.camera_image_sub_callback, camera_image_qos,
            callback_group=self.cb_group)
    
        # 尝试从文件读取上次断电时间
        self.load_last_power_off_time()
        
    def camera_image_sub_callback(self, msg):
        """只记录最新一帧的时间, 用于判断相机是否有数据。"""
        self.camera_last_data_time = time.time()

    def camera_has_data(self):
        """相机在 camera_data_timeout 秒内是否发布过图像。"""
        if not self.enable_camera_check:
            return True
        if self.camera_last_data_time <= 0.0:
            return False
        return (time.time() - self.camera_last_data_time) <= self.camera_data_timeout

    def load_last_power_off_time(self):
        """从文件加载上次断电重启的时间，如果文件不存在则创建并写入0"""
        try:
            file_path = '/map/last_power_off_time.txt'
            if os.path.exists(file_path):
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    if content:  # 确保文件内容不为空
                        self.last_power_off_on_time = float(content)
                    else:
                        self.last_power_off_on_time = 0.0
                self.get_logger().info(f'Loaded last power off time: {self.last_power_off_on_time}')
            else:
                # 文件不存在，创建文件并写入0
                self.get_logger().info('Power off time file not found, creating new file with initial value 0')
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write('0')
                self.last_power_off_on_time = 0.0
                self.get_logger().info('Created new power off time file with initial value 0')
        except ValueError as e:
            self.get_logger().warn(f'Invalid power off time format in file: {str(e)}, resetting to 0')
            self.last_power_off_on_time = 0.0
            # 尝试修复文件内容
            try:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write('0')
            except Exception as write_err:
                self.get_logger().warn(f'Failed to fix power off time file: {str(write_err)}')
        except Exception as e:
            self.get_logger().warn(f'Failed to load last power off time: {str(e)}')
            self.last_power_off_on_time = 0.0

    def save_last_power_off_time(self):
        """保存本次断电重启的时间到文件"""
        try:
            file_path = '/map/last_power_off_time.txt'
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(str(time.time()))
            self.last_power_off_on_time = time.time()
            self.get_logger().info(f'Saved last power off time: {self.last_power_off_on_time}')
        except Exception as e:
            self.get_logger().warn(f'Failed to save last power off time: {str(e)}')

    def can_perform_power_off_on(self):
        """检查是否可以执行断电重启操作"""
        current_time = time.time()
        time_since_last = current_time - self.last_power_off_on_time
        
        if time_since_last < self.power_off_on_interval:
            self.get_logger().info(
                f'Power off on cooling period: need to wait {self.power_off_on_interval - time_since_last:.1f} seconds. '
                f'Last power off was {time_since_last:.1f} seconds ago.'
            )
            return False
        return True
    
    def init_params(self):        
        # 初始化蓝牙相关参数
        self.mac = ''
        self.bluetooth_connected = False
        self.future_connect_bluetooth = None
        self.future_disconnect_bluetooth = None
        self.bluetooth_connected_time = 0.0

        # apriltag相关参数
        self.apriltag_detecting = False
        self.start_apriltag_detecting_executing = False
        self.stop_apriltag_detecting_executing = False
        self.future_start_apriltag = None
        self.future_stop_apriltag = None
        
        # switch_resolution 相关参数
        self.resolution_high = False
        self.switch_resolution_executing = False
        self.future_switch_resolution = None

        self.bluetooth_state_stored = False
        self.core_monitor_state_stored = False

        # 蓝牙连接和dock对接状态控制,避免执行状态中再次重复发送goal
        self.dock_executing = False
        self.connect_bluetooth_executing = False     
        
        self.power_off_on_executing = False  

        # init self.charger_state
        self.charger_state = ChargeState()

        # 初始化timer_loop
        self.timer_loop = None

        self.dock_completed = False
        self.stop_loop = False

        self.bluetooth_connect_num = 0
        self.bluetooth_connect_num_max = 99999

        # add process when dock goal is rejected
        self.dock_goal_rejected = False

        # charge action result error code tracking
        self.charge_cancelled = False
        self.dock_failed = False

        # 滑动窗口相关
        self.raw_vel_window = collections.deque()   # 存储 (timestamp, exceed_bool)
        self.raw_vel_lock = threading.Lock()

        # /charger/start_docking2 传入的额外参数
        self.request_marker = ''
        self.request_protocol = ''
        self.request_delta = None

        # 本次流程的 session(start_docking2 生成, 经 Charge.Goal 透传, 其余入口为空串)
        self.session_id = ''
        # 本次流程的首个不可重试错误码(SUCCESS 表示无错误), 同时作为 action result 的 code
        self.charge_error_code = ChargeErrorCode.SUCCESS
        # dock action 返回的细分码
        self.dock_result_code = ChargeErrorCode.SUCCESS
        # 连续失败上限: 无 hci 设备 / 蓝牙连接失败
        self.bluetooth_connect_fail_num_max = 5
        

    def publish_charge_error(self, code, message):
        """发布本次流程的首个不可重试错误码(每个流程最多一条, 同时作为 action result 的 code)。"""
        if self.charge_error_code != ChargeErrorCode.SUCCESS:
            self.get_logger().info(
                f'charge error already reported(code={self.charge_error_code}), ignore code={code}',
                throttle_duration_sec=5.0)
            return
        self.charge_error_code = code
        msg = ChargeErrorInfo()
        msg.session_id = self.session_id
        msg.code = code
        msg.message = message
        msg.source = 'charge_action'
        self.charge_error_info_pub_.publish(msg)
        self.get_logger().info(
            f'publish /charge/error_info: session={msg.session_id or "<empty>"}, code={code}, '
            f'message={message}, source=charge_action')

    def is_undocking_state_sub_callback(self, msg):
        self.is_undocking_state = msg.data
        self.is_undocking_state_last_time = time.time()
    
    def battery_sub_callback(self, msg):
        self.battery_ = msg.res_cap
    
    def charger_state_sub_callback(self, msg):
        self.charger_state = msg
        # fix bug for bluetooth_connected state out of sync(topic slower )
        if time.time() - self.bluetooth_connected_time > 1.0:
            self.bluetooth_connected =True if msg.pid == self.mac and msg.pid != '' else False

    def charger_position_bool_sub_callback(self, msg):
        self.charger_position_bool = msg.data
    
    @staticmethod
    def get_all_hci_devices() -> List[str]:
        """获取所有 hci 设备列表（如 ['hci0', 'hci1', 'hci2']）"""
        devices = glob.glob('/sys/class/bluetooth/hci*')
        # 排序，确保 hci0, hci1, hci2 的顺序
        return sorted([d.split('/')[-1] for d in devices])
    
    @staticmethod
    def get_hci_devices_pattern(pattern: str = r'hci\d+') -> List[str]:
        """使用正则表达式获取匹配的蓝牙设备"""
        devices = ChargeAction.get_all_hci_devices()
        regex = re.compile(pattern)
        return [d for d in devices if regex.match(d)]
    
    def raw_vel_sub_callback(self, msg):
        if not self.stop_loop and self.dock_completed:
            now = time.time()
            linear_x = abs(msg.linear_x)
            angular_z = abs(msg.angular_z)
            exceed = (linear_x > 0.1 or angular_z > 0.1)
            
            with self.raw_vel_lock:
                # 添加当前样本
                self.raw_vel_window.append((now, exceed))
                # 移除超过3秒的旧样本
                while self.raw_vel_window and (now - self.raw_vel_window[0][0]) > 3.0:
                    self.raw_vel_window.popleft()
                
                # 只有当窗口时长达到3秒时才进行比例判断
                if self.raw_vel_window and (now - self.raw_vel_window[0][0]) >= 2.5:  # 确保窗口内数据覆盖了最近3秒
                    total = len(self.raw_vel_window)
                    exceed_count = sum(1 for _, e in self.raw_vel_window if e)
                    ratio = exceed_count / total
                    if ratio >= 0.8:
                        self.get_logger().info(
                            f'检测到/raw_vel topic: 3秒内超过阈值的比例 {ratio:.2%} >= 80% (total: {total}, exceed_count: {exceed_count})，停止充电。'
                        )
                        self.stop_loop = True
                        self.publish_charge_error(
                            ChargeErrorCode.NOT_IN_POSITION,
                            f'moved while charging, raw_vel exceed ratio {ratio:.2%} >= 80%')
                    else:
                        self.get_logger().info(
                            f'检测到/raw_vel topic: 3秒内超过阈值的比例 {ratio:.2%} (total: {total}, exceed_count: {exceed_count})', throttle_duration_sec=5.0
                        )
    def timer_loop_callback(self):        
        if self.connect_bluetooth_client_.wait_for_service(2):
            self.bluetooth_setup = True
        else:
            self.bluetooth_setup = False
        
        # switch resolution to 1280x1024
        if not self.resolution_high and not self.dock_completed and not self.stop_loop and not self.switch_resolution_executing:
            if not self.camera_has_data():
                # 相机无数据: 不切换分辨率, 上报 20 camera not ready(只上报一次, 停止由上层 stop 触发)
                self.get_logger().info(
                    f'camera not ready: no data on {self.camera_image_topic} within '
                    f'{self.camera_data_timeout}s, skip switch_resolution',
                    throttle_duration_sec=5.0)
                self.publish_charge_error(
                    ChargeErrorCode.CAMERA_NOT_READY,
                    f'camera not ready: no data on {self.camera_image_topic} within {self.camera_data_timeout}s')
            else:
                self.get_logger().info('-------- call /rgb_camera_manager_server/switch_resolution service with resolution:1280x1024 --------')
                self.switch_resolution_executing = True
                request = SwitchResolution.Request()
                request.resolution_mode = SwitchResolution.Request.RESOLUTION_HIGH
                self.future_switch_resolution = self.switch_resolution_client_.call_async(request)
                self.future_switch_resolution.add_done_callback(self.switch_resolution_future_done_callback)
        
        if not self.apriltag_detecting and not self.stop_loop and not self.start_apriltag_detecting_executing:
            self.get_logger().info('-------- call /start_detect_apriltag service --------')
            self.start_apriltag_detecting_executing = True
            request = StartDetectApriltag.Request()
            self.future_start_apriltag = self.start_apriltag_client_.call_async(request)
            self.future_start_apriltag.add_done_callback(self.start_apriltag_detect_future_done_callback)
        
        if self.bluetooth_setup:
            if not self.bluetooth_connected and  not self.connect_bluetooth_executing and not self.stop_loop: # do not connect bluetooth when rebooting bluetooth server
                self.connect_bluetooth_executing = True
                
                hci_devices = ChargeAction.get_hci_devices_pattern()
                if len(hci_devices) > 0:
                    self.get_logger().info(f'hci devices: {hci_devices}')
                else:
                    self.get_logger().info(f'No hci device detected.')
                    if self.bluetooth_connect_num >= self.bluetooth_connect_fail_num_max:
                        # 连续多轮都没有 hci 设备, 判定不可恢复: 仅上报错误码,
                        # 停止本次回充由上层调用 stop(取消 goal)触发
                        self.publish_charge_error(
                            ChargeErrorCode.BLUETOOTH_NOT_FOUND,
                            f'bluetooth not found: no hci device in {self.bluetooth_connect_num} attempts')
                    if (not self.power_off_on_executing and 
                        self.charge_action_allow_power_off_on and 
                        self.power_off_on_client_.wait_for_service(2) and
                        self.can_perform_power_off_on()):  # 添加冷却时间检查
                        self.power_off_on_executing = True
                        self.save_last_power_off_time()  # 在调用前先保存时间
                        self.get_logger().info('-------- call /off_pc_power service --------')
                        off_pc_power_request = TurnoffPcPower.Request()
                        off_pc_power_request.request_stu = True
                        self.future_power_off_on = self.power_off_on_client_.call_async(off_pc_power_request)
                        self.future_power_off_on.add_done_callback(self.power_off_on_done_callback)
                
                self.get_logger().info(f"-------- call /connect_bluetooth service, {self.bluetooth_connect_num + 1} / {self.bluetooth_connect_num_max} --------")

                request = ConnectBluetooth.Request()
                request.mac = self.mac
                self.get_logger().info(f'request.mac {request.mac}')
                self.bluetooth_connect_num += 1
                
                self.future_connect_bluetooth = self.connect_bluetooth_client_.call_async(request)
                self.future_connect_bluetooth.add_done_callback(self.connect_bluetooth_done_callback)                   
        else:
            self.get_logger().info('waiting for bluetooth node ...', throttle_duration_sec = 3)

        if not self.dock_executing and not self.dock_completed:
            self.dock_executing = True
            self.get_logger().info('-------- call /dock action --------')
            dock_msg = Dock.Goal()
            dock_msg.mac = self.mac
            dock_msg.marker = self.request_marker
            dock_msg.protocol = self.request_protocol
            dock_msg.session_id = self.session_id
            dock_msg.delta = self.request_delta
            while not self.dock_client_.wait_for_server(2):
                self.get_logger().info('Dock action server not available.', throttle_duration_sec = 2)
            self.dock_client_sendgoal_future = self.dock_client_.send_goal_async(dock_msg, self.dock_feedback_callback)
            self.dock_client_sendgoal_future.add_done_callback(self.dock_response_callback)
        else:
            if self.charger_state.has_contact and not self.charger_state.is_charging:
                request = EmptyForSrv.Request()
                if time.time() - self.charger_start_client_last_request_time > 2.0:
                    self.charger_start_client_.call_async(request)
                    self.charger_start_client_last_request_time = time.time()
            elif self.charger_state.has_contact and self.charger_state.is_charging:
                self.feedback_msg.state = ChargeActionState.charging
            else:
                pass

        if (self.connect_bluetooth_executing):
            self.feedback_msg.state = ChargeActionState.connectbluetooth
        elif self.dock_executing:
            self.feedback_msg.state = ChargeActionState.docking
        elif self.charger_state.is_charging:
            self.feedback_msg.state = ChargeActionState.charging
        # self.get_logger().info(f"=== charge action ===      state: {self.feedback_msg.state}", throttle_duration_sec=1)
        self.goal_handle.publish_feedback(self.feedback_msg)

    def power_off_on_done_callback(self, future):
        response = future.result()
        if response.response_stu == True:
            self.get_logger().info('/off_pc_power success, pc will power off after 150s.')
            try:
                self.get_logger().info("after 10 seconds, systemctl poweroff will be executed.")
                time.sleep(10)
                result = subprocess.run(['sudo', 'systemctl', 'poweroff'], check=True)
                if result.returncode == 0:
                    self.get_logger().info('systemctl poweroff successfully, host will shut down.')
                else:
                    self.get_logger().error(f'systemctl poweroff failed: {result.stderr}')
            except Exception as e:
                self.get_logger().error(f'Exception during poweroff: {str(e)}')
        else:
            self.get_logger().info('/off_pc_power failed, waiting for call /off_pc_power again.')
            self.power_off_on_executing = False
    
    def start_apriltag_detect_future_done_callback(self, future):
        response = future.result()
        if response.success:
            self.get_logger().info('start_detect_apriltag service result: success.')
            self.apriltag_detecting = True
        else:
            self.get_logger().info('start_detect_apriltag service result: failed.')
            self.apriltag_detecting = False
        self.start_apriltag_detecting_executing = False
    
    def stop_apriltag_detect_future_done_callback(self, future):
        response = future.result()
        if response.success:
            self.get_logger().info('stop_detect_apriltag service result: success.')
            self.apriltag_detecting = False
        else:
            self.get_logger().info('stop_detect_apriltag service result: failed.')
            self.apriltag_detecting = True        
        self.stop_apriltag_detecting_executing = False
        
    def switch_resolution_future_done_callback(self, future):
        try:
            response = future.result()
            if response.success:
                self.get_logger().info(f'switch resolution mode to {response.resolution_mode} success.')
            else:
                self.get_logger().info(f'switch resolution mode to {response.resolution_mode} failed.')
            self.resolution_high = (response.resolution_mode == RgbCameraResolution.RESOLUTION_HIGH)
        except Exception as e:
            self.get_logger().error(f'switch_resolution service call failed: {str(e)}')
            self.resolution_high = False
        finally:
            self.switch_resolution_executing = False

    # charge_action goal_callback
    def charge_action_goal_callback(self, goal_request):
        if goal_request.restore == 1:
            self.charge_type = 'restore'
        else:
            if goal_request.type == 0:
                self.charge_type = 'auto'
            elif goal_request.type == 1:
                self.charge_type = 'manual'

        self.get_logger().info(f'Received a new /Charge action request, type: {self.charge_type}')
        if self.msg_state_pub.data:
            self.get_logger().info('The /charge action server is executing Charge action. Reject')
            return GoalResponse.REJECT
        else:
            self.mac = goal_request.mac
            self.request_marker = goal_request.marker
            self.request_protocol = goal_request.protocol
            self.request_delta = goal_request.delta
            self.session_id = goal_request.session_id
            self.get_logger().info('charge_action_goal_callback')
            self.get_logger().info(f'self.mac: {self.mac}')
            self.get_logger().info(f'marker: {self.request_marker}, protocol: {self.request_protocol}')
            self.msg_state_pub.data = True
            self.get_logger().info('The /charge action server is idle, accepted and executing.')
            return GoalResponse.ACCEPT

    # charge_action handle_accepted_callback
    def charge_action_handle_accepted_callback(self, goal_handle):
        self.get_logger().info('charge_action_handle_accepted_callback')
        self.goal_handle = goal_handle
        goal_handle.execute()
    
    # charge_action 服务端 execute_callback
    def charge_action_execute_callback(self, goal_handle):
        self.get_logger().info("charge_action_execute_callback.")
        self.init_params()
        self.mac = goal_handle.request.mac
        re_restore = goal_handle.request.restore
        re_charge_type = goal_handle.request.type
        self.request_marker = goal_handle.request.marker
        self.request_protocol = goal_handle.request.protocol
        self.request_delta = goal_handle.request.delta
        self.session_id = goal_handle.request.session_id
        self.get_logger().info(f'request_marker: {self.request_marker}, request_protocol: {self.request_protocol}')
        self.get_logger().info(f'session_id: {self.session_id or "<empty>"}')
        if re_restore or re_charge_type:
            self.dock_completed = True
            self.charger_position_bool = True

        self.feedback_msg = Charge.Feedback()
        self.feedback_msg.state = ChargeActionState.idle

        if not self.core_monitor_state_stored: # Compatible with manual charging, do not delete!!!
            self.core_monitor_state_stored = True
            try:
                self.get_logger().info(f'write 1 to /map/core_restart.txt when /charge action started')
                with open('/map/core_restart.txt', 'w', encoding='utf-8') as f:
                    f.write('1\n')
                    # f.write(self.mac)
            except Exception as e:
                self.get_logger().info(f"catch exception {str(e)} when write 1 to /map/core_restart.txt for processing /charge action started.")
        
        self.loop_thread = threading.Thread(target=self.loop_,daemon=True)
        self.loop_thread.start()

        while True:
            if self.dock_goal_rejected:
                self.get_logger().info("return Charge action for reason: dock action is rejected.")
                self.publish_charge_error(
                    ChargeErrorCode.INTERFACE_FAILED, 'dock failed: dock action goal rejected')
                result = Charge.Result()
                result.success = False
                result.code = ChargeErrorCode.INTERFACE_FAILED
                self.goal_handle.abort()
                try:
                    self.get_logger().info(f'存储充电状态 0 和 mac: {self.mac} 到/map/charge_restore.txt.')
                    with open('/map/charge_restore.txt', 'w', encoding='utf-8') as f:
                        f.write('0\n')
                        f.write(self.mac)
                except Exception as e:
                    self.get_logger().info(f'存储充电状态 0 catch exception: {str(e)}')
                self.msg_state_pub.data = False
                return result

            if self.dock_completed:
                self.msg_state_pub.data = False
                if not self.bluetooth_state_stored:
                    self.bluetooth_state_stored = True
                    try:
                        self.get_logger().info(f"存储充电状态 1 和 mac: {self.mac} 到/map/charge_restore.txt.")
                        with open('/map/charge_restore.txt', 'w', encoding='utf-8') as f:
                            f.write('1\n')
                            f.write(self.mac)
                    except Exception as e:
                        self.get_logger().info(f"存储充电状态 1 catch exception: {str(e)}")
                
                if (not self.charger_position_bool and not self.charger_state.has_contact) or self.stop_loop:
                    time.sleep(1)
                    self.get_logger().info(f'charger_position_bool: {"True" if self.charger_position_bool else "False"}')
                    self.get_logger().info(f'charger_state.has_contact: {"True" if self.charger_state.has_contact else "False"}')
                    self.get_logger().info(f'stop_loop: {"True" if self.stop_loop else "False"}')
                    self.get_logger().info(f'stop /charge action, type: {self.charge_type} ...... ')
                    self.get_logger().info(f"write 0 to /map/core_start.txt for stop charge action")
                    if self.disconnect_bluetooth_client_.wait_for_service(2):
                        self.get_logger().info('-------- call /disconnect_bluetooth service --------')
                        re_disconnect_bluetooth = DisconnectBluetooth.Request()
                        self.disconnect_bluetooth_future = self.disconnect_bluetooth_client_.call_async(re_disconnect_bluetooth)
                        self.disconnect_bluetooth_future.add_done_callback(self.disconnect_bluetooth_callback)
                    else:
                        self.get_logger().info('/disconnect_bluetooth service is not on line')
                    try:
                        with open('/map/core_restart.txt', 'w', encoding='utf-8') as f:
                            f.write('0\n')
                    except Exception as e:
                        self.get_logger().info(f"catch exception {str(e)} when write 0 to /map/core_restart.txt for processing stop /charge action.")
                    result = Charge.Result()
                    if self.charge_cancelled:
                        result.code = ChargeErrorCode.CANCELLED
                    elif self.charge_error_code != ChargeErrorCode.SUCCESS:
                        # 已上报的细分码: 移动停充/无接触, 或由 dock result 透传的码
                        result.code = self.charge_error_code
                    elif self.dock_failed:
                        result.code = ChargeErrorCode.UNKNOWN
                    else:
                        # 对接流程结束但没有建立充电接触
                        self.publish_charge_error(
                            ChargeErrorCode.NOT_IN_POSITION,
                            'not in position: charge action finished without charger contact')
                        result.code = self.charge_error_code
                    result.success = (result.code == ChargeErrorCode.SUCCESS)
                    self.get_logger().info(f'/charge action result => code: {result.code}, success: {result.success}')
                    self.goal_handle.succeed()
                    try:
                        self.get_logger().info(f'存储充电状态 0 和 mac: {self.mac} 到/map/charge_restore.txt.')
                        with open('/map/charge_restore.txt', 'w', encoding='utf-8') as f:
                            f.write('0\n')
                            f.write(self.mac)
                    except Exception as e:
                        self.get_logger().info(f'存储充电状态 0 catch exception: {str(e)}')
                    return result
                else:                    
                    now_time = time.time()
                    self.is_docking_state_pub.publish(self.msg_state_pub)
                    if now_time - self.is_undocking_state_last_time > 5.0:
                        self.is_undocking_state = False
                    if not self.is_undocking_state:    
                        self.zero_cmd_vel_publisher.publish(self.msg_zero_cmd)
                    time.sleep(1)
            else:
                self.is_docking_state_pub.publish(self.msg_state_pub)
                time.sleep(1)
            
    def loop_(self):
        self.get_logger().info('loop started')
        while True:
            self.timer_loop_callback()
            if self.dock_completed:
                if self.battery_ >= 1.01 or self.stop_loop:
                    self.get_logger().info("break loop_")
                    self.get_logger().info(f"battery: {self.battery_}, stop_loop: {str(self.stop_loop)}, charge_position_bool: {str(self.charger_position_bool)}")
                    
                    # 停止检测apriltag，对接完成后就停止检测apriltag
                    if self.apriltag_detecting and not self.stop_apriltag_detecting_executing:
                        self.get_logger().info('-------- call /stop_detect_apriltag service --------')
                        self.stop_apriltag_detecting_executing = True
                        request = StopDetectApriltag.Request()
                        future_stop_apriltag = self.stop_apriltag_client_.call_async(request)
                        future_stop_apriltag.add_done_callback(self.stop_apriltag_detect_future_done_callback)
                    
                    # switch resolution to 640x480
                    if self.resolution_high and not self.switch_resolution_executing:
                        self.get_logger().info('-------- call /rgb_camera_manager_server/switch_resolution service with resolution:640x480 --------')
                        self.switch_resolution_executing = True
                        request = SwitchResolution.Request()
                        request.resolution_mode = SwitchResolution.Request.RESOLUTION_LOW
                        self.future_switch_resolution = self.switch_resolution_client_.call_async(request)
                        self.future_switch_resolution.add_done_callback(self.switch_resolution_future_done_callback)
                    break
            else:
                if self.dock_goal_rejected:
                    self.get_logger().info("dock action reject => stop loop.")
                    break
                # continue
            time.sleep(1)

    
    # charge_action cancel callback
    def charge_action_cancel_callback(self, goal_handle):
        self.get_logger().info("Received request to cancel charge action servo goal")
        self.charge_cancelled = True
        self.dock_completed = True
        self.stop_loop = True
        if self.dock_executing:
            if self.dock_client_sendgoal_future is not None:
                goal_handle = self.dock_client_sendgoal_future.result()
                goal_handle.cancel_goal_async()
            else:
                self.dock_executing = False
            self.get_logger().info('cancel dock action')
            
        if self.apriltag_detecting:
            self.get_logger().info('-------- call /stop_detect_apriltag service --------')
            request = StopDetectApriltag.Request()
            future_stop_apriltag = self.stop_apriltag_client_.call_async(request)
            future_stop_apriltag.add_done_callback(self.stop_apriltag_detect_future_done_callback)
            self.get_logger().info('cancel apriltag detecting by calling /stop_detect_apriltag service')
        
        # switch resolution to 640x480
        if self.resolution_high and not self.switch_resolution_executing:
            self.get_logger().info('-------- call /rgb_camera_manager_server/switch_resolution service with resolution:640x480 --------')
            self.switch_resolution_executing = True
            request = SwitchResolution.Request()
            request.resolution_mode = SwitchResolution.Request.RESOLUTION_LOW
            self.future_switch_resolution = self.switch_resolution_client_.call_async(request)
            self.future_switch_resolution.add_done_callback(self.switch_resolution_future_done_callback)
        
        return CancelResponse.ACCEPT

    def connect_bluetooth_done_callback(self, future_connect_bluetooth):
        response = future_connect_bluetooth.result()
        self.get_logger().info(
            f'bluetooth connection {"True" if response.success else "False"}, '
            f'cost {response.connection_time} seconds, code =>{response.code}, result =>{response.result}')
        self.bluetooth_connected = response.success
        self.bluetooth_connected_time = time.time()
        if response.success:
            self.bluetooth_connect_num = 0
        elif self.bluetooth_connect_num >= self.bluetooth_connect_fail_num_max:
            # 连续多次连接失败, 判定不可恢复: 仅上报错误码, 码由蓝牙服务器判定
            # (30 对端蓝牙设备不存在 / 31 蓝牙连接失败 / 2 有其它蓝牙操作在进行)
            # 停止本次回充由上层调用 stop(取消 goal)触发, 避免本节点自做收尾导致状态与原因不一致
            self.publish_charge_error(
                response.code if response.code else ChargeErrorCode.BLUETOOTH_CONNECT_ERROR,
                response.result or f'bluetooth connect failed {self.bluetooth_connect_num} times')
        self.connect_bluetooth_executing = False
    
    def disconnect_bluetooth_callback(self, future):
        response = future.result()        
        self.get_logger().info(f'/disconnect_bluetooth service: {"success" if response.success else "Failed" }, cost time: {response.cost_time}, seconds, infos: {response.infos}"')

    
    def dock_feedback_callback(self, feedback_msg):
        feedback = feedback_msg.feedback
        self.get_logger().info('***************** Dock Feedback *****************')
        self.get_logger().info('dock feedback => sees_dock : {}'.format(feedback.sees_dock))
        self.get_logger().info('dock feedback => state     : {}'.format(feedback.state))
        self.get_logger().info('dock feedback => infos     : {}'.format(feedback.infos))
        self.get_logger().info('*************************************************')

    def dock_response_callback(self, future):
        goal_handle = future.result()
        if not goal_handle.accepted:
            self.get_logger().info('dock goal rejected !')
            self.dock_goal_rejected = True
        else:
            self.get_logger().info('dock goal accepted.')
            self._dock_get_future_result = goal_handle.get_result_async()
            self._dock_get_future_result.add_done_callback(self.dock_get_result_callback)

    def dock_get_result_callback(self, future):
        result = future.result().result
        self.dock_result_code = result.code
        self.get_logger().info('Dock result => is_docked: {}, code: {}'.format(result.is_docked, result.code))
        if not result.is_docked:
            self.get_logger().info('Dock action failed, Charge Action aborted')
            self.dock_failed = True
            self.stop_loop = True
            # 透传 dock 的细分码作为本次流程的码; dock 侧已自行上报同一码, 这里不重复上报
            if result.code != ChargeErrorCode.SUCCESS and self.charge_error_code == ChargeErrorCode.SUCCESS:
                self.charge_error_code = result.code
        self.dock_executing = False
        self.dock_completed = True
        
        # switch resolution to 640x480
        if self.dock_completed and self.resolution_high and not self.switch_resolution_executing:
            self.get_logger().info('-------- call /rgb_camera_manager_server/switch_resolution service with resolution:640x480 --------')
            self.switch_resolution_executing = True
            request = SwitchResolution.Request()
            request.resolution_mode = SwitchResolution.Request.RESOLUTION_LOW
            self.future_switch_resolution = self.switch_resolution_client_.call_async(request)
            self.future_switch_resolution.add_done_callback(self.switch_resolution_future_done_callback)

def main(args=None):
    rclpy.init(args=args)
    charge_action_node = ChargeAction()
    multi_executor = MultiThreadedExecutor()
    multi_executor.add_node(charge_action_node)
    multi_executor.spin()
    multi_executor.shutdown()

if __name__ == '__main__':
    main()  

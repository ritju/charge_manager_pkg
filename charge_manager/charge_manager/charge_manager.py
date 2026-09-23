import rclpy
from rclpy.node import Node
from rclpy.callback_groups import ReentrantCallbackGroup, MutuallyExclusiveCallbackGroup
from rclpy.action import ActionClient
from rclpy.qos import DurabilityPolicy,ReliabilityPolicy,QoSProfile,HistoryPolicy
from rclpy.task import Future
from rclpy.executors import MultiThreadedExecutor
import psutil
import subprocess
from signal import SIGINT, SIGTERM
import time
import os
import re
import math
import threading
import traceback

from std_srvs.srv import Empty
from std_msgs.msg import String
from std_msgs.msg import UInt8, Bool
from charge_manager_msgs.action import Charge
from charge_manager_msgs.msg import ChargeState2
from charge_manager_msgs.msg import BluetoothStatus
from charge_manager_msgs.msg import BluetoothCommand
from charge_manager_msgs.srv import StartBluetooth, StopBluetooth
from capella_ros_service_interfaces.msg import ChargeState
from capella_ros_service_interfaces.srv import ChargeStart, DockStart
from charge_manager_msgs.srv import ChargeCommand
from capella_ros_dock_msgs.msg import ChargeErrorCode
from capella_ros_dock_msgs.msg import ChargeErrorInfo
from geometry_msgs.msg import Twist

# Charge action / dock 错误码 → 消息映射(码值取自 ChargeErrorCode.msg, 文本用于发布端未提供 message 时兜底)
CHARGE_ERROR_MESSAGES = {
    ChargeErrorCode.SUCCESS: 'success',
    ChargeErrorCode.ALREADY_RUNNING: 'already running',
    ChargeErrorCode.BLOCKED: 'blocked',
    ChargeErrorCode.INVALID_PARAM: 'invalid param',
    ChargeErrorCode.CANCELLED: 'cancelled',
    ChargeErrorCode.INTERFACE_FAILED: 'interface failed',
    ChargeErrorCode.EXCEED_RUNTIME: 'exceed runtime',
    ChargeErrorCode.TIMEOUT_CHANGE_POSE: 'timeout change pose',
    ChargeErrorCode.TIMEOUT_RESPONSE: 'timeout response',
    ChargeErrorCode.CAMERA_NOT_READY: 'camera not ready',
    ChargeErrorCode.OBSTACLE: 'obstacle',
    ChargeErrorCode.MARKER_NOT_VISIBLE: 'marker not visible',
    ChargeErrorCode.NOT_IN_POSITION: 'not in position',
    ChargeErrorCode.BLUETOOTH_NOT_FOUND: 'bluetooth not found',
    ChargeErrorCode.BLUETOOTH_CONNECT_ERROR: 'bluetooth connect error',
    ChargeErrorCode.BLUETOOTH_NO_DATA: 'bluetooth no data',
    ChargeErrorCode.INVALID_PROTOCOL: 'invalid protocol',
    ChargeErrorCode.UNKNOWN: 'unknown',
}

# /charger/start_docking2 等待底层 /charge action 结果的兜底超时
DOCKING2_RESULT_TIMEOUT = 600.0
# mac 格式: XX:XX:XX:XX:XX:XX
MAC_PATTERN = re.compile(r'([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}')
# marker 格式: 对应 apriltag_ros 的 marker_id_and_bluetooth_mac_vec 配置项 "<id>[:<id_correction>]/<mac>",
# 即单个 id("0") 或双 marker 的 id:id_correction("0:1")
MARKER_PATTERN = re.compile(r'\d+(:\d+)?')

class chargeManager(Node):
    
    def __init__(self):
        super().__init__('charge_manager_node')
        self.get_logger().info('*** charge_manager_node *** started.')
        self.get_logger().info(f'manger_node => pid: {os.getpid()}')

        self.mac = ''
        self.charge_action_client_sendgoal_future = None

        callback_group_type = ReentrantCallbackGroup()
        single_cb_group = MutuallyExclusiveCallbackGroup()

        # init bluetooth params
        self.bluetooth_status = BluetoothStatus.DOWN
        self.bluetooth_proc = None

        # 初始化 self.charger_state
        self.charger_state = ChargeState()
        self.charger_state.pid = ''
        self.charger_state.has_contact = False
        self.charger_state.is_charging = False
        self.charger_state.is_docking = False
        self.charger_state.is_waterflooding = False
        self.charger_state.water_mode = 'unknown'

        self.contact_state_last_ = False

        # /charger/id subscription
        charger_id_sub_qos = QoSProfile(depth=1)
        charger_id_sub_qos.reliability = ReliabilityPolicy.RELIABLE
        charger_id_sub_qos.history = HistoryPolicy.KEEP_LAST
        charger_id_sub_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
        self.charger_id_sub = self.create_subscription(String, '/charger/id', self.charger_id_sub_callback, charger_id_sub_qos)
        
        charger_state_qos = QoSProfile(depth=1)
        charger_state_qos.reliability = ReliabilityPolicy.BEST_EFFORT
        charger_state_qos.history = HistoryPolicy.KEEP_LAST
        charger_state_qos.durability = DurabilityPolicy.VOLATILE

        # 订阅蓝牙server发送的 ChargeState2
        charger_state_qos2 = QoSProfile(depth=1)
        charger_state_qos2.reliability = ReliabilityPolicy.RELIABLE
        charger_state_qos2.history = HistoryPolicy.KEEP_LAST
        charger_state_qos2.durability = DurabilityPolicy.VOLATILE
        self.charger_state2_sub_ = self.create_subscription(ChargeState2, '/charger/state2', self.charger_state2_sub_callback, charger_state_qos, callback_group=callback_group_type)

        # 订阅加水控制开关话题
        add_water_ctr_qos = QoSProfile(depth=1)
        add_water_ctr_qos.reliability = ReliabilityPolicy.RELIABLE
        add_water_ctr_qos.history = HistoryPolicy.KEEP_LAST
        add_water_ctr_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
        self.add_water_ctr_sub_ = self.create_subscription(Bool, '/add_water_ctr', self.add_water_ctr_sub_callback, add_water_ctr_qos, callback_group=callback_group_type)

        self.manual_add_water_ctr_sub_ = self.create_subscription(Bool, '/manual_add_water_ctr', self.manual_add_water_ctr_sub_callback, add_water_ctr_qos, callback_group=callback_group_type)
        
        # 初始化 /charger/state publisher        
        self.charger_state_publisher = self.create_publisher(ChargeState, '/charger/state', charger_state_qos2, callback_group=callback_group_type)
        self.timer_pub_charger_state = self.create_timer(0.05, self.timer_pub_charger_state_callback, callback_group=callback_group_type)
        
        water_status_publisher_qos = QoSProfile(depth=1)
        water_status_publisher_qos.reliability = ReliabilityPolicy.RELIABLE
        water_status_publisher_qos.history = HistoryPolicy.KEEP_LAST
        water_status_publisher_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
        self.water_status_publisher = self.create_publisher(UInt8, "/add_water_stu", water_status_publisher_qos, callback_group=callback_group_type)

        self.add_water_status_last = 100
        self.add_water_status = 100
        
        # 初始化 zero_cmd_vel_publisher
        # self.zero_cmd_vel_publisher = self.create_publisher(Twist, '/cmd_vel', 1, callback_group=callback_group_type)
        
        # 新旧接口开关(可配置): false 时只创建旧接口(start/stop/water/start_docking/stop_docking),
        # 不创建新接口 /charger/start2 与 /charger/start_docking2
        env_new_services = os.environ.get('CHARGE_MANAGER_ENABLE_NEW_SERVICES', 'true')
        self.declare_parameter("enable_new_services", env_new_services.strip().lower() in ('true', 'yes', 'on', '1'))
        self.enable_new_services = self.get_parameter("enable_new_services").get_parameter_value().bool_value
        self.get_logger().info(
            f'enable_new_services: {self.enable_new_services} '
            f'({"new + old interfaces" if self.enable_new_services else "old interfaces only"})')

        # /charger/start service
        self.charger_start_service = self.create_service(Empty, '/charger/start', self.charger_start_service_callback, callback_group=callback_group_type)
        
        # /charger/stop service
        self.charger_stop_service = self.create_service(Empty, '/charger/stop', self.charger_stop_service_callback, callback_group=callback_group_type)

        # /water/start service
        self.water_start_service = self.create_service(Empty, '/water/start', self.water_start_service_callback, callback_group=callback_group_type)

        # /water/stop service
        self.water_stop_service = self.create_service(Empty, '/water/stop', self.water_stop_service_callback, callback_group=callback_group_type)
        
        # /charger/start_docking
        self.charger_start_docking_service = self.create_service(Empty, '/charger/start_docking', self.charger_start_docking_service_callback, callback_group=callback_group_type)
        
        # /charger/stop_docking
        self.charger_stop_docking_service = self.create_service(Empty, '/charger/stop_docking', self.charger_stop_docking_service_callback, callback_group=callback_group_type)

        # /charger/start2 service (新接口, 受 enable_new_services 控制)
        self.charger_start2_service = None
        # /charger/start_docking2 service (新接口, 受 enable_new_services 控制)
        self.charger_start_docking2_service = None
        if self.enable_new_services:
            self.charger_start2_service = self.create_service(ChargeStart, '/charger/start2', self.charger_start2_service_callback, callback_group=callback_group_type)
            self.charger_start_docking2_service = self.create_service(DockStart, '/charger/start_docking2', self.charger_start_docking2_service_callback, callback_group=single_cb_group)
        else:
            self.get_logger().info(
                'enable_new_services=False: /charger/start2 and /charger/start_docking2 are NOT created')

        self.charge_action_client = ActionClient(self, Charge, 'charge', callback_group=callback_group_type)

        # /charge_command service client (async, with error codes)
        self.charge_command_client = self.create_client(ChargeCommand, '/charge_command', callback_group=callback_group_type)

        # /charge/error_info: 回充流程错误码
        # 订阅放 Reentrant 组, 不能与 /charger/start_docking2 的 MutuallyExclusive 组同组,
        # 否则等待期间该回调得不到执行
        charge_error_qos = QoSProfile(depth=1)
        charge_error_qos.reliability = ReliabilityPolicy.RELIABLE
        charge_error_qos.history = HistoryPolicy.KEEP_LAST
        charge_error_qos.durability = DurabilityPolicy.VOLATILE
        self.charge_error_info_pub_ = self.create_publisher(ChargeErrorInfo, '/charge/error_info', charge_error_qos, callback_group=callback_group_type)
        self.charge_error_info_sub_ = self.create_subscription(ChargeErrorInfo, '/charge/error_info', self.charge_error_info_sub_callback, charge_error_qos, callback_group=callback_group_type)

        # 当前 /charger/start_docking2 的 session, 其余时刻为空串
        self.charge_session = ''
        # 本次 session 收到的首个错误信息
        self.charge_error = None
        self.charge_error_event = threading.Event()
        # start_docking2 提前成功标准: 连续两次 feedback 为 charging(不等整个 action 结束)
        self.charge_charging_consecutive = 0
        self.charge_charging_event = threading.Event()

        # restore charge
        self.get_logger().info('restore charging or not ...')
        time.sleep(3)
        try:
            with open('/map/charge_restore.txt', 'r', encoding='utf-8') as f:
                restore = (int)(f.readline().strip('\n'))
                self.get_logger().info(f'restore: {restore}')
                if restore == 1:
                    self.get_logger().info(f"执行恢复充电")
                    self.get_logger().info('Need to restore charge behavior. ')
                    self.mac = f.readline().strip('\n')
                    if not self.charge_action_client.wait_for_server(5):
                        self.get_logger().info('charge action server not on line. Failed to restore charge behavior')
                    else:
                        self.get_logger().info('Starting restore charing behavior ...')
                        self.get_logger().info(f'restore: {restore}, mac: {self.mac}')
                        charge_msg = Charge.Goal()
                        charge_msg.restore = restore
                        charge_msg.mac = self.mac
                        self.charge_action_client_sendgoal_future = self.charge_action_client.send_goal_async(charge_msg, self.charge_action_feedback_callback)
                        self.charge_action_client_sendgoal_future.add_done_callback(self.charge_action_response_callback)
                else:
                    self.get_logger().info('Don\'t need to restore charge behavior.')
        except Exception as e:
            self.get_logger().info(f'catch exception {str(e)}, when charge_manage node init.')

      
    def timer_pub_charger_state_callback(self):
        self.charger_state_publisher.publish(self.charger_state)
        if self.contact_state_last_ != self.charger_state.has_contact:
            self.get_logger().info(f"managed node => contact state change from {str(self.contact_state_last_)} to {str(self.charger_state.has_contact)}")
            self.contact_state_last_ = self.charger_state.has_contact
        if self.charger_state.is_waterflooding == True:
            if self.charger_state.water_mode == 'auto':
                self.add_water_status = 1
            elif self.charger_state.water_mode == 'manual':
                self.add_water_status = 2
        else:
            self.add_water_status = 0
        
        if self.add_water_status != self.add_water_status_last:
            msg = UInt8()
            msg.data = self.add_water_status
            self.water_status_publisher.publish(msg)
            self.add_water_status_last = self.add_water_status
        
        
        #  if self.charger_state.is_charging and self.charger_state.has_contact:
        #      zero_cmd = Twist()
        #      zero_cmd.linear.x = 0.0
        #      zero_cmd.angular.z = 0.0
        #      self.zero_cmd_vel_publisher.publish(zero_cmd)

    def charger_state2_sub_callback(self, msg):
        self.charger_state.pid = msg.pid
        self.charger_state.has_contact = msg.has_contact
        self.charger_state.is_charging = msg.is_charging
        self.charger_state.is_waterflooding = msg.is_waterflooding
        self.charger_state.water_mode = msg.water_mode
        self.charger_state.manual_enable_stu = msg.manual_enable_stu
        self.charger_state.fault_stu = msg.fault_stu
        self.charger_state.left_dis_sensor = msg.left_dis_sensor
        self.charger_state.right_dis_sensor = msg.right_dis_sensor
        self.charger_state.switch_stu = msg.switch_stu
        
        if msg.has_contact:
            self.charger_state.is_docking = False
    
    def _call_charge_command(self, command):
        """Helper to call /charge_command service."""
        if not self.charge_command_client.wait_for_service(timeout_sec=5.0):
            self.get_logger().info(f'_call_charge_command: /charge_command service not available')
            return
        cmd_req = ChargeCommand.Request()
        cmd_req.command = command
        future = self.charge_command_client.call_async(cmd_req)
        future.add_done_callback(self.charge_command_done_callback)

    def charge_command_done_callback(self, future):
        """把 /charge_command 返回的非 0 错误码透传到 /charge/error_info。

        蓝牙服务器内部判定(如协议版本不匹配、蓝牙无数据)只能通过该响应到达这里,
        因此 charge_manager 作为发布端代发这些码。
        """
        try:
            cmd_resp = future.result()
        except Exception as e:
            self.get_logger().info(f'/charge_command response exception: {e}')
            return
        if cmd_resp is None:
            self.get_logger().info('/charge_command returned None')
            return
        self.get_logger().info(f'/charge_command response code={cmd_resp.code}, message={cmd_resp.message}')
        if cmd_resp.code != ChargeErrorCode.SUCCESS:
            self.publish_charge_error(cmd_resp.code, cmd_resp.message, 'charge_manager')

    def charge_error_info_sub_callback(self, msg):
        """只接受本次 start_docking2 session 的首个错误码, 其余一律忽略。"""
        if not self.charge_session or msg.session_id != self.charge_session:
            self.get_logger().info(
                f'received /charge/error_info not for current session: session={msg.session_id or "<empty>"}, '
                f'code={msg.code}, message={msg.message}, source={msg.source} (ignored)')
            return
        if self.charge_error is not None:
            self.get_logger().info(
                f'received /charge/error_info but first one already set, ignore: code={msg.code}, source={msg.source}')
            return
        self.charge_error = msg
        self.get_logger().info(
            f'received first /charge/error_info for session {msg.session_id}: code={msg.code}, '
            f'message={msg.message}, source={msg.source}')
        self.charge_error_event.set()

    def publish_charge_error(self, code, message, source):
        msg = ChargeErrorInfo()
        msg.session_id = self.charge_session
        msg.code = code
        msg.message = message
        msg.source = source
        self.charge_error_info_pub_.publish(msg)
        self.get_logger().info(
            f'publish /charge/error_info: session={msg.session_id or "<empty>"}, code={code}, '
            f'message={message}, source={source}')

    @staticmethod
    def _new_charge_session():
        return f'{int(time.time() * 1000)}-{os.getpid()}'

    @staticmethod
    def _validate_dock_start_request(request):
        """校验 /charger/start_docking2 入参, 返回非法参数名, 全部合法返回空串。

        marker 允许空串(dock 侧会回退为按 mac 查 marker_and_mac_vector), 非空时必须是
        "id" 或 "id:id_correction" 形式(如 "0" / "0:1", 对应 apriltag_ros 的配置项
        "<id>[:<id_correction>]/<mac>"); mac 必须为 XX:XX:XX:XX:XX:XX 格式,
        否则 dock 侧无法定位 marker。
        """
        if not MAC_PATTERN.fullmatch(request.mac or ''):
            return 'mac'
        marker = request.marker or ''
        if marker and not MARKER_PATTERN.fullmatch(marker):
            return 'marker'
        delta_values = (
            request.delta.position.x, request.delta.position.y, request.delta.position.z,
            request.delta.orientation.x, request.delta.orientation.y,
            request.delta.orientation.z, request.delta.orientation.w,
        )
        if abs(request.delta.position.x) > 0.1 or abs(request.delta.position.y) > 0.1:
            return 'delta'
        if any(math.isnan(v) or math.isinf(v) for v in delta_values):
            return 'delta'
        return ''

    def _charge_error_response(self, code, message):
        return code, (message if message else CHARGE_ERROR_MESSAGES.get(code, 'unknown'))

    def add_water_ctr_sub_callback(self, msg):
        if msg.data == True:
            self.get_logger().info(f'received the topic /add_water_ctr with value {msg.data}')
            self._call_charge_command(BluetoothCommand.WATER_START)
        else:
            self.get_logger().info(f'received the topic /add_water_ctr with value {msg.data}')
            self._call_charge_command(BluetoothCommand.WATER_STOP)

    def manual_add_water_ctr_sub_callback(self, msg):
        if msg.data == True:
            self.get_logger().info(f'received the topic /manual_add_water_ctr with value {msg.data}')
            self._call_charge_command(BluetoothCommand.ENABLE_MANUAL_ADD_WATER)
        else:
            self.get_logger().info(f'received the topic /manual_add_water_ctr with value {msg.data}')
            self._call_charge_command(BluetoothCommand.DISABLE_MANUAL_ADD_WATER)            
    
    def charger_id_sub_callback(self, msg):
        if msg.data != '':
            self.mac = msg.data
    
    def charger_start_service_callback(self, request, response):
        try:
            self.get_logger().info('received a request for /charger/start service')
            self._call_charge_command(BluetoothCommand.CHARGER_START)
        except Exception:
            self.get_logger().error(f'charger_start_service_callback exception:\n{traceback.format_exc()}')
        return response

    def charger_stop_service_callback(self, request, response):
        try:
            self.get_logger().info('received a request for /charger/stop service')
            self._call_charge_command(BluetoothCommand.CHARGER_STOP)
        except Exception:
            self.get_logger().error(f'charger_stop_service_callback exception:\n{traceback.format_exc()}')
        return response

    def water_start_service_callback(self, request, response):
        try:
            self.get_logger().info('received a request for /water/start service')
            self._call_charge_command(BluetoothCommand.WATER_START)
        except Exception:
            self.get_logger().error(f'water_start_service_callback exception:\n{traceback.format_exc()}')
        return response

    def water_stop_service_callback(self, request, response):
        try:
            self.get_logger().info('received a request for /water/stop service')
            self._call_charge_command(BluetoothCommand.WATER_STOP)
        except Exception:
            self.get_logger().error(f'water_stop_service_callback exception:\n{traceback.format_exc()}')
        return response

    def charger_start_docking_service_callback(self, request, response):
        try:
            self.get_logger().info('received a request for /charger/start_docking service')
            self.get_logger().info("start charge action")
            self.get_logger().info(f"write 1 to /map/core_restart.txt for /charger/start_docking")
            try:
                with open('/map/core_restart.txt', 'w', encoding='utf-8') as f:
                    f.write('1\n')
                    # f.write('self.mac')
            except Exception as e:
                self.get_logger().info(f"catch exception {str(e)} when write 1 to /map/core_restart.txt for processing /charger/start_docking service.")
            self.charger_state.is_docking = True
            charge_msg = Charge.Goal()
            charge_msg.mac = self.mac
            # charge_msg.mac = '94:C9:60:43:BD:FD'
            while not self.charge_action_client.wait_for_server(2):
                self.get_logger().info('Charge action server not available.')
            self.charge_action_client_sendgoal_future = self.charge_action_client.send_goal_async(charge_msg, self.charge_action_feedback_callback)
            self.charge_action_client_sendgoal_future.add_done_callback(self.charge_action_response_callback)
        except Exception:
            self.get_logger().error(f'charger_start_docking_service_callback exception:\n{traceback.format_exc()}')
        return response
    
    def charger_start2_service_callback(self, request, response):
        self.get_logger().info('received a request for /charger/start2 service')
        # Call /charge_command service asynchronously
        if not self.charge_command_client.wait_for_service(timeout_sec=5.0):
            self.get_logger().info('/charger/start2: /charge_command service not available')
            response.code = ChargeErrorCode.TIMEOUT_RESPONSE
            response.message = '/charge_command not exist'
            return response
        
        cmd_req = ChargeCommand.Request()
        cmd_req.command = BluetoothCommand.CHARGER_START
        try:
            future = self.charge_command_client.call_async(cmd_req)
            # 节点已由 MultiThreadedExecutor 托管, 不能嵌套 spin, 否则节点会被移出主 executor
            end = time.monotonic() + 15.0
            while not future.done() and time.monotonic() < end:
                time.sleep(0.05)
            cmd_resp = future.result()
            if cmd_resp is not None:
                response.code = cmd_resp.code
                response.message = cmd_resp.message
                self.get_logger().info(f'/charger/start2: response code={cmd_resp.code}, message={cmd_resp.message}')
            else:
                self.get_logger().info('/charger/start2: /charge_command returned None')
                response.code = ChargeErrorCode.TIMEOUT_RESPONSE
                response.message = 'timeout response'
        except Exception as e:
            self.get_logger().info(f'/charger/start2: exception calling /charge_command: {e}')
            response.code = ChargeErrorCode.UNKNOWN
            response.message = f'unknown error: {e}'
        return response

    def charger_start_docking2_service_callback(self, request, response):
        self.get_logger().info('received a request for /charger/start_docking2 service')
        self.get_logger().info(
            f'/charger/start_docking2: mac={request.mac}, marker={request.marker}, protocol={request.protocol}, '
            f'delta=({request.delta.position.x}, {request.delta.position.y})')
        goal_accepted = False
        try:
            # pre-check: invalid params
            invalid_param = self._validate_dock_start_request(request)
            if invalid_param:
                self.get_logger().info(f'/charger/start_docking2: invalid param {invalid_param}')
                response.code = ChargeErrorCode.INVALID_PARAM
                response.message = f'invalid param {invalid_param}'
                return response
            # pre-check: cancelled - already docking
            if self.charger_state.is_docking:
                self.get_logger().info('/charger/start_docking2: cancelled - dock already in progress')
                response.code = ChargeErrorCode.ALREADY_RUNNING
                response.message = 'already running'
                return response
            # pre-check: charge action server available (with timeout)
            if not self.charge_action_client.wait_for_server(5):
                self.get_logger().info('/charger/start_docking2: charge action server not available')
                response.code = ChargeErrorCode.INTERFACE_FAILED
                response.message = 'charge action server not exist'
                return response
            # pre-check: camera not ready (placeholder - user will implement camera check later)
            # TODO: Add camera data availability check when camera status is exposed
            self.get_logger().info("start charge action via /charger/start_docking2")
            self.get_logger().info(f"write 1 to /map/core_restart.txt for /charger/start_docking2")
            try:
                with open('/map/core_restart.txt', 'w', encoding='utf-8') as f:
                    f.write('1\n')
            except Exception as e:
                self.get_logger().info(f"catch exception {str(e)} when write 1 to /map/core_restart.txt for /charger/start_docking2.")
            # 开启本次回充流程的 session: 清空上次残留的错误码缓存, 之后才接受 /charge/error_info
            self.charge_session = self._new_charge_session()
            self.charge_error = None
            self.charge_error_event.clear()
            # 重置"连续两次 charging"判断
            self.charge_charging_consecutive = 0
            self.charge_charging_event.clear()
            self.get_logger().info(f'/charger/start_docking2: session={self.charge_session}')
            self.charger_state.is_docking = True
            charge_msg = Charge.Goal()
            charge_msg.mac = request.mac
            charge_msg.marker = request.marker
            charge_msg.protocol = request.protocol
            charge_msg.delta = request.delta
            charge_msg.session_id = self.charge_session
            self.charge_action_client_sendgoal_future = self.charge_action_client.send_goal_async(charge_msg, self.charge_action_feedback_callback)

            # 节点已由 MultiThreadedExecutor 托管, 不能嵌套 spin, 否则节点会被移出主 executor
            end = time.monotonic() + 10.0
            while not self.charge_action_client_sendgoal_future.done() and time.monotonic() < end:
                time.sleep(0.05)
            if not self.charge_action_client_sendgoal_future.done():
                self.get_logger().info('/charger/start_docking2: charge action goal timeout')
                self.charge_session = ''
                self.charger_state.is_docking = False
                response.code = ChargeErrorCode.TIMEOUT_RESPONSE
                response.message = 'timeout response'
                return response
            goal_handle = self.charge_action_client_sendgoal_future.result()
            if not goal_handle.accepted:
                self.get_logger().info('=== charge action ===     goal rejected !')
                self.charge_session = ''
                self.charger_state.is_docking = False
                response.code = ChargeErrorCode.INTERFACE_FAILED
                response.message = 'action /charge failed'
                return response
            self.get_logger().info('=== charge action ===     goal accepted.')
            goal_accepted = True
            charge_get_future_result = goal_handle.get_result_async()

            # 等待首个匹配 session 的 /charge/error_info, 或 action 结果, 或兜底超时
            end = time.monotonic() + DOCKING2_RESULT_TIMEOUT
            while time.monotonic() < end:
                if self.charge_error_event.wait(0.1):
                    break
                if self.charge_charging_event.is_set():
                    break
                if charge_get_future_result.done():
                    break

            if self.charge_error is not None:
                err = self.charge_error
                self.charge_session = ''
                code, message = self._charge_error_response(err.code, err.message)
                self.get_logger().info(
                    f'/charger/start_docking2: return early from /charge/error_info, code={code}, message={message}')
                # 底层流程仍在执行, is_docking 保持 True, 由上层下发 /charger/stop_docking 结束本次回充
                response.code = code
                response.message = message
                return response

            if self.charge_charging_event.is_set():
                # 已连续两次收到 charging feedback: 判定回充成功并提前返回, 不等整个 action 结束
                self.charge_session = ''
                self.charger_state.is_docking = False
                response.code = ChargeErrorCode.SUCCESS
                response.message = 'success'
                self.get_logger().info(
                    '/charger/start_docking2: charge action entered charging state twice consecutively, return success')
                return response

            if not charge_get_future_result.done():
                self.get_logger().info('/charger/start_docking2: charge action result timeout')
                self.charge_session = ''
                response.code = ChargeErrorCode.TIMEOUT_RESPONSE
                response.message = 'timeout response'
                # 底层流程可能仍在执行, is_docking 保持 True, 由上层下发 /charger/stop_docking 结束本次回充
                return response

            dock_result = charge_get_future_result.result().result
            self.charge_session = ''
            # action 已结束, 回充流程终止
            self.charger_state.is_docking = False
            self.get_logger().info('=== Charge action ===     result => success: {}, code: {}'.format(dock_result.success, dock_result.code))
            response.code = dock_result.code
            response.message = CHARGE_ERROR_MESSAGES.get(dock_result.code, 'unknown')

            return response
        except Exception as e:
            self.get_logger().info(f'/charger/start_docking2: exception {e}')
            self.charge_session = ''
            if not goal_accepted:
                # goal 未 accepted, 底层没有流程在跑
                self.charger_state.is_docking = False
            response.code = ChargeErrorCode.UNKNOWN
            response.message = f'unknown error: {e}'
            return response

    def charger_stop_docking_service_callback(self, request, response):
        try:
            self.charger_state.is_docking = False
            self.get_logger().info('received a request for /charger/stop_docking service')
            self.get_logger().info("stop charge action")
            self.get_logger().info(f"write 0 to /map/core_restart.txt for /charger/stop_docking")
            try:
                with open('/map/core_restart.txt', 'w', encoding='utf-8') as f:
                    f.write('0\n')
            except Exception as e:
                self.get_logger().info(f"catch exception {str(e)} when write 0 to /map/core_restart.txt for processing /charger/start_docking service.")
            if self.charge_action_client_sendgoal_future != None and self.charge_action_client_sendgoal_future.done():
                charge_goal_handle = self.charge_action_client_sendgoal_future.result()
                charge_goal_handle.cancel_goal_async()
                self.get_logger().info("Charge action canceled! ")
            else:
                self.get_logger().info('charge action had completed or not executing.')
        except Exception:
            self.get_logger().error(f'charger_stop_docking_service_callback exception:\n{traceback.format_exc()}')
        return response

    def charge_action_feedback_callback(self, feedback_msg):
        try:
            state = feedback_msg.feedback.state
            self.get_logger().info(f"=== charge action Feedback ===     {state}", throttle_duration_sec=10)
            # start_docking2 提前成功标准: 连续两次 feedback 为 'charging'
            # (对应 charge_action 的 ChargeActionState.charging), 非 charging 则计数清零
            if state == 'charging':
                self.charge_charging_consecutive += 1
                if self.charge_charging_consecutive >= 2:
                    self.charge_charging_event.set()
            else:
                self.charge_charging_consecutive = 0
        except Exception:
            self.get_logger().error(f'charge_action_feedback_callback exception:\n{traceback.format_exc()}')

    def charge_action_response_callback(self, future):
        try:
            goal_handle = future.result()
            if not goal_handle.accepted:
                self.get_logger().info('=== charge action ===     goal rejected !')
            else:
                self.get_logger().info('=== charge action ===     goal accepted.')
                self.charge_get_future_result = goal_handle.get_result_async()
                self.charge_get_future_result.add_done_callback(self.charge_get_result_callback)
        except Exception:
            self.get_logger().error(f'charge_action_response_callback exception:\n{traceback.format_exc()}')

    def charge_get_result_callback(self, future):
        try:
            result = future.result().result
            self.charger_state.is_docking = False
            self.get_logger().info('=== Charge action ===     result => success: {}'.format(result.success))
        except Exception:
            self.get_logger().error(f'charge_get_result_callback exception:\n{traceback.format_exc()}')
    
    def terminate(self, proc: subprocess.Popen):
        parent_pid = proc.pid 
        parent = psutil.Process(parent_pid)
        index = 1
        self.get_logger().info(f'parent\'childeren num: {len(parent.children(recursive=True))}')
        for child in parent.children(recursive=True):
            self.get_logger().info(f'child pid: {child.pid}, name: {child.name()}')
            self.get_logger().info(f'-----------------------------------------------------------')
        for child in parent.children(recursive=True):  # or parent.children() for recursive=False
            self.get_logger().info(f'child_{index}\'s children num: {len(child.children(recursive=True))}')
            self.get_logger().info(f'Terminating child {index}, pid: {child.pid}, name: {child.name()} ......')
            child.send_signal(SIGINT)
            rt_code = child.wait(15)
            if rt_code == None:
                self.get_logger().info(f'Terminate child {index} (pid: {child.pid}) failed.(need fixed.)')
                # cmd = f'/usr/bin/kill -9 {child.pid}'
                # self.get_logger().info(f'execute "{cmd}" for kill child process.')
                # os.system(cmd)
            else:
                self.get_logger().info(f'Terminate child {index} (pid: {child.pid}) success. rt_code: {rt_code}')            
            index += 1

        parent.send_signal(SIGINT)
        rt_code = parent.wait(20)
        if rt_code == None:
                self.get_logger().info(f'Terminate parent (pid: {parent.pid}) failed.')
        else:
            self.get_logger().info(f'Terminate parent (pid: {parent.pid}) success. rt_code: {rt_code}')
    
def main(args=None):
    rclpy.init(args=args)
    charger_manager_node = chargeManager()
    multi_executor = None
    try:
        multi_executor = MultiThreadedExecutor()
        multi_executor.add_node(charger_manager_node)
        multi_executor.spin()
    except Exception:
        charger_manager_node.get_logger().error(
            f'charge_manager main exception:\n{traceback.format_exc()}')
    finally:
        # SIGINT 时 rclpy 已触发 shutdown, 这里用 rclpy.ok() 避免重复关闭抛 RCLError
        if rclpy.ok():
            rclpy.shutdown()

if __name__ == '__main__':
    main()  
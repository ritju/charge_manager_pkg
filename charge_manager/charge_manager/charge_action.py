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
from capella_ros_msg.msg import Battery
from capella_ros_service_interfaces.msg import ChargeState
from std_srvs.srv import Empty as EmptyForSrv
from std_msgs.msg import Bool
from geometry_msgs.msg import Twist
from capella_ros_msg.msg import Velocities

sigint_received = 0

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
        self.bluetooth_rebooting_num = -1
        self.bluetooth_rebooting_num_last = -1
        self.bluetooth_setup = False
        self.bluetooth_reboot_requested = True
        self.charger_position_bool = False
        self.bluetooth_state_stored = False
        self.core_monitor_state_stored = False
        self.bluetooth_node_stopped = True
        
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
        
        # 创建对接充电桩的客户端
        self.dock_client_ = ActionClient(self, Dock, "dock", callback_group=self.cb_group)

        # /charger/start client
        self.charger_start_client_ = self.create_client(EmptyForSrv, '/charger/start', callback_group=self.cb_group)
        self.charger_start_client_last_request_time = time.time()

        # bluetooth start/stop client
        self.bluetooth_start_client_ = self.create_client(StartBluetooth, '/bluetooth/start', callback_group=self.cb_group)
        self.bluetooth_stop_client_ = self.create_client(StopBluetooth, '/bluetooth/stop', callback_group=self.cb_group)
        
        # 创建 charge action 服务端
        self.charge_action_server_ = ActionServer(self, Charge, 'charge', 
                                                  execute_callback=self.charge_action_execute_callback, 
                                                  callback_group= self.cb_group,
                                                  goal_callback=self.charge_action_goal_callback,
                                                  handle_accepted_callback=self.charge_action_handle_accepted_callback,
                                                  cancel_callback=self.charge_action_cancel_callback,
                                                  )

        self.charge_type = ''
        self.goal_handle = None
        self.init_params() 
        
    def init_params(self):        
        # 初始化蓝牙相关参数
        self.mac = ''
        self.bluetooth_connected = False
        self.future_connect_bluetooth = None
        self.future_disconnect_bluetooth = None
        self.bluetooth_rebooting = False
        self.bluetooth_connected_time = 0.0
        # self.bluetooth_node_stopped = True # fix bug for stopping bluetooth failed(request bluetooth/start before bluetooth/stop completed.)
        if self.connect_bluetooth_client_.wait_for_service(2):
            self.bluetooth_node_stopped = False
            self.bluetooth_setup = True
            self.get_logger().info('bluetooth server is on line')
        else:
            self.bluetooth_node_stopped = True
            self.bluetooth_setup = False
            self.get_logger().info('bluetooth server is off line')
        self.bluetooth_state_stored = False
        self.core_monitor_state_stored = False

        # 蓝牙连接和dock对接状态控制,避免执行状态中再次重复发送goal
        self.dock_executing = False
        self.connect_bluetooth_executing = False       

        # init self.charger_state
        self.charger_state = ChargeState()

        # 初始化timer_loop
        self.timer_loop = None

        self.dock_completed = False
        self.stop_loop = False

        self.bluetooth_connect_num = 0
        self.bluetooth_connect_num_max = 2

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
    
    def raw_vel_sub_callback(self, msg):
        if not self.stop_loop and self.dock_completed:
            linear_x = abs(msg.linear_x)
            angular_z = abs(msg.angular_z)
            if linear_x > 0.1 or angular_z > 0.1:
                self.get_logger().info(f'检测到/raw_vel topic linear_x: {linear_x}, angular_z: {angular_z}, 停止充电。')
                self.stop_loop = True
            else:
                pass
        else:
            pass

    def timer_loop_callback(self):
        # self.get_logger().info(f'setup: {self.bluetooth_setup}, requested: {self.bluetooth_reboot_requested}, stopped: {self.bluetooth_node_stopped}, stop_loop: {self.stop_loop}', throttle_duration_sec = 3)
        if not self.bluetooth_setup and self.bluetooth_reboot_requested and self.bluetooth_node_stopped and not self.stop_loop:
            self.get_logger().info('setup bluetooth server node ......')
            # self.get_logger().info(f'self.stop_loop: {"True" if self.stop_loop else "False"}')
            if self.bluetooth_start_client_.wait_for_service(1):
                self.bluetooth_reboot_requested = False
                self.connect_bluetooth_executing = False
                self.bluetooth_rebooting_num += 1
                self.get_logger().info(f'-------- call /bluetooth/start service --------')
                self.bluetooth_start_future = self.bluetooth_start_client_.call_async(StartBluetooth.Request())
                self.bluetooth_start_future.add_done_callback(self.bluetooth_start_future_done_callback)
            else:
                self.get_logger().info('bluetooth/start service not on line, waiting', throttle_duration_sec = 5)

        if self.bluetooth_rebooting_num_last != self.bluetooth_rebooting_num:
            self.get_logger().info(f'bluetooth server node reboot numbers: {self.bluetooth_rebooting_num}.')
            self.bluetooth_rebooting_num_last = self.bluetooth_rebooting_num
                
        # self.get_logger().info(f'connected: {self.bluetooth_connected}, connect_bluetooth_executing: {self.connect_bluetooth_executing}, setup: {self.bluetooth_setup}', throttle_duration_sec=10)
        if self.bluetooth_setup:
            if not self.bluetooth_connected and  not self.connect_bluetooth_executing and not self.bluetooth_rebooting and not self.stop_loop: # do not connect bluetooth when rebooting bluetooth server
                self.connect_bluetooth_executing = True
                self.get_logger().info(f"-------- call /connect_bluetooth service, {self.bluetooth_connect_num + 1} / {self.bluetooth_connect_num_max} --------")
                # self.get_logger().info(f'self.stop_loop: {"True" if self.stop_loop else "False"}')
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
        # self.get_logger().info(f"=== charge action ===      state: {self.feedback_msg.state}", throttle_duration_sec=4)
        self.goal_handle.publish_feedback(self.feedback_msg)
        

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
            self.bluetooth_rebooting_num = -1
            self.bluetooth_rebooting_num_last = -1
            self.get_logger().info('charge_action_goal_callback')
            self.get_logger().info(f'self.mac: {self.mac}')
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
                    f.write(self.mac)
            except Exception as e:
                self.get_logger().info(f"catch exception {str(e)} when write 1 to /map/core_restart.txt for processing /charge action started.")
        
        self.loop_thread = threading.Thread(target=self.loop_,daemon=True)
        self.loop_thread.start()

        while True:
            # if self.goal_handle and sigint_received:
            #     self.get_logger().info(f'received a SIGINT signal when executing /charge action, aborting ......')
            #     self.goal_handle.abort()

            if self.dock_completed:
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
                    result.success = True
                    self.goal_handle.succeed()
                    try:
                        self.get_logger().info(f'存储充电状态 0 和 mac: {self.mac} 到/map/charge_restore.txt.')
                        with open('/map/charge_restore.txt', 'w', encoding='utf-8') as f:
                            f.write('0\n')
                            f.write(self.mac)
                    except Exception as e:
                        self.get_logger().info(f'存储充电状态 0 catch exception: {str(e)}')
                    self.msg_state_pub.data = False
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
        # self.timer_loop = self.create_timer(0.2, self.timer_loop_callback, self.cb_group)
        while True:
            self.timer_loop_callback()
            if self.dock_completed:
                if self.battery_ >= 1.01 or self.stop_loop or not self.charger_position_bool:
                    break
            else:
                continue
            time.sleep(1)

    
    # charge_action cancel callback
    def charge_action_cancel_callback(self, goal_handle):
        self.get_logger().info("Received request to cancel charge action servo goal")
        self.dock_completed = True
        self.stop_loop = True
        if self.dock_executing:
            goal_handle = self.dock_client_sendgoal_future.result()
            goal_handle.cancel_goal_async()
            self.get_logger().info('cancel dock action')
            self.dock_executing = True
        return CancelResponse.ACCEPT

    def connect_bluetooth_done_callback(self, future_connect_bluetooth):
        response = future_connect_bluetooth.result()
        self.get_logger().info(f'bluetooth connection {"True" if response.success else "False"}, cost {response.connection_time} seconds, result =>{response.result}')
        self.bluetooth_connected = response.success
        self.bluetooth_connected_time = time.time()
        if response.success:
            self.bluetooth_connect_num = 0
            self.bluetooth_rebooting_num = -1
            self.bluetooth_rebooting_num_last = -1
        
        if self.bluetooth_connect_num >= self.bluetooth_connect_num_max and not response.success:
            num_old = self.bluetooth_connect_num
            self.bluetooth_connect_num = 0
            self.bluetooth_reboot_requested = True
            self.bluetooth_rebooting = True
            # self.bluetooth_setup = False #fix bug for stopping bluetooth failed(request bluetooth/start before bluetooth/stop completed.)
            # self.get_logger().info(f'bluetooth_connect_num is {num_old} >= {self.bluetooth_connect_num_max}, reboot bluetooth server node')
            self.get_logger().info('-------- call /bluetooth/stop service --------')
            self.bluetooth_stop_future = self.bluetooth_stop_client_.call_async(StopBluetooth.Request())
            self.bluetooth_stop_future.add_done_callback(self.bluetooth_stop_future_done_callback)      
            
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
        else:
            self.get_logger().info('dock goal accepted.')
            self._dock_get_future_result = goal_handle.get_result_async()
            self._dock_get_future_result.add_done_callback(self.dock_get_result_callback)

    def dock_get_result_callback(self, future):
        result = future.result().result
        self.get_logger().info('Dock result => is_docked: {}'.format(result.is_docked))
        if not result.is_docked:
            self.get_logger().info('Dock action failed, Charge Action aborted')
            self.stop_loop = True
        self.dock_executing = False
        self.dock_completed = True
    
    def bluetooth_stop_future_done_callback(self, future):
        response = future.result()
        if response.success:
            self.get_logger().info('bluetooth/stop service result: success.')
            self.get_logger().info(f'infos: {response.infos}')
            self.bluetooth_setup = False
            self.bluetooth_node_stopped = True
        else:
            self.get_logger().info('bluetooth/stop service result: failed.')
            self.get_logger().warn(f'infos: {response.infos}')
            self.get_logger().info(f'cost_time: {response.cost_time} seconds.')


    def bluetooth_start_future_done_callback(self, future):
        response = future.result()
        if response.success:
            self.get_logger().info('bluetooth/start service result: success.')
            self.get_logger().info(f'infos: {response.infos}')
            self.get_logger().info(f'cost_time: {response.cost_time} seconds.')
            self.bluetooth_setup = True
            self.bluetooth_node_stopped = False
            self.bluetooth_rebooting = False
        else:
            self.get_logger().info('bluetooth/start service result: failed.')
            self.get_logger().warn(f'infos: {response.infos}')
            self.bluetooth_rebooting = False

def sigint_handle(signal, frame):
    sigint_received = 1
    print('************ received a SIGINT signal ************')
    time.sleep(5)

def main(args=None):
    # signal.signal(signal.SIGINT, sigint_handle)
    rclpy.init(args=args)
    charge_action_node = ChargeAction()
    multi_executor = MultiThreadedExecutor()
    multi_executor.add_node(charge_action_node)
    multi_executor.spin()
    multi_executor.shutdown()

if __name__ == '__main__':
    main()  
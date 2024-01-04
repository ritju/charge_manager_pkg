import bleak
from bleak import BleakClient, BleakScanner

import rclpy
from rclpy.node import Node
from rclpy.action import ActionServer, GoalResponse, CancelResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.action import ActionClient
from rclpy.executors import MultiThreadedExecutor

import time
import threading

from charge_manager_msgs.srv import ConnectBluetooth
from charge_manager_msgs.action import Charge
from capella_ros_dock_msgs.action import Dock
from capella_ros_msg.msg import Battery
from capella_ros_service_interfaces.msg import ChargeState
from std_srvs.srv import Empty

class ChargeActionState():
    idle = 'idle'
    connectbluetooth = 'connecting_bluetooth'
    docking = 'docking'
    charging = 'charging'

class ChargeAction(Node):
    
    def __init__(self):
        super().__init__('charge_action_server')
        self.get_logger().info('*** charge action ***     started.')
        self.battery_ = 0.0

        # 定义 callback_group 类型
        self.cb_group = ReentrantCallbackGroup()

        self.init_params() 

        # sub for battery
        self.battery_sub_ = self.create_subscription(Battery, 'battery', self.battery_sub_callback, 10, callback_group=self.cb_group)

        # sub for /charger/state
        self.charger_state_sub = self.create_subscription(ChargeState, '/charger/state', self.charger_state_sub_callback, 10, callback_group=self.cb_group)
        
        # 创建连接蓝牙的客户端
        self.connect_bluetooth_client_ = self.create_client(ConnectBluetooth, 'connect_bluetooth',callback_group=self.cb_group)
        
        # 创建对接充电桩的客户端
        self.dock_client_ = ActionClient(self, Dock, "dock", callback_group=self.cb_group)

        # /charger/start client
        self.charger_start_client_ = self.create_client(Empty, '/charger/start', callback_group=self.cb_group)
        self.charger_start_client_last_request_time = time.time()

        # bluetooth start/stop client
        self.bluetooth_start_client_ = self.create_client(Empty, '/bluetooth/start', callback_group=self.cb_group)
        self.bluetooth_stop_client_ = self.create_client(Empty, '/bluetooth/stop', callback_group=self.cb_group)
        
        # 创建 charge action 服务端
        self.charge_action_server_ = ActionServer(self, Charge, 'charge', 
                                                  execute_callback=self.charge_action_execute_callback, 
                                                  callback_group= self.cb_group,
                                                  goal_callback=self.charge_action_goal_callback,
                                                  handle_accepted_callback=self.charge_action_handle_accepted_callback,
                                                  cancel_callback=self.charge_action_cancel_callback,
                                                  )
        
    def init_params(self):
        # 初始化蓝牙相关参数
        self.mac = ''
        self.bluetooth_connected = False
        self.future_connect_bluetooth = None
        self.bluetooth_setup = False
        self.bluetooth_rebooting = False

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
        self.bluetooth_connect_num_max = 5

    def battery_sub_callback(self, msg):
        self.battery_ = msg.res_cap
    
    def charger_state_sub_callback(self, msg):
        self.bluetooth_connected = True if msg.pid == self.mac and msg.pid != '' else False
        self.charger_state = msg

    def timer_loop_callback(self):
        if not self.bluetooth_setup and not self.bluetooth_rebooting:
            self.get_logger().info('setup bluetooth server node ......')
            self.bluetooth_rebooting = True
            self.bluetooth_start_future = self.bluetooth_start_client_.call_async(Empty.Request())
            self.bluetooth_start_future.add_done_callback(self.bluetooth_start_future_done_callback)

        if not self.bluetooth_connected and  not self.connect_bluetooth_executing :
            if self.bluetooth_setup:
                self.connect_bluetooth_executing = True
                self.get_logger().info(f"-------- call /connect_bluetooth service, {self.bluetooth_connect_num + 1} / {self.bluetooth_connect_num_max} --------")
                request = ConnectBluetooth.Request()
                request.mac = self.mac
                self.get_logger().info(f'request.mac {request.mac}')
                self.bluetooth_connect_num += 1
                
                self.future_connect_bluetooth = self.connect_bluetooth_client_.call_async(request)
                self.future_connect_bluetooth.add_done_callback(self.connect_bluetooth_done_callback)
            else:
                self.get_logger().info('waiting for bluetooth node ...', throttle_duration_sec = 1)

        # if not self.dock_executing and not self.dock_completed:
        #     self.dock_executing = True
        #     self.get_logger().info('-------- call /dock action --------')
        #     dock_msg = Dock.Goal()
        #     while not self.dock_client_.wait_for_server(2):
        #         self.get_logger().info('Dock action server not available.', throttle_duration_sec = 2)
        #     self.dock_client_sendgoal_future = self.dock_client_.send_goal_async(dock_msg, self.dock_feedback_callback)
        #     self.dock_client_sendgoal_future.add_done_callback(self.dock_response_callback)
        # else:
        #     if self.charger_state.has_contact and not self.charger_state.is_charging:
        #         request = Empty.Request()
        #         if time.time() - self.charger_start_client_last_request_time > 2.0:
        #             self.charger_start_client_.call_async(request)
        #             self.charger_start_client_last_request_time = time.time()
        #     elif self.charger_state.has_contact and self.charger_state.is_charging:
        #         self.feedback_msg.state = ChargeActionState.charging
        #     else:
        #         pass

        if (self.connect_bluetooth_executing):
            self.feedback_msg.state = ChargeActionState.connectbluetooth
        elif self.dock_executing:
            self.feedback_msg.state = ChargeActionState.docking
        elif self.charger_state.is_charging:
            self.feedback_msg.state = ChargeActionState.charging
        self.get_logger().info(f"=== charge action ===      state: {self.feedback_msg.state}", throttle_duration_sec=4)
        self.goal_handle.publish_feedback(self.feedback_msg)
        

    # charge_action goal_callback
    def charge_action_goal_callback(self, goal_request):
        self.mac = goal_request.mac
        self.get_logger().info('charge_action_goal_callback')
        self.get_logger().info(f'self.mac: {self.mac}')
        if self.dock_executing:
            self.get_logger().info('Received new Charge Action when executing Charge action.')
            return GoalResponse.ACCEPT
        else:
            self.get_logger().info('Received a Charge Action, accepted and executing.')
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

        self.feedback_msg = Charge.Feedback()
        self.feedback_msg.state = ChargeActionState.idle
        
        self.loop_thread = threading.Thread(target=self.loop_,daemon=True)
        self.loop_thread.start()

        while True:
            if self.battery_ > 0.999:
                result = Charge.Result()
                result.success = True
                self.goal_handle.succeed()
                return result
            else:
                if self.dock_completed and not self.charger_state.has_contact:
                    result = Charge.Result()
                    result.success = False
                    self.goal_handle.succeed()
                    return result
            time.sleep(2)
    
    def loop_(self):
        self.get_logger().info('loop started')
        # self.timer_loop = self.create_timer(0.2, self.timer_loop_callback, self.cb_group)
        while True:
            self.timer_loop_callback()
            if self.battery_ > 0.999 or self.stop_loop:
                break
            time.sleep(0.2)

    
    # charge_action cancel callback
    def charge_action_cancel_callback(self, goal_handle):
        self.get_logger().info("Received request to cancel charge action servo goal")
        self.stop_loop = True
        if self.dock_executing:
            goal_handle = self.dock_client_sendgoal_future.result()
            goal_handle.cancel_goal_async()
            self.get_logger().info('cancel dock action')
            self.dock_executing = True
        return CancelResponse.ACCEPT

    def connect_bluetooth_done_callback(self, future_connect_bluetooth):
        self.connect_bluetooth_executing = False
        response = future_connect_bluetooth.result()
        self.bluetooth_connected = response.success
        if response.success:
            self.bluetooth_connect_num = 0

        if self.bluetooth_connect_num >= self.bluetooth_connect_num_max:
            self.get_logger().info(f'bluetooth_connect_num is {self.bluetooth_connect_num} >= {self.bluetooth_connect_num_max}, reboot bluetooth server node')
            self.bluetooth_connect_num = 0
            self.bluetooth_setup = False
            self.bluetooth_rebooting = False
            self.bluetooth_stop_future = self.bluetooth_stop_client_.call_async(Empty.Request())
            self.bluetooth_stop_future.add_done_callback(self.bluetooth_stop_future_done_callback)

        self.get_logger().info(f'bluetooth connection {"True" if response.success else "False"}, cost {response.connection_time} seconds, result =>{response.result}')
    
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
        time.sleep(6)
        self.get_logger().info('stop bluetooth node success.')

    def bluetooth_start_future_done_callback(self, future):
        self.get_logger().info('start bluetooth node success')
        self.bluetooth_setup = True


def main(args=None):
    rclpy.init(args=args)
    charge_action_node = ChargeAction()
    multi_executor = MultiThreadedExecutor()
    multi_executor.add_node(charge_action_node)
    multi_executor.spin()
    rclpy.shutdown()

if __name__ == '__main__':
    main()  
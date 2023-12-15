import bleak
from bleak import BleakClient, BleakScanner

import rclpy
from rclpy.node import Node
from rclpy.action import ActionServer, GoalResponse, CancelResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.action import ActionClient

import time

from capella_ros_service_interfaces import ChargerState
from charge_manager_msgs.msg import ChargerState2
from charge_manager_msgs.msg import ConnectBluetooth
from charge_manager_msgs.action import Charge
from capella_ros_dock_msgs.action import Dock

class ChargeAction(Node):
    
    def __init__(self):
        super.__init__('charge_action_server')      

        # 定义 callback_group 类型
        self.cb_group = ReentrantCallbackGroup()

        self.init_params() 

        # 订阅蓝牙server发送的 ChargerState2
        self.charger_state2_sub_ = self.create_subscription(ChargerState2, '/charger/state2', self.charger_state2_sub_callback, callback_group=self.cb_group)
        # 创建连接蓝牙的客户端
        self.connect_bluetooth_client_ = self.create_client(ConnectBluetooth, 'connect_bluetooth',callback_group=self.cb_group)
        # 创建对接充电桩的客户端
        self.dock_client_ = ActionClient(self, Dock, "dock", callback_group=self.cb_group)
        # 创建 charge action 服务端
        self.charge_action_server_ = ActionServer(self, Charge, 'charge', 
                                                  execute_callback=self.charge_action_execute_callback, 
                                                  callback_group= self.cb_group,
                                                  goal_callback=self.charge_action_goal_callback,
                                                  handle_accepted_callback=self.charge_action_handle_accepted_callback,
                                                  cancel_callback=self.charge_action_cancel_callback,
                                                  )
        
        # 初始化 /charger/state publisher
        self.charger_state_publisher = self.create_publisher(ChargerState, '/charger/state', 5, callback_group=self.cb_group)
        self.timer_pub_charger_state = self.create_timer(0.2, self.timer_pub_charger_state_callback, callback_group=self.cb_group)
    
    def init_params(self):
        # 初始化蓝牙相关参数
        self.mac = None
        self.ble_device = None
        self.bleak_client = None
        self.bluetooth_connected = False
        self.future_connect_bluetooth = None

        # 初始化 self.charger_state
        self.charger_state = ChargerState()
        self.charger_state.pid = ''
        self.charger_state.has_contact = False
        self.charger_state.is_charging = False
        self.charger_state.is_docking = True

        # 蓝牙连接和dock对接状态控制,避免执行状态中再次重复发送goal
        self.dock_executing = False
        self.connect_bluetooth_executing = False

        # 初始化timer_loop
        self.timer_loop = None

    def timer_pub_charger_state_callback(self):
         self.charger_state_publisher.publish(self.charger_state)

    def charger_state2_sub_callback(self, msg):
        self.bluetooth_connected = True if msg.pid == self.mac else False
        self.charger_state.pid = msg.pid
        self.charger_state.has_contact = msg.has_contact
        self.charger_state.is_charging = msg.is_charging

    # charge_action 服务端 execute_callback
    def charge_action_execute_callback(self, goal_handle):
        self.get_logger().info("executing charge action......")
        self.mac = goal_handle.request.mac
        self.goal_handle = goal_handle
        
        self.init_params()
        self.timer_loop = self.create_timer(0.2, self.timer_loop_callback, self.cb_group)

    def timer_loop_callback(self):
        if self.bluetooth_connected:
            if not self.dock_executing:
                self.dock_executing = True
                self.get_logger().info('start docking action')
                dock_msg = Dock.Goal()
                while not self.dock_client.wait_for_server(2):
                    self.get_logger().info('Dock action server not available.', throttle_duration_sec = 2)
                self.dock_client_sendgoal_future = self.dock_client_.send_goal_async(dock_msg, self.dock_feedback_callback)
                self.dock_client_sendgoal_future.add_done_callback(self.dock_response_callback)
            else:
                pass
        else:            
            if not self.connect_bluetooth_executing:
                self.connect_bluetooth_executing = True
                request = ConnectBluetooth.Request()
                request.mac = self.mac
                self.future_connect_bluetooth = self.connect_bluetooth_client_.call_async(request)
                self.future_connect_bluetooth.add_done_callback(self.connect_bluetooth_done_callback)
            else:
                pass

    # charge_action goal_callback
    def charge_action_goal_callback(self, goal_request):
        self.mac = goal_request.mac
        self.get_logger().info('charge_action_goal_callback')
        self.get_logger.info(f'self.mac: {self.mac}')
        if self.dock_executing:
            self.get_logger().info('Received new Charge Action when executing Charge action.')
            return GoalResponse.ACCEPT
        else:
            self.get_logger().info('Received a Charge Action, accepted and executing.')
            return GoalResponse.ACCEPT

    # charge_action handle_accepted_callback
    def charge_action_handle_accepted_callback(self, goal_handle):
        self.get_logger().info('charge_action_handle_accepted_callback')
        goal_handle.execute()
    
    # charge_action cancel callback
    def charge_action_cancel_callback(self, goal_handle):
        self.get_logger().info("Received request to cancel charge action servo goal")
        self.timer_loop.reset()
        if self.dock_executing:
            self.dock_goal_handle.cancel_goal_async()
            self.get_logger().info('cancel dock action')
            self.dock_executing = True
        return CancelResponse.ACCEPT

    def connect_bluetooth_done_callback(self, future_connect_bluetooth):
        self.connect_bluetooth_executing = False
        response = future_connect_bluetooth.result()
        self.bluetooth_connected = response.success
        self.get_logger().info(f'bluetooth connection cost {response.connection_time} seconds, result =>{response.result}')
    
    def dock_feedback_callback(self, feedback_msg):
        feedback = feedback_msg.feedback
        self.get_logger().info('dock feedback => sees_dock : {}'.format(feedback.sees_dock))

    def dock_response_callback(self, future):
        goal_handle = future.result()
        if not goal_handle.accepted:
            self.get_logger().info('dock goal rejected !')
            self.charge_state.is_docking = False
        else:
            self.get_logger().info('dock goal accepted.')
            self.charge_state.is_docking = True
            self._dock_get_future_result = goal_handle.get_result_async()
            self._dock_get_future_result.add_done_callback(self.dock_get_result_callback)

    def dock_get_result_callback(self, future):
        result = future.result().result
        self.get_logger().info('Dock result => is_docked: {}'.format(result.is_docked))
        self.charger_state.is_docking =  False
        self.dock_executing = False

def main(args=None):
    rclpy.init(args=args)
    service = ChargeAction('charger_action_server_node')
    rclpy.spin(service)
    rclpy.shutdown()

if __name__ == '__main__':
    main()  
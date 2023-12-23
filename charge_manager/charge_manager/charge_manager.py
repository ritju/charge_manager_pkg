import rclpy
from rclpy.node import Node
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.action import ActionClient
from rclpy.qos import DurabilityPolicy,ReliabilityPolicy,QoSProfile,HistoryPolicy
from rclpy.task import Future

from std_srvs.srv import Empty
from std_msgs.msg import Int8
from std_msgs.msg import String
from charge_manager_msgs.action import Charge
from charge_manager_msgs.msg import ChargeState2
from capella_ros_service_interfaces.msg import ChargeState
from geometry_msgs.msg import Twist

class chargeManager(Node):
    
    def __init__(self):
        super().__init__('charge_manager_node')
        self.get_logger().info('*** charge_manager_node *** started.')

        self.mac = ''
        self.charge_action_client_sendgoal_future = None

        callback_group_type = ReentrantCallbackGroup()
        self.command_publisher = self.create_publisher(Int8, 'bluetooth_command', 1, callback_group=callback_group_type)

        # 初始化 self.charger_state
        self.charger_state = ChargeState()
        self.charger_state.pid = ''
        self.charger_state.has_contact = False
        self.charger_state.is_charging = False
        self.charger_state.is_docking = False

        # /charger/id subscription
        charger_id_sub_qos = QoSProfile(depth=1)
        charger_id_sub_qos.reliability = ReliabilityPolicy.RELIABLE
        charger_id_sub_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
        charger_id_sub_qos.history = HistoryPolicy.KEEP_LAST
        self.charger_id_sub = self.create_subscription(String, '/charger/id', self.charger_id_sub_callback, charger_id_sub_qos)
        
        # 订阅蓝牙server发送的 ChargeState2
        self.charger_state2_sub_ = self.create_subscription(ChargeState2, '/charger/state2', self.charger_state2_sub_callback, 5, callback_group=callback_group_type)

        # 初始化 /charger/state publisher
        self.charger_state_publisher = self.create_publisher(ChargeState, '/charger/state', 5, callback_group=callback_group_type)
        self.timer_pub_charger_state = self.create_timer(0.2, self.timer_pub_charger_state_callback, callback_group=callback_group_type)
        
        # 初始化 zero_cmd_vel_publisher
        self.zero_cmd_vel_publisher = self.create_publisher = self.create_publisher(Twist, '/cmd_vel', 1, callback_group=callback_group_type)
        
        # /charger/start service
        self.charger_start_service = self.create_service(Empty, '/charger/start', self.charger_start_service_callback, callback_group=callback_group_type)
        
        # /charger/stop service
        self.charger_stop_service = self.create_service(Empty, '/charger/stop', self.charger_stop_service_callback, callback_group=callback_group_type)
        
        # /charger/start_docking
        self.charger_start_docking_service = self.create_service(Empty, '/charger/start_docking', self.charger_start_docking_service_callback, callback_group=callback_group_type)
        
        # /charger/stop_docking
        self.charger_start_docking_service = self.create_service(Empty, '/charger/stop_docking', self.charger_stop_docking_service_callback, callback_group=callback_group_type)

        self.charge_action_client = ActionClient(self, Charge, 'charge', callback_group=callback_group_type)
      
    def timer_pub_charger_state_callback(self):
         self.charger_state_publisher.publish(self.charger_state)
         if self.charger_state.is_charging and self.charger_state.has_contact:
             zero_cmd = Twist()
             zero_cmd.linear.x = 0.0
             zero_cmd.angular.z = 0.0
             self.zero_cmd_vel_publisher.publish(zero_cmd)

    def charger_state2_sub_callback(self, msg):
        self.charger_state.pid = msg.pid
        self.charger_state.has_contact = msg.has_contact
        self.charger_state.is_charging = msg.is_charging
        if msg.has_contact:
            self.charger_state.is_docking = False
    
    def charger_id_sub_callback(self, msg):
        self.mac = msg.data
    
    def charger_start_service_callback(self, request, response):
        self.get_logger().info('received a request for /charger/start service')
        msg = Int8()
        msg.data = 1
        self.command_publisher.publish(msg)
        return response

    def charger_stop_service_callback(self, request, response):
        self.get_logger().info('received a request for /charger/stop service')
        msg = Int8()
        msg.data = 0
        self.command_publisher.publish(msg)
        return response

    def charger_start_docking_service_callback(self, request, response):
        self.get_logger().info('received a request for /charger/start_docking service')
        self.get_logger().info("start charge action")
        self.charger_state.is_docking = True
        charge_msg = Charge.Goal()
        charge_msg.mac = self.mac
        charge_msg.mac = '94:C9:60:43:BD:FD'
        while not self.charge_action_client.wait_for_server(2):
            self.get_logger().info('Charge action server not available.')
        self.charge_action_client_sendgoal_future = self.charge_action_client.send_goal_async(charge_msg, self.charge_action_feedback_callback)
        self.charge_action_client_sendgoal_future.add_done_callback(self.charge_action_response_callback)
        return response
    
    def charger_stop_docking_service_callback(self, request, response):
        self.charger_state.is_docking = False
        self.get_logger().info('received a request for /charger/stop_docking service')
        self.get_logger().info("stop charge action")
        if self.charge_action_client_sendgoal_future != None and isinstance(self.charge_action_client_sendgoal_future, Future):
            self.charge_goal_handle = self.charge_action_client_sendgoal_future.result()
            cancel_goal_future = self.charge_goal_handle.cancel_goal_async()
            self.get_logger().info("Charge action canceled! ")
        else:
            self.get_logger().info('charge action had completed or not executing.')
        return response

    def charge_action_feedback_callback(self, feedback_msg):
        self.get_logger().info(f"=== charge action ===     feedback: {feedback_msg.feedback.state}", throttle_duration_sec=3)

    def charge_action_response_callback(self, future):
        goal_handle = future.result()
        if not goal_handle.accepted:
            self.get_logger().info('=== charge action ===     goal rejected !')
        else:
            self.get_logger().info('=== charge action ===     goal accepted.')
            self.charge_get_future_result = goal_handle.get_result_async()
            self.charge_get_future_result.add_done_callback(self.charge_get_result_callback)

    def charge_get_result_callback(self, future):
        result = future.result().result
        self.charger_state.is_docking = False
        self.get_logger().info('=== Charge action ===     result => success: {}'.format(result.success)) 
    
def main(args=None):
    rclpy.init(args=args)
    charger_manager_node = chargeManager()
    rclpy.spin(charger_manager_node)
    rclpy.shutdown()

if __name__ == '__main__':
    main()  
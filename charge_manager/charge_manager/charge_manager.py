import rclpy
from rclpy.node import Node
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.action import ActionClient

from capella_ros_service_interfaces.msg import ChargerState
from std_srvs.srv import Empty
from std_msgs.msg import Int8
from charge_manager_msgs.action import Charge

class chargeManager(Node):
    
    def __init__(self):
        super.__init__('charge_manager_node')
        self.get_logger().info('charge_manager_node started ......')

        callback_group_type = ReentrantCallbackGroup()
        self.command_publisher = self.create_publisher(Int8, 'bluetooth_command', callback_group=callback_group_type)
        # /charger/start service
        self.charger_start_service = self.create_service(Empty, '/charger/start', self.charger_start_service_callback, callback_group=callback_group_type)
        # /charger/stop service
        self.charger_stop_service = self.create_service(Empty, '/charger/stop', self.charger_stop_service_callback, callback_group=callback_group_type)
        # /charger/start_docking
        self.charger_start_docking_service = self.create_service(Empty, '/charger/start_docking', self.charger_start_docking_service_callback, callback_group=callback_group_type)
        # /charger/stop_docking
        self.charger_start_docking_service = self.create_service(Empty, '/charger/stop_docking', self.charger_stop_docking_service_callback, callback_group=callback_group_type)

        self.charge_action_client = ActionClient(Charge, 'charge', callback_group=callback_group_type)

    def charger_start_service_callback(self, request, response):
        msg = Int8()
        msg.data = 1
        self.command_publisher.publish(msg)

    def charger_stop_service_callback(self, request, response):
        msg = Int8()
        msg.data = 0
        self.command_publisher.publish(msg)

    def charger_start_docking_service_callback(self, request, response):
        charge_msg = Charge.Goal()
        while not self.charge_action_client.wait_for_server(2):
            self.get_logger().info('Charge action server not available.')
        self.charge_client_sendgoal_future = self.charge_client.send_goal_async(charge_msg, self.charge_feedback_callback)
        self.charge_client_sendgoal_future.add_done_callback(self.charge_response_callback)

    def charge_feedback_callback(self, feedback_msg):
        pass

    def charge_response_callback(self, future):
        goal_handle = future.result()
        if not goal_handle.accepted:
            self.get_logger().info('charge goal rejected !')
        else:
            self.get_logger().info('charge goal accepted.')
            self.charge_get_future_result = goal_handle.get_result_async()
            self.charge_get_future_result.add_done_callback(self.charge_get_result_callback)

    def charge_get_result_callback(self, future):
        result = future.result().result
        self.get_logger().info('Dock result => success: {}'.format(result.success)) 

    def charger_stop_docking_service_callback(self, request, response):
        self.get_logger().info("stop charge action")
        self.charge_goal_handle = self.charge_client_sendgoal_future.result()
        cancel_goal_future = self.charge_goal_handle.cancel_goal_async()
        self.get_logger().info("Charge action canceled! ")
    
def main(args=None):
    rclpy.init(args=args)
    service = chargeManager()
    rclpy.spin(service)
    rclpy.shutdown()

if __name__ == '__main__':
    main()  
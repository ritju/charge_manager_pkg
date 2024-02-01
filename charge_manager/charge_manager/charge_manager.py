import rclpy
from rclpy.node import Node
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.action import ActionClient
from rclpy.qos import DurabilityPolicy,ReliabilityPolicy,QoSProfile,HistoryPolicy
from rclpy.task import Future
from rclpy.executors import MultiThreadedExecutor
import psutil
import subprocess
from signal import SIGINT, SIGTERM
import time
import os

from std_srvs.srv import Empty
from std_msgs.msg import Int8
from std_msgs.msg import String
from charge_manager_msgs.action import Charge
from charge_manager_msgs.msg import ChargeState2
from charge_manager_msgs.msg import BluetoothStatus
from charge_manager_msgs.srv import StartBluetooth, StopBluetooth
from capella_ros_service_interfaces.msg import ChargeState
from geometry_msgs.msg import Twist

class chargeManager(Node):
    
    def __init__(self):
        super().__init__('charge_manager_node')
        self.get_logger().info('*** charge_manager_node *** started.')
        self.get_logger().info(f'manger_node => pid: {os.getpid()}')

        self.mac = ''
        self.charge_action_client_sendgoal_future = None

        callback_group_type = ReentrantCallbackGroup()
        self.command_publisher = self.create_publisher(Int8, 'bluetooth_command', 1, callback_group=callback_group_type)

        # init bluetooth params
        self.bluetooth_status = BluetoothStatus.DOWN
        self.bluetooth_proc = None

        # 初始化 self.charger_state
        self.charger_state = ChargeState()
        self.charger_state.pid = ''
        self.charger_state.has_contact = False
        self.charger_state.is_charging = False
        self.charger_state.is_docking = False

        # /charger/id subscription
        charger_id_sub_qos = QoSProfile(depth=1)
        charger_id_sub_qos.reliability = ReliabilityPolicy.BEST_EFFORT
        charger_id_sub_qos.history = HistoryPolicy.KEEP_LAST
        charger_id_sub_qos.durability = DurabilityPolicy.VOLATILE
        self.charger_id_sub = self.create_subscription(String, '/charger/id', self.charger_id_sub_callback, charger_id_sub_qos)
        
        charger_state_qos = QoSProfile(depth=1)
        charger_state_qos.reliability = ReliabilityPolicy.BEST_EFFORT
        charger_state_qos.history = HistoryPolicy.KEEP_LAST
        charger_state_qos.durability = DurabilityPolicy.VOLATILE

        charger_state_qos2 = QoSProfile(depth=1)
        charger_state_qos2.reliability = ReliabilityPolicy.RELIABLE
        charger_state_qos2.history = HistoryPolicy.KEEP_LAST
        charger_state_qos2.durability = DurabilityPolicy.VOLATILE
        # 订阅蓝牙server发送的 ChargeState2
        self.charger_state2_sub_ = self.create_subscription(ChargeState2, '/charger/state2', self.charger_state2_sub_callback, charger_state_qos, callback_group=callback_group_type)

        # 初始化 /charger/state publisher        
        self.charger_state_publisher = self.create_publisher(ChargeState, '/charger/state', charger_state_qos2, callback_group=callback_group_type)
        self.timer_pub_charger_state = self.create_timer(0.05, self.timer_pub_charger_state_callback, callback_group=callback_group_type)
        
        # 初始化 zero_cmd_vel_publisher
        # self.zero_cmd_vel_publisher = self.create_publisher(Twist, '/cmd_vel', 1, callback_group=callback_group_type)
        
        # /charger/start service
        self.charger_start_service = self.create_service(Empty, '/charger/start', self.charger_start_service_callback, callback_group=callback_group_type)
        
        # /charger/stop service
        self.charger_stop_service = self.create_service(Empty, '/charger/stop', self.charger_stop_service_callback, callback_group=callback_group_type)
        
        # /charger/start_docking
        self.charger_start_docking_service = self.create_service(Empty, '/charger/start_docking', self.charger_start_docking_service_callback, callback_group=callback_group_type)
        
        # /charger/stop_docking
        self.charger_stop_docking_service = self.create_service(Empty, '/charger/stop_docking', self.charger_stop_docking_service_callback, callback_group=callback_group_type)

        # /bluetooth/start
        self.bluetooth_start_service = self.create_service(StartBluetooth, '/bluetooth/start', self.bluetooth_start_callback, callback_group=callback_group_type)

        # /bluetooth/stop
        self.bluetooth_stop_service = self.create_service(StopBluetooth, '/bluetooth/stop', self.bluetooth_stop_callback, callback_group=callback_group_type)

        self.charge_action_client = ActionClient(self, Charge, 'charge', callback_group=callback_group_type)

        # restore charge
        self.get_logger().info('restore charging or not ...')
        time.sleep(3)
        with open('/map/charge_restore.txt', 'r', encoding='utf-8') as f:
            restore = (int)(f.readline().strip('\n'))
            self.get_logger().info(f'restore: {restore}')
            if restore == 1:
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

      
    def timer_pub_charger_state_callback(self):
         self.charger_state_publisher.publish(self.charger_state)
        #  if self.charger_state.is_charging and self.charger_state.has_contact:
        #      zero_cmd = Twist()
        #      zero_cmd.linear.x = 0.0
        #      zero_cmd.angular.z = 0.0
        #      self.zero_cmd_vel_publisher.publish(zero_cmd)

    def charger_state2_sub_callback(self, msg):
        self.charger_state.pid = msg.pid
        self.charger_state.has_contact = msg.has_contact
        self.charger_state.is_charging = msg.is_charging
        if msg.has_contact:
            self.charger_state.is_docking = False
    
    def charger_id_sub_callback(self, msg):
        if msg.data != '':
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
        # charge_msg.mac = '94:C9:60:43:BD:FD'
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
        self.get_logger().info(f"=== charge action Feedback ===     {feedback_msg.feedback.state}", throttle_duration_sec=10)

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
    
    def bluetooth_start_callback(self, request, response):
        self.get_logger().info('received a request for /bluetooth/start service request.')
        
        try:
            if self.bluetooth_proc != None:
                self.get_logger().info('bluetooth server is online, do noting')
            else:
                self.bluetooth_proc = subprocess.Popen(
                    ["ros2", "run", "charge_manager", "charge_bluetooth_old"])
            
            time.sleep(10)        # waiting for bluetooth server node setup completed.
            self.bluetooth_status = BluetoothStatus.UP
            response.success = True
            response.infos = "start bluetooth node success"
            self.get_logger().info(f'{response.infos}')
        except Exception as e:
            response.success = False
            response.infos = str(e)
            self.get_logger().info(f'{response.infos}')

        return response

    def bluetooth_stop_callback(self, request, response):
        self.get_logger().info('received a request for /bluetooth/stop service request.')
        try:
            if self.bluetooth_proc != None:
                self.terminate(self.bluetooth_proc)
                self.bluetooth_proc = None
            else:
                self.get_logger().info('bluetooth server is not online, do nothing')
            response.success = True
            response.infos = "stop bluetooth node success."
            self.get_logger().info(f'{response.infos}')
        except Exception as e:
            response.success = True
            response.infos = str(e)
            self.get_logger().info(f'{response.infos}')
        
        # don't kill child process success
        # self.bluetooth_proc.terminate()
        # self.bluetooth_proc.wait()
        # os.killpg(self.bluetooth_proc.pid, SIGINT)
        
        self.bluetooth_status = BluetoothStatus.DOWN
        return response
    

    def terminate(self, proc: subprocess.Popen):
        parent_pid = proc.pid 
        parent = psutil.Process(parent_pid)
        index = 1
        self.get_logger().info(f'parent\'childeren num: {len(parent.children(recursive=True))}')
        for child in parent.children(recursive=True):  # or parent.children() for recursive=False
            self.get_logger().info(f'child_{index}\'s children num: {len(child.children(recursive=True))}')
            self.get_logger().info(f'Terminating child {index}, pid: {child.pid} ......')
            child.send_signal(SIGINT)
            rt_code = child.wait(5)
            if rt_code == None:
                self.get_logger().info(f'Terminate child {index} (pid: {child.pid}) failed.')
                cmd = f'/usr/bin/kill -9 {child.pid}'
                self.get_logger().info(f'execute "{cmd}" for kill child process.')
                os.system(cmd)
            else:
                self.get_logger().info(f'Terminate child {index} (pid: {child.pid}) success. rt_code: {rt_code}')            
            index += 1

        parent.send_signal(SIGINT)
        rt_code = parent.wait(5)
        if rt_code == None:
                self.get_logger().info(f'Terminate parent (pid: {parent.pid}) failed.')
        else:
            self.get_logger().info(f'Terminate parent (pid: {parent.pid}) success. rt_code: {rt_code}')
    
def main(args=None):
    rclpy.init(args=args)
    charger_manager_node = chargeManager()
    multi_executor = MultiThreadedExecutor()
    multi_executor.add_node(charger_manager_node)
    multi_executor.spin()
    rclpy.shutdown()

if __name__ == '__main__':
    main()  
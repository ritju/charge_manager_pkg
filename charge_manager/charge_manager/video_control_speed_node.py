from typing import List
import rclpy
from rclpy.context import Context
from rclpy.node import Node
from geometry_msgs.msg import Twist
from rclpy.parameter import Parameter
import numpy as np
import cv2
import time


class Video_Control_Speed(object):
        def __init__(self,publish_frequency = 100):
                # super().__init__(node_name)
                self.speed_list = []
                self.publish_frequency = publish_frequency/1000 # ms
                self.node = Node('vedio_control_speed')
                self.publisher = self.node.create_publisher(Twist,"/cmd_vel",100)
                self.rate = self.node.create_rate(1/self.publish_frequency)
                print('发布频率：',1/self.publish_frequency)


        def left_rotate(self,time,angle):
                publish_times = round(time / self.publish_frequency)
                radian = (angle / 180) * np.pi
                average_rotate_speed = radian / time
                for i in range(publish_times):
                        msg = Twist()
                        msg.angular.z = average_rotate_speed
                        self.speed_list.append(msg)
                
        def right_rotate(self,time,angle):
                publish_times = round(time / self.publish_frequency)
                radian = (angle / 180) * np.pi
                average_rotate_speed = radian / time
                for i in range(publish_times):
                        msg = Twist()
                        msg.angular.z = average_rotate_speed
                        self.speed_list.append(msg)

        def forward(self,time,speed):
                publish_times = round(time / self.publish_frequency)
                for i in range(publish_times):
                        msg = Twist()
                        msg.linear.x = speed
                        self.speed_list.append(msg)

        def backward(self,time,speed):
                publish_times = round(time / self.publish_frequency)
                for i in range(publish_times):
                        msg = Twist()
                        msg.linear.x = speed
                        self.speed_list.append(msg)

        def wait(self,time,):
                publish_times = round(time / self.publish_frequency)
                for i in range(publish_times):
                        msg = Twist()
                        msg.linear.x = 0.0
                        msg.angular.z = 0.0
                        self.speed_list.append(msg)

        def run(self,):
                for i in self.speed_list:
                        # time.sleep(0.1)
                        rclpy.spin_once(self.node)
                        self.publisher.publish(i)
                        self.rate.sleep()
                print('发布完成')




def main(args=None):
        rclpy.init(args=args)
        node = Video_Control_Speed()#'vedio_control_speed'
        node.wait(1)
        node.forward(1,0.3)
        node.wait(0.3)
        node.left_rotate(5, 90)
        node.wait(0.3)
        node.right_rotate(10, -180)
        node.wait(0.3)
        node.left_rotate(5, 90)
        node.wait(0.3)
        node.backward(1, -0.3)
        print(node.speed_list)
        node.run()

        # video = cv2.VideoCapture('/workspaces/capella_ros_docker/src/video_control_speed/video_control_speed/video_control_speed_node.py')
        # while video.isOpened():
        #         #video.read() : 一次读取视频中的每一帧，会返回两个值；
        #         #res : 为bool类型表示这一帧是否真确读取，正确读取为True，如果文件读取到结尾，它的返回值就为False;
        #         #frame : 表示这一帧的像素点数组
        #         ret, frame = video.read()
        #         if frame is None: 
        #                 break
        #         if ret == True:
        #                 cv2.imshow("result", frame)
        #         #100 ： 表示一帧等待一百毫秒在进入下一帧， 0xFF : 表示键入键 27 = esc
        #         if cv2.waitKey(10) & 0xFF == 27 :
        #                 break 
        #         #video.release()释放视频
        #         video.release()
        #         cv2.destroyAllWindows()


if __name__ == "__main__":
        main()
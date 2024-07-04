import rclpy
from rclpy.node import Node
import time
import os
import threading
import crcmod.predefined
from charge_manager_msgs.srv import ConnectBluetooth, DisconnectBluetooth
from charge_manager_msgs.msg import ChargeState2
from std_msgs.msg import Int8
from rclpy.action import ActionClient
from rclpy.qos import DurabilityPolicy,ReliabilityPolicy,QoSProfile,HistoryPolicy
# 蓝牙模块相关的库
import asyncio
from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError
import subprocess
import psutil
from signal import SIGINT, SIGTERM

from rclpy.callback_groups import ReentrantCallbackGroup

from bleak.assigned_numbers import AdvertisementDataType
from bleak.backends.bluezdbus.advertisement_monitor import OrPattern
from bleak.backends.bluezdbus.scanner import BlueZScannerArgs
from bleak.backends.device import BLEDevice


# 需要实现的功能:
# 1. 实时发布topic(间隔一秒或更短):(序列号,接触状态,充电状态,对接执行状态)
# 2. 接收开始/停止充电的指令(ros服务,这里是服务端)
# 3. 接收开始/停止对接充电桩的指令(ros服务,这里是服务端)

# 自动充电流程:
# 1. agent接收到下发的充电桩坐标前往对接位置
# 2. 到达对接位置后,agent向ros发送对接充电桩的指令,此时状态为("", false, false, true)
# 3. 机器人原地旋转直到搜索到红外信号,此时状态为不变
# 4. ros连接上充电桩wifi,此时状态为("123456", false, false, true)
# 4. 机器人开始行驶,直到接触充电桩后停止,此时状态为("123456", true, false, false)
# 5. agent发送开始充电的指令,此时状态为("123456", true, true, false)

# 手动充电流程:
# 1. 用户把机器人推上充电桩
# 2. 机器人检测到红外信号,此时状态为("", false, false, false)
# 3. ros连接上充电桩wifi,此时状态为("123456", false, false, false)
# 4. 机器人发送接触状态,此时状态为("123456", true, false, false)
# 5. agent发送开始充电的指令,此时状态为("123456", true, true, false)
# 6. agent向服务器发送占用充电桩的通知

# 停止充电流程:
# 1. agent接收到下发的停止充电命令,向ros发送停止充电或对接的指令,此时状态为("123456", true, false, false)
# 2. 机器人驶离充电桩,因为不再与充电桩接触,因此认为离开充电桩,将序列号清空,此时状态为("", false, false, false)

# 话题:
# /charger/state:序列号,是否在充电,是否在对接,是否有接触等字段

# 服务:
# /charger/start: 开始充电
# /charger/stop:停止充电
# /charger/start_docking:开始对接
# /charger/stop_docking:停止对接

class BluetoothChargeServer(Node):
    def __init__(self, name):
        super().__init__(name)
        # 是否断开与充电桩的蓝牙连接
        self.bluetooth_connected = False
        # 蓝牙数据notify的uuid
        self.uuid_notify = None
        # 蓝牙数据write的uuid
        self.uuid_write = None
        # 初始化发送的数据
        self.send_data = None
        # 初始化心跳数据
        self.send_heartbeat_data = ['6b', '00', '00', '00', '00', '6b', '00', '00', '00', '21', '09', '00']
        # 初始化本地接收蓝牙的心跳时间(上一次收到蓝牙数据帧的时间)
        self.heartbeat_time = 0
        # 是否断开蓝牙的属性
        self.disconnect_bluetooth = False
        # 通过bssid链接充电桩WIFI服务
        self.bluetooth_concact_server = self.create_service(ConnectBluetooth, '/connect_bluetooth', self.connect_bluetooth)
        # 断开蓝牙服务
        self.bluetooth_disconnect_server = self.create_service(DisconnectBluetooth, '/disconnect_bluetooth', self.disconnect_bluetooth_callback)
        # # 话题和订阅器的qos
        charger_state_qos = QoSProfile(depth=1)
        charger_state_qos.reliability = ReliabilityPolicy.BEST_EFFORT
        charger_state_qos.history = HistoryPolicy.KEEP_LAST
        charger_state_qos.durability = DurabilityPolicy.VOLATILE
        # 初始化充电状态信息
        self.charge_state = ChargeState2()
        self.charge_state.pid = ""
        self.charge_state.has_contact = False
        self.charge_state.is_charging = False
        # 在机器人状态发布器
        self.charge_state_publisher = self.create_publisher(ChargeState2, '/charger/state2', charger_state_qos, callback_group=ReentrantCallbackGroup())
        self.publish_rate = self.create_rate(20)
        # 创建充电服务
        self.start_stop_charge_server = self.create_subscription(Int8, '/bluetooth_command', self.start_stop_charge_callback, 5, callback_group=ReentrantCallbackGroup())
        # 接受充电桩的数据帧
        self.udp_data = []
        # 创建线程开始发布充电状态
        self.charge_state_publish_thread = threading.Thread(target=self.charge_state_pub,daemon=True)
        self.charge_state_publish_thread.start()

        self.get_logger().info("Bluetooth charge Server starting")

    def disconnect_bluetooth_callback(self, request, response):
         start_time = time.time()
         self.get_logger().info('received a request for /disconnect_bluetooth')
         self.disconnect_bluetooth = True
         response.success = True
         response.infos = '断开蓝牙连接服务响应成功。'
         while self.charge_state.pid != '':
            if time.time() - start_time > 10.0:
                response.success = False
                response.infos = '断开蓝牙连接服务响应超时（10s）。'
                break
            else:
                self.get_logger().info('等待蓝牙连接服务断开中......', throttle_duration_sec=1)
                time.sleep(0.1)
         if self.charge_state.pid == '':
              self.heartbeat_time = 0
              self.get_logger().info('断开蓝牙连接服务响应成功。')
         response.cost_time = round(time.time() - start_time, 1)
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
            rt_code = child.wait(2)
            if rt_code == None:
                self.get_logger().info(f'Terminate child {index} (pid: {child.pid}) failed.')
                cmd = f'/usr/bin/kill -9 {child.pid}'
                self.get_logger().info(f'execute "{cmd}" for kill child process.')
                os.system(cmd)
            else:
                self.get_logger().info(f'Terminate child {index} (pid: {child.pid}) success. rt_code: {rt_code}')            
            index += 1

        parent.send_signal(SIGINT)
        rt_code = parent.wait(2)
        if rt_code == None:
                self.get_logger().info(f'Terminate parent (pid: {parent.pid}) failed.')
        else:
            self.get_logger().info(f'Terminate parent (pid: {parent.pid}) success. rt_code: {rt_code}')

    # 定时发布充电状态
    def charge_state_pub(self, ):
        self.get_logger().info(f'charger_state_pub thread => Process: {os.getpid()}, Thread: {threading.get_ident()}')
        while True:
            if not rclpy.ok():
                 self.get_logger().info('rclpy\'s context is invalid, exiting...')
            if self.charge_state.pid == '':
                self.charge_state.pid = ''
                self.charge_state.has_contact = False
                self.charge_state.is_charging = False
            try:
                if not self.bleak_client.is_connected:
                    self.charge_state.pid = ''
                    self.charge_state.has_contact = False
                    self.charge_state.is_charging = False
            except:
                self.charge_state.pid = ''
                self.charge_state.has_contact = False
                self.charge_state.is_charging = False
            self.charge_state_publisher.publish(self.charge_state)
            if time.time() - self.heartbeat_time > 20 and self.bluetooth_connected != None and self.heartbeat_time != 0:
                self.get_logger().info("No data received more than 20 seconds.")
                self.get_logger().info(f"current_time: {time.time()}")
                self.get_logger().info(f"heartbeat_time: {self.heartbeat_time}")
                self.charge_state.pid = ''
                self.disconnect_bluetooth = True
                self.bluetooth_connected = None
                self.heartbeat_time = 0
            self.publish_rate.sleep()

    def start_stop_charge_callback(self,msgs):
        if msgs.data == 1:
            # 开始充电服务回调函数,向充电桩发送开始充电数据帧
            # 判断是否已经连接上充电桩的蓝牙,没连上无法通讯
            time.sleep(0.5)
            self.get_logger().info('收到开始充电命令')
            if self.charge_state.pid != '':
                # 判断是否还没接触上充电桩，没接触上直接返回失败
                if self.charge_state.has_contact == False:
                    self.get_logger().info("还未与充电桩接触,请接触好在充电。")
                # 判断是否早就已经充着电
                elif self.charge_state.is_charging == True:
                    self.get_logger().info("早已经在充电了。")
                # 发送充电数据帧
                send_d = self.send_heartbeat_data.copy()
                # 设置数据帧的命令码
                send_d[8] = '80'
                send_d[9] = '00'
                # 设置数据帧的长度域
                send_d[10] = '02'
                send_d[11] = '00'
                # 设置数据帧的数据域
                send_d.append('02')
                send_d.append('00')
                # 设置数据帧的校验码
                send_d.append(self.crc8(send_d))
                # 设置数据帧的结束符
                send_d.append('16')
                # 发送数据帧
                self.send_data = bytes.fromhex(''.join(send_d))
                # # 循环等待充电桩的响应结果
                t1 = time.time()
                while True:
                    if self.charge_state.is_charging == True:
                        self.get_logger().info('成功开始充电！')
                        break
                    elif time.time() - t1 > 10:
                        self.get_logger().info('开始充电失败！')
                        break
                    else:
                        time.sleep(1)
                        
            else:
                self.get_logger().info('未连接充电桩bluetooth,请先连接！')
        elif msgs.data == 0:
            # 停止充电服务回调函数,向充电桩发送停止充电数据帧
            # 判断是否已经连接上充电桩的蓝牙,没连上无法通讯
            self.get_logger().info('收到停止充电命令')
            if self.charge_state.pid != '':
                    # 判断当前WiFi连接状态
                    if self.charge_state.is_charging == False:
                        self.get_logger().info('本来就没充电。')
                    send_d = self.send_heartbeat_data.copy()
                    # 设置数据帧的命令码
                    send_d[8] = '80'
                    send_d[9] = '00'
                    # 设置数据帧的长度域
                    send_d[10] = '02'
                    send_d[11] = '00'
                    # 设置数据帧的数据域
                    send_d.append('01')
                    send_d.append('00')
                    # 设置数据帧的校验码
                    send_d.append(self.crc8(send_d))
                    # 设置数据帧的
                    send_d.append('16')
                    # 发送数据帧
                    self.send_data = bytes.fromhex(''.join(send_d))
                    # 等待充电桩回复
                    t1 = time.time()
                    # 循环等待充电桩的响应结果
                    while True:
                        if self.charge_state.is_charging == False:
                            self.get_logger().info('成功关闭充电！')
                            # self.disconnect_bluetooth = True
                            break
                        elif time.time() - t1 > 10:
                            self.get_logger().info('关闭充电失败！')
                            break
                        else:
                            time.sleep(1)
                        
            else:
                self.get_logger().info('未连接充电桩WiFi,请先连接！')

    # 连接充电桩蓝牙
    def connect_bluetooth(self,request, response):
        time.sleep(3)
        restore = 0 # 蓝牙是否正在恢复中
        with open('/map/bluetooth_restore.txt', 'r', encoding='utf-8') as f:
            restore = (int)(f.readline().strip('\n'))
            self.get_logger().info(f'restore: {restore}')
        time_wait = time.time()
        while restore  and (time.time() - time_wait) < 25.0:
            self.get_logger().info("Waiting for bluetooth restoring ......")
            time.sleep(2)
            with open('/map/bluetooth_restore.txt', 'r', encoding='utf-8') as f:
                restore = (int)(f.readline().strip('\n'))
        self.get_logger().info("正在重连蓝牙...")
        self.heartbeat_time = 0
        self.connect_start_time = time.time()
        self.connect_exception = ""
        # print(os.system('sudo rfkill block bluetooth')) # bluetoothctl power off
        # blue_stop = subprocess.Popen(['sudo', 'rfkill', 'block', 'bluetooth'])
        # time.sleep(2)
        # self.terminate(blue_stop)
        self.charge_state.pid = ''
        self.charge_state.has_contact = False
        self.charge_state.is_charging = False
        # print(os.system('sudo rfkill unblock bluetooth')) # bluetoothctl power on
        # blue_start = subprocess.Popen(['sudo', 'rfkill', 'unblock', 'bluetooth'])
        # time.sleep(2)
        # self.terminate(blue_start)
        self.bluetooth_connected = None
        b_thread = threading.Thread(target=self.bluetooth_thread,kwargs={'mac_address':request.mac})
        b_thread.start()
        # 等待蓝牙连接结果
        start_time = time.time()
        while True:
            if self.bluetooth_connected != None:
                break
            elif time.time() - start_time > 25:
                self.get_logger().info(f"连接蓝牙超时: {request.mac} ......")
                self.bluetooth_connected = False
                self.disconnect_bluetooth = True
                break
            else:              
                self.get_logger().info(f"等待蓝牙连接: {request.mac} ......", throttle_duration_sec=1)
                time.sleep(0.1)
                continue
        # 判断蓝牙连接结果
        if self.bluetooth_connected == True:
            self.get_logger().info('蓝牙连接成功.')
            response.success = True
            response.connection_time = round(time.time() - self.connect_start_time, 1)
            response.result = f"蓝牙连接成功 {self.connect_exception}"
            try:
                with open('/map/bluetooth_restore.txt', 'w', encoding='utf-8') as f:
                    f.write('0\n')
            except Exception as e:
                self.get_logger().info(f"catch exception {str(e)} when write 0 to /map/bluetooth_restore.txt when bluetooth is connected success.")
        else:
            self.get_logger().info('蓝牙连接失败.')
            response.success = False
            response.connection_time = round(time.time() - self.connect_start_time, 1)
            response.result = f"蓝牙连接失败  {self.connect_exception}"
            try:
                with open('/map/bluetooth_restore.txt', 'w', encoding='utf-8') as f:
                    f.write('1\n')
            except Exception as e:
                self.get_logger().info(f"catch exception {str(e)} when write 1 to /map/bluetooth_restore.txt when bluetooth is connected failed.")
        return response

    # 创建bleak客户端
    async def create_bleakclient(self,address):
        try:
            self.get_logger().info("搜索附近的蓝牙......")
            # args = BlueZScannerArgs(or_patterns=
            #                         [OrPattern(0, AdvertisementDataType.MANUFACTURER_SPECIFIC_DATA, b"\xe1\x02")])
            # devices = await BleakScanner(scanning_mode='passive', bluez=args).discover(return_adv=True)
            devices = await BleakScanner(scanning_mode='active').discover(return_adv=True)
            devices_num = len(devices)
            self.get_logger().info(f'共搜索到 {devices_num} 个蓝牙信号。')
            self.bluetooth_searched = False
            if devices_num > 0:
                self.get_logger().info('--------Mac-------- | --------Name-------')
                for key in devices:
                    self.get_logger().info(f'{key}   | {devices[key][1].local_name}')
                    if key == address:
                        self.bluetooth_searched = True
                        self.ble_device = devices[key][0]
                        
            else:
                #  try:
                #     if 'marker_id_and_bluetooth_mac' in os.environ:
                #         marker_id_and_bluetooth_mac = [os.environ.get('marker_id_and_bluetooth_mac')]
                #         bluetooth_mac=marker_id_and_bluetooth_mac[0].split('/')[1]
                #         self.ble_device = BLEDevice(address=bluetooth_mac, name='ai-thinker')
                #  except:
                #     self.get_logger().info("Please input aruco marker_id and bluetooth_mac environment in docker-compose.yml file!")
                #     self.ble_device = BLEDevice(address='94:C9:60:43:C0:6D', name='ai-thinker')
                pass
            
            
            if self.bluetooth_searched:
                self.get_logger().info(f'搜索到mac: {address}')
                self.get_logger().info(f'address: {self.ble_device.address}')
                self.get_logger().info(f'name: {self.ble_device.name}')
                self.get_logger().info(f'details: {self.ble_device.details}')
                self.get_logger().info(f'rssi: {devices[address][1].rssi}')
            else:
                # for test (not wroking yet)
                self.get_logger().info(f'未搜索到mac: {address}')
                # try:
                #     self.get_logger().info(f'try to assign self.ble_device from docker-compose.yml file.')
                #     if 'marker_id_and_bluetooth_mac' in os.environ:
                #         marker_id_and_bluetooth_mac = [os.environ.get('marker_id_and_bluetooth_mac')]
                #         bluetooth_mac=marker_id_and_bluetooth_mac[0].split('/')[1]
                #         self.ble_device = BLEDevice(address=bluetooth_mac, name='ai-thinker')
                #     else:
                #         self.get_logger().info("Please input aruco marker_id and bluetooth_mac environment in docker-compose.yml file!")
                #         self.ble_device = BLEDevice(address='94:C9:60:43:BE:6A', name='ai-thinker', details='abc', rssi=100)
                # except Exception as e:
                #     self.get_logger().info(f"catch exception when assign self.ble_device: {str(e)}")
                #     self.ble_device = BLEDevice(address='94:C9:60:43:BE:6A', name='ai-thinker')
                
                        
            self.uuid_write = None
            self.uuid_notify = None
            if self.bluetooth_searched:
                self.bleak_client = BleakClient(self.ble_device)
            else:
                self.bleak_client = BleakClient(address)
            await self.bleak_client.connect()
            self.disconnect_bluetooth = False
            # print('蓝牙连接成功')
            # print('查找蓝牙服务')
            services = self.bleak_client.services
            for service in services:
                    # print('服务的uuid：', service.uuid)
                    for character in service.characteristics:
                            # print('特征值uuid：', character.uuid)
                            # print('特征值属性：', character.properties)
                            # 获取发送数据的蓝牙服务uuid
                            if character.properties == ['write-without-response', 'write']:
                                    self.uuid_write = character.uuid
                            # 获取接收数据的蓝牙服务uuid
                            elif character.properties == ['read', 'notify']:
                                    self.uuid_notify = character.uuid
                            else:
                                    continue
                    # print('*************************************')
            if self.uuid_write != None or self.uuid_notify != None:
                self.charge_state.pid = address
                self.bluetooth_connected = True
                await self.bleak_client.start_notify(self.uuid_notify, self.notify_data)
                while True:
                        if  not rclpy.ok():
                             self._logger().info('rclpy\'s context is invalid, exiting ...')
                             await self.bleak_client.disconnect()
                             break
                        if not self.bleak_client.is_connected:
                                await self.bleak_client.stop_notify(self.uuid_notify)
                                break
                        if self.disconnect_bluetooth:
                            self.get_logger().info(f"调用disconnect()尝试断开蓝牙。")
                            await self.bleak_client.disconnect()
                            break
                        if self.send_data is not None:
                            await self.bleak_client.write_gatt_char(self.uuid_write,self.send_data)
                            self.send_data = None
                        if self.udp_data is not None:
                                # 回复充电桩
                                send_d = self.send_heartbeat_data.copy()
                                # 设置数据帧的命令码
                                send_d[8] = '80'
                                send_d[9] = '21'
                                # 设置数据帧的长度域
                                send_d[10] = '01'
                                send_d[11] = '00'
                                # 设置数据帧的数据域
                                send_d.append('00')
                                # 设置数据帧的校验码
                                send_d.append(self.crc8(send_d))
                                # 设置数据帧的结束符
                                send_d.append('16')
                                # 发送数据帧
                                self.heart_data = bytes.fromhex(''.join(send_d))
                                # print('已发送',self.heart_data)
                                await self.bleak_client.write_gatt_char(self.uuid_write,self.heart_data)
                        await asyncio.sleep(0.5)
            else:
                print('self.uuid_write None or self.uuid_notify None is None')                
                self.charge_state.pid = ""
                await self.bleak_client.disconnect()
                self.bluetooth_connected = False
            self.charge_state.pid = ""
        except Exception as e:
            self.bluetooth_connected = False
            self.charge_state.pid = ""
            self.get_logger().info('catch exception ......')
            self.get_logger().info(f'exception: {str(e)}')
            self.connect_exception = str(e)
            time.sleep(2)
        
    def bluetooth_thread(self,mac_address):
        self.get_logger().info(f'bluetooth thread => Process: {os.getpid()}, Thread: {threading.get_ident()}')
        asyncio.run(self.create_bleakclient(mac_address))

    # 接收蓝牙数据的回调函数，解析充电桩发送的数据帧
    def notify_data(self,sender,data ):
        # 接受服务端的数据帧
        # self.get_logger().info('-------------------receive data---------------------')
        # 将数据解码
        data = ','.join('{:02x}'.format(x) for x in data).replace(' ','')
        # 将数据帧转化为列表
        data_list = data.split(',')
        self.get_logger().info(f'解析后的数据为： {data_list}', throttle_duration_sec=10)
        self.heartbeat_time = time.time()
        # self.get_logger().debug(f'收到服务器的信息: {data}')
        # self.get_logger().debug(f'解析后的数据为: {data_list}', )
        # self.get_logger().debug(f'数据列表长度为: {len(data_list)} 字节')
        # self.get_logger().debug(f'帧起始符(6BH,1字节): {data_list[0]}')
        # self.get_logger().debug(f'地址域(4字节): {data_list[1:5]}')
        # self.get_logger().debug(f'帧起始符(6BH,1字节): {data_list[5]}')
        # self.get_logger().debug(f'帧序号(2字节): {data_list[6:8]}')            
        # self.get_logger().debug(f'命令码(2字节): {data_list[8:10]}')
        # self.get_logger().debug(f'长度域(2字节): {data_list[10:12]}')
        # self.get_logger().debug(f'数据域: {data_list[12:-2]}')
        # self.get_logger().debug(f'校验码(1字节): {data_list[-2]}')
        # self.get_logger().debug(f'结束符(16H,1字节): {data_list[-1]}')
        # self.get_logger().debug(f"正在校验信息......")
        # 校验数据
        crc8_ = self.crc8(data_list[:-2])
        if crc8_ == data_list[-2].upper():
            # self.get_logger().debug('数据校验通过！')
            # self.get_logger().info('解析后的数据为：{}'.format(data_list))
            self.udp_data = data_list
            # 判断机器人与充电桩的接触状态与充电状态
            # 通过命令码是否是充电桩工作状态的信息帧
            if data_list[8:10] == ['00', '21']:
                # print('self.charge_state.is_charging:',data_list[12:-2][0])
                # print('self.charge_state.has_contact:',data_list[12:-2][5])
                # print('******************************')
                if data_list[12:-2][0] == '00':
                    self.charge_state.is_charging = False
                    # self.get_logger().info(f'is_charging: {self.charge_state.is_charging}', throttle_duration_sec=5)
                elif data_list[12:-2][0] == '01':
                    self.charge_state.is_charging = True
                    # self.get_logger().info(f'is_charging: {self.charge_state.is_charging}', throttle_duration_sec=5)
                else:
                    self.get_logger().info('is_charging 数据段数据错误。')
                if data_list[12:-2][5] == '00':
                    self.charge_state.has_contact = False
                    # self.get_logger().info(f'has_contact: {self.charge_state.has_contact}', throttle_duration_sec=5)
                elif data_list[12:-2][5] == '01':
                    self.charge_state.has_contact = True
                    # self.get_logger().info(f'has_contact: {self.charge_state.has_contact}', throttle_duration_sec=5)
                    # now_time = self.get_clock().now()
                    # self.charge_state.stamp = now_time.to_msg()
                else:
                    self.get_logger().info('has_contact 数据段数据错误。')  
                
        else:
            # self.get_logger().debug(f'self crc: {crc8_}')
            # self.get_logger().debug(f'recv crc: {data_list[-2].upper()}')
            self.get_logger().info('数据未通过校验,舍弃数据！')
            self.get_logger().info('----------------------------')


    # CRC-8/MAXIM　x8+x5+x4+1  循环冗余校验 最后在取了反的
    # 计算校验码
    def crc8(self, data):
        crc8 = crcmod.predefined.Crc('crc-8-maxim')
        # crc8.update(bytes().fromhex(' '.join(data)))
        self.get_logger().debug(f'data: {data}')
        self.get_logger().debug(f"data_join: {' '.join(data)}")

        crc8.update(bytes().fromhex(' '.join(data)))
        crc8_value = hex(~crc8.crcValue & 0xff)[2:].upper()
        crc8_value = crc8_value if len(crc8_value) > 1 else '0' + crc8_value
        return crc8_value

    # 析构函数
    def __del__(self, ):
        pass


def main(args=None):
    rclpy.init(args=args)
    node = BluetoothChargeServer('bluetooth_charge_server')
    rclpy.spin(node)
    rclpy.shutdown()


if __name__ == '__main__':
    main()
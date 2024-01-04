import bleak
from bleak import BleakClient, BleakScanner

import rclpy
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.qos import DurabilityPolicy,ReliabilityPolicy,QoSProfile,HistoryPolicy
from rclpy.node import Node

from std_msgs.msg import Int8 # 0 => /charger/stop; 1 => /charger/start
from charge_manager_msgs.msg import ChargeState2
from charge_manager_msgs.srv import ConnectBluetooth

import crcmod
import time
import asyncio

start_charge_data = ''
stop_charge_data = ''
mac = '94:C9:60:43:BE:FD'

class ConnectBluetoothService(Node):
    
    def __init__(self):
        super().__init__('connect_bluetooth_srv_server')
        self.get_logger().info('连接蓝牙服务启动......')
        self.bleak_client = None
        self.uuid_notify = None
        self.uuid_write = None
        self.connect_completed = False
        # 初化化话题 /charger/state
        self.charger_state2 = ChargeState2()
        self.charger_state2.pid = ''
        self.charger_state2.has_contact = False
        self.charger_state2.is_charging = False

        cb_group_type = ReentrantCallbackGroup()
        self.charger_state_pub = self.create_publisher(ChargeState2, '/charger/state2', 5, callback_group= cb_group_type)
        self.timer_charger_state_pub = self.create_timer(0.2, self.timer_charger_state_pub_callback)
        self.srv = self.create_service(ConnectBluetooth, 'connect_bluetooth', self.service_callback_wrapper, callback_group=cb_group_type)
        self.command_sub_ = self.create_subscription(Int8, 'bluetooth_command', self.command_callback, 1)
        # self.timer_notify = self.create_timer(0.2, self.notify_callback_wrapper)

    def timer_charger_state_pub_callback(self):
        # try:
        #     mtu_size = self.bleak_client.mtu_size
        #     self.get_logger().info(f'mtu_size: {mtu_size}')
        # except Exception as e:
        #     self.charger_state2.pid = ''
        #     self.charger_state2.has_contact = False
        #     self.charger_state2.is_charging = False
        if isinstance(self.bleak_client, BleakClient):            
            self.get_logger().info(f'self.bleak_client.is_connected: {self.bleak_client.is_connected}', throttle_duration_sec = 3)
            if self.bleak_client.is_connected:
                self.charger_state2.pid = self.bleak_client.address
            else:
                self.charger_state2.pid = ''
        else:
            self.charger_state2.pid = ''
        self.charger_state_pub.publish(self.charger_state2)

    def notify_callback_wrapper(self):
        asyncio.run(self.timer_notify_callback())
    
    async def timer_notify_callback(self):
        if self.bleak_client != None:
            await self.bleak_client.start_notify(self.uuid_notify, self.notify_data)
    
    async def command_callback(self, msg):
        send_data = None
        if msg.data == 0:
            send_data = start_charge_data
        elif msg.data == 1:
            send_data = stop_charge_data
        await self.bleak_client.write_gatt_char(self.uuid_write, send_data)

     # 接收蓝牙数据的回调函数，解析充电桩发送的数据帧
    def notify_data(self, BleakGATTCharacteristic, data ):
        # 接受服务端的数据帧
        # self.get_logger().debug('-------------------receive data---------------------')
        # 将数据解码
        data = ','.join('{:02x}'.format(x) for x in data).replace(' ','')
        # 将数据帧转化为列表
        data_list = data.split(',')
        # 校验数据
        crc8_ = self.crc8(data_list[:-2])
        if crc8_ == data_list[-2].upper():
            # self.get_logger().debug('数据校验通过！')
            # self.get_logger().info('解析后的数据为：{}'.format(data_list))
            self.udp_data = data_list
            # 判断机器人与充电桩的接触状态与充电状态
            # 通过命令码是否是充电桩工作状态的信息帧
            if data_list[8:10] == ['00', '21']:
                if data_list[12:-2][0] == '00':
                    self.charge_state.is_charging = False
                elif data_list[12:-2][0] == '01':
                    self.charge_state.is_charging = True
                else:
                    self.get_logger().info('is_charging 数据段数据错误。')
                if data_list[12:-2][5] == '00':
                    self.charge_state.has_contact = False
                elif data_list[12:-2][5] == '01':
                    self.charge_state.has_contact = True
                    self.charge_state.is_docking = False
                    # now_time = self.get_clock().now()
                    # self.charge_state.stamp = now_time.to_msg()
                else:
                    self.get_logger().info('has_contact 数据段数据错误。')
        else:
            self.get_logger().debug(f'self crc: {crc8_}')
            self.get_logger().debug(f'recv crc: {data_list[-2].upper()}')
            self.get_logger().info('蓝牙数据未通过校验,舍弃数据！')

    def service_callback_wrapper(self, request, response):
        req = []
        res = []
        req.append(request)
        res.append(response)
        asyncio.run(self.service_callback(req, res))
        while not self.connect_completed:
            self.get_logger().info('connecting bluetooth ......', throttle_duration_sec=1)
        return res[0]
    
    async def service_callback(self, request, response):
        time_start = time.time()
        bluetooth_searched = False
        mac = request[0].mac
        ble_device = None
        self.get_logger().info(f'try to connect bluetooth: {mac}')
        self.get_logger().info("搜索附近的蓝牙......")
        devices = await BleakScanner().discover(return_adv=True)
        devices_num = len(devices)
        self.get_logger().info(f'共搜索到 {devices_num} 个蓝牙信号。')
        if devices_num > 0:
            self.get_logger().info('--------Mac-------- | --------Name-------')
            for key in devices:
                self.get_logger().info(f'{key}   | {devices[key][1].local_name}')
                if key == mac:
                    bluetooth_searched = True
                    ble_device = key
        if bluetooth_searched:
            self.get_logger().info(f'成功搜索到蓝牙 {mac} .')
            self.bleak_client = BleakClient(ble_device, disconnected_callback=self.disconnect_bluetooth_callback)
            self.connect_completed = False
            try:
                await self.bleak_client.connect()
                time_end = time.time()
                response[0].connection_time = time_end - time_start
                response[0].success = True
                response[0].result = f'连接mac地址为： {mac} 的蓝牙成功。'
                self.charger_state2.pid = mac
                services = self.bleak_client.services
                for service in services:
                    for character in service.characteristics:
                        # 获取发送数据的蓝牙服务uuid
                        if character.properties == ['write-without-response', 'write']:
                            self.uuid_write = character.uuid
                            self.get_logger().info(f'uuid_write: {self.uuid_write}')
                        # 获取接收数据的蓝牙服务uuid
                        elif character.properties == ['read', 'notify']:
                            self.uuid_notify = character.uuid                                
                            self.get_logger().info(f'uuid_notify: {self.uuid_notify}')
            except Exception as e:
                self.get_logger().info('get exception when connect bluetooth ...')
                print(e)    
        else:
            log_str = f'未搜索到蓝牙 {mac}'
            self.get_logger().info(f'{log_str}')
            response[0].success = False
            time_end = time.time()
            response[0].connection_time = time_end - time_start
            response[0].result = f"{log_str}"
        self.connect_completed = True
    
    def disconnect_bluetooth_callback(client):
        print('disconnected called .............')
    
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

def main(args=None):
    rclpy.init(args=args)
    service = ConnectBluetoothService()
    rclpy.spin(service)
    rclpy.shutdown()

if __name__ == '__main__':
    main()    

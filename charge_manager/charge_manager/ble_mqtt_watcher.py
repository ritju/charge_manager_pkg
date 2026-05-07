#!/usr/bin/env python3
"""
BLE MQTT Watcher - 在宿主机上运行，负责蓝牙连接和通讯
通过 MQTT 与容器内的 ROS2 程序通信
"""

import asyncio
import json
import time
import threading
import os
import signal
import sys
from typing import Optional, Dict, Any

import crcmod.predefined
from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError
import paho.mqtt.client as mqtt


class BLEMQTTWatcher:
    def __init__(self, mqtt_host: str = "localhost", mqtt_port: int = 1883):
        # MQTT 配置
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.mqtt_client: Optional[mqtt.Client] = None
        
        # BLE 状态
        self.bluetooth_connected = False
        self.current_mac: str = ""
        self.uuid_notify: Optional[str] = None
        self.uuid_write: Optional[str] = None
        self.send_data: Optional[bytes] = None
        
        # 心跳数据模板
        self.send_heartbeat_data = ['6b', '00', '00', '00', '00', '6b', '00', '00', '00', '21', '09', '00']
        
        # 时间记录
        self.heartbeat_time: float = 0
        self.data_received_time: float = 0
        self.last_status_publish_time: float = 0
        
        # 充电状态 (映射 ChargeState2)
        self.charge_state = {
            "pid": "",
            "has_contact": False,
            "is_charging": False,
            "is_waterflooding": False,
            "water_mode": "auto",
            "timestamp": 0.0
        }
        
        # 并发控制
        self._connect_lock = threading.Lock()
        self._ble_task: Optional[asyncio.Future] = None
        self._client: Optional[BleakClient] = None
        self._client_lock = threading.Lock()
        self._shutdown_event = threading.Event()
        self.disconnect_requested = False
        
        # 事件循环
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self._run_event_loop, daemon=True)
        self.loop_thread.start()
        
        # 状态发布周期
        self.status_publish_interval = 0.5  # 2Hz
        
        # 启动状态发布定时器
        self._start_status_timer()
        
        print("BLE MQTT Watcher 初始化完成")
    
    def _run_event_loop(self):
        """在独立线程中运行 asyncio 事件循环"""
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()
    
    def _start_status_timer(self):
        """启动定时发布状态的定时器"""
        def publish_status_loop():
            while not self._shutdown_event.is_set():
                time.sleep(self.status_publish_interval)
                self._publish_status()
        
        status_thread = threading.Thread(target=publish_status_loop, daemon=True)
        status_thread.start()
    
    # ==================== MQTT 回调函数 ====================
    
    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            print(f"MQTT 连接成功，使用 {self.mqtt_host}:{self.mqtt_port}")
            # 订阅控制命令
            client.subscribe("charger/ble/command")
            client.subscribe("charger/ble/connect")
            client.subscribe("charger/ble/disconnect")
            print("已订阅: charger/ble/command, charger/ble/connect, charger/ble/disconnect")
        else:
            print(f"MQTT 连接失败，返回码: {rc}")
    
    def on_message(self, client, userdata, msg):
        topic = msg.topic
        try:
            payload = json.loads(msg.payload.decode('utf-8'))
            print(f"收到 MQTT 消息 - Topic: {topic}, Payload: {payload}")
            
            if topic == "charger/ble/connect":
                mac = payload.get("mac", "")
                if mac:
                    self._handle_connect_request(mac)
                else:
                    print("连接请求缺少 mac 地址")
            
            elif topic == "charger/ble/disconnect":
                self._handle_disconnect_request()
            
            elif topic == "charger/ble/command":
                command = payload.get("command")
                if command is not None:
                    self._handle_command(command)
                else:
                    print("命令请求缺少 command 字段")
        
        except json.JSONDecodeError as e:
            print(f"JSON 解析错误: {e}")
        except Exception as e:
            print(f"处理 MQTT 消息时出错: {e}")
    
    # ==================== MQTT 发布函数 ====================
    
    def _publish_ble_data(self, data_list: list, crc_valid: bool):
        """发布 BLE 原始解析数据到 charger/ble/data"""
        if self.mqtt_client is None:
            return
        
        message = {
            "raw_hex": data_list,
            "crc_valid": crc_valid,
            "timestamp": time.time()
        }
        self.mqtt_client.publish("charger/ble/data", json.dumps(message))
    
    def _publish_charge_state(self):
        """发布充电状态到 charger/ble/state"""
        if self.mqtt_client is None:
            return
        
        self.charge_state["timestamp"] = time.time()
        self.mqtt_client.publish("charger/ble/state", json.dumps(self.charge_state))
    
    def _publish_status(self):
        """发布连接状态到 charger/ble/status (2Hz)"""
        if self.mqtt_client is None:
            return
        
        current_time = time.time()
        # 控制发布频率
        if current_time - self.last_status_publish_time < self.status_publish_interval:
            return
        
        status = {
            "connected": self.bluetooth_connected,
            "mac": self.current_mac if self.bluetooth_connected else "",
            "last_data_received": self.data_received_time if self.data_received_time > 0 else 0.0
        }
        self.mqtt_client.publish("charger/ble/status", json.dumps(status))
        self.last_status_publish_time = current_time
    
    def _publish_connect_response(self, success: bool, mac: str, connection_time: float, result: str):
        """发布连接结果响应"""
        if self.mqtt_client is None:
            return
        
        response = {
            "success": success,
            "mac": mac,
            "connection_time": connection_time,
            "result": result,
            "timestamp": time.time()
        }
        self.mqtt_client.publish("charger/ble/connect_response", json.dumps(response))
    
    def _publish_disconnect_response(self, success: bool, infos: str, cost_time: float):
        """发布断开连接结果响应"""
        if self.mqtt_client is None:
            return
        
        response = {
            "success": success,
            "infos": infos,
            "cost_time": cost_time,
            "timestamp": time.time()
        }
        self.mqtt_client.publish("charger/ble/disconnect_response", json.dumps(response))
    
    # ==================== 命令处理函数 ====================
    
    def _handle_connect_request(self, mac: str):
        """处理连接请求"""
        print(f"收到连接请求，MAC: {mac}")
        
        if not self._connect_lock.acquire(blocking=False):
            self._publish_connect_response(False, mac, 0.0, "另一个连接操作正在进行中")
            return
        
        try:
            # 如果已经连接且是同一个 MAC
            if self.current_mac == mac and self.bluetooth_connected:
                print(f"已连接到 {mac}，跳过重复连接")
                self._publish_connect_response(True, mac, 0.0, f"已连接到 {mac}")
                return
            
            # 取消正在进行的 BLE 任务
            if self._ble_task is not None and not self._ble_task.done():
                print("正在取消之前的 BLE 任务...")
                self._ble_task.cancel()
                try:
                    self._ble_task.result(timeout=2.0)
                except:
                    pass
                
                with self._client_lock:
                    if self._client is not None:
                        print("强制断开之前的客户端...")
                        try:
                            future_disconnect = asyncio.run_coroutine_threadsafe(
                                self._client.disconnect(), self.loop
                            )
                            future_disconnect.result(timeout=2.0)
                        except Exception as e:
                            print(f"强制断开错误: {e}")
                        finally:
                            self._client = None
            
            # 清空状态
            self.disconnect_requested = False
            self.bluetooth_connected = False
            self.heartbeat_time = 0
            self.data_received_time = 0
            
            # 重置充电状态
            self.charge_state["pid"] = ""
            self.charge_state["has_contact"] = False
            self.charge_state["is_charging"] = False
            self.charge_state["is_waterflooding"] = False
            
            connect_start_time = time.time()
            self.connect_exception = ""
            
            # 启动异步连接协程
            future = asyncio.run_coroutine_threadsafe(
                self._create_bleakclient(mac, connect_start_time),
                self.loop
            )
            self._ble_task = future
            
            def done_callback(fut):
                try:
                    fut.result()
                except Exception as e:
                    print(f"BLE 协程异常: {e}")
            future.add_done_callback(done_callback)
            
            # 等待连接结果
            start_time = time.time()
            while True:
                if self.bluetooth_connected is not None:
                    break
                elif time.time() - start_time > 25:
                    print(f"连接蓝牙超时: {mac}")
                    self.bluetooth_connected = False
                    self.disconnect_requested = True
                    break
                else:
                    time.sleep(0.1)
            
            if self.bluetooth_connected:
                print("蓝牙连接成功")
                self.data_received_time = time.time()
                connection_time = round(time.time() - connect_start_time, 1)
                self._publish_connect_response(True, mac, connection_time, f"蓝牙连接成功 {self.connect_exception}")
            else:
                print("蓝牙连接失败")
                connection_time = round(time.time() - connect_start_time, 1)
                self._publish_connect_response(False, mac, connection_time, f"蓝牙连接失败 {self.connect_exception}")
        
        finally:
            self._connect_lock.release()
    
    def _handle_disconnect_request(self):
        """处理断开连接请求"""
        print("收到断开连接请求")
        start_time = time.time()
        
        # 设置标志
        self.disconnect_requested = True
        
        # 主动断开 BLE 客户端
        with self._client_lock:
            client = self._client
        
        if client is not None and client.is_connected:
            try:
                future = asyncio.run_coroutine_threadsafe(client.disconnect(), self.loop)
                future.result(timeout=3.0)
                print("主动断开成功")
            except Exception as e:
                print(f"主动断开异常: {e}")
        
        # 取消 BLE 任务
        if self._ble_task and not self._ble_task.done():
            self._ble_task.cancel()
            try:
                self._ble_task.result(timeout=2.0)
            except Exception as e:
                print(f"取消 BLE 任务异常: {e}")
        
        # 清空状态
        self.charge_state["pid"] = ""
        self.bluetooth_connected = False
        self.heartbeat_time = 0
        self.current_mac = ""
        
        cost_time = round(time.time() - start_time, 1)
        self._publish_disconnect_response(True, "断开蓝牙连接成功", cost_time)
        print("断开蓝牙连接完成")
    
    def _handle_command(self, command: int):
        """处理控制命令"""
        # 命令常量
        CHARGER_START = 0
        CHARGER_STOP = 1
        WATER_START = 2
        WATER_STOP = 3
        
        if self.charge_state["pid"] == "":
            print("未连接充电桩蓝牙，请先连接！")
            return
        
        if command == CHARGER_START:
            print("收到开始充电命令")
            if not self.charge_state["has_contact"]:
                print("还未与充电桩接触，请接触好在充电。")
                return
            if self.charge_state["is_charging"]:
                print("早已经在充电了。")
                return
            
            send_d = self.send_heartbeat_data.copy()
            send_d[8] = '80'
            send_d[9] = '00'
            send_d[10] = '02'
            send_d[11] = '00'
            send_d.append('02')
            send_d.append('00')
            send_d.append(self._crc8(send_d))
            send_d.append('16')
            self.send_data = bytes.fromhex(''.join(send_d))
            
            # 等待充电状态变化
            t1 = time.time()
            while True:
                if self.charge_state["is_charging"]:
                    print("成功开始充电！")
                    break
                elif time.time() - t1 > 10:
                    print("开始充电失败！")
                    break
                else:
                    time.sleep(1)
        
        elif command == CHARGER_STOP:
            print("收到停止充电命令")
            if not self.charge_state["is_charging"]:
                print("本来就没充电。")
                return
            
            send_d = self.send_heartbeat_data.copy()
            send_d[8] = '80'
            send_d[9] = '00'
            send_d[10] = '02'
            send_d[11] = '00'
            send_d.append('01')
            send_d.append('00')
            send_d.append(self._crc8(send_d))
            send_d.append('16')
            self.send_data = bytes.fromhex(''.join(send_d))
            
            t1 = time.time()
            while True:
                if not self.charge_state["is_charging"]:
                    print("成功关闭充电！")
                    break
                elif time.time() - t1 > 10:
                    print("关闭充电失败！")
                    break
                else:
                    time.sleep(1)
        
        elif command == WATER_START:
            print("收到开始加水命令")
            if not self.charge_state["has_contact"]:
                print("还未与充电桩接触，请接触好在加水。")
                return
            if self.charge_state["is_waterflooding"]:
                print("已经在加水了。")
                return
            
            send_d = self.send_heartbeat_data.copy()
            send_d[8] = '80'
            send_d[9] = '00'
            send_d[10] = '02'
            send_d[11] = '00'
            send_d.append('00')
            send_d.append('01')
            send_d.append(self._crc8(send_d))
            send_d.append('16')
            self.send_data = bytes.fromhex(''.join(send_d))
            
            t1 = time.time()
            while True:
                if self.charge_state["is_waterflooding"]:
                    print("成功开始加水！")
                    break
                elif time.time() - t1 > 10:
                    print("开始加水失败！")
                    break
                else:
                    time.sleep(1)
        
        elif command == WATER_STOP:
            print("收到停止加水命令")
            if not self.charge_state["is_waterflooding"]:
                print("本来就没加水。")
                return
            
            send_d = self.send_heartbeat_data.copy()
            send_d[8] = '80'
            send_d[9] = '00'
            send_d[10] = '02'
            send_d[11] = '00'
            send_d.append('00')
            send_d.append('02')
            send_d.append(self._crc8(send_d))
            send_d.append('16')
            self.send_data = bytes.fromhex(''.join(send_d))
            
            t1 = time.time()
            while True:
                if not self.charge_state["is_waterflooding"]:
                    print("成功关闭加水！")
                    break
                elif time.time() - t1 > 10:
                    print("关闭加水失败！")
                    break
                else:
                    time.sleep(1)
    
    # ==================== BLE 相关函数 ====================
    
    async def _create_bleakclient(self, address: str, connect_start_time: float):
        """创建 BLE 客户端并保持连接"""
        client = None
        try:
            print("搜索附近的蓝牙...")
            devices = await BleakScanner(discover=True, timeout=5.0)
            devices_num = len(devices)
            print(f"共搜索到 {devices_num} 个蓝牙信号。")
            
            ble_device = None
            if devices_num > 0:
                print('--------Mac-------- | --------Name-------')
                for device in devices:
                    print(f'{device.address}   | {device.name}')
                    if device.address == address:
                        ble_device = device
            
            if ble_device:
                print(f'搜索到 mac: {address}')
                print(f'name: {ble_device.name}')
                client = BleakClient(ble_device)
            else:
                print(f'未搜索到 mac: {address}，尝试直接连接')
                client = BleakClient(address)
            
            with self._client_lock:
                self._client = client
            
            await client.connect()
            print("BLE 连接成功")
            
            # 发现服务
            self.uuid_write = None
            self.uuid_notify = None
            services = client.services
            for service in services:
                for char in service.characteristics:
                    if set(['write-without-response', 'write']).intersection(set(char.properties)):
                        self.uuid_write = char.uuid
                        print(f"uuid_write: {self.uuid_write}, properties: {char.properties}")
                    elif set(['read', 'notify']).intersection(set(char.properties)):
                        self.uuid_notify = char.uuid
                        print(f"uuid_notify: {self.uuid_notify}, properties: {char.properties}")
            
            if self.uuid_write is None or self.uuid_notify is None:
                raise Exception("未找到需要的 write 或 notify 特征")
            
            await client.start_notify(self.uuid_notify, self._notify_data)
            print("start_notify 成功")
            
            # 更新状态
            self.current_mac = address
            self.charge_state["pid"] = address
            self.bluetooth_connected = True
            self.data_received_time = time.time()
            
            # 主循环：发送心跳和待发送数据
            while True:
                if not self.bluetooth_connected:
                    print("BLE 连接状态异常，退出循环")
                    break
                if not client.is_connected:
                    print("BLE 连接意外断开")
                    break
                if self.disconnect_requested:
                    print("收到断开请求，退出循环")
                    break
                
                if self.send_data is not None:
                    await client.write_gatt_char(self.uuid_write, self.send_data, response=False)
                    self.send_data = None
                
                current_time = time.time()
                if current_time - self.heartbeat_time > 0.5:
                    send_d = self.send_heartbeat_data.copy()
                    send_d[8] = '80'
                    send_d[9] = '21'
                    send_d[10] = '01'
                    send_d[11] = '00'
                    send_d.append('00')
                    send_d.append(self._crc8(send_d))
                    send_d.append('16')
                    heart_bytes = bytes.fromhex(''.join(send_d))
                    await client.write_gatt_char(self.uuid_write, heart_bytes, response=False)
                    self.heartbeat_time = current_time
                
                await asyncio.sleep(0.5)
        
        except Exception as e:
            print(f'BLE 连接/通信异常: {str(e)}')
            self.connect_exception = str(e)
        finally:
            self.bluetooth_connected = False
            self.charge_state["pid"] = ""
            self.current_mac = ""
            with self._client_lock:
                self._client = None
            if client is not None:
                try:
                    if client.is_connected:
                        await client.disconnect()
                except Exception as e:
                    print(f'断开连接时异常: {e}')
            print('BLE 连接已关闭')
    
    def _notify_data(self, sender, data: bytes):
        """处理 BLE 通知数据"""
        self.data_received_time = time.time()
        data_list = ['{:02x}'.format(x) for x in data]
        
        # 节流日志
        current_time = time.time()
        if not hasattr(self, '_last_log_time') or current_time - self._last_log_time > 10:
            print(f'解析后的数据为：{data_list}')
            self._last_log_time = current_time
        
        if len(data_list) < 10:
            print(f'数据太短: {data_list}')
            return
        
        crc8_ = self._crc8(data_list[:-2])
        crc_valid = crc8_ == data_list[-2].upper()
        
        # 发布原始数据
        self._publish_ble_data(data_list, crc_valid)
        
        if crc_valid:
            # 解析数据更新充电状态
            if data_list[8:10] == ['00', '21']:
                try:
                    self.charge_state["is_charging"] = (data_list[12] == '01')
                    self.charge_state["has_contact"] = (data_list[17] == '01')
                    self.charge_state["is_waterflooding"] = (data_list[19] == '01')
                    self.charge_state["water_mode"] = "manual" if data_list[18] == '01' else "auto"
                    
                    # 发布更新后的充电状态
                    self._publish_charge_state()
                except IndexError as e:
                    print(f'解析数据索引错误: {e}')
        else:
            print('CRC 校验失败')
    
    def _crc8(self, data: list) -> str:
        """计算 CRC8 校验值"""
        crc8 = crcmod.predefined.Crc('crc-8-maxim')
        hex_str = ' '.join(data)
        crc8.update(bytes.fromhex(hex_str))
        crc8_value = hex(~crc8.crcValue & 0xff)[2:].upper()
        return crc8_value.zfill(2)
    
    # ==================== 无数据超时检查 ====================
    
    def check_data_timeout(self):
        """检查数据接收超时"""
        if self.bluetooth_connected and self.data_received_time > 0:
            if time.time() - self.data_received_time > 20:
                print("超过20秒未收到数据，断开连接")
                self.charge_state["pid"] = ""
                self.charge_state["has_contact"] = False
                self.charge_state["is_charging"] = False
                self.charge_state["is_waterflooding"] = False
                self.disconnect_requested = True
                self.bluetooth_connected = False
                self.heartbeat_time = 0
                self._publish_charge_state()
    
    # ==================== MQTT 连接和启动 ====================
    
    def start_mqtt(self):
        """启动 MQTT 客户端"""
        self.mqtt_client = mqtt.Client()
        self.mqtt_client.on_connect = self.on_connect
        self.mqtt_client.on_message = self.on_message
        
        # 尝试连接
        try:
            self.mqtt_client.connect(self.mqtt_host, self.mqtt_port, 60)
            self.mqtt_client.loop_start()
            return True
        except Exception as e:
            print(f"MQTT 连接失败: {e}")
            return False
    
    def stop(self):
        """停止服务"""
        print("正在停止 BLE MQTT Watcher...")
        self._shutdown_event.set()
        self.disconnect_requested = True
        
        if self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
        
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._cleanup(), self.loop)
            self.loop.call_soon_threadsafe(self.loop.stop)
        
        print("服务已停止")
    
    async def _cleanup(self):
        """清理资源"""
        with self._client_lock:
            if self._client and self._client.is_connected:
                await self._client.disconnect()


def signal_handler(signum, frame):
    """信号处理函数"""
    print(f"\n收到信号 {signum}，正在退出...")
    sys.exit(0)


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='BLE MQTT Watcher')
    parser.add_argument('--host', default='localhost', help='MQTT broker 主机地址')
    parser.add_argument('--port', type=int, default=1883, help='MQTT broker 端口号')
    args = parser.parse_args()
    
    # 注册信号处理
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # 创建并启动服务
    watcher = BLEMQTTWatcher(mqtt_host=args.host, mqtt_port=args.port)
    
    if not watcher.start_mqtt():
        print("无法连接到 MQTT broker，程序退出")
        return 1
    
    print(f"BLE MQTT Watcher 已启动")
    print(f"MQTT Broker: {args.host}:{args.port}")
    print("订阅主题:")
    print("  - charger/ble/command")
    print("  - charger/ble/connect")
    print("  - charger/ble/disconnect")
    print("发布主题:")
    print("  - charger/ble/data")
    print("  - charger/ble/state")
    print("  - charger/ble/status")
    print("  - charger/ble/connect_response")
    print("  - charger/ble/disconnect_response")
    print("\n按 Ctrl+C 退出...")
    
    # 数据超时检查循环
    try:
        while True:
            time.sleep(1)
            watcher.check_data_timeout()
    except KeyboardInterrupt:
        print("\n收到中断信号")
    finally:
        watcher.stop()
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
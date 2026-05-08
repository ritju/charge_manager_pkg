import asyncio
import json
import time
import logging
import os
from logging.handlers import RotatingFileHandler
import crcmod.predefined
from bleak import BleakClient, BleakScanner
from aiomqtt import Client as MqttClient

# Setup logging with rotation
logger = logging.getLogger('bt_watcher')
logger.setLevel(logging.INFO)

# RotatingFileHandler: max 20MB per file, keep 5 backups
handler = RotatingFileHandler(
    os.path.expanduser('~/.local/share/bt_watcher/logs/bt_watcher.log'),
    maxBytes=20 * 1024 * 1024,  # 20MB
    backupCount=5
)
handler.setFormatter(logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
))
logger.addHandler(handler)

# Also log to console
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter(
    '%(asctime)s - %(levelname)s - %(message)s'
))
logger.addHandler(console_handler)


async def _safe_disconnect(client):
    """Disconnect a BleakClient safely, swallowing any errors."""
    try:
        await client.disconnect()
    except Exception as e:
        logger.debug(f'Error during disconnect (ignored): {e}')


class MQTT_BLEClient:
    # MQTT Topics
    TOPIC_DATA = "charger/ble/data"
    TOPIC_STATE = "charger/ble/state"
    TOPIC_STATUS = "charger/ble/status"
    TOPIC_COMMAND = "charger/ble/command"
    TOPIC_CONNECT = "charger/ble/connect"
    TOPIC_DISCONNECT = "charger/ble/disconnect"
    TOPIC_RESPONSE = "charger/ble/response"

    # BLE Commands
    CMD_CHARGER_START = 0
    CMD_CHARGER_STOP = 1
    CMD_WATER_START = 2
    CMD_WATER_STOP = 3

    def __init__(self, mqtt_broker="localhost", mqtt_port=1883):
        # State
        self.bluetooth_connected = False
        self.uuid_notify = None
        self.uuid_write = None
        self.send_heartbeat_data = ['6b', '00', '00', '00', '00', '6b', '00', '00', '00', '21', '09', '00']
        self.data_received_time = 0
        self.charge_state = {
            "pid": "",
            "has_contact": False,
            "is_charging": False,
            "is_waterflooding": False,
            "water_mode": "manual"
        }
        self.current_mac = ""
        self.udp_data = None

        # Async primitives
        self._ble_task = None
        self._ble_client = None
        self._disconnect_flag = False
        self._command_queue = asyncio.Queue()
        self._notify_queue = asyncio.Queue()
        self._shutdown_event = asyncio.Event()

        # MQTT config
        self.mqtt_broker = mqtt_broker
        self.mqtt_port = mqtt_port

        # Independent message-ID counters per topic
        self._data_id = 0
        self._state_id = 0
        self._status_id = 0

    async def _publish_status(self):
        self._status_id += 1
        status = {
            "id": self._status_id,
            "connected": self.bluetooth_connected,
            "mac": self.current_mac,
            "last_data_received": self.data_received_time
        }
        await self.mqtt_client.publish(self.TOPIC_STATUS, json.dumps(status), qos=1)
        logger.info(f"Published status: id={self._status_id}, connected={self.bluetooth_connected}, mac={self.current_mac or 'N/A'}")

    async def _publish_state(self):
        self._state_id += 1
        state_payload = {
            "id": self._state_id,
            "pid": self.charge_state["pid"],
            "has_contact": self.charge_state["has_contact"],
            "is_charging": self.charge_state["is_charging"],
            "is_waterflooding": self.charge_state["is_waterflooding"],
            "water_mode": self.charge_state["water_mode"],
            "timestamp": time.time()
        }
        await self.mqtt_client.publish(self.TOPIC_STATE, json.dumps(state_payload), qos=1)
        logger.info(f"Published state: id={self._state_id}, pid={self.charge_state['pid']}, charging={self.charge_state['is_charging']}, flooding={self.charge_state['is_waterflooding']}")

    async def _publish_data(self, payload):
        self._data_id += 1
        payload["id"] = self._data_id
        await self.mqtt_client.publish(self.TOPIC_DATA, json.dumps(payload), qos=1)

    async def _publish_response(self, request_id, topic, code, msg="", **extra):
        """Publish a response using the request's own ID and source topic."""
        resp = {
            "request_id": request_id,
            "topic": topic,
            "code": code,
            "timestamp": time.time()
        }
        if msg:
            resp["msg"] = msg
        resp.update(extra)
        await self.mqtt_client.publish(self.TOPIC_RESPONSE, json.dumps(resp), qos=1)

    def _notify_callback(self, sender, data):
        """Synchronous callback from Bleak — bridge to async loop."""
        self.data_received_time = time.time()
        self._notify_queue.put_nowait(data)

    async def _process_notify_data(self, data):
        """Async processing of BLE notification data."""
        data_list = ['{:02x}'.format(x) for x in data]

        raw_payload = {
            "raw_hex": data_list,
            "crc_valid": False,
            "timestamp": time.time()
        }

        if len(data_list) < 10:
            logger.warning(f'data is too short: {data_list}')
            await self._publish_data(raw_payload)
            return

        crc8_val = self.crc8(data_list[:-2])
        crc_valid = (crc8_val == data_list[-2].upper())
        raw_payload["crc_valid"] = crc_valid

        if crc_valid:
            self.udp_data = data_list
            if data_list[8:10] == ['00', '21']:
                try:
                    self.charge_state["is_charging"] = (data_list[12] == '01')
                    self.charge_state["has_contact"] = (data_list[17] == '01')
                    self.charge_state["is_waterflooding"] = (data_list[19] == '01')
                    self.charge_state["water_mode"] = "manual" if data_list[18] == '01' else "auto"
                except IndexError:
                    pass
            await self._publish_state()
        else:
            logger.warning('CRC check failed')

        await self._publish_data(raw_payload)

    async def _send_charger_start(self):
        if not self.charge_state["pid"]:
            logger.warning("Not connected to any charger")
            return
        send_d = self.send_heartbeat_data.copy()
        send_d[8] = '80'
        send_d[9] = '00'
        send_d[10] = '02'
        send_d[11] = '00'
        send_d.append('02')
        send_d.append('00')
        send_d.append(self.crc8(send_d))
        send_d.append('16')
        await self._command_queue.put(bytes.fromhex(''.join(send_d)))

    async def _send_charger_stop(self):
        if not self.charge_state["pid"]:
            logger.warning("Not connected to any charger")
            return
        send_d = self.send_heartbeat_data.copy()
        send_d[8] = '80'
        send_d[9] = '00'
        send_d[10] = '02'
        send_d[11] = '00'
        send_d.append('01')
        send_d.append('00')
        send_d.append(self.crc8(send_d))
        send_d.append('16')
        await self._command_queue.put(bytes.fromhex(''.join(send_d)))

    async def _send_water_start(self):
        if not self.charge_state["pid"]:
            logger.warning("Not connected to any charger")
            return
        send_d = self.send_heartbeat_data.copy()
        send_d[8] = '80'
        send_d[9] = '00'
        send_d[10] = '02'
        send_d[11] = '00'
        send_d.append('00')
        send_d.append('01')
        send_d.append(self.crc8(send_d))
        send_d.append('16')
        await self._command_queue.put(bytes.fromhex(''.join(send_d)))

    async def _send_water_stop(self):
        if not self.charge_state["pid"]:
            logger.warning("Not connected to any charger")
            return
        send_d = self.send_heartbeat_data.copy()
        send_d[8] = '80'
        send_d[9] = '00'
        send_d[10] = '02'
        send_d[11] = '00'
        send_d.append('00')
        send_d.append('02')
        send_d.append(self.crc8(send_d))
        send_d.append('16')
        await self._command_queue.put(bytes.fromhex(''.join(send_d)))

    async def _handle_command(self, payload):
        request_id = payload.get("id")
        command = payload.get("command")
        if command == self.CMD_CHARGER_START:
            await self._send_charger_start()
        elif command == self.CMD_CHARGER_STOP:
            await self._send_charger_stop()
        elif command == self.CMD_WATER_START:
            await self._send_water_start()
        elif command == self.CMD_WATER_STOP:
            await self._send_water_stop()
        else:
            await self._publish_response(request_id, self.TOPIC_COMMAND, "invalid_command",
                                         f"Unknown command: {command}")
            return
        await self._publish_response(request_id, self.TOPIC_COMMAND, "ok", msg="command_executed")

    async def _handle_connect(self, payload):
        request_id = payload.get("id")
        mac = payload.get("mac", "")
        if not mac:
            await self._publish_response(request_id, self.TOPIC_CONNECT, "invalid_request",
                                         "Missing 'mac' field")
            return

        # Already connected to a DIFFERENT device? Cancel existing connection
        if self.bluetooth_connected and self.current_mac != mac:
            logger.warning(
                f"Connect request for {mac}, currently connected to {self.current_mac}. Reconnecting..."
            )
            self._disconnect_flag = True
            if self._ble_task and not self._ble_task.done():
                self._ble_task.cancel()
                try:
                    await self._ble_task
                except (asyncio.CancelledError, Exception):
                    pass
            self._disconnect_flag = False

        # Connect first, then respond
        success = await self._ble_connect(mac)
        if success:
            # Start the heartbeat loop in background
            await self._publish_response(
                request_id, self.TOPIC_CONNECT, "ok",
                f"Connected to {mac}"
            )
            self._ble_task = asyncio.create_task(self._ble_run_loop())
        else:
            await self._publish_response(
                request_id, self.TOPIC_CONNECT, "connection_failed",
                f"Failed to connect to {mac}"
            )

    async def _handle_disconnect(self, payload):
        request_id = payload.get("id")
        self._disconnect_flag = True
        if self._ble_task and not self._ble_task.done():
            self._ble_task.cancel()
            try:
                await self._ble_task
            except (asyncio.CancelledError, Exception):
                pass

        await self._publish_response(request_id, self.TOPIC_DISCONNECT, "ok", msg="disconnected")

    async def _ble_connect(self, address):
        """Connect to a BLE device and discover its services. Returns True on success."""
        client = None
        try:
            logger.info(f"Scanning for Bluetooth devices...")
            devices = await BleakScanner(scanning_mode='active').discover(return_adv=True, timeout=5.0)
            logger.info(f'Found {len(devices)} Bluetooth devices.')

            ble_device = None
            if address in devices:
                ble_device = devices[address][0]
                logger.info(f'Found device at {address}')
                logger.info(f'address: {ble_device.address}')
                logger.info(f'name: {ble_device.name}')
                client = BleakClient(ble_device)
            else:
                logger.warning(f'Device not found at {address}, trying direct connection')
                client = BleakClient(address)

            await client.connect()

            self.uuid_write = None
            self.uuid_notify = None
            for service in client.services:
                for char in service.characteristics:
                    props = char.properties
                    if isinstance(props, list):
                        if 'write-without-response' in props or 'write' in props:
                            self.uuid_write = char.uuid
                            logger.info(f"uuid_write: {self.uuid_write}, properties: {props}")
                        if 'read' in props and 'notify' in props:
                            self.uuid_notify = char.uuid
                            logger.info(f"uuid_notify: {self.uuid_notify}, properties: {props}")
                    elif isinstance(props, str):
                        if 'write' in props:
                            self.uuid_write = char.uuid
                            logger.info(f"uuid_write: {self.uuid_write}, properties: {props}")
                        if 'notify' in props:
                            self.uuid_notify = char.uuid
                            logger.info(f"uuid_notify: {self.uuid_notify}, properties: {props}")

            if self.uuid_write is None or self.uuid_notify is None:
                raise Exception("Required write or notify characteristic not found")

            await client.start_notify(self.uuid_notify, self._notify_callback)
            logger.info("start_notify")

            self._ble_client = client
            self.charge_state["pid"] = address
            self.current_mac = address
            self.bluetooth_connected = True
            self._disconnect_flag = False
            return True

        except Exception as e:
            logger.error(f'BLE connection error: {str(e)}')
            # Disconnect the local client if setup failed but connect succeeded
            if client is not None:
                await _safe_disconnect(client)
            await self._ble_cleanup()
            return False

    async def _ble_run_loop(self):
        """Run the heartbeat loop after connection is established."""
        client = self._ble_client
        if client is None:
            return

        try:
            heartbeat_time = 0
            while True:
                if not client.is_connected:
                    logger.error('BLE disconnected unexpectedly')
                    break
                if self._disconnect_flag:
                    logger.info('Received disconnect request')
                    break

                # Drain pending commands
                try:
                    cmd_data = self._command_queue.get_nowait()
                    await client.write_gatt_char(self.uuid_write, cmd_data, response=False)
                    self._command_queue.task_done()
                except asyncio.QueueEmpty:
                    pass

                # Drain pending BLE notifications
                while True:
                    try:
                        notify_data = self._notify_queue.get_nowait()
                        await self._process_notify_data(notify_data)
                        self._notify_queue.task_done()
                    except asyncio.QueueEmpty:
                        break

                # Heartbeat
                current_time = time.time()
                if current_time - heartbeat_time > 0.5:
                    send_d = self.send_heartbeat_data.copy()
                    send_d[8] = '80'
                    send_d[9] = '21'
                    send_d[10] = '01'
                    send_d[11] = '00'
                    send_d.append('00')
                    send_d.append(self.crc8(send_d))
                    send_d.append('16')
                    heart_bytes = bytes.fromhex(''.join(send_d))
                    await client.write_gatt_char(self.uuid_write, heart_bytes, response=False)
                    heartbeat_time = current_time

                await asyncio.sleep(0.5)

        except asyncio.CancelledError:
            logger.info("BLE task cancelled")
        except Exception as e:
            logger.error(f'BLE communication error: {str(e)}')
        finally:
            await self._ble_cleanup()

    async def _ble_cleanup(self):
        """Clean up BLE connection state."""
        self.bluetooth_connected = False
        self.charge_state["pid"] = ""
        self.current_mac = ""
        # Drain remaining notify data
        while not self._notify_queue.empty():
            try:
                notify_data = self._notify_queue.get_nowait()
                await self._process_notify_data(notify_data)
            except asyncio.QueueEmpty:
                break
        await _safe_disconnect(self._ble_client)
        self._ble_client = None

    async def _mqtt_loop(self, mqtt_client):
        """Listen to MQTT messages and dispatch to handlers."""
        async for message in mqtt_client.messages:
            topic = str(message.topic)
            try:
                payload = json.loads(message.payload.decode())
                if topic == self.TOPIC_COMMAND:
                    await self._handle_command(payload)
                elif topic == self.TOPIC_CONNECT:
                    await self._handle_connect(payload)
                elif topic == self.TOPIC_DISCONNECT:
                    await self._handle_disconnect(payload)
            except Exception as e:
                logger.error(f"MQTT message handling error: {e}")

    async def _status_publisher_loop(self):
        """Async status publisher (2Hz)."""
        while not self._shutdown_event.is_set():
            await self._publish_status()
            await asyncio.sleep(0.5)

    async def run(self):
        """Main entry point — all async, single event loop."""
        logger.info("Starting bt_watcher service...")
        while not self._shutdown_event.is_set():
            try:
                async with MqttClient(self.mqtt_broker, self.mqtt_port) as mqtt:
                    self.mqtt_client = mqtt
                    logger.info("Connected to MQTT broker")
                    await mqtt.subscribe(self.TOPIC_COMMAND, qos=1)
                    await mqtt.subscribe(self.TOPIC_CONNECT, qos=1)
                    await mqtt.subscribe(self.TOPIC_DISCONNECT, qos=1)
                    logger.info(f"Subscribed to {self.TOPIC_COMMAND}, {self.TOPIC_CONNECT}, {self.TOPIC_DISCONNECT}")

                    # Launch tasks manually (compatible with Python < 3.11)
                    mqtt_task = asyncio.create_task(self._mqtt_loop(mqtt))
                    status_task = asyncio.create_task(self._status_publisher_loop())
                    shutdown_task = asyncio.create_task(self._shutdown_event.wait())

                    # Wait for any task to complete
                    done, pending = await asyncio.wait(
                        {mqtt_task, status_task, shutdown_task},
                        return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in pending:
                        task.cancel()
            except Exception as e:
                logger.error(f"MQTT connection error: {e}")
                logger.info("Reconnecting in 5 seconds...")
                await asyncio.sleep(5)

        # Cleanup
        if self._ble_task and not self._ble_task.done():
            self._ble_task.cancel()
            try:
                await self._ble_task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("bt_watcher shut down")

    @staticmethod
    def crc8(data):
        crc8 = crcmod.predefined.Crc('crc-8-maxim')
        hex_str = ' '.join(data)
        crc8.update(bytes.fromhex(hex_str))
        crc8_value = hex(~crc8.crcValue & 0xff)[2:].upper()
        return crc8_value.zfill(2)

    def shutdown(self):
        """Signal shutdown from synchronous context."""
        self._shutdown_event.set()


async def async_main():
    node = MQTT_BLEClient()
    await node.run()


def main():
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        logger.info("Shutting down...")


if __name__ == '__main__':
    main()


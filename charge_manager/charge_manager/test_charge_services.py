#!/usr/bin/env python3
"""System test for chargeManager services.

Compares old (std_srvs/Empty) vs new (ChargeStart/DockStart) service endpoints
side-by-side to verify the new interfaces return structured error codes correctly.

Prerequisites:
  - chargeManager node running (via charge_dock.launch.py)
  - charge_action node running
  - bluetooth_charge_server node running (or mocked)

Usage:
  python3 charge_manager/test/test_charge_services.py
  # or after build:
  ros2 run charge_manager test_charge_services
"""

import rclpy
from rclpy.node import Node
from std_srvs.srv import Empty
from capella_ros_service_interfaces.srv import ChargeStart, DockStart
from geometry_msgs.msg import Pose, Quaternion
import time
import sys


class ChargeServiceTest(Node):
    def __init__(self):
        super().__init__('test_charge_services')
        self.get_logger().info('Initializing test node...')

        # Old-style clients (std_srvs/Empty)
        self.client_start = self.create_client(Empty, '/charger/start')
        self.client_start_docking = self.create_client(Empty, '/charger/start_docking')
        self.client_stop_docking = self.create_client(Empty, '/charger/stop_docking')
        self.client_stop = self.create_client(Empty, '/charger/stop')

        # New-style clients (with error codes)
        self.client_start2 = self.create_client(ChargeStart, '/charger/start2')
        self.client_start_docking2 = self.create_client(DockStart, '/charger/start_docking2')

    def wait_for_services(self, timeout=10.0):
        """Wait for all required services to be available. Returns True if all ready."""
        services = {
            '/charger/start': self.client_start,
            '/charger/start2': self.client_start2,
            '/charger/start_docking': self.client_start_docking,
            '/charger/start_docking2': self.client_start_docking2,
            '/charger/stop_docking': self.client_stop_docking,
            '/charger/stop': self.client_stop,
        }
        all_ready = True
        for name, client in services.items():
            if client.wait_for_service(timeout_sec=timeout):
                self.get_logger().info(f'  {name}: available')
            else:
                self.get_logger().error(f'  {name}: NOT available (timeout {timeout}s)')
                all_ready = False
        return all_ready

    def call_start(self):
        """Call /charger/start (old, Empty). Returns (ok, info_str)."""
        req = Empty.Request()
        try:
            future = self.client_start.call_async(req)
            rclpy.spin_until_future_complete(self, future, timeout_sec=10.0)
            if future.result() is not None:
                return True, 'Empty response (no error code)'
            else:
                return False, 'Service call failed (no response)'
        except Exception as e:
            return False, f'Exception: {e}'

    def call_start2(self):
        """Call /charger/start2 (new, ChargeStart). Returns (ok, code, message)."""
        req = ChargeStart.Request()
        try:
            future = self.client_start2.call_async(req)
            rclpy.spin_until_future_complete(self, future, timeout_sec=10.0)
            resp = future.result()
            if resp is not None:
                return True, resp.code, resp.message
            else:
                return False, -1, 'Service call failed (no response)'
        except Exception as e:
            return False, -1, f'Exception: {e}'

    def call_start_docking(self, mac=''):
        """Call /charger/start_docking (old, Empty). Returns (ok, info_str)."""
        req = Empty.Request()
        try:
            future = self.client_start_docking.call_async(req)
            rclpy.spin_until_future_complete(self, future, timeout_sec=10.0)
            if future.result() is not None:
                return True, 'Empty response (no error code)'
            else:
                return False, 'Service call failed (no response)'
        except Exception as e:
            return False, f'Exception: {e}'

    def call_start_docking2(self, mac='', marker='', protocol='', delta=None):
        """Call /charger/start_docking2 (new, DockStart). Returns (ok, code, message)."""
        req = DockStart.Request()
        req.mac = mac
        req.marker = marker
        req.protocol = protocol
        if delta is None:
            delta = Pose()
        req.delta = delta
        try:
            future = self.client_start_docking2.call_async(req)
            rclpy.spin_until_future_complete(self, future, timeout_sec=10.0)
            resp = future.result()
            if resp is not None:
                return True, resp.code, resp.message
            else:
                return False, -1, 'Service call failed (no response)'
        except Exception as e:
            return False, -1, f'Exception: {e}'

    def call_stop_docking(self):
        """Call /charger/stop_docking (old, Empty). Returns (ok, info_str)."""
        req = Empty.Request()
        try:
            future = self.client_stop_docking.call_async(req)
            rclpy.spin_until_future_complete(self, future, timeout_sec=10.0)
            if future.result() is not None:
                return True, 'Empty response (no error code)'
            else:
                return False, 'Service call failed (no response)'
        except Exception as e:
            return False, f'Exception: {e}'

    def call_stop(self):
        """Call /charger/stop (old, Empty). Returns (ok, info_str)."""
        req = Empty.Request()
        try:
            future = self.client_stop.call_async(req)
            rclpy.spin_until_future_complete(self, future, timeout_sec=10.0)
            if future.result() is not None:
                return True, 'Empty response (no error code)'
            else:
                return False, 'Service call failed (no response)'
        except Exception as e:
            return False, f'Exception: {e}'


def separator():
    print('=' * 56)


def run_all_tests():
    separator()
    print('  chargeManager Service System Test')
    separator()
    print()

    rclpy.init()
    node = ChargeServiceTest()

    # Step 1: Wait for services
    print('[INIT] Waiting for services...')
    if not node.wait_for_services(timeout=15.0):
        node.get_logger().error('Not all services available. Aborting.')
        rclpy.shutdown()
        sys.exit(1)
    print('  All services available.')
    print()

    passed = 0
    failed = 0

    # ----------------------------------------------------------------
    # TEST 1: /charger/start vs /charger/start2
    # ----------------------------------------------------------------
    print('[TEST 1] /charger/start vs /charger/start2')
    print('-' * 40)

    ok_old, info_old = node.call_start()
    print(f'  /charger/start:    {"OK" if ok_old else "FAIL"} ({info_old})')

    ok_new, code_new, msg_new = node.call_start2()
    code_str = str(code_new) if ok_new else 'N/A'
    print(f'  /charger/start2:   {"OK" if ok_new else "FAIL"} (code={code_str}, message="{msg_new}")')

    # Comparison
    if ok_old and ok_new:
        if code_new == 0:
            print('  Comparison: Both succeeded. /start2 confirms success (code=0).')
            passed += 1
        elif code_new in (30, 32):
            print(f'  Comparison: /start returned OK (fire-and-forget), /start2 detected '
                  f'issue (code={code_new}). /start2 provides better feedback.')
            passed += 1
        else:
            print(f'  Comparison: Unexpected code={code_new}. Review needed.')
            failed += 1
    elif ok_new and not ok_old:
        print('  Comparison: /start2 succeeded but /start failed.')
        failed += 1
    else:
        print('  Comparison: Both or /start2 failed.')
        failed += 1
    print()

    # ----------------------------------------------------------------
    # TEST 2: /charger/start_docking vs /charger/start_docking2
    # ----------------------------------------------------------------
    print('[TEST 2] /charger/start_docking vs /charger/start_docking2')
    print('-' * 40)

    ok_old, info_old = node.call_start_docking()
    print(f'  /charger/start_docking:  {"OK" if ok_old else "FAIL"} ({info_old})')

    ok_new, code_new, msg_new = node.call_start_docking2()
    code_str = str(code_new) if ok_new else 'N/A'
    print(f'  /charger/start_docking2: {"OK" if ok_new else "FAIL"} (code={code_str}, message="{msg_new}")')

    # Comparison
    if ok_old and ok_new:
        if code_new == 0:
            print('  Comparison: Both succeeded. /start_docking2 dispatches charge action (code=0).')
            passed += 1
        elif code_new == 10:
            print('  Comparison: /start_docking returned OK (fire-and-forget), /start_docking2 '
                  'detected charge action server unavailable (code=10). /start_docking2 is more robust.')
            passed += 1
        else:
            print(f'  Comparison: Unexpected code={code_new}. Review needed.')
            failed += 1
    elif ok_new and not ok_old:
        print('  Comparison: /start_docking2 succeeded but /start_docking failed.')
        failed += 1
    else:
        print('  Comparison: Both or /start_docking2 failed.')
        failed += 1
    print()

    # ----------------------------------------------------------------
    # TEST 3: /charger/stop_docking (cleanup from test 2)
    # ----------------------------------------------------------------
    print('[TEST 3] /charger/stop_docking')
    print('-' * 40)
    ok, info = node.call_stop_docking()
    print(f'  /charger/stop_docking: {"OK" if ok else "FAIL"} ({info})')
    if ok:
        passed += 1
    else:
        failed += 1
    print()

    # ----------------------------------------------------------------
    # TEST 4: /charger/stop (cleanup from test 1)
    # ----------------------------------------------------------------
    print('[TEST 4] /charger/stop')
    print('-' * 40)
    ok, info = node.call_stop()
    print(f'  /charger/stop: {"OK" if ok else "FAIL"} ({info})')
    if ok:
        passed += 1
    else:
        failed += 1
    print()

    # ----------------------------------------------------------------
    # Summary
    # ----------------------------------------------------------------
    separator()
    total = passed + failed
    print(f'  Results: {passed}/{total} passed')
    separator()

    node.destroy_node()
    rclpy.shutdown()

    if failed > 0:
        sys.exit(1)


if __name__ == '__main__':
    run_all_tests()

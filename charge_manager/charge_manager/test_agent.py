from bleak import *
from bleak.backends.bluezdbus.manager import *
from types import MethodType

import logging
import asyncio

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def no_scan(
    self: BlueZManager,
    adapter_path: str,
    filters: Dict[str, Variant],
    advertisement_callback: AdvertisementCallback,
    device_removed_callback: DeviceRemovedCallback,
) -> Callable[[], Coroutine]:
    async with self._bus_lock:
        self._check_adapter(adapter_path)

        callback_and_state = CallbackAndState(advertisement_callback, adapter_path)
        self._advertisement_callbacks.append(callback_and_state)

        device_removed_callback_and_state = DeviceRemovedCallbackAndState(
            device_removed_callback, adapter_path
        )
        self._device_removed_callbacks.append(device_removed_callback_and_state)

        try:
            async def stop() -> None:
                self._advertisement_callbacks.remove(callback_and_state)
                self._device_removed_callbacks.remove(
                    device_removed_callback_and_state
                )

            return stop
        except BaseException:
            self._advertisement_callbacks.remove(callback_and_state)
            self._device_removed_callbacks.remove(device_removed_callback_and_state)
            raise

async def scan():
    scanner = BleakScanner()

    async def monitor():
        async for (device, adv) in scanner.advertisement_data():
            logger.info('found device {}'.format(device.address))

    manager = await get_global_bluez_manager()
    manager.active_scan = MethodType(no_scan, manager)

    await scanner.start()
    await monitor()

asyncio.run(scan())
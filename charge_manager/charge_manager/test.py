import bleak
import asyncio
import time


async def test_scan():
    for i in range(10000):
        print(f'第{i+1}次扫描')
        scan = await bleak.BleakScanner().discover()
        for i in scan:
            print(i)
        time.sleep(2)

asyncio.run(test_scan())
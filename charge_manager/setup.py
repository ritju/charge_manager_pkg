from setuptools import find_packages, setup
import os
from glob import glob

package_name = 'charge_manager'

# 自动获取restore目录下所有txt文件
restore_files = []
for root, dirs, files in os.walk('restore'):
    for file in files:
        if file.endswith('.txt'):
            restore_files.append(os.path.join(root, file))

setup(
    name=package_name,
    version='0.0.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
         # 动态添加restore文件夹下的所有txt文件
        ('share/' + package_name + '/restore', restore_files),
        # 添加 launch 文件
        (os.path.join('share', package_name, 'launch'), glob('launch/*.py')),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='ros',
    maintainer_email='ros@todo.todo',
    description='TODO: Package description',
    license='TODO: License declaration',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
                'connect_bluetooth_srv_server=charge_manager.connect_bluetooth_srv_server:main',
                'charge_action=charge_manager.charge_action:main',
                'charge_manage=charge_manager.charge_manager:main',
                'charge_bluetooth_old=charge_manager.charge_service_bluetooth:main',
                'test_dance=charge_manager.video_control_speed_node:main',
                'mqtt_bridge=charge_manager.mqtt_bridge_for_ros2:main',
        ],
    },
)

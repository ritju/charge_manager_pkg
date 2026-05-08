from setuptools import setup

package_name = 'bt_watcher'

setup(
    name=package_name,
    version='0.1.0',
    packages=[package_name],
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
    ],
    install_requires=['setuptools', 'aiomqtt>=2.0.0', 'bleak>=0.21.0', 'crcmod>=1.7'],
    zip_safe=True,
    maintainer='sherlock',
    maintainer_email='sherlock@example.com',
    description='Bluetooth watcher with MQTT communication',
    license='Apache-2.0',
    entry_points={
        'console_scripts': [
            'bt_watcher = bt_watcher.bt_watcher:main',
        ],
    },
)

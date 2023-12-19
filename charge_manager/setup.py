from setuptools import find_packages, setup

package_name = 'charge_manager'

setup(
    name=package_name,
    version='0.0.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
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
                'charge_bluetooth_old=charge_manager.charge_service_bluetooth:main'
        ],
    },
)

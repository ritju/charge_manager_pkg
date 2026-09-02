import os
from ament_index_python import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription, GroupAction
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch_ros.actions import Node
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch.conditions import IfCondition
from nav2_common.launch import RewrittenYaml


def get_environment_value(env, default):
    """获取环境变量值，不存在时使用默认值"""
    try:
        if env in os.environ:
            value = os.environ.get(env, default)
            print(f'get {env} value: {value} from environment')
            return value
        else:
            print(f"Using default {env} value: {default}.")
            return default
    except Exception as e:
        print(f'exception: {str(e)}')
        print(f"Please input {env} in environment")
        return default


def generate_launch_description():

    launch_description = LaunchDescription()

    # ==================== 功能分组开关参数 ====================
    enable_charge_system_arg = DeclareLaunchArgument(
        'enable_charge_system',
        default_value='true',
        description='Enable charge management system (charge_manager, charge_action, bluetooth_old)'
    )

    enable_dock_system_arg = DeclareLaunchArgument(
        'enable_dock_system',
        default_value='true',
        description='Enable docking system (manual_dock, motion_control, coord_optimize)'
    )

    enable_camera_arg = DeclareLaunchArgument(
        'enable_camera',
        default_value='true',
        description='Enable camera and marker detection (camera, aruco/apriltag)'
    )

    enable_serial_arg = DeclareLaunchArgument(
        'enable_serial',
        default_value='false',
        description='Enable serial communication node'
    )

    enable_wifi_bluetooth_arg = DeclareLaunchArgument(
        'enable_wifi_bluetooth',
        default_value='false',
        description='Enable WiFi and Bluetooth service nodes'
    )

    # ==================== 日志级别参数 ====================
    motion_control_log_level_arg = DeclareLaunchArgument(
        'motion_control_log_level',
        default_value='info',
        description='Define motion_control node log level'
    )

    apriltag_double_log_level_arg = DeclareLaunchArgument(
        'apriltag_double_log_level',
        default_value='info',
        description='Define apriltag_double node log level'
    )

    robot_version_arg = DeclareLaunchArgument(
        'robot_version',
        default_value='capella',
        description='Define robot version, it will affect the robot footprint and some parameters'
    )

    # ==================== 获取 Launch 配置 ====================
    enable_charge_system = LaunchConfiguration('enable_charge_system')
    enable_dock_system = LaunchConfiguration('enable_dock_system')
    enable_camera = LaunchConfiguration('enable_camera')
    enable_serial = LaunchConfiguration('enable_serial')
    enable_wifi_bluetooth = LaunchConfiguration('enable_wifi_bluetooth')
    robot_version = LaunchConfiguration('robot_version')

    # ==================== 获取包路径 ====================
    try:
        camera_pkg_path = get_package_share_directory('astra_camera')
    except Exception:
        camera_pkg_path = ''
        print('Warning: astra_camera package not found')

    try:
        aruco_pkg_path = get_package_share_directory('aruco_ros')
    except Exception:
        aruco_pkg_path = ''
        print('Warning: aruco_ros package not found')

    try:
        apriltag_pkg_path = get_package_share_directory('apriltag_ros')
    except Exception:
        apriltag_pkg_path = ''
        print('Warning: apriltag_ros package not found')

    try:
        dock_pkg_path = get_package_share_directory('capella_ros_dock')
    except Exception:
        dock_pkg_path = ''
        print('Warning: capella_ros_dock package not found')

    # ==================== 获取环境变量值 ====================
    dock_param_file_name = get_environment_value("DOCK_PARAM_FILE", "config.yaml")
    charger_contact_type = get_environment_value("CHARGER_CONTACT_CONDITION_TYPE", "BLUETOOTH_ONLY")
    last_docked_offset = get_environment_value("LAST_DOCKED_DISTANCE_OFFSET", "0.30")
    camera_baselink_distance = get_environment_value("CAMERA_BASELINK_DIS", "0.3")
    goal_y_correction = get_environment_value("DOCK_GOAL_Y_CORRECTION", "0.0")
    garage_test = get_environment_value("DOCK_GARAGE_TEST", "false")
    offset_buffer_goal2_x = get_environment_value("DOCK_OFFSET_BUFFER_POINT2_X", "1.5")
    offset_buffer_goal2_y = get_environment_value("DOCK_OFFSET_BUFFER_POINT2_Y", "0.0")
    contacted_keep_move_time = get_environment_value("DOCK_CONTACTED_KEEP_MOVE_TIME", "0.3")

    # 类型映射
    type_mapping = {
        'BLUETOOTH_ONLY': 0,
        'CAMERA_ONLY': 1,
        'BLUETOOTH_AND_CAMERA': 2
    }

    # 构建参数文件路径
    params_file_path = PathJoinSubstitution([
        dock_pkg_path, 'params', dock_param_file_name
    ])

    # 参数替换配置
    param_substitutions = {
        "charger_contact_condition_type": str(type_mapping.get(charger_contact_type, 0)),
        "offset_last_docked_distance": str(last_docked_offset),
        "camera_baselink_dis": str(camera_baselink_distance),
        "goal_y_correction": str(goal_y_correction),
        "garage_test": str(garage_test),
        "offset_buffer_goal2_x": str(offset_buffer_goal2_x),
        "offset_buffer_goal2_y": str(offset_buffer_goal2_y),
        "contacted_keep_move_time": str(contacted_keep_move_time),
    }

    # 配置参数文件
    configured_params = RewrittenYaml(
        source_file=params_file_path,
        param_rewrites=param_substitutions,
        convert_types=True
    )

    # ==================== 充电管理系统节点 ====================
    charge_manager_node = Node(
        executable='charge_manage',
        package='charge_manager',
        name='charge_manager_node',
        respawn=True,
        condition=IfCondition(enable_charge_system)
    )

    charge_action_node = Node(
        executable='charge_action',
        package='charge_manager',
        name='charge_action_node',
        respawn=True,
        condition=IfCondition(enable_charge_system)
    )

    bluetooth_old_node = Node(
        executable='charge_bluetooth_old',
        package='charge_manager',
        name='charge_bluetooth_server_node',
        respawn=True,
        condition=IfCondition(enable_charge_system)
    )

    # ==================== 对接系统节点 ====================
    manual_dock_node = Node(
        executable='manual_dock',
        package='capella_ros_dock',
        name='manual_dock',
        namespace='',
        output='screen',
        parameters=[configured_params],
        condition=IfCondition(enable_dock_system)
    )

    motion_control_node = Node(
        executable='motion_control',
        package='capella_ros_dock',
        name='motion_control',
        namespace='',
        output='screen',
        parameters=[configured_params, {"robot_version": robot_version}],
        arguments=['--ros-args', '--log-level', ['motion_control:=', LaunchConfiguration("motion_control_log_level")]],
        condition=IfCondition(enable_dock_system)
    )

    coord_optimize_node = Node(
        executable='coord_optimize_node',
        package='capella_ros_dock',
        name='coord_optimize_node',
        namespace='',
        output='screen',
        parameters=[configured_params],
        condition=IfCondition(enable_dock_system)
    )

    # ==================== 串口节点 ====================
    serial_node = Node(
        executable='serial_port_node',
        package='capella_ros_serial',
        name='serial_node',
        namespace='',
        condition=IfCondition(enable_serial)
    )

    # ==================== WiFi/蓝牙服务节点 ====================
    wifi_node = Node(
        executable='charge_server_node',
        package='capella_charge_service',
        name='wifi_server',
        respawn=True,
        condition=IfCondition(enable_wifi_bluetooth)
    )

    bluetooth_node = Node(
        executable='charge_server_bluetooth',
        package='capella_charge_service',
        name='bluetooth_server',
        respawn=True,
        condition=IfCondition(enable_wifi_bluetooth)
    )

    # ==================== 相机和标签检测 Launch ====================
    # 根据环境变量选择标签类型
    marker_type = os.environ.get('CHARGER_MARKER_TYPE', 'ARUCO').upper()

    camera_launch_file = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(camera_pkg_path, 'launch', 'dabai_dcw.launch.py')),
        condition=IfCondition(enable_camera)
    ) if camera_pkg_path else None

    aruco_launch_file = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(aruco_pkg_path, 'launch', 'single.launch.py')),
        condition=IfCondition(enable_camera)
    ) if aruco_pkg_path else None

    apriltag_launch_file = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(apriltag_pkg_path, 'launch', 'apriltag_ros.launch.py')),
        condition=IfCondition(enable_camera)
    ) if apriltag_pkg_path else None

    apriltag_double_launch_file = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(apriltag_pkg_path, 'launch', 'apriltag_ros_double.launch.py')),
        launch_arguments={
            "log_level": LaunchConfiguration("apriltag_double_log_level")
        }.items(),
        condition=IfCondition(enable_camera)
    ) if apriltag_pkg_path else None

    # ==================== 添加所有参数声明 ====================
    launch_description.add_action(enable_charge_system_arg)
    launch_description.add_action(enable_dock_system_arg)
    launch_description.add_action(enable_camera_arg)
    launch_description.add_action(enable_serial_arg)
    launch_description.add_action(enable_wifi_bluetooth_arg)
    launch_description.add_action(motion_control_log_level_arg)
    launch_description.add_action(apriltag_double_log_level_arg)
    launch_description.add_action(robot_version_arg)

    # ==================== 添加充电管理系统节点 ====================
    launch_description.add_action(charge_manager_node)
    launch_description.add_action(charge_action_node)
    launch_description.add_action(bluetooth_old_node)

    # ==================== 添加对接系统节点 ====================
    launch_description.add_action(manual_dock_node)
    launch_description.add_action(motion_control_node)

    # coord_optimize_node 仅在 APRILTAG_DOUBLE 模式下启用
    if marker_type == 'APRILTAG_DOUBLE':
        launch_description.add_action(coord_optimize_node)

    # ==================== 添加串口节点 ====================
    launch_description.add_action(serial_node)

    # ==================== 添加 WiFi/蓝牙节点 ====================
    launch_description.add_action(wifi_node)
    launch_description.add_action(bluetooth_node)

    # ==================== 添加相机和标签检测 ====================
    if camera_launch_file:
        launch_description.add_action(camera_launch_file)

    # 根据标签类型添加对应的 launch 文件
    if marker_type == 'ARUCO' and aruco_launch_file:
        launch_description.add_action(aruco_launch_file)
    elif marker_type == 'APRILTAG' and apriltag_launch_file:
        launch_description.add_action(apriltag_launch_file)
    elif marker_type == 'APRILTAG_DOUBLE' and apriltag_double_launch_file:
        launch_description.add_action(apriltag_double_launch_file)
    else:
        # 默认使用 aruco
        if aruco_launch_file:
            print(f'Unknown CHARGER_MARKER_TYPE: {marker_type}, using ARUCO as default')
            launch_description.add_action(aruco_launch_file)

    return launch_description

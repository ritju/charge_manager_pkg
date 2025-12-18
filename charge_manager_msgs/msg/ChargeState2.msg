string pid
bool has_contact
bool is_charging
bool is_waterflooding
string water_mode
int8 bit0  # 无法关闭加水电磁阀
int8 bit1  # 一直处于手动加水状态
int8 bit2  # 无法关闭充电
int8 bit3  # 左距离传感器不在线（有延时）
int8 bit4  # 右距离传感器不在线（有延时）
int8 bit5  # 距离传感器到位，行程开关不到位（有延时）
int8 bit6  # 行程开关到位，但距离传感器没到位
int8 bit7  # 暂时无
int64 left_sensor_dis  # 左侧距离传感器距离，单位mm
int64 right_sensor_dis # 右侧距离传感器距离，单位mm
int8 switch_stu  # 行程关电开关状态 行程关电开关状态： 0x00：行程到位， 0x01：行程未到位







def parse_fault(fault_stu_str: str, fault_map: dict, default_ret: str) -> str:
    """
    解析故障字节（十六进制字符串），根据传入的故障映射表返回故障描述。

    Args:
        fault_stu_str: 十六进制字符串，如 '20' 或 '0x20'
        fault_map: 字典，键为故障掩码（int），值为对应的描述（str）

    Returns:
        故障描述字符串，多个用 '|' 连接，无故障返回 default_ret
    """
    value = int(fault_stu_str, 16)
    descriptions = [desc for mask, desc in fault_map.items() if value & mask]
    return "|".join(descriptions) if descriptions else default_ret

def calculate_dis(arg1: str, arg2: str) -> int:
    """
    将两个十六进制字符串（低字节和高字节）组合成一个 16 位整数。

    Args:
        arg1: 低字节，如 'f1' 或 '0xf1'
        arg2: 高字节，如 'f2' 或 '0xf2'

    Returns:
        组合后的整数值，即 (高字节 << 8) | 低字节
    """
    low = int(arg1, 16)   # 低字节数值 (0~255)
    high = int(arg2, 16)  # 高字节数值 (0~255)
    return (high << 8) | low

if __name__ == '__main__':
    fault_map = {
        0x01: "无法关闭加水电磁阀",
        0x02: "一直处于手动加水状态",
        0x04: "无法关闭充电",
        0x08: "左距离传感器不在线（有延时）",
        0x10: "右距离传感器不在线（有延时）",
        0x20: "距离传感器到位，行程开关不到位，可能存在行程开关故障/不在线",
        0x40: "行程开关到位，但距离传感器没到位"
    }

    switch_stu_map = {
        0x00: "行程到位",
        0x01: "行程未到位",
    }
    print(parse_fault('00', fault_map, "无故障"))
    print(parse_fault('01', fault_map, "无故障"))
    print(parse_fault('02', fault_map, "无故障"))
    print(parse_fault('04', fault_map, "无故障"))
    print(parse_fault('08', fault_map, "无故障"))
    print(parse_fault('10', fault_map, "无故障"))
    print(parse_fault('20', fault_map, "无故障"))
    print(parse_fault('40', fault_map, "无故障"))
    print(parse_fault('60', fault_map, "无故障"))
    print(parse_fault('44', fault_map, "无故障"))

    print(calculate_dis('b2', '00'))
    print(calculate_dis('b2', '01'))
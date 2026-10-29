def bytes_to_gb(bytes_count: int, decimals: int = 4) -> float:
    """Converts bytes to gigabytes (GB) rounded to specified decimals"""
    if not bytes_count or bytes_count <= 0:
        return 0.0
    gb = bytes_count / (1024 ** 3)
    return round(gb, decimals)

def gb_to_bytes(gb_count: float) -> int:
    """Converts gigabytes (GB) to bytes integer"""
    if not gb_count or gb_count <= 0:
        return 0
    return int(gb_count * (1024 ** 3))
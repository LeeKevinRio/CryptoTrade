"""在 Windows 上阻止系統睡眠（家用電腦跑真金用）

SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)：呼叫執行緒存活期間
系統不會因閒置進入睡眠；程式結束即自動恢復，不改電源設定。螢幕仍可正常關閉。
非 Windows 平台不做事。
"""

import sys

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def keep_awake() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED))
    except Exception:  # noqa: BLE001 — 失敗只代表可能會睡眠，不影響交易
        return False

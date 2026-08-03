#!/usr/bin/env python3
"""安裝精靈——把需要的元件裝好。由 1_安裝.bat 呼叫。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# 讓中文與符號在任何系統編碼下都不會讓程式當掉
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).parent
LINE = "=" * 58


def box(*lines: str) -> None:
    print()
    print(LINE)
    for ln in lines:
        print("  " + ln)
    print(LINE)
    print()


def main() -> int:
    box("第 1 步：安裝需要的元件")

    print(f"已偵測到 Python {sys.version.split()[0]}")
    print(f"位置：{sys.executable}\n")

    if sys.version_info < (3, 8):
        box(
            "Python 版本太舊 ✗",
            "",
            f"這台電腦是 Python {sys.version.split()[0]}，程式需要 3.8 以上。",
            "",
            "請到 https://www.python.org/downloads/ 下載新版，",
            "安裝時記得勾選 Add python.exe to PATH。",
        )
        return 1

    print("正在下載元件，請稍等一下（大約 30 秒）……\n")

    sys.path.insert(0, str(HERE))
    from deps import ensure, missing  # noqa: E402

    # 就算元件已經在了也重跑一次 pip，確保是最新且完整的
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(HERE / "requirements.txt")],
        cwd=HERE,
    )

    if missing() and not ensure():
        return 1  # ensure() 已經印過詳細的失敗說明了

    box(
        "安裝完成！ ✓",
        "",
        "接下來請關掉這個視窗，",
        "改點兩下「2_設定.bat」。",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

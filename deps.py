#!/usr/bin/env python3
"""檢查需要的元件在不在；不在的話自動幫忙裝好。

三個程式（install / setup / ptt_watcher）都會用到這裡，
確保「安裝的 Python」和「執行的 Python」一定是同一個。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
REQUIREMENTS = HERE / "requirements.txt"

# 套件的「安裝名稱」對「import 名稱」
NEEDED = {"requests": "requests", "beautifulsoup4": "bs4"}


def missing() -> list[str]:
    """回傳還沒裝好的套件安裝名稱。"""
    lack = []
    for pip_name, import_name in NEEDED.items():
        try:
            __import__(import_name)
        except ImportError:
            lack.append(pip_name)
    return lack


def _pip(*args: str) -> int:
    return subprocess.call([sys.executable, "-m", "pip", *args], cwd=HERE)


def ensure(verbose: bool = True) -> bool:
    """確保元件都在。缺的話自動安裝，成功回傳 True。"""
    lack = missing()
    if not lack:
        return True

    if verbose:
        print()
        print("=" * 58)
        print("  偵測到還缺少元件，正在自動安裝，請稍等約 30 秒……")
        print(f"  缺少：{', '.join(lack)}")
        print("=" * 58)
        print()

    rc = _pip("install", "-r", str(REQUIREMENTS))
    if missing():
        # 沒有寫入權限時，改裝到個人資料夾再試一次
        if verbose:
            print("\n第一次沒成功，改用個人資料夾模式再試一次……\n")
        rc = _pip("install", "--user", "-r", str(REQUIREMENTS))

    # 剛裝好的套件路徑可能還沒進到搜尋清單，重新整理一下
    import importlib
    import site

    importlib.invalidate_caches()
    try:
        importlib.reload(site)
    except Exception:
        pass

    lack = missing()
    if lack:
        if verbose:
            print()
            print("=" * 58)
            print("  自動安裝失敗 ✗")
            print("")
            print(f"  還是缺少：{', '.join(lack)}")
            print(f"  pip 回傳碼：{rc}")
            print("")
            print("  請試試看：")
            print("  1. 確認這台電腦有連上網路")
            print("  2. 關掉所有黑色視窗，重新點兩下「1_安裝.bat」")
            print("  3. 如果公司電腦有防火牆擋住，可能要換個網路試試")
            print("")
            print(f"  目前使用的 Python：")
            print(f"  {sys.executable}")
            print("=" * 58)
            print()
        return False

    if verbose:
        print("\n元件安裝完成 ✓\n")
    return True


if __name__ == "__main__":
    sys.exit(0 if ensure() else 1)

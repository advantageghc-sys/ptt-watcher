#!/usr/bin/env python3
"""互動式設定精靈——問幾個問題就幫你把 config.json 寫好，不用自己編輯檔案。"""

from __future__ import annotations

import json
import secrets
import string
import sys
from pathlib import Path

# 這台電腦的系統編碼可能是 cp950，不是 UTF-8。加上這段才不會當掉。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "config.json"
STATE_PATH = HERE / "seen.json"

LINE = "=" * 58


def box(*lines: str) -> None:
    print()
    print(LINE)
    for ln in lines:
        print("  " + ln)
    print(LINE)
    print()


def ask(prompt: str, default: str = "") -> str:
    suffix = f"（直接按 Enter 就用 {default}）" if default else ""
    val = input(f"{prompt}{suffix}\n> ").strip()
    return val or default


def random_topic() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "ptt-" + "".join(secrets.choice(alphabet) for _ in range(14))


def main() -> None:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")) if CONFIG_PATH.exists() else {}
    cfg.setdefault("boards", ["SportLottery"])
    cfg.setdefault("interval_seconds", 60)
    cfg.setdefault("board_delay_seconds", 1.5)
    cfg.setdefault("skip_reply", False)
    cfg.setdefault("title_include", [])
    cfg.setdefault("title_exclude", [])
    nt = cfg.setdefault("ntfy", {})
    nt.setdefault("server", "https://ntfy.sh")
    nt.setdefault("priority", 4)
    nt.setdefault("tags", ["loudspeaker"])
    nt.setdefault("token", "")

    box("PTT 發文監控 — 設定精靈", "", "接下來會問你幾個問題，照著打字就好。")

    # 先確認元件都在，免得問完一大堆問題最後才發現不能用
    sys.path.insert(0, str(HERE))
    from deps import ensure  # noqa: E402

    if not ensure():
        print("元件沒裝好，無法繼續。請照上面的說明處理後再試一次。")
        return

    # ---------- 1. 要監控的帳號 ----------
    print("【第 1 步】要監控哪些 PTT 帳號？")
    print("（就是文章列表左邊那個作者 ID，大小寫不用管）\n")
    authors: list[str] = []
    while True:
        n = len(authors) + 1
        a = input(f"第 {n} 個帳號（沒有了就直接按 Enter）\n> ").strip()
        if not a:
            if authors:
                break
            print("至少要填一個帳號喔。\n")
            continue
        if a in authors:
            print("這個帳號已經加過了。\n")
            continue
        authors.append(a)
        print(f"  ✓ 已加入：{a}\n")
    cfg["authors"] = authors

    # ---------- 2. 要不要連留言也監控 ----------
    print("\n【第 2 步】除了「發文」，要不要連「留言（推文）」也監控？")
    print("")
    print("  Y = 他們每推一則文，你就收到一則通知，通知上直接看得到留言內容")
    print("  N = 只有發新文章才通知")
    print("")
    print("  ※ 留言比發文頻繁很多，一天可能有幾十則通知。")
    print("  ※ 開啟後程式要一篇篇打開文章去找留言，會比較耗網路。")
    print("")
    ans = ask("要監控留言嗎？(Y/N)", "Y" if cfg.get("watch_comments", True) else "N")
    cfg["watch_comments"] = ans.upper().startswith("Y")
    print(f"  ✓ 留言監控：{'開啟' if cfg['watch_comments'] else '關閉'}")

    # ---------- 3. 頻道名稱 ----------
    old_topic = nt.get("topic", "")
    if old_topic:
        print(f"\n【第 3 步】你目前的手機頻道名稱是：{old_topic}")
        keep = ask("要繼續用這個嗎？(Y=沿用 / N=重新產生一組)", "Y")
        topic = old_topic if keep.upper().startswith("Y") else random_topic()
    else:
        print("\n【第 3 步】手機通知頻道")
        print("如果你手機的 ntfy 之前「已經訂閱過」一個頻道，把那串頻道名稱")
        print("原封不動打進來，就可以繼續用同一個，手機不用重新設定。")
        t = ask("頻道名稱（沒有訂閱過就直接按 Enter，幫你產生新的）")
        topic = t.strip() or random_topic()
    nt["topic"] = topic

    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n設定已存檔 ✓")

    # ---------- 4. 手機設定 ----------
    box(
        "【第 4 步】請拿起手機，照著做：",
        "",
        "1. 打開 Google Play 商店",
        "2. 搜尋「ntfy」，安裝那個綠色擴音器圖示的 App",
        "3. 打開 App，按右下角的「+」圓形按鈕",
        "4. 在輸入框裡「一字不漏」打進下面這串：",
        "",
        f"      {topic}",
        "",
        "5. 按下 Subscribe（訂閱）",
        "6. 如果手機問要不要允許通知，選「允許」",
    )
    print("※ 這串字就是你的私人頻道，不要給別人。")
    print("※ 建議用手機拍下來或抄下來，才不會打錯。\n")
    input("手機弄好了以後，回到這裡按一下 Enter ⏎")

    # ---------- 4. 測試 ----------
    print("\n正在發送測試通知……")
    try:
        from ptt_watcher import push  # noqa: E402
    except ImportError as e:
        box(
            "沒辦法發送測試通知 ✗",
            "",
            f"原因：{e}",
            "",
            "請關掉這個視窗，重新點兩下「1_安裝.bat」，",
            "等它跑完出現「安裝完成」之後，再回來跑這個設定精靈。",
        )
        return

    ok = push(cfg, "PTT 監控測試", "看到這則就代表設定成功了 ✅", click="https://www.ptt.cc")
    if ok:
        box(
            "測試通知已送出，請看手機！",
            "",
            "有跳出通知 → 全部設定完成，關掉這個視窗，",
            "                改點兩下「3_開始監控.bat」就開始跑了。",
            "",
            "沒跳出通知 → 通常是頻道名稱打錯。重新執行這個",
            "                設定精靈，第 3 步選 Y 沿用同一組，",
            "                再仔細對一次手機上打的字。",
        )
    else:
        box("送不出去 ✗", "", "請檢查這台電腦有沒有連上網路，然後再跑一次。")

    if STATE_PATH.exists():
        reset = ask("要清除舊的通知紀錄、重新開始嗎？(Y/N)", "N")
        if reset.upper().startswith("Y"):
            STATE_PATH.unlink()
            print("已清除 ✓")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n已取消。")

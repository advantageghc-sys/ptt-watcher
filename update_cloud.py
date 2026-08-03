#!/usr/bin/env python3
"""一鍵把這個資料夾的最新程式上傳到 GitHub 雲端，並立刻啟動掃描。

點兩下「5_更新雲端.bat」就會執行這支程式。流程：
1. 確認電腦上有 GitHub 官方工具（gh），沒有就自動安裝
2. 第一次使用會請你在瀏覽器登入 GitHub 授權（只要做這麼一次）
3. 自動找到你的 PTT 監控儲存庫，把改過的檔案傳上去
4. 上傳完立刻觸發一輪掃描，不用等排程
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

# 這台電腦的系統編碼可能是 cp950，不是 UTF-8。加上這段才不會當掉。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).parent
LINE = "=" * 58

# 要同步到雲端的檔案（seen.json 是雲端的執行紀錄，絕對不能從本機覆蓋）
FILES = [
    ".github/workflows/watch.yml",
    "ptt_watcher.py",
    "config.json",
    "deps.py",
    "install.py",
    "setup.py",
    "today.py",
    "update_cloud.py",
    "requirements.txt",
    "README.md",
    ".gitignore",
    "python-install-help.txt",
    "1_安裝.bat",
    "2_設定.bat",
    "3_開始監控.bat",
    "4_查看今天.bat",
    "5_更新雲端.bat",
]

# config.json 只同步「調校用」的欄位；帳號、看板、頻道以雲端現有的為準,
# 免得把雲端已經設定好的東西蓋成本機的空白範本
CONFIG_TUNING_KEYS = [
    "interval_seconds",
    "board_delay_seconds",
    "comment_interval_seconds",
    "comment_hot_hours",
    "comment_quiet_hours",
    "comment_window_hours",
    "comment_delay_seconds",
]

WORKFLOW_PATH = ".github/workflows/watch.yml"

# 記住上次找到的儲存庫（只存在這台電腦，不會上傳）
REPO_CACHE = HERE / "cloud_repo.txt"

CONFIG_PATH = HERE / "config.json"

# 跟 ptt_watcher.py 的 PLACEHOLDER_TOPIC 保持同步——
# 設定檔裡的頻道含這串字時，程式會拒絕啟動（避免推播打到空氣沒人發現）
PLACEHOLDER_TOPIC = "請改成你自己的隨機字串"


def box(*lines: str) -> None:
    print()
    print(LINE)
    for ln in lines:
        print("  " + ln)
    print(LINE)
    print()


# --------------------------------------------------------------------------- #
# GitHub 官方工具（gh）
# --------------------------------------------------------------------------- #
def find_gh() -> str | None:
    hit = shutil.which("gh")
    if hit:
        return hit
    # 剛用 winget 裝好時，這個視窗的搜尋路徑還沒更新，直接到常見位置找
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "GitHub CLI" / "gh.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "GitHub CLI" / "gh.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "gh.exe",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def ensure_gh() -> str | None:
    gh = find_gh()
    if gh:
        return gh

    box(
        "需要先安裝 GitHub 官方工具（免費、只裝這一次）",
        "",
        "正在自動安裝，請稍等 1～2 分鐘……",
        "如果畫面跳出「是否允許變更」的視窗，請按「是」。",
    )
    try:
        subprocess.call(
            [
                "winget", "install", "--id", "GitHub.cli", "-e",
                "--accept-source-agreements", "--accept-package-agreements",
            ]
        )
    except FileNotFoundError:
        pass

    gh = find_gh()
    if gh:
        print("\nGitHub 工具安裝完成 ✓\n")
        return gh

    box(
        "自動安裝沒有成功 ✗",
        "",
        "請手動安裝（很簡單）：",
        "1. 等一下會自動打開網頁 cli.github.com",
        "2. 點紫色的「Download for Windows」按鈕",
        "3. 下載後點兩下安裝，一直按「下一步」到完成",
        "4. 裝好後，回來再點兩下一次「5_更新雲端.bat」",
    )
    webbrowser.open("https://cli.github.com")
    return None


def ensure_login(gh: str) -> bool:
    if subprocess.call([gh, "auth", "status", "-h", "github.com"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0:
        return True

    box(
        "第一次使用，需要登入 GitHub 授權（只要做這麼一次）",
        "",
        "等一下畫面會出現一組代碼（像 ABCD-1234），",
        "然後瀏覽器會自動打開 GitHub：",
        "",
        "1. 先在下面的英文提示按一下 Enter",
        "2. 在瀏覽器登入你的 GitHub 帳號（如果還沒登入）",
        "3. 把代碼打進網頁的格子裡",
        "4. 按綠色的「Authorize / 授權」按鈕",
        "5. 回到這個黑色視窗，等它自己繼續",
    )
    rc = subprocess.call(
        [gh, "auth", "login", "--hostname", "github.com", "--git-protocol", "https", "--web"]
    )
    if rc != 0:
        print("\n登入沒有完成。請再點兩下一次「5_更新雲端.bat」重試。")
        return False
    print("\n登入成功 ✓\n")
    return True


def gh_token(gh: str) -> str:
    out = subprocess.run([gh, "auth", "token"], capture_output=True, text=True)
    return out.stdout.strip()


# --------------------------------------------------------------------------- #
# GitHub API
# --------------------------------------------------------------------------- #
def api(token: str):
    import requests

    s = requests.Session()
    s.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    )
    return s


def find_repo(s) -> dict | None:
    """在你的 GitHub 帳號裡找出 PTT 監控的儲存庫（認 ptt_watcher.py）。

    依「最近有推送」排序來找——監控儲存庫每十幾分鐘就會自動存一次紀錄，
    幾乎一定排在最前面。只認 ptt_watcher.py 不認 watch.yml，
    排程檔就算被改名也照樣找得到。
    """
    checked = 0
    for page in (1, 2, 3):
        r = s.get("https://api.github.com/user/repos",
                  params={"per_page": 100, "sort": "pushed", "page": page}, timeout=30)
        r.raise_for_status()
        repos = r.json()
        if not repos:
            break
        for repo in repos:
            if not repo.get("permissions", {}).get("push"):
                continue
            if checked >= 50:  # 只檢查最近有動靜的前 50 個，避免灌爆 API
                return None
            checked += 1
            b = s.get(
                f"https://api.github.com/repos/{repo['full_name']}/contents/ptt_watcher.py",
                timeout=30,
            )
            if b.status_code == 200:
                return repo
    return None


def whoami(s) -> str:
    """回傳目前登入的 GitHub 帳號名稱，拿不到就回空字串。"""
    try:
        r = s.get("https://api.github.com/user", timeout=30)
        if r.status_code == 200:
            return r.json().get("login", "")
    except Exception:
        pass
    return ""


def repo_by_name(s, full: str) -> dict | None:
    """用「帳號/名稱」直接開儲存庫；開不了或沒有上傳權限就回 None。"""
    try:
        r = s.get(f"https://api.github.com/repos/{full}", timeout=30)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    d = r.json()
    return d if d.get("permissions", {}).get("push") else None


def parse_repo_ref(text: str) -> str | None:
    """把使用者貼的儲存庫網址整理成「帳號/名稱」。"""
    t = text.strip().rstrip("/")
    for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
        if t.lower().startswith(prefix):
            t = t[len(prefix):]
            break
    if t.endswith(".git"):
        t = t[:-4]
    parts = [p for p in t.split("/") if p]
    return "/".join(parts[:2]) if len(parts) >= 2 else None


def get_remote(s, full: str, path: str) -> tuple[str | None, bytes | None]:
    """回傳雲端上某個檔案的 (版本代號, 內容)；檔案不存在回傳 (None, None)。"""
    r = s.get(f"https://api.github.com/repos/{full}/contents/{path}", timeout=30)
    if r.status_code == 404:
        return None, None
    r.raise_for_status()
    d = r.json()
    content = None
    if d.get("encoding") == "base64" and d.get("content"):
        content = base64.b64decode(d["content"])
    return d.get("sha"), content


def put_file(s, full: str, path: str, data: bytes, sha: str | None):
    payload = {
        "message": f"update: {path}",
        "content": base64.b64encode(data).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha
    r = s.put(f"https://api.github.com/repos/{full}/contents/{path}",
              json=payload, timeout=30)
    r.raise_for_status()


def merged_config(local: bytes, remote: bytes | None) -> bytes:
    """帳號、看板、頻道用雲端的；速度調校欄位用本機最新的。"""
    try:
        local_cfg = json.loads(local.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return remote if remote is not None else local
    if remote is None:
        # 第一次上傳到新儲存庫（公開的）：頻道名稱等於密碼，不能寫進檔案。
        # 放佔位字，讓程式在 Secrets 沒設好時會大聲報錯而不是默默打到空氣；
        # 真正的頻道名稱由建立流程寫進 GitHub Secrets（NTFY_TOPIC）。
        nt = local_cfg.setdefault("ntfy", {})
        nt["topic"] = PLACEHOLDER_TOPIC + "（雲端實際用的是 Secrets，這裡不用改）"
        nt["token"] = ""
        return (json.dumps(local_cfg, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    try:
        remote_cfg = json.loads(remote.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return remote  # 看不懂就不要動雲端的
    for k in CONFIG_TUNING_KEYS:
        if k in local_cfg:
            remote_cfg[k] = local_cfg[k]
    return (json.dumps(remote_cfg, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def create_repo_flow(s, gh: str) -> dict | None:
    """從零建立雲端監控：開新儲存庫 + 把手機頻道寫進 Secrets。"""
    if not CONFIG_PATH.exists():
        box(
            "要先做一次基本設定才能建立雲端監控",
            "",
            "請關掉這個視窗，點兩下「2_設定.bat」（約 1 分鐘，",
            "會問你要追誰、手機頻道用哪個），",
            "做完再回來點一次「5_更新雲端.bat」。",
        )
        return None
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        box("config.json 壞掉了，請先重跑「2_設定.bat」再回來。")
        return None
    topic = (cfg.get("ntfy") or {}).get("topic", "")
    if not topic or PLACEHOLDER_TOPIC in topic:
        box("設定檔裡還沒有手機頻道，請先重跑「2_設定.bat」再回來。")
        return None

    print("正在建立雲端儲存庫……")
    r = s.post(
        "https://api.github.com/user/repos",
        json={
            "name": "ptt-watcher",
            "description": "PTT 發文/留言監控（由 5_更新雲端.bat 自動建立）",
            "private": False,  # 公開才有無限的免費執行時間；祕密都放 Secrets，不會外洩
            "auto_init": True,
        },
        timeout=30,
    )
    if r.status_code not in (200, 201):
        print(f"建立儲存庫失敗（{r.status_code}）：{r.text[:200]}")
        return None
    repo = r.json()
    full = repo["full_name"]
    print(f"儲存庫建立完成：{full} ✓")

    # 頻道名稱（＝密碼）放 GitHub Secrets，gh 會自動加密
    rc = subprocess.call([gh, "secret", "set", "NTFY_TOPIC", "--repo", full, "--body", topic])
    if rc != 0:
        print("[警告] 頻道 Secrets 沒設成功，等一下雲端會啟動失敗；請再跑一次這個程式。")
    tok = (cfg.get("ntfy") or {}).get("token", "")
    if tok:
        subprocess.call([gh, "secret", "set", "NTFY_TOKEN", "--repo", full, "--body", tok])

    time.sleep(2)  # 新儲存庫要一兩秒才能開始寫入檔案
    return repo


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def manual_help() -> None:
    box(
        "自動上傳沒有成功，改用手動方式也很快：",
        "",
        "1. 用瀏覽器打開 github.com 並登入",
        "2. 右上角頭像 → Your repositories → 點進 PTT 監控的儲存庫",
        "3. Add file → Upload files",
        "4. 把這個資料夾裡的 ptt_watcher.py 和 .github 資料夾拖進網頁",
        "5. 按綠色的 Commit changes",
    )


def main() -> int:
    box("PTT 監控 — 一鍵更新雲端", "", "會自動把改好的程式傳到 GitHub，讓通知變快。")

    # 元件檢查（requests）
    sys.path.insert(0, str(HERE))
    from deps import ensure

    if not ensure():
        return 1

    gh = ensure_gh()
    if not gh:
        return 1
    if not ensure_login(gh):
        return 1

    token = gh_token(gh)
    if not token:
        print("拿不到授權，請再點兩下一次「5_更新雲端.bat」重試。")
        return 1

    import requests

    s = api(token)

    login = whoami(s)
    if login:
        print(f"已登入 GitHub 帳號：{login}")

    # 1) 上次記住的儲存庫 → 2) 自動搜尋 → 3) 請使用者貼網址 → 4) 換帳號重登
    repo = None
    if REPO_CACHE.exists():
        cached = REPO_CACHE.read_text(encoding="utf-8-sig").strip()
        if cached:
            repo = repo_by_name(s, cached)

    if not repo:
        print("正在找你的 PTT 監控儲存庫……")
        try:
            repo = find_repo(s)
        except requests.RequestException as e:
            print(f"連不上 GitHub：{e}")
            manual_help()
            return 1

    if not repo:
        box(
            f"用帳號「{login or '不明'}」找不到 PTT 監控的儲存庫",
            "",
            "最常見的原因：這台電腦登入的 GitHub 帳號，",
            "跟當初放監控程式的帳號不是同一個。",
        )
        url = input("如果你知道儲存庫的網址，貼上後按 Enter（不知道就直接按 Enter）\n> ").strip()
        if url:
            ref = parse_repo_ref(url)
            repo = repo_by_name(s, ref) if ref else None
            if not repo:
                print(f"\n用帳號「{login}」開不了這個儲存庫（網址有誤，或這個帳號沒有權限）。\n")
        if not repo:
            ans = input("要改用別的 GitHub 帳號重新登入找找看嗎？(Y=重新登入 / N=不用，繼續)\n> ").strip() or "N"
            if ans.upper().startswith("Y"):
                subprocess.call([gh, "auth", "logout", "--hostname", "github.com"])
                if not ensure_login(gh):
                    return 1
                token = gh_token(gh)
                s = api(token)
                login = whoami(s)
                if login:
                    print(f"已登入 GitHub 帳號：{login}")
                print("再找一次你的 PTT 監控儲存庫……")
                try:
                    repo = find_repo(s)
                except requests.RequestException:
                    repo = None
        if not repo:
            print()
            ans = input(
                "看起來這個帳號裡「還沒有」PTT 監控的雲端。\n"
                "要現在幫你從零建立一個嗎？建好之後電腦關機也會照常通知。\n"
                "(Y/N，直接按 Enter = Y)\n> "
            ).strip() or "Y"
            if ans.upper().startswith("Y"):
                repo = create_repo_flow(s, gh)
        if not repo:
            manual_help()
            return 1

    full = repo["full_name"]
    branch = repo.get("default_branch", "main")
    print(f"找到了：{full} ✓\n")
    try:
        REPO_CACHE.write_text(full + "\n", encoding="utf-8")
    except OSError:
        pass

    if repo.get("private"):
        box(
            "提醒：你的儲存庫是「私人」的",
            "",
            "改成連續掃描後，私人儲存庫的免費額度大約幾天就會用完，",
            "額度用完雲端就會停止、通知會斷掉。",
            "建議到儲存庫的 Settings 最下面把它改成 Public（公開）。",
            "（程式裡沒有你的密碼，公開沒有安全問題）",
        )

    uploaded = skipped = 0
    failed: list[str] = []
    for rel in FILES:
        local_path = HERE / rel
        if not local_path.is_file():
            continue
        data = local_path.read_bytes()
        try:
            sha, remote = get_remote(s, full, rel)
            if rel == "config.json":
                data = merged_config(data, remote)
            if remote is not None and remote == data:
                skipped += 1
                continue
            put_file(s, full, rel, data, sha)
            uploaded += 1
            print(f"  ⬆ 已更新 {rel}")
        except requests.RequestException:
            # 排程檔需要多一種授權，自動補授權後再試一次
            if rel == WORKFLOW_PATH:
                print("\n更新排程檔需要多一項授權，瀏覽器會再打開一次，請照剛才的方式按授權。\n")
                subprocess.call([gh, "auth", "refresh", "-h", "github.com", "-s", "workflow"])
                s = api(gh_token(gh))
                try:
                    sha, remote = get_remote(s, full, rel)
                    if remote != data:
                        put_file(s, full, rel, data, sha)
                        uploaded += 1
                        print(f"  ⬆ 已更新 {rel}")
                    else:
                        skipped += 1
                    continue
                except requests.RequestException:
                    pass
            failed.append(rel)
            print(f"  ✗ 上傳失敗 {rel}")

    if failed:
        print(f"\n有 {len(failed)} 個檔案沒傳成功：{'、'.join(failed)}")
        manual_help()
        return 1

    # 停掉還在跑的「舊程式」任務——不停的話它最長會再跑 10 幾分鐘，
    # 新程式要排隊等它結束，那段時間的通知會慢半拍
    stopped = 0
    if uploaded:
        try:
            for status in ("queued", "in_progress"):
                r = s.get(
                    f"https://api.github.com/repos/{full}/actions/workflows/watch.yml/runs",
                    params={"status": status, "per_page": 10}, timeout=30,
                )
                if r.status_code != 200:
                    continue
                for run in r.json().get("workflow_runs", []):
                    c = s.post(
                        f"https://api.github.com/repos/{full}/actions/runs/{run['id']}/cancel",
                        timeout=30,
                    )
                    if c.status_code in (202, 409):
                        stopped += 1
            if stopped:
                print("已請雲端停掉舊程式，等它把紀錄存好……")
                time.sleep(20)  # 給舊任務一點時間把「已通知紀錄」存回去，避免重複通知
        except requests.RequestException:
            pass

    # 立刻觸發一輪掃描，不用等排程慢慢醒來
    started = False
    try:
        r = s.post(
            f"https://api.github.com/repos/{full}/actions/workflows/watch.yml/dispatches",
            json={"ref": branch}, timeout=30,
        )
        started = r.status_code == 204
    except requests.RequestException:
        pass

    if uploaded:
        box(
            "全部完成 ✓",
            "",
            f"更新了 {uploaded} 個檔案" + (f"（{skipped} 個沒變、不用傳）" if skipped else ""),
            "雲端掃描" + ("已經立刻重新啟動。" if started else "會在 5 分鐘內自動啟動。"),
            "",
            "接下來 3 分鐘內，手機會收到一則「PTT 監控已更新」通知，",
            "收到它 = 新版程式已經在雲端跑起來了。",
            "（如果超過 10 分鐘都沒收到，代表更新沒成功，再跑一次這個程式）",
            "",
            "這個視窗可以關掉了。電腦也可以關機，雲端會自己一直跑。",
        )
    else:
        box(
            "雲端已經是最新版 ✓",
            "",
            "所有檔案都沒有變動，不需要更新。",
            "這個視窗可以關掉了。",
        )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n已取消。")
        sys.exit(1)
    except Exception as e:  # 任何意外都給看得懂的訊息，不要噴一堆紅字
        print(f"\n發生預期外的錯誤：{e}")
        manual_help()
        sys.exit(1)

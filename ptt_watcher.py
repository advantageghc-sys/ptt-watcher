#!/usr/bin/env python3
"""PTT 作者發文監控 —— 指定帳號一發新文章，就推播到手機。

用法:
    python ptt_watcher.py            # 常駐模式，依 config.json 的 interval_seconds 輪詢
    python ptt_watcher.py --once     # 只跑一輪就結束（給 Windows 工作排程 / GitHub Actions 用）
    python ptt_watcher.py --test     # 發一則測試通知，確認手機收得到
    python ptt_watcher.py --reset    # 清掉已通知紀錄，下次重新建立基準
"""

from __future__ import annotations  # 讓 str | None 這類標註在舊版 Python 也能跑

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    # 元件還沒裝好——自動補裝一次再繼續，不要讓使用者看到一堆紅字
    sys.path.insert(0, str(Path(__file__).parent))
    from deps import ensure

    if not ensure():
        sys.exit(1)
    import requests
    from bs4 import BeautifulSoup
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

# 這台電腦的系統編碼可能是 cp950，不是 UTF-8。
# 加上這段，中文和符號在任何編碼下都不會讓程式當掉。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 程式版本。每次改程式就往上加；部署到雲端後的第一輪會推一則
# 「PTT 監控已更新」通知到手機，讓使用者不用看網頁就能確認新版已生效。
VERSION = "2.3"

BASE = "https://www.ptt.cc"
HERE = Path(__file__).parent
CONFIG_PATH = HERE / "config.json"
STATE_PATH = HERE / "seen.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

# 文章 ID 形如 M.1737000000.A.1B2，中間那段是 unix timestamp
ARTICLE_ID_RE = re.compile(r"^M\.(\d+)\.A\.")

# 索引頁的頁碼，用來往回翻頁
INDEX_NUM_RE = re.compile(r"index(\d+)\.html")

# 推文的 ip/時間欄位，形如 "39.10.0.209 08/02 18:33"（舊文章可能沒有 IP）
PUSH_TIME_RE = re.compile(r"(\d{2}/\d{2}\s+\d{2}:\d{2})\s*$")

PLACEHOLDER_TOPIC = "請改成你自己的隨機字串"


# --------------------------------------------------------------------------- #
# 設定與狀態
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit("還沒設定過。請先點兩下「2_設定.bat」跑一次設定精靈。")
    # utf-8-sig：記事本存檔時可能多加 BOM 標記，這樣讀兩種都吃
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))

    # 舊版設定檔的「預設值」自動升級成新的快速預設，
    # 這樣程式更新後不用動設定檔就能變快；使用者自己改過的值不動。
    if cfg.get("interval_seconds") == 180:
        cfg["interval_seconds"] = 60
    if cfg.get("comment_delay_seconds") == 0.5:
        cfg["comment_delay_seconds"] = 0.3

    # 環境變數優先（給 GitHub Actions / 不想把祕密寫進檔案的情況用）
    if os.getenv("PTT_AUTHORS"):
        cfg["authors"] = [s.strip() for s in os.environ["PTT_AUTHORS"].split(",") if s.strip()]
    if os.getenv("PTT_BOARDS"):
        cfg["boards"] = [s.strip() for s in os.environ["PTT_BOARDS"].split(",") if s.strip()]
    if os.getenv("NTFY_TOPIC"):
        cfg["ntfy"]["topic"] = os.environ["NTFY_TOPIC"]
    if os.getenv("NTFY_TOKEN"):
        cfg["ntfy"]["token"] = os.environ["NTFY_TOKEN"]
    if os.getenv("NTFY_SERVER"):
        cfg["ntfy"]["server"] = os.environ["NTFY_SERVER"]

    if not cfg.get("authors"):
        sys.exit("config.json 的 authors 是空的，請填要監控的 PTT 帳號")
    if any(a in ("帳號一", "帳號二") for a in cfg["authors"]):
        # 設定檔還是空白範本（例如更新程式時不小心蓋掉了設定）
        sys.exit("設定檔是空白範本，還沒填入真正要監控的帳號。\n請點兩下「2_設定.bat」重新設定一次（約一分鐘）。")
    if not cfg.get("boards"):
        sys.exit("config.json 的 boards 是空的，請填要監控的看板")
    if not cfg["ntfy"].get("topic") or PLACEHOLDER_TOPIC in cfg["ntfy"]["topic"]:
        sys.exit("請先把 config.json 的 ntfy.topic 改成一組別人猜不到的隨機字串（見 README）")
    return cfg


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("[警告] seen.json 壞掉了，重新建立")
    return {"seen": {}, "baselined": []}


def save_state(state: dict) -> None:
    # 每個帳號只留最近幾千筆，避免檔案無限長大。
    # 上限要遠大於「時間範圍內可能出現的則數」，不然舊紀錄被砍掉會重複通知。
    for bucket, cap in (("seen", 2000), ("comments", 5000)):
        for author, ids in state.get(bucket, {}).items():
            if len(ids) > cap:
                state[bucket][author] = ids[-cap:]
    state["updated"] = datetime.now().isoformat(timespec="seconds")
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 抓 PTT
# --------------------------------------------------------------------------- #
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = UA
    # 沒有這個 cookie，Gossiping 等 18 禁看板會被擋在成年確認頁
    s.cookies.set("over18", "1", domain=".ptt.cc")
    # PTT 偶爾會直接斷線或回 5xx，自動退避重試三次
    retry = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def article_time(pid: str) -> int:
    m = ARTICLE_ID_RE.match(pid)
    return int(m.group(1)) if m else 0


def cache_bust() -> dict:
    # PTT 網頁版的回應標頭是 max-age=900（允許快取 15 分鐘）。
    # 帶一個每次不同的參數，確保沿路任何快取層都不會回舊頁面；
    # 參數本身會被 PTT 忽略，不影響內容。
    return {"_": random.randint(1, 10**9)}


def parse_push_time(when: str) -> datetime | None:
    """把推文的「08/03 19:33」轉成完整時間；解析不了就回 None。"""
    try:
        dt = datetime.strptime(when, "%m/%d %H:%M").replace(year=datetime.now().year)
    except ValueError:
        return None
    if dt - datetime.now() > timedelta(days=1):  # 跨年邊界：去年 12 月的推文
        dt = dt.replace(year=dt.year - 1)
    return dt


def fetch_posts(session: requests.Session, board: str, author: str) -> list[dict]:
    """用 PTT 網頁版的看板內搜尋，抓某個作者在該看板的文章列表。"""
    url = f"{BASE}/bbs/{board}/search"
    r = session.get(url, params={"q": f"author:{author}"}, timeout=20)
    if r.status_code == 404:
        print(f"[警告] 看板 {board} 不存在或無法瀏覽，跳過")
        return []
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    posts = []
    for ent in soup.select("div.r-ent"):
        link = ent.select_one("div.title a")
        if not link:  # 已被刪除的文章沒有連結
            continue

        author_div = ent.select_one("div.author")
        post_author = author_div.get_text(strip=True) if author_div else ""
        # PTT 的 author: 搜尋是模糊比對，這裡再做一次精確過濾
        if post_author.lower() != author.lower():
            continue

        href = link["href"]
        pid = href.rsplit("/", 1)[-1]
        if pid.endswith(".html"):
            pid = pid[: -len(".html")]

        date_div = ent.select_one("div.date")
        posts.append(
            {
                "key": f"{board}/{pid}",
                "board": board,
                "author": post_author,
                "title": link.get_text(strip=True),
                "url": BASE + href,
                "date": date_div.get_text(strip=True) if date_div else "",
                "ts": article_time(pid),
            }
        )
    return posts


# --------------------------------------------------------------------------- #
# 抓留言（推文）
#
# PTT 沒有「留言搜尋」，只能把文章一篇篇打開、掃裡面的推文列表。
# 所以做法是：抓看板索引 → 取最近 N 小時的文章 → 逐篇讀推文 → 比對帳號。
# --------------------------------------------------------------------------- #
def parse_index(html: str, board: str) -> list[dict]:
    """從一頁看板索引解析出文章清單。"""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for ent in soup.select("div.r-ent"):
        link = ent.select_one("div.title a")
        if not link:  # 已刪除
            continue
        href = link["href"]
        pid = href.rsplit("/", 1)[-1]
        if pid.endswith(".html"):
            pid = pid[: -len(".html")]
        nrec_el = ent.select_one("div.nrec")
        author_el = ent.select_one("div.author")
        out.append(
            {
                "pid": pid,
                "board": board,
                "title": link.get_text(strip=True),
                "url": BASE + href,
                "ts": article_time(pid),
                # 發文偵測直接從索引認作者——「作者搜尋」的索引常慢 30 分鐘以上
                "author": author_el.get_text(strip=True) if author_el else "",
                # 索引頁顯示的推文數（推減噓的淨值；滿百顯示「爆」），
                # 快速掃描拿它當「這篇有沒有新動靜」的判斷
                "nrec": nrec_el.get_text(strip=True) if nrec_el else "",
            }
        )
    return out


def recent_articles(
    session: requests.Session, board: str, hours: float, max_pages: int = 6
) -> list[dict]:
    """取得看板上最近 N 小時內的文章（會自動往回翻頁）。"""
    cutoff = time.time() - hours * 3600

    r = session.get(f"{BASE}/bbs/{board}/index.html", params=cache_bust(), timeout=20)
    r.raise_for_status()
    nums = [int(n) for n in INDEX_NUM_RE.findall(r.text)]
    # index.html 上的「上頁」指向 index{目前頁碼-1}，所以最大值 +1 就是目前頁碼
    current = max(nums) + 1 if nums else 0

    collected: list[dict] = []
    html = r.text
    for i in range(max_pages):
        page = parse_index(html, board)
        collected.extend(page)

        # 這頁完全沒有落在時間範圍內的文章，就不用再往回翻了。
        # （不能用「最舊的一篇」判斷——置底文永遠很舊，會害我們第一頁就停手）
        if not any(a["ts"] >= cutoff for a in page):
            break

        prev_page = current - i - 1
        if prev_page < 1:
            break
        time.sleep(0.5)
        rr = session.get(f"{BASE}/bbs/{board}/index{prev_page}.html", params=cache_bust(), timeout=20)
        if rr.status_code != 200:
            break
        html = rr.text

    # 去重（翻頁時偶爾會重複），再濾出時間範圍內的
    seen_pid = set()
    result = []
    for a in collected:
        if a["ts"] >= cutoff and a["pid"] not in seen_pid:
            seen_pid.add(a["pid"])
            result.append(a)
    result.sort(key=lambda a: a["ts"])
    return result


def fetch_comments(session: requests.Session, article: dict) -> list[dict]:
    """讀一篇文章，解析出所有推文。"""
    r = session.get(article["url"], params=cache_bust(), timeout=20)
    if r.status_code == 404:  # 文章剛被刪掉
        return []
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for node in soup.select("div.push"):
        uid_el = node.select_one("span.push-userid")
        if not uid_el:
            continue  # 系統訊息（例如「檔案過大！部分文章無法顯示」），不是真的推文
        user = uid_el.get_text(strip=True)

        tag_el = node.select_one("span.push-tag")
        con_el = node.select_one("span.push-content")
        dt_el = node.select_one("span.push-ipdatetime")

        content = con_el.get_text(strip=True) if con_el else ""
        if content.startswith(":"):  # 推文內容前面固定有個冒號
            content = content[1:].strip()

        raw_dt = dt_el.get_text(strip=True) if dt_el else ""
        m = PUSH_TIME_RE.search(raw_dt)

        # 去重用的指紋：同一個人、同樣內容、同一時間，就當作同一則。
        # 不用「第幾則」當編號——中間有推文被刪掉時編號會整個位移，會害你被重複通知洗版。
        digest = hashlib.md5(f"{user}|{content}|{raw_dt}".encode("utf-8")).hexdigest()[:12]

        out.append(
            {
                "user": user,
                "tag": tag_el.get_text(strip=True) if tag_el else "",
                "content": content,
                "when": m.group(1) if m else raw_dt,
                "digest": digest,
            }
        )
    return out


def passes_filters(cfg: dict, post: dict) -> bool:
    title = post["title"]
    if cfg.get("skip_reply") and title.startswith("Re:"):
        return False
    include = cfg.get("title_include") or []
    if include and not any(k in title for k in include):
        return False
    exclude = cfg.get("title_exclude") or []
    if exclude and any(k in title for k in exclude):
        return False
    return True


# --------------------------------------------------------------------------- #
# 推播
# --------------------------------------------------------------------------- #
def push(cfg: dict, title: str, message: str, click: str | None = None) -> bool:
    """用 ntfy 的 JSON 發布端點送出通知（JSON 才能安全帶中文）。"""
    nt = cfg["ntfy"]
    payload = {
        "topic": nt["topic"],
        "title": title,
        "message": message,
        "priority": nt.get("priority", 4),
        "tags": nt.get("tags", ["loudspeaker"]),
    }
    if click:
        payload["click"] = click

    headers = {}
    if nt.get("token"):
        headers["Authorization"] = f"Bearer {nt['token']}"

    try:
        r = requests.post(nt["server"].rstrip("/"), json=payload, headers=headers, timeout=20)
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"[錯誤] 推播失敗: {e}")
        return False


def notify_post(cfg: dict, post: dict) -> bool:
    when = (
        datetime.fromtimestamp(post["ts"]).strftime("%m/%d %H:%M")
        if post["ts"]
        else post["date"]
    )
    return push(
        cfg,
        title=f"{post['author']} 在 {post['board']} 發文了",
        message=f"{post['title']}\n\n🕒 {when}",
        click=post["url"],
    )


def notify_comment(cfg: dict, article: dict, c: dict, diag: str | None = None) -> bool:
    tag = c["tag"] or "→"
    msg = f"{tag} {c['content']}\n\n📄 {article['title']}\n🕒 {c['when']}"
    if diag:
        msg += f"\n{diag}"
    return push(
        cfg,
        title=f"{c['user']} 留言了",
        message=msg,
        click=article["url"],
    )


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def scan_comments(
    cfg: dict,
    state: dict,
    session: requests.Session,
    stamp: str,
    full: bool,
    recent: dict[str, list[dict]],
) -> None:
    """掃最近的文章，找出目標帳號的留言。

    full=True  完整掃描：時間範圍內每一篇都重新讀（慢，當保險網用）
    full=False 快速掃描：重讀「看得出有動靜」和「動靜可能藏得住」的文章，
               其他沒動靜的跳過，一輪只要幾秒到幾十秒，可以掃得很頻繁。
    """
    seen_all = state.setdefault("comments", {})
    baselined = state.setdefault("baselined_comments", [])
    nrec_all = state.setdefault("nrec", {})
    # 帳號比對不分大小寫，但通知時要顯示 PTT 上的原始寫法
    targets = {a.lower(): a for a in cfg["authors"]}
    delay = cfg.get("comment_delay_seconds", 0.5)
    hot_secs = cfg.get("comment_hot_hours", 1) * 3600
    quiet_secs = cfg.get("comment_quiet_hours", 3) * 3600

    seen_sets = {a: set(seen_all.setdefault(a, [])) for a in cfg["authors"]}
    # 目標帳號留過言的文章（尤其 LIVE 文）之後很可能繼續留言，每輪必看
    active_pids = {e.split("#", 1)[0] for ids in seen_all.values() for e in ids}

    # 偵錯用：上一輪是什麼時候掃的、哪些留言推播失敗還在重試
    prev_scan = state.get("last_scan_at", "")
    pending = state.setdefault("pending_comments", {})

    for board in cfg["boards"]:
        articles = recent.get(board)
        if articles is None:  # 這一輪索引沒抓成功，下一輪再補
            continue

        prev_nrec = nrec_all.get(board, {})
        now = time.time()
        if full:
            to_scan = articles
        else:
            # 板面推文數是「推減噓」的淨值，很多留言根本不會讓它動：
            #   噓到 0～-9 分時顯示空白、-10 起每 10 則噓才從 X1 變一次 X2、
            #   -100 以下固定 XX、滿百固定「爆」，純箭頭（→）則完全不計。
            # SportLottery 的 LIVE 文整串都在噓，等於快速掃描看不到任何動靜，
            # 所以這些「數字不可信」的文章不能等變動，一律直接重讀。
            to_scan = [
                a
                for a in articles
                if prev_nrec.get(a["pid"]) != a["nrec"]  # 數字有變
                or a["nrec"] == "爆"                      # 顯示固定不再動
                or a["nrec"].startswith("X")              # 負分區，10 則才動一次
                or now - a["ts"] < hot_secs               # 剛發的新文
                or (not a["nrec"] and now - a["ts"] < quiet_secs)  # 空白可能是 0～-9 的隱形區
                or a["pid"] in active_pids                # 目標帳號留過言的文
            ]

        mode = "完整掃描" if full else "快速掃描"
        print(f"[{stamp}] {board} {mode}：{len(articles)} 篇裡重看 {len(to_scan)} 篇……")

        new_marks = {f"{board}#{a}" for a in cfg["authors"]} - set(baselined)
        base_counts = {a: 0 for a in cfg["authors"]}
        retry_pids: set[str] = set()

        for art in to_scan:
            try:
                comments = fetch_comments(session, art)
            except requests.RequestException as e:
                retry_pids.add(art["pid"])
                print(f"[錯誤] 讀取文章失敗（下一輪會重試）{art['url']}: {e}")
                continue
            for c in comments:
                real = targets.get(c["user"].lower())
                if not real:
                    continue
                key = f"{art['pid']}#{c['digest']}"
                if key in seen_sets[real]:
                    continue
                if f"{board}#{real}" in new_marks:
                    # 第一次掃這個板#帳號：默默記起來當基準，不然會被歷史留言洗版
                    seen_all[real].append(key)
                    seen_sets[real].add(key)
                    base_counts[real] += 1
                    continue

                # 這則留言如果明顯晚到（偵測時已超過 3 分鐘），通知裡附上偵錯資訊，
                # 一眼看出是「掃描沒在跑」「PTT 給舊頁面」還是「推播被擋重試」害的
                pend = pending.get(key) or {}
                first_seen = pend.get("found") or datetime.now().strftime("%H:%M:%S")
                tries = pend.get("tries", 0)
                diag = None
                pdt = parse_push_time(c["when"])
                if pdt and (datetime.now() - pdt) > timedelta(minutes=3):
                    diag = f"🔎 {first_seen} 首次掃到｜上輪掃描 {prev_scan or '無紀錄'}"
                    if tries:
                        diag += f"｜推播重試 {tries} 次"

                if notify_comment(cfg, art, c, diag):
                    # 找到就立刻通知，不等整輪掃完——完整掃描一輪可能要好幾分鐘
                    seen_all[real].append(key)
                    seen_sets[real].add(key)
                    active_pids.add(art["pid"])
                    pending.pop(key, None)
                    print(f"[{stamp}] 💬 {real}「{c['content'][:30]}」← {art['title'][:24]}")
                else:
                    # 推播失敗就不記錄，並強迫下一輪重讀這篇來補送
                    pending[key] = {"found": first_seen, "tries": tries + 1}
                    retry_pids.add(art["pid"])
            time.sleep(delay)

        # 更新推文數快取。讀取或推播失敗的那幾篇不記，下一輪一定會再被挑出來重試。
        nrec_all[board] = {
            a["pid"]: a["nrec"] for a in articles if a["pid"] not in retry_pids
        }

        if retry_pids:
            print(f"[{stamp}] 有 {len(retry_pids)} 篇沒處理完，下一輪會自動補")

        for author in cfg["authors"]:
            mark = f"{board}#{author}"
            if mark in new_marks:
                baselined.append(mark)
                print(f"[{stamp}] {author}: 留言首次執行，記錄 {base_counts[author]} 則當基準，不發通知")

        # 每掃完一個板就先存檔，中途被中斷也不會把已通知的再通知一次
        save_state(state)

    # 給下一輪的偵錯資訊用：這一輪是幾點掃的
    state["last_scan_at"] = datetime.now().strftime("%H:%M:%S")
    # 推播重試暫存不讓它無限長大（正常情況下裡面應該是空的）
    if len(pending) > 500:
        for k in list(pending)[:-500]:
            pending.pop(k, None)


def run_once(cfg: dict, state: dict) -> None:
    session = make_session()
    stamp = datetime.now().strftime("%H:%M:%S")

    if state.get("version") != VERSION:
        # 新版程式的第一輪：發通知回報，收到就代表更新真的生效了
        if push(cfg, "PTT 監控已更新", f"新版程式（v{VERSION}）開始執行 ✅"):
            state["version"] = VERSION
            print(f"[{stamp}] 已推播版本更新通知（v{VERSION}）")

    baselined = state.setdefault("baselined", [])

    # 每個板的近期文章列表一輪只抓一次，發文偵測和留言掃描共用。
    # 發文偵測主要靠板面索引（即時）；「作者搜尋」的索引常慢 30 分鐘以上，
    # 只拿來當備援（例如文章太舊已經超出索引掃描範圍時）。
    hours = cfg.get("comment_window_hours", 24)
    recent: dict[str, list[dict]] = {}
    for board in cfg["boards"]:
        try:
            recent[board] = recent_articles(session, board, hours)
        except requests.RequestException as e:
            print(f"[錯誤] 取得 {board} 文章列表失敗: {e}")

    for author in cfg["authors"]:
        seen = state["seen"].setdefault(author, [])
        # 用獨立的 baselined 清單判斷，不能用 seen 是否為空——
        # 帳號在該板還沒有任何文章時 seen 本來就是空的，那時第一篇新文必須通知
        first_run = author not in baselined
        seen_set = set(seen)
        found = []

        for board in cfg["boards"]:
            for a in recent.get(board, []):
                if a.get("author", "").lower() == author.lower():
                    found.append(
                        {
                            "key": f"{board}/{a['pid']}",
                            "board": board,
                            "author": a["author"],
                            "title": a["title"],
                            "url": a["url"],
                            "date": "",
                            "ts": a["ts"],
                        }
                    )
            try:
                found.extend(fetch_posts(session, board, author))
            except requests.RequestException as e:
                print(f"[錯誤] 抓 {board} 失敗: {e}")
            time.sleep(cfg.get("board_delay_seconds", 1.5))  # 對 PTT 客氣一點

        # 索引和搜尋兩個來源會重複，用文章 key 去重
        uniq: dict[str, dict] = {}
        for p in found:
            uniq.setdefault(p["key"], p)

        new_posts = [p for p in uniq.values() if p["key"] not in seen_set]
        new_posts.sort(key=lambda p: p["ts"])  # 舊的先通知

        if first_run:
            # 第一次跑：只記錄現況當基準，不然會被歷史文章洗版
            seen.extend(p["key"] for p in new_posts)
            baselined.append(author)
            print(f"[{stamp}] {author}: 首次執行，記錄 {len(new_posts)} 篇既有文章當基準，不發通知")
            continue

        sent = 0
        for post in new_posts:
            if not passes_filters(cfg, post):
                seen.append(post["key"])  # 過濾掉的也記起來，免得每輪重算
                continue
            if notify_post(cfg, post):
                seen.append(post["key"])
                sent += 1
                print(f"[{stamp}] 🔔 {post['board']} / {post['title']} → {post['url']}")
            # 推播失敗就不記錄，下一輪會重試

        if sent == 0 and not new_posts:
            print(f"[{stamp}] {author}: 沒有新文章（掃了 {len(cfg['boards'])} 個看板）")

    # 留言掃描每一輪都跑「快速掃描」（只重讀有動靜的文章，幾秒就好），
    # 每隔 comment_interval_seconds 再跑一次「完整掃描」當保險網，
    # 撿回快速掃描可能漏掉的（例如舊文上不影響推文數的純箭頭留言）。
    if cfg.get("watch_comments"):
        gap = cfg.get("comment_interval_seconds", 180)
        full = time.time() - state.get("last_comment_scan", 0) >= gap - 5  # 減 5 秒容許誤差
        # 新加入的帳號或看板還沒建立基準時，必須用完整範圍建基準，
        # 不然之後的完整掃描會把它的歷史留言當成新留言洗版
        marks = state.setdefault("baselined_comments", [])
        if any(f"{b}#{a}" not in marks for b in cfg["boards"] for a in cfg["authors"]):
            full = True
        try:
            scan_comments(cfg, state, session, stamp, full, recent)
            if full:
                state["last_comment_scan"] = time.time()
        except Exception as e:
            print(f"[錯誤] 掃留言時出錯（不影響發文監控）: {e}")

    save_state(state)


def main() -> None:
    ap = argparse.ArgumentParser(description="PTT 作者發文監控")
    ap.add_argument("--once", action="store_true", help="只跑一輪就結束")
    ap.add_argument("--test", action="store_true", help="發一則測試通知")
    ap.add_argument("--reset", action="store_true", help="清掉已通知紀錄")
    args = ap.parse_args()

    cfg = load_config()

    if args.test:
        ok = push(cfg, "PTT 監控測試", "看到這則就代表推播設定成功 ✅", click="https://www.ptt.cc")
        print("測試通知已送出，請看手機" if ok else "送出失敗，請檢查 config.json 的 ntfy 設定")
        return

    if args.reset:
        STATE_PATH.unlink(missing_ok=True)
        print("已清除 seen.json，下次執行會重新建立基準")
        return

    state = load_state()

    if args.once:
        run_once(cfg, state)
        return

    interval = cfg.get("interval_seconds", 300)
    line = "=" * 58
    print(line)
    print("  PTT 發文監控 執行中")
    print("")
    print(f"  監控帳號：{'、'.join(cfg['authors'])}")
    print(f"  監控看板：{'、'.join(cfg['boards'])}")
    print(f"  檢查頻率：每 {interval} 秒一次")
    print("")
    print("  ※ 這個視窗要「一直開著」，關掉就停止監控。")
    print("  ※ 要停止：直接關掉視窗，或按 Ctrl + C。")
    print(line)
    print("")
    while True:
        try:
            run_once(cfg, state)
        except Exception as e:  # 任何意外都不該讓常駐程式掛掉
            print(f"[錯誤] 本輪失敗: {e}")
        # 加一點隨機偏移，避免固定節奏打 PTT
        time.sleep(interval + random.randint(0, 10))


if __name__ == "__main__":
    main()

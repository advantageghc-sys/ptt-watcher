#!/usr/bin/env python3
"""查看指定帳號「今天」的發文與留言，產生一份報表用瀏覽器打開。

用法:
    python today.py            # 今天
    python today.py --days 3   # 最近 3 天
"""

from __future__ import annotations

import argparse
import html
import sys
import time
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from deps import ensure  # noqa: E402

if not ensure():
    sys.exit(1)

import requests  # noqa: E402

from ptt_watcher import (  # noqa: E402
    fetch_comments,
    fetch_posts,
    load_config,
    make_session,
    recent_articles,
)

REPORT_PATH = HERE / "今日報表.html"

# 留言可能留在前幾天的文章底下，所以掃描的文章範圍要比查詢天數更寬。
# 這個板一天約 45 篇文章，多回溯 1 天 = 多掃約 45 篇 = 多等約 1 分鐘。
EXTRA_LOOKBACK_DAYS = 1


def collect(cfg: dict, days: int) -> tuple[dict, dict, list[str]]:
    """回傳 (每個帳號的發文, 每個帳號的留言, 想查的日期字串清單)。"""
    session = make_session()
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    since = today - timedelta(days=days - 1)
    since_ts = since.timestamp()

    # 推文時間只有 "08/03 18:33"（沒有年份），所以用 MM/DD 比對
    wanted_dates = [(since + timedelta(days=i)).strftime("%m/%d") for i in range(days)]

    posts: dict[str, list] = {a: [] for a in cfg["authors"]}
    comments: dict[str, list] = {a: [] for a in cfg["authors"]}
    targets = {a.lower(): a for a in cfg["authors"]}

    # ---------- 發文 ----------
    print("正在查發文……")
    for board in cfg["boards"]:
        for author in cfg["authors"]:
            try:
                found = fetch_posts(session, board, author)
            except requests.RequestException as e:
                print(f"  [略過] {board} 的 {author} 查詢失敗: {e}")
                continue
            hits = [p for p in found if p["ts"] >= since_ts]
            posts[author].extend(hits)
            print(f"  {board} / {author}：{len(hits)} 篇")
            time.sleep(1)

    # ---------- 留言 ----------
    hours = (days + EXTRA_LOOKBACK_DAYS) * 24
    for board in cfg["boards"]:
        try:
            articles = recent_articles(session, board, hours, max_pages=10)
        except requests.RequestException as e:
            print(f"  [略過] 取得 {board} 文章列表失敗: {e}")
            continue

        print(f"\n正在掃 {board} 最近 {len(articles)} 篇文章找留言（約需 {len(articles)//2} 秒）……")
        for i, art in enumerate(articles, 1):
            try:
                for c in fetch_comments(session, art):
                    real = targets.get(c["user"].lower())
                    if real and c["when"][:5] in wanted_dates:
                        comments[real].append((art, c))
            except requests.RequestException:
                pass  # 單篇讀不到就跳過，報表照樣產得出來
            if i % 5 == 0 or i == len(articles):
                print(f"  已掃 {i}/{len(articles)} 篇")
            time.sleep(0.5)

    for a in posts:
        posts[a].sort(key=lambda p: p["ts"])
        comments[a].sort(key=lambda ac: ac[1]["when"])
    return posts, comments, wanted_dates


def build_html(cfg: dict, posts: dict, comments: dict, dates: list[str]) -> str:
    e = html.escape
    when = f"{dates[0]} ~ {dates[-1]}" if len(dates) > 1 else dates[0]
    now = datetime.now().strftime("%Y/%m/%d %H:%M")

    blocks = []
    for author in cfg["authors"]:
        ps, cs = posts.get(author, []), comments.get(author, [])
        rows = []

        rows.append(f'<h3>發文 <span class="n">{len(ps)}</span></h3>')
        if ps:
            for p in ps:
                t = datetime.fromtimestamp(p["ts"]).strftime("%m/%d %H:%M")
                rows.append(
                    f'<div class="item"><div class="time">{t}</div>'
                    f'<a href="{e(p["url"])}" target="_blank">{e(p["title"])}</a></div>'
                )
        else:
            rows.append('<div class="empty">這段期間沒有發文</div>')

        rows.append(f'<h3>留言 <span class="n">{len(cs)}</span></h3>')
        if cs:
            for art, c in cs:
                tag = c["tag"] or "→"
                cls = {"推": "push", "噓": "boo"}.get(tag, "arrow")
                rows.append(
                    f'<div class="item"><div class="time">{e(c["when"])}</div>'
                    f'<span class="tag {cls}">{e(tag)}</span>'
                    f'<span class="content">{e(c["content"])}</span>'
                    f'<div class="src">在 <a href="{e(art["url"])}" target="_blank">'
                    f'{e(art["title"])}</a></div></div>'
                )
        else:
            rows.append('<div class="empty">這段期間沒有留言</div>')

        blocks.append(f'<section><h2>{e(author)}</h2>{"".join(rows)}</section>')

    return f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PTT 動態報表 {e(when)}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: "Microsoft JhengHei", "PingFang TC", sans-serif;
         max-width: 820px; margin: 0 auto; padding: 24px 16px 60px; line-height: 1.7; }}
  header {{ border-bottom: 2px solid #888; padding-bottom: 12px; margin-bottom: 8px; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .sub {{ color: #888; font-size: 13px; }}
  section {{ margin-top: 34px; }}
  h2 {{ font-size: 19px; background: #4a7; color: #fff; padding: 6px 12px;
        border-radius: 6px; margin: 0 0 6px; }}
  h3 {{ font-size: 15px; margin: 20px 0 6px; color: #666; }}
  .n {{ background: #666; color: #fff; border-radius: 10px;
        padding: 1px 9px; font-size: 13px; margin-left: 4px; }}
  .item {{ padding: 8px 0 8px 12px; border-left: 3px solid #ddd; margin-bottom: 6px; }}
  .time {{ font-size: 12px; color: #999; }}
  .tag {{ display: inline-block; width: 1.6em; text-align: center;
          font-weight: bold; border-radius: 4px; margin-right: 6px; }}
  .push {{ color: #c33; }} .boo {{ color: #36c; }} .arrow {{ color: #999; }}
  .content {{ word-break: break-word; }}
  .src {{ font-size: 12px; color: #999; margin-top: 2px; }}
  .empty {{ color: #aaa; font-size: 14px; padding: 4px 0 4px 12px; }}
  a {{ color: #27a; }}
  @media (prefers-color-scheme: dark) {{
    .item {{ border-left-color: #444; }} h2 {{ background: #276; }}
    a {{ color: #6bf; }}
  }}
</style></head><body>
<header>
  <h1>PTT 動態報表</h1>
  <div class="sub">期間：{e(when)}　·　看板：{e("、".join(cfg["boards"]))}　·　產生於 {now}</div>
</header>
{"".join(blocks)}
</body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser(description="查看帳號今天的發文與留言")
    ap.add_argument("--days", type=int, default=1, help="往回查幾天（預設 1 = 今天）")
    args = ap.parse_args()
    days = max(1, args.days)

    cfg = load_config()
    line = "=" * 58
    print(line)
    print(f"  查詢 {'今天' if days == 1 else f'最近 {days} 天'}的動態")
    print(f"  帳號：{'、'.join(cfg['authors'])}")
    print(f"  看板：{'、'.join(cfg['boards'])}")
    print(line)
    print()

    posts, comments, dates = collect(cfg, days)

    print("\n" + line)
    total_p = sum(len(v) for v in posts.values())
    total_c = sum(len(v) for v in comments.values())
    for a in cfg["authors"]:
        print(f"  {a}：發文 {len(posts[a])} 篇、留言 {len(comments[a])} 則")
    print(f"  合計：發文 {total_p} 篇、留言 {total_c} 則")
    print(line)

    REPORT_PATH.write_text(build_html(cfg, posts, comments, dates), encoding="utf-8")
    print(f"\n報表已產生：{REPORT_PATH.name}")
    print("正在用瀏覽器打開……")
    webbrowser.open(REPORT_PATH.as_uri())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n已取消。")

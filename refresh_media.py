#!/usr/bin/env python3
"""Snapshot CI Athletics Instagram + CISPN Songdo YouTube into assets/media.js.

Instagram CDN image URLs are signed and expire, so post thumbnails are
downloaded into assets/insta/. YouTube thumbnails hotlink from i.ytimg.com
(stable). Re-run this script any time to refresh the media grids.

Usage: python3 refresh_media.py
"""
import json, os, re, subprocess, time

BASE = os.path.dirname(os.path.abspath(__file__))
INSTA_DIR = os.path.join(BASE, "assets", "insta")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126 Safari/537.36")

IG_USERS = ["ci_athletics", "intl_chadwick"]
YT_CHANNEL_ID = "UCtb9NVCcZ241NO1j--vnM_w"  # CISPN Songdo
N_POSTS = 6
N_VIDEOS = 6


def get(url, headers=None):
    # curl avoids the missing-CA-bundle issue in framework Python builds
    cmd = ["curl", "-sfL", "--http1.1", "--max-time", "30", "-A", UA]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    cmd.append(url)
    for attempt in range(4):
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode == 0:
            return r.stdout
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"curl failed ({r.returncode}) for {url}")


IG_APP_ID = {"x-ig-app-id": "936619743392459"}


def get_ig(url):
    # Instagram resets plain-curl TLS fingerprints; curl_cffi impersonation
    # is required for the API endpoints (CDN image downloads are fine via curl)
    from curl_cffi import requests as cffi_requests
    resp = cffi_requests.get(url, headers=IG_APP_ID, impersonate="safari", timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:80]}")
    return resp.content


def _save_post(ig_user, code, img_url, is_video, caption):
    fname = f"{ig_user}_{code}.jpg"
    with open(os.path.join(INSTA_DIR, fname), "wb") as f:
        f.write(get(img_url))
    print(f"  IG {code} saved")
    return {"code": code, "img": f"assets/insta/{fname}",
            "video": is_video, "caption": (caption or "")[:120]}


def _ig_via_profile_api(ig_user):
    url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={ig_user}"
    user = json.loads(get_ig(url))["data"]["user"]
    posts = []
    for e in user["edge_owner_to_timeline_media"]["edges"][:N_POSTS]:
        n = e["node"]
        cap = n.get("edge_media_to_caption", {}).get("edges", [])
        posts.append(_save_post(ig_user, n["shortcode"], n["display_url"],
                                n.get("is_video", False),
                                cap[0]["node"]["text"] if cap else ""))
    return {"followers": user["edge_followed_by"]["count"], "posts": posts}


IG_KNOWN_IDS = {"intl_chadwick": "60923618020"}


def _ig_via_feed_api(ig_user):
    # Fallback for accounts where web_profile_info 400s (broken business
    # category schema): resolve the numeric id, then use the public feed
    # endpoint.
    uid = IG_KNOWN_IDS.get(ig_user)
    if not uid:
        html = get(f"https://www.instagram.com/{ig_user}/").decode("utf-8", "replace")
        m = re.search(r'"profile_id":"(\d+)"', html) or re.search(r"profilePage_(\d+)", html)
        if not m:
            raise RuntimeError("could not resolve profile id")
        uid = m.group(1)
    data = json.loads(get_ig(f"https://i.instagram.com/api/v1/feed/user/{uid}/?count=12"))
    posts = []
    for item in data.get("items", [])[:N_POSTS]:
        media = item.get("carousel_media", [item])[0]
        candidates = media.get("image_versions2", {}).get("candidates", [])
        if not candidates:
            continue
        caption = (item.get("caption") or {}).get("text", "")
        posts.append(_save_post(ig_user, item["code"], candidates[0]["url"],
                                item.get("media_type") == 2, caption))
    if not posts:
        raise RuntimeError("feed endpoint returned no renderable items")
    return {"followers": None, "posts": posts}


def fetch_instagram(ig_user):
    os.makedirs(INSTA_DIR, exist_ok=True)
    first, second = ((_ig_via_feed_api, _ig_via_profile_api)
                     if ig_user in IG_KNOWN_IDS
                     else (_ig_via_profile_api, _ig_via_feed_api))
    try:
        return first(ig_user)
    except Exception as ex:
        print(f"  {first.__name__} failed ({ex}); trying {second.__name__}")
        return second(ig_user)


def fetch_youtube():
    xml = get(f"https://www.youtube.com/feeds/videos.xml?channel_id={YT_CHANNEL_ID}").decode()
    entries = re.findall(
        r"<entry>.*?<yt:videoId>([^<]+)</yt:videoId>.*?<title>([^<]+)</title>"
        r".*?<published>([^<]+)</published>", xml, re.S)
    videos = [{"id": vid, "title": title.strip(), "published": pub[:10]}
              for vid, title, pub in entries[:N_VIDEOS]]
    for v in videos:
        print(f"  YT {v['id']} {v['title'][:50]}")
    return videos


import urllib.parse
from email.utils import parsedate_to_datetime

ADMISSIONS_OUTLETS = [
    # (display name, tag, google-news query, expected " - Suffix" on titles)
    ("Inside Higher Ed", "IHE", 'admissions source:"Inside Higher Ed" when:90d', "Inside Higher Ed"),
    ("Forbes", "Forbes", 'college source:Forbes when:90d', "Forbes"),
    ("U.S. News", "USN", 'college source:"U.S. News & World Report" when:90d', "U.S. News & World Report"),
    ("Niche", "Niche", 'college source:Niche when:365d', "Niche"),
]

WORLD_FEEDS = [
    ("NYT · World", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml"),
    ("NYT · Science", "https://rss.nytimes.com/services/xml/rss/nyt/Science.xml"),
    ("BBC", "https://feeds.bbci.co.uk/news/world/rss.xml"),
]


def _unescape(s):
    import html as _html
    return _html.unescape(re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s, flags=re.S)).strip()


def _items(xml):
    return re.findall(r"<item>(.*?)</item>", xml, re.S)


def _field(item, tag):
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", item, re.S)
    return _unescape(m.group(1)) if m else ""


def fetch_admissions():
    articles = []
    for name, tag, query, suffix in ADMISSIONS_OUTLETS:
        url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(query)
               + "&hl=en-US&gl=US&ceid=US:en")
        # Google News RSS intermittently returns a valid-but-empty feed; retry
        items = []
        for attempt in range(5):
            try:
                items = _items(get(url).decode("utf-8", "replace"))
            except Exception as ex:
                print(f"  admissions {name} fetch error: {ex}")
            if items:
                break
            time.sleep(4)
        kept = 0
        for item in items:
            title = _field(item, "title")
            if not title.endswith(" - " + suffix):
                continue  # Google News mixes in other outlets; keep exact matches only
            title = title[: -len(" - " + suffix)]
            try:
                dt = parsedate_to_datetime(_field(item, "pubDate"))
            except Exception:
                continue
            articles.append({"outlet": name, "tag": tag, "title": title,
                             "url": _field(item, "link"), "date": dt.strftime("%Y-%m-%d"),
                             "_ts": dt.timestamp()})
            kept += 1
            if kept >= 3:
                break
        print(f"  admissions {name}: {kept} articles")
    articles.sort(key=lambda a: -a["_ts"])
    for a in articles:
        a.pop("_ts")
    return articles


def fetch_world():
    articles = []
    for source, feed in WORLD_FEEDS:
        try:
            xml = get(feed).decode("utf-8", "replace")
        except Exception as ex:
            print(f"  world {source} FAILED: {ex}")
            continue
        count = 0
        for item in _items(xml):
            thumb = ""
            m = re.search(r'<media:(?:content|thumbnail)[^>]*url="([^"]+)"', item)
            if m:
                thumb = m.group(1)
            try:
                dt = parsedate_to_datetime(_field(item, "pubDate"))
            except Exception:
                continue
            articles.append({"source": source, "title": _field(item, "title"),
                             "url": _field(item, "link"), "thumb": thumb,
                             "date": dt.strftime("%Y-%m-%d %H:%M"), "_ts": dt.timestamp()})
            count += 1
            if count >= 4:
                break
        print(f"  world {source}: {count} articles")
    articles.sort(key=lambda a: -a["_ts"])
    for a in articles:
        a.pop("_ts")
    return articles[:9]


def main():
    # Keep previously fetched data for accounts that fail this run
    prev = {}
    out_path = os.path.join(BASE, "assets", "media.js")
    if os.path.exists(out_path):
        try:
            prev = json.loads(open(out_path).read().split("window.CI_MEDIA = ", 1)[1].rstrip().rstrip(";"))
        except Exception:
            prev = {}
    ig = {}
    for ig_user in IG_USERS:
        print("Fetching Instagram @" + ig_user)
        try:
            ig[ig_user] = fetch_instagram(ig_user)
        except Exception as ex:
            print(f"  FAILED ({ex}); keeping previous snapshot if any")
            if prev.get("instagram", {}).get(ig_user):
                ig[ig_user] = prev["instagram"][ig_user]
        time.sleep(5)
    print("Fetching YouTube CISPN Songdo")
    yt = fetch_youtube()
    print("Fetching admissions news")
    admissions = fetch_admissions()
    # Google News RSS often returns empty for a subset of outlets per run;
    # keep the previous snapshot's articles for any outlet missing this run
    got = {a["outlet"] for a in admissions}
    carried = [a for a in prev.get("admissions", []) if a["outlet"] not in got]
    if carried:
        print(f"  carried over {len(carried)} previous articles for missing outlets")
    admissions = sorted(admissions + carried, key=lambda a: a["date"], reverse=True)
    print("Fetching world news")
    world = fetch_world()
    out = os.path.join(BASE, "assets", "media.js")
    updated = time.strftime("%Y-%m-%d %H:%M")
    with open(out, "w", encoding="utf-8") as f:
        f.write("// Generated by refresh_media.py — do not edit by hand\n")
        f.write("window.CI_MEDIA = ")
        json.dump({"updated": updated, "instagram": ig, "youtube": yt,
                   "admissions": admissions, "world": world},
                  f, ensure_ascii=False, indent=2)
        f.write(";\n")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()

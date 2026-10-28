#!/usr/bin/env python3
"""Manage Dice Dungeon's Meta ads from files in ads/.

Each ad is a file in ads/*.yml. `create` builds the campaign, ad set and ad
with status PAUSED, so nothing spends money until `activate` runs on a file
that says `approved: true`. Ids are written back into the file so a re-run
never creates duplicates.

Env:
  META_ACCESS_TOKEN  required (GitHub secret, never committed)

Usage:
  python scripts/meta_ads.py check              # token, ad account, pages, campaigns (read-only)
  python scripts/meta_ads.py report [--days 7]  # spend and results per campaign (read-only)
  python scripts/meta_ads.py create  --ad ID    # build a PAUSED campaign from ads/ID.yml
  python scripts/meta_ads.py pause   --ad ID    # pause it (always allowed)
  python scripts/meta_ads.py activate --ad ID   # turn it on, only if approved: true
"""
import argparse
import datetime as dt
import glob
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config", "meta.yml")


def cfg():
    with open(CONFIG) as f:
        return yaml.safe_load(f)


def api(method, path, params=None, soft=False, token=None):
    token = token or os.environ.get("META_ACCESS_TOKEN")
    if not token:
        sys.exit("META_ACCESS_TOKEN is not set")
    c = cfg()
    url = f"https://graph.facebook.com/{c['api_version']}/{path.lstrip('/')}"
    params = dict(params or {})
    for k, v in list(params.items()):
        if isinstance(v, (dict, list, bool)):
            params[k] = json.dumps(v)
    data = None
    headers = {"Authorization": f"Bearer {token}"}
    if method == "GET":
        if params:
            url += "?" + urllib.parse.urlencode(params)
    else:
        data = urllib.parse.urlencode(params).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if soft:
            print(f"(skipped {path}: {body[:200]})")
            return None
        try:
            err = json.loads(body)["error"]
            msg = err.get("error_user_msg") or err.get("message")
            sys.exit(f"Meta API error ({err.get('code')}/{err.get('error_subcode')}): {msg}")
        except (ValueError, KeyError):
            sys.exit(f"Meta API HTTP {e.code}: {body[:500]}")


def get_all(path, params):
    out, res = [], api("GET", path, params)
    while True:
        out += res.get("data", [])
        nxt = res.get("paging", {}).get("next")
        if not nxt:
            return out
        req = urllib.request.Request(nxt, headers={"Authorization": f"Bearer {os.environ['META_ACCESS_TOKEN']}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            res = json.load(r)


def act():
    return f"act_{cfg()['ad_account_id']}"


def load_ads():
    ads = {}
    for p in sorted(glob.glob(os.path.join(ROOT, "ads", "*.yml"))):
        with open(p) as f:
            d = yaml.safe_load(f) or {}
        d["_path"] = p
        ads[os.path.splitext(os.path.basename(p))[0]] = d
    return ads


def save(ad):
    path = ad.pop("_path")
    with open(path, "w") as f:
        yaml.safe_dump(ad, f, sort_keys=False, allow_unicode=True)
    ad["_path"] = path


def summary(lines):
    text = "\n".join(lines)
    print(text)
    s = os.environ.get("GITHUB_STEP_SUMMARY")
    if s:
        with open(s, "a") as f:
            f.write(text + "\n")


def known_campaign_ids():
    return {str(a.get("ids", {}).get("campaign")) for a in load_ads().values() if a.get("ids")}


def check():
    me = api("GET", "me", {"fields": "id,name"})
    a = api("GET", act(), {"fields": "name,account_status,currency,amount_spent,disable_reason"})
    status = {1: "active", 2: "disabled", 3: "unsettled", 7: "pending review", 9: "in grace period", 101: "closed"}
    lines = [
        "## Meta check",
        f"Token belongs to: {me.get('name')} ({me.get('id')})",
        f"Ad account: {a.get('name')} ({act()}), status {status.get(a.get('account_status'), a.get('account_status'))}, "
        f"currency {a.get('currency')}, lifetime spend {int(a.get('amount_spent', 0)) / 100:.2f}",
        "",
        "Pages this account can advertise for:",
    ]
    for p in get_all(f"{act()}/promote_pages", {"fields": "id,name"}):
        lines.append(f"- {p['name']}: {p['id']}")
        info = api("GET", p["id"], {"fields": "followers_count,fan_count,instagram_business_account{id,username,followers_count,media_count}"}, soft=True)
        if info:
            lines.append(f"  Facebook followers {info.get('followers_count')}, likes {info.get('fan_count')}")
            ig = info.get("instagram_business_account")
            if ig:
                lines.append(f"  Instagram @{ig.get('username')} ({ig['id']}): {ig.get('followers_count')} followers, {ig.get('media_count')} posts")
    lines += ["", campaign_table()]
    summary(lines)


def ended(c):
    st = c.get("stop_time")
    return bool(st) and dt.datetime.strptime(st, "%Y-%m-%dT%H:%M:%S%z") < dt.datetime.now(dt.timezone.utc)


def live_status(c):
    return "ENDED" if c["effective_status"] == "ACTIVE" and ended(c) else c["effective_status"]


def campaign_table():
    known = known_campaign_ids()
    rows = ["| Campaign | Status | Effective | Budget | In ads/ |", "|---|---|---|---|---|"]
    for c in get_all(f"{act()}/campaigns", {"fields": "id,name,status,effective_status,daily_budget,lifetime_budget,stop_time", "limit": 100}):
        if c.get("daily_budget"):
            b = f"{int(c['daily_budget']) / 100:.0f}/day"
        elif c.get("lifetime_budget"):
            b = f"{int(c['lifetime_budget']) / 100:.0f} total"
        else:
            b = "ad set level"
        mine = "yes" if c["id"] in known else "**no**"
        rows.append(f"| {c['name']} | {c['status']} | {live_status(c)} | {b} | {mine} |")
    return "\n".join(rows)


def strangers_detail():
    out = ["", "### Campaign details (all, incl. deleted)", ""]
    for c in get_all(f"{act()}/campaigns", {"fields": "id,name,objective,effective_status,created_time,start_time,stop_time,updated_time", "limit": 100, "filtering": [{"field": "effective_status", "operator": "IN", "value": ["ACTIVE","PAUSED","DELETED","ARCHIVED","CAMPAIGN_PAUSED","ADSET_PAUSED","IN_PROCESS","WITH_ISSUES"]}]}):
        out.append(f"- {c['id']} {c['name']} {c.get('objective')} {c['effective_status']} created {c.get('created_time')} start {c.get('start_time')} stop {c.get('stop_time')}")
    return out


def daily(days, match):
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    until = dt.date.today().isoformat()
    rows = get_all(f"{act()}/insights", {
        "level": "campaign", "time_increment": 1,
        "fields": "campaign_id,campaign_name,spend,impressions",
        "time_range": {"since": since, "until": until}, "limit": 500,
    })
    lines = [f"## Daily spend {since} to {until}, campaigns matching '{match}'", "", "| Day | Campaign | Spend € | Impr. |", "|---|---|---|---|"]
    tot = 0.0
    for r in rows:
        if match.lower() not in r["campaign_name"].lower() or float(r.get("spend", 0)) == 0:
            continue
        tot += float(r["spend"])
        lines.append(f"| {r['date_start']} | {r['campaign_name'][:40]} {r['campaign_id']} | {float(r['spend']):.2f} | {r.get('impressions')} |")
    lines += ["", f"Total: {tot:.2f} €"]
    summary(lines)


def report(days):
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    until = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    rows = get_all(f"{act()}/insights", {
        "level": "campaign",
        "fields": "campaign_id,campaign_name,spend,impressions,reach,frequency,clicks,cpm,actions,video_thruplay_watched_actions,date_start,date_stop",
        "time_range": {"since": since, "until": until},
        "limit": 100,
    })
    lines = [f"## Meta ads {since} to {until}", "",
             "| Campaign | Spend € | Reach | Impr. | Freq | CPM € | ThruPlays | Link clicks | Post engagement |",
             "|---|---|---|---|---|---|---|---|---|"]
    total = 0.0
    for r in rows:
        acts = {x["action_type"]: x["value"] for x in r.get("actions", [])}
        thru = sum(int(x["value"]) for x in r.get("video_thruplay_watched_actions", []))
        spend = float(r.get("spend", 0))
        total += spend
        lines.append(
            f"| {r['campaign_name']} | {spend:.2f} | {r.get('reach', 0)} | {r.get('impressions', 0)} | "
            f"{float(r.get('frequency', 0)):.2f} | {float(r.get('cpm', 0)):.2f} | {thru} | "
            f"{acts.get('link_click', 0)} | {acts.get('post_engagement', 0)} |")
    lines += ["", f"Total spend: {total:.2f} €", "", "### All actions per campaign", ""]
    for r in rows:
        acts = ", ".join(f"{x['action_type']}={x['value']}" for x in r.get("actions", []))
        lines.append(f"- {r['campaign_name']} [{r.get('campaign_id')} {r.get('date_start')}..{r.get('date_stop')}]: {acts[:300]}")
    lines.append("")
    try:
        lines += strangers_detail()
    except SystemExit as e:
        lines.append(f"(details failed: {e})")
    known = known_campaign_ids()
    strangers = [c for c in get_all(f"{act()}/campaigns", {"fields": "id,name,effective_status,stop_time", "limit": 100})
                 if live_status(c) == "ACTIVE" and c["id"] not in known]
    if strangers:
        lines.append("**Warning: active campaigns not in ads/:** " + ", ".join(c["name"] for c in strangers))
    summary(lines)


def to_unix(when, not_before_now=False):
    if isinstance(when, str):
        when = dt.datetime.fromisoformat(when)
    t = int(when.timestamp())
    if not_before_now:
        t = max(t, int(dt.datetime.now(dt.timezone.utc).timestamp()) + 300)
    return t


_PAGE_TOKEN = {}


def page_token(page_id):
    if page_id not in _PAGE_TOKEN:
        _PAGE_TOKEN[page_id] = api("GET", str(page_id), {"fields": "access_token"})["access_token"]
    return _PAGE_TOKEN[page_id]


_PAGE_TOKEN = {}


def page_token(page_id):
    if page_id not in _PAGE_TOKEN:
        _PAGE_TOKEN[page_id] = api("GET", str(page_id), {"fields": "access_token"})["access_token"]
    return _PAGE_TOKEN[page_id]


def post_text(post_id):
    with open(os.path.join(ROOT, "posts", f"{post_id}.yml")) as f:
        return (yaml.safe_load(f) or {}).get("text", "")


def norm(t):
    return " ".join((t or "").split())[:60].lower()


def recent_page_content(page_id):
    pt = page_token(page_id)
    posts = api("GET", f"{page_id}/posts", {"fields": "id,message,created_time,attachments{media_type,target{id}}", "limit": 25}, token=pt).get("data", [])
    reels = api("GET", f"{page_id}/video_reels", {"fields": "id,description,created_time,post_id", "limit": 25}, soft=True, token=pt) or {}
    return posts, reels.get("data", [])


def find_published(page_id, post_id):
    """Find the Facebook post Buffer published for posts/<post_id>.yml by its caption."""
    key = norm(post_text(post_id))
    posts, reels = recent_page_content(page_id)
    for p in posts:
        if norm(p.get("message")) == key:
            vid = None
            for a in (p.get("attachments") or {}).get("data", []):
                if a.get("media_type") == "video":
                    vid = (a.get("target") or {}).get("id")
            return p["id"], vid
    for r in reels:
        if norm(r.get("description")) == key:
            story = r.get("post_id")
            if story and "_" not in str(story):
                story = f"{page_id}_{story}"
            return story, r["id"]
    return None, None


def recent_ig_media(limit=50):
    c = cfg()
    return api("GET", f"{c['instagram_id']}/media",
               {"fields": "id,caption,timestamp,media_type,permalink", "limit": limit},
               token=page_token(c["page_id"])).get("data", [])


def find_ig_media(post_id):
    """Find the Instagram post Buffer published for posts/<post_id>.yml by its caption."""
    key = norm(post_text(post_id))
    for m in recent_ig_media():
        if norm(m.get("caption")) == key:
            return m["id"]
    return None


def list_posts():
    c = cfg()
    page = api("GET", c["page_id"], {"fields": "name,leadgen_tos_accepted"}, soft=True, token=page_token(c["page_id"])) or {}
    lines = [f"## Recent content on {page.get('name')}", f"Lead ads terms accepted: {page.get('leadgen_tos_accepted')}", ""]
    posts, reels = recent_page_content(c["page_id"])
    for p in posts:
        kinds = ",".join(a.get("media_type", "?") for a in (p.get("attachments") or {}).get("data", []))
        lines.append(f"- post {p['id']} {p['created_time'][:16]} [{kinds}] {norm(p.get('message'))[:50]}")
    for r in reels:
        lines.append(f"- reel {r['id']} post {r.get('post_id')} {r['created_time'][:16]} {norm(r.get('description'))[:50]}")
    lines += ["", "## Recent Instagram posts", ""]
    try:
        for m in recent_ig_media(20):
            lines.append(f"- ig {m['id']} {m['timestamp'][:16]} {m.get('media_type')} {norm(m.get('caption'))[:50]}")
    except SystemExit as e:
        lines.append(f"(Instagram media lookup failed: {e})")
    summary(lines)


def lead_form(ad, page_id):
    """Create the ad's instant form once and remember its id."""
    if ad.get("form_id"):
        return ad["form_id"]
    f = ad["form"]
    r = api("POST", f"{page_id}/leadgen_forms", {
        "name": f["name"],
        "locale": f.get("locale", "fi_FI"),
        "questions": f["questions"],
        "privacy_policy": {"url": f["privacy_url"], "link_text": f.get("privacy_text", "Tietosuojaseloste")},
        "context_card": {"title": f["intro_title"], "style": "PARAGRAPH_STYLE", "content": [f["intro_text"]]},
        "thank_you_page": {"title": f["thanks_title"], "body": f["thanks_body"],
                           "button_type": "VIEW_WEBSITE", "button_text": "dicedungeon.fi", "website_url": ad["link"]},
        # Higher Intent form: adds a review step before submit, fewer accidental leads
        "is_optimized_for_quality": bool(f.get("higher_intent")),
    }, token=page_token(page_id))
    ad["form_id"] = r["id"]
    save(ad)
    print("lead form", r["id"])
    return r["id"]


def cta(ad, page_id):
    if ad.get("form"):
        return {"type": ad.get("cta", "GET_QUOTE"), "value": {"lead_gen_form_id": lead_form(ad, page_id)}}
    return {"type": ad.get("cta", "BOOK_NOW"), "value": {"link": ad["link"]}}


def create(ad_id):
    ads = load_ads()
    if ad_id not in ads:
        sys.exit(f"No ads/{ad_id}.yml")
    ad = ads[ad_id]
    ids = ad.setdefault("ids", {})
    c = cfg()
    page_id = ad.get("page_id") or c.get("page_id")
    if not page_id:
        sys.exit("Set page_id in config/meta.yml (run `check` to list it)")

    if ad.get("post") and not ad.get("fb_post_id"):
        story, video = find_published(page_id, ad["post"])
        if not story:
            sys.exit(f"posts/{ad['post']} is not on the Facebook Page yet; nothing created")
        ad["fb_post_id"], ad["video_id"] = story, video
        save(ad)

    if not ids.get("campaign"):
        r = api("POST", f"{act()}/campaigns", {
            "name": ad["name"],
            "objective": ad.get("objective", "OUTCOME_ENGAGEMENT"),
            "status": "PAUSED",
            "special_ad_categories": [],
            "is_adset_budget_sharing_enabled": False,
        })
        ids["campaign"] = r["id"]
        save(ad)
        print("campaign", r["id"])

    if not ids.get("adset"):
        t = ad["targeting"]
        targeting = {
            "geo_locations": {"custom_locations": [{
                "latitude": t.get("lat", 60.1699), "longitude": t.get("lng", 24.9384),
                "radius": t.get("radius_km", 25), "distance_unit": "kilometer"}]},
            "age_min": t.get("age_min", 18),
            "age_max": t.get("age_max", 65),
            "publisher_platforms": t.get("platforms", ["facebook", "instagram"]),
            "targeting_automation": {"advantage_audience": 1 if t.get("advantage_audience") else 0},
        }
        if t.get("custom_audiences"):
            # warm audiences (video viewers, Instagram engagers, site visitors).
            # With advantage_audience they are suggestions Meta expands from; without it a hard filter.
            targeting["custom_audiences"] = [{"id": str(a)} for a in t["custom_audiences"]]
        r = api("POST", f"{act()}/adsets", {
            "name": f"{ad['name']} ad set",
            "campaign_id": ids["campaign"],
            "status": "PAUSED",
            "lifetime_budget": int(round(float(ad["budget_eur"]) * 100)),
            "start_time": to_unix(ad["start"], not_before_now=True),
            "end_time": to_unix(ad["end"]),
            "billing_event": "IMPRESSIONS",
            "optimization_goal": ad.get("optimization_goal", "THRUPLAY"),
            "destination_type": ad.get("destination_type", "ON_VIDEO"),
            "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
            "targeting": targeting,
            **({"promoted_object": {"page_id": page_id}} if ad.get("form") else {}),
        })
        ids["adset"] = r["id"]
        save(ad)
        print("ad set", r["id"])

    for entry in (ad.get("ads") or [None]):
        build_ad(ad, ad_id, entry, ids, c, page_id)
    print(f"{ad_id}: built and PAUSED. Review it in Ads Manager, then set approved: true and run activate.")


def build_ad(ad, ad_id, entry, ids, c, page_id):
    """Create one ad in the ad set. entry=None means the classic single-ad file; otherwise one item of ads:."""
    if entry is None:
        key, sub = "ad", ad
    else:
        key, sub = f"ad:{entry['post']}", {**ad, **entry}
        sub.pop("ads", None)
    if ids.get(key):
        return
    if True:
        story, video = sub.get("fb_post_id"), sub.get("video_id")
        if not story and sub.get("post"):
            story, video = find_published(page_id, sub["post"])
            if not story:
                print(f"posts/{sub['post']} is not on the Facebook Page yet; skipped for now")
                return
            if entry is None:
                ad["fb_post_id"], ad["video_id"] = story, video
            else:
                entry["fb_post_id"], entry["video_id"] = story, video
            save(ad)
        if not story and ad.get("post"):
            story, video = find_published(page_id, ad["post"])
            if not story:
                sys.exit(f"posts/{ad['post']} is not on the Facebook Page yet; ad set left paused, try again later")
            ad["fb_post_id"], ad["video_id"] = story, video
            save(ad)
        story = str(story)
        if "_" not in story:
            story = f"{page_id}_{story}"
        # Instagram placement: reuse the real Instagram post so likes/views land on the grid,
        # instead of Meta making a separate ad copy. Falls back to the copy if no IG post is found.
        ig_media = sub.get("ig_media_id")
        if not ig_media and sub.get("post") and c.get("instagram_id") and "instagram" in ad.get("targeting", {}).get("platforms", ["facebook", "instagram"]):
            ig_media = find_ig_media(sub["post"])
            if ig_media:
                (ad if entry is None else entry)["ig_media_id"] = ig_media
                save(ad)
        label = f"{ad['name']} {sub.get('post', '')}".strip()
        if sub.get("creative", "boost") == "boost":
            spec = {"name": f"{label} creative", "object_story_id": story}
            if c.get("instagram_id"):
                spec["instagram_user_id"] = c["instagram_id"]
            if ig_media:
                spec["source_instagram_media_id"] = str(ig_media)
        else:
            if not video:
                sys.exit("link creative needs a video post")
            thumb = api("GET", str(video), {"fields": "picture"}).get("picture")
            message = sub.get("message") or post_text(sub["post"]).split("\n\n#")[0].strip()
            spec = {
                "name": f"{label} creative",
                "object_story_spec": {
                    "page_id": page_id,
                    "instagram_user_id": c.get("instagram_id"),
                    "video_data": {
                        "video_id": str(video),
                        "image_url": thumb,
                        "message": message,
                        "call_to_action": cta(sub, page_id),
                    },
                },
                "url_tags": f"utm_source=meta&utm_medium=paid&utm_campaign={ad_id}&utm_content={sub.get('post', 'ad')}",
            }
            if ig_media and sub.get("cta") != "GET_QUOTE":
                spec["source_instagram_media_id"] = str(ig_media)
        cr = api("POST", f"{act()}/adcreatives", spec)
        r = api("POST", f"{act()}/ads", {
            "name": f"{label} ad",
            "adset_id": ids["adset"],
            "creative": {"creative_id": cr["id"]},
            "status": "PAUSED",
        })
        ids[key] = r["id"]
        save(ad)
        print("ad", key, r["id"])


def set_status(ad_id, status):
    ads = load_ads()
    if ad_id not in ads:
        sys.exit(f"No ads/{ad_id}.yml")
    ad = ads[ad_id]
    ids = ad.get("ids") or {}
    if not ids.get("campaign"):
        sys.exit(f"{ad_id} has no campaign yet; run create first")
    if status == "ACTIVE" and ad.get("approved") is not True:
        sys.exit(f"{ad_id} is not approved: true, so it stays paused")
    ad_keys = [k for k in ids if k == "ad" or k.startswith("ad:")]
    order = ad_keys + ["adset", "campaign"] if status == "PAUSED" else ["campaign", "adset"] + ad_keys
    for k in order:
        if ids.get(k):
            api("POST", str(ids[k]), {"status": status})
            print(f"{k} {ids[k]} -> {status}")
    for old in ids.get("retired") or []:
        api("POST", str(old), {"status": "PAUSED"})
        print(f"retired ad {old} -> PAUSED")


def set_budget(ad_id):
    ad = load_ads().get(ad_id)
    if not ad:
        sys.exit(f"no ads/{ad_id}.yml")
    adset = (ad.get("ids") or {}).get("adset")
    cents = int(round(float(ad["budget_eur"]) * 100))
    if not adset:
        print(f"{ad_id}: not created yet, budget {cents / 100:.2f} EUR will be used at launch")
        return
    api("POST", adset, {"lifetime_budget": cents})
    got = api("GET", adset, {"fields": "lifetime_budget,effective_status"})
    print(f"{ad_id}: ad set {adset} lifetime budget now {int(got['lifetime_budget']) / 100:.2f} EUR ({got['effective_status']})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["check", "report", "posts", "create", "launch", "pause", "activate", "budget", "daily"])
    ap.add_argument("--ad", default="")
    ap.add_argument("--days", type=int, default=7)
    a = ap.parse_args()
    if a.action == "check":
        check()
    elif a.action == "report":
        report(a.days)
    elif a.action == "daily":
        daily(a.days, a.ad or "")
    elif a.action == "posts":
        list_posts()
    else:
        if not a.ad:
            sys.exit("--ad is required")
        if a.action == "create":
            create(a.ad)
        elif a.action == "budget":
            set_budget(a.ad)
        elif a.action == "launch":
            create(a.ad)
            set_status(a.ad, "ACTIVE")
        else:
            set_status(a.ad, "PAUSED" if a.action == "pause" else "ACTIVE")


if __name__ == "__main__":
    main()

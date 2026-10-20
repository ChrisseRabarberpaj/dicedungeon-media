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


def api(method, path, params=None, soft=False):
    token = os.environ.get("META_ACCESS_TOKEN")
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


def campaign_table():
    known = known_campaign_ids()
    rows = ["| Campaign | Status | Effective | Budget | In ads/ |", "|---|---|---|---|---|"]
    for c in get_all(f"{act()}/campaigns", {"fields": "id,name,status,effective_status,daily_budget,lifetime_budget", "limit": 100}):
        if c.get("daily_budget"):
            b = f"{int(c['daily_budget']) / 100:.0f}/day"
        elif c.get("lifetime_budget"):
            b = f"{int(c['lifetime_budget']) / 100:.0f} total"
        else:
            b = "ad set level"
        mine = "yes" if c["id"] in known else "**no**"
        rows.append(f"| {c['name']} | {c['status']} | {c['effective_status']} | {b} | {mine} |")
    return "\n".join(rows)


def report(days):
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    until = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    rows = get_all(f"{act()}/insights", {
        "level": "campaign",
        "fields": "campaign_id,campaign_name,spend,impressions,reach,frequency,clicks,cpm,actions,video_thruplay_watched_actions",
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
    lines += ["", f"Total spend: {total:.2f} €", ""]
    known = known_campaign_ids()
    strangers = [c for c in get_all(f"{act()}/campaigns", {"fields": "id,name,effective_status", "limit": 100})
                 if c["effective_status"] == "ACTIVE" and c["id"] not in known]
    if strangers:
        lines.append("**Warning: active campaigns not in ads/:** " + ", ".join(c["name"] for c in strangers))
    summary(lines)


def to_unix(when):
    if isinstance(when, str):
        when = dt.datetime.fromisoformat(when)
    return int(when.timestamp())


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
            "targeting_automation": {"advantage_audience": 0},
        }
        r = api("POST", f"{act()}/adsets", {
            "name": f"{ad['name']} ad set",
            "campaign_id": ids["campaign"],
            "status": "PAUSED",
            "lifetime_budget": int(round(float(ad["budget_eur"]) * 100)),
            "start_time": to_unix(ad["start"]),
            "end_time": to_unix(ad["end"]),
            "billing_event": "IMPRESSIONS",
            "optimization_goal": ad.get("optimization_goal", "THRUPLAY"),
            "destination_type": ad.get("destination_type", "ON_VIDEO"),
            "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
            "targeting": targeting,
        })
        ids["adset"] = r["id"]
        save(ad)
        print("ad set", r["id"])

    if not ids.get("ad"):
        post = str(ad["fb_post_id"])
        story = post if "_" in post else f"{page_id}_{post}"
        cr = api("POST", f"{act()}/adcreatives", {"name": f"{ad['name']} creative", "object_story_id": story})
        r = api("POST", f"{act()}/ads", {
            "name": f"{ad['name']} ad",
            "adset_id": ids["adset"],
            "creative": {"creative_id": cr["id"]},
            "status": "PAUSED",
        })
        ids["ad"] = r["id"]
        save(ad)
        print("ad", r["id"])
    print(f"{ad_id}: built and PAUSED. Review it in Ads Manager, then set approved: true and run activate.")


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
    order = ["ad", "adset", "campaign"] if status == "PAUSED" else ["campaign", "adset", "ad"]
    for k in order:
        if ids.get(k):
            api("POST", str(ids[k]), {"status": status})
            print(f"{k} {ids[k]} -> {status}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["check", "report", "create", "pause", "activate"])
    ap.add_argument("--ad", default="")
    ap.add_argument("--days", type=int, default=7)
    a = ap.parse_args()
    if a.action == "check":
        check()
    elif a.action == "report":
        report(a.days)
    else:
        if not a.ad:
            sys.exit("--ad is required")
        if a.action == "create":
            create(a.ad)
        else:
            set_status(a.ad, "PAUSED" if a.action == "pause" else "ACTIVE")


if __name__ == "__main__":
    main()

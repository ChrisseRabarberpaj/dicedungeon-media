#!/usr/bin/env python3
"""Queue approved Dice Dungeon posts in Buffer.

Reads posts/*.yml. A post is sent only when `approved: true`, and only to
channels it has not already been queued on (tracked in `queued:`). Media are
referenced by their raw.githubusercontent.com URL, so the files must be
committed and pushed to main before the run.

Env:
  BUFFER_API_KEY     required (GitHub secret, never committed)
  GITHUB_REPOSITORY  owner/repo, set automatically in Actions
  MEDIA_REF          branch to build raw URLs from (default: main)

Usage:
  python scripts/queue_posts.py --dry-run          # show what would be sent
  python scripts/queue_posts.py                    # send approved posts
  python scripts/queue_posts.py --post 000a-comeback
  python scripts/queue_posts.py --introspect       # print Buffer input schema
  python scripts/queue_posts.py --channels         # list Buffer channel ids
"""
import argparse
import datetime as dt
import glob
import json
import os
import sys
import urllib.request

import yaml

API = "https://api.buffer.com"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIDEO_EXT = (".mp4", ".mov", ".m4v")


def gql(query, variables=None):
    key = os.environ.get("BUFFER_API_KEY")
    if not key:
        sys.exit("BUFFER_API_KEY is not set")
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        API,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.load(r)
    if data.get("errors"):
        raise RuntimeError(json.dumps(data["errors"], indent=2))
    return data["data"]


def raw_url(path):
    repo = os.environ.get("GITHUB_REPOSITORY", "ChrisseRabarberpaj/dicedungeon-media")
    ref = os.environ.get("MEDIA_REF", "main")
    return f"https://raw.githubusercontent.com/{repo}/{ref}/{path}"


def to_utc(when):
    if isinstance(when, dt.datetime):
        t = when
    else:
        t = dt.datetime.fromisoformat(str(when))
    if t.tzinfo is None:
        sys.exit(f"'when' needs a UTC offset, e.g. 2026-10-19T18:00:00+03:00 (got {when})")
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def build_input(post, channel_name, channel_id):
    media = post.get("media") or []
    assets = []
    for path in media:
        if not os.path.exists(os.path.join(ROOT, path)):
            sys.exit(f"{post['id']}: media file missing: {path}")
        kind = "video" if path.lower().endswith(VIDEO_EXT) else "image"
        assets.append({kind: {"url": raw_url(path)}})

    inp = {
        "text": post["text"].strip(),
        "channelId": channel_id,
        "schedulingType": "automatic",
        "needsApproval": False,
        "assets": assets,
    }
    when = post.get("when", "queue")
    if when == "queue":
        inp["mode"] = "addToQueue"
    else:
        inp["mode"] = "customScheduled"
        inp["dueAt"] = to_utc(when)
    has_video = any(list(a)[0] == "video" for a in assets)
    defaults = {
        "instagram": {"type": "reel" if has_video else "post", "shouldShareToFeed": True},
        "facebook": {"type": "reel" if has_video else "post"},
    }
    meta = {**defaults.get(channel_name, {}), **((post.get("metadata") or {}).get(channel_name) or {})}
    if meta:
        inp["metadata"] = {channel_name: meta}
    return inp


CREATE = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess { post { id dueAt } }
    ... on MutationError { message }
  }
}
"""


def load_posts(only=None):
    posts = []
    for f in sorted(glob.glob(os.path.join(ROOT, "posts", "*.yml"))):
        with open(f) as fh:
            p = yaml.safe_load(fh)
        p["_file"] = f
        if only and p["id"] != only:
            continue
        posts.append(p)
    return posts


def _block(dumper, s):
    return dumper.represent_scalar("tag:yaml.org,2002:str", s, style="|" if "\n" in s else None)


yaml.add_representer(str, _block, Dumper=yaml.SafeDumper)


def save(post):
    data = {k: v for k, v in post.items() if not k.startswith("_")}
    with open(post["_file"], "w") as fh:
        fh.write("# Set approved: true (or tell Claude 'go') to let the workflow queue it.\n")
        yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True, width=1000)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--post")
    ap.add_argument("--introspect", action="store_true")
    ap.add_argument("--channels", action="store_true")
    a = ap.parse_args()

    if a.introspect:
        names = ["CreatePostInput", "InstagramPostMetadataInput", "FacebookPostMetadataInput",
                 "TikTokPostMetadataInput", "ShareMode", "SchedulingType", "PostType", "PostTypeFacebook"]
        for n in names:
            q = """query($n:String!){ __type(name:$n){ kind enumValues{ name }
                   inputFields{ name defaultValue type{ name kind ofType{ name kind ofType{ name } } } } } }"""
            t = gql(q, {"n": n})["__type"]
            if not t:
                print(f"{n}: not found")
                continue
            if t.get("enumValues"):
                print(f"{n}: " + ", ".join(e["name"] for e in t["enumValues"]))
                continue
            print(n)
            for f in t["inputFields"]:
                ty = f["type"]
                parts = []
                while ty:
                    parts.append(ty.get("name") or ty.get("kind"))
                    ty = ty.get("ofType")
                print(f"  {f['name']}: {'/'.join(p for p in parts if p)} default={f['defaultValue']}")
        return
    if a.channels:
        orgs = gql("{ account { organizations { id name } } }")["account"]["organizations"]
        for o in orgs:
            q = "query($id: OrganizationId!){ channels(input:{organizationId:$id}){ id name service } }"
            try:
                ch = gql(q, {"id": o["id"]})["channels"]
            except RuntimeError:
                ch = gql("{ channels(input:{organizationId:\"%s\"}){ id name service } }" % o["id"])["channels"]
            print(f"org {o['name']} ({o['id']})")
            for c in ch:
                print(f"  {c['service']:<10} {c['id']}  {c['name']}")
        return

    with open(os.path.join(ROOT, "config", "channels.yml")) as fh:
        channels = yaml.safe_load(fh)

    sent = 0
    for post in load_posts(a.post):
        if not post.get("approved"):
            print(f"skip {post['id']}: not approved")
            continue
        if not post.get("queued"):
            post["queued"] = {}
        if not post.get("media") and set(post["channels"]) & {"instagram", "tiktok"}:
            print(f"skip {post['id']}: Instagram/TikTok need media, none set")
            continue
        for name in post["channels"]:
            if name in post["queued"]:
                print(f"skip {post['id']} -> {name}: already queued ({post['queued'][name]})")
                continue
            cid = channels.get(name)
            if not cid:
                print(f"skip {post['id']} -> {name}: no channel id in config/channels.yml")
                continue
            inp = build_input(post, name, cid)
            if a.dry_run:
                print(f"DRY {post['id']} -> {name}:\n{json.dumps(inp, indent=2, ensure_ascii=False)}")
                continue
            res = gql(CREATE, {"input": inp})["createPost"]
            if "post" in res:
                post["queued"][name] = res["post"]["id"]
                print(f"queued {post['id']} -> {name}: {res['post']['id']} due {res['post'].get('dueAt')}")
                sent += 1
                save(post)
            else:
                print(f"ERROR {post['id']} -> {name}: {res.get('message')}")
    print(f"done, {sent} sent")


if __name__ == "__main__":
    main()

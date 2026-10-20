# dicedungeon-media

Public media host and post queue for Dice Dungeon's social channels (Buffer).

## How a post goes out

1. Each post is a file in `posts/`: text, channels, time (`when`, with UTC offset, or `queue`), and media paths under `media/`.
2. Every post starts as `approved: false`. Nothing is sent until it says `approved: true`.
3. Actions → "Queue approved posts in Buffer" → Run workflow. Pick dry-run (default) to preview, send to queue.
4. The workflow fills `queued:` with Buffer post ids, so re-running never double-posts.

On a daily schedule (08:15 Helsinki) the workflow also moves approved posts into free Buffer slots, since the free plan holds 10 scheduled posts per channel.

Media are served to Buffer from `raw.githubusercontent.com`, which is why this repo is public. Nothing private goes in here.

The Buffer key lives only in the repo secret `BUFFER_API_KEY`.

LinkedIn posts (`li-*`) have no Buffer channel; post those by hand.

## Meta ads

Ads live in `ads/*.yml` (template: `ads/_example.yml.txt`). Actions → "Meta ads":
`check` and `report` only read; `create` builds the campaign, ad set and ad PAUSED;
`activate` turns one on only if its file says `approved: true`; `pause` always works.
A report runs every Monday and flags any active campaign that isn't in `ads/`.

The token lives only in the repo secret `META_ACCESS_TOKEN` (a system-user token
from Talecraft's business portfolio). Account and page ids are in `config/meta.yml`.

# dicedungeon-media

Public media host and post queue for Dice Dungeon's social channels (Buffer).

## How a post goes out

1. Each post is a file in `posts/`: text, channels, time (`when`, with UTC offset, or `queue`), and media paths under `media/`.
2. Every post starts as `approved: false`. Nothing is sent until it says `approved: true`.
3. Actions → "Queue approved posts in Buffer" → Run workflow. Dry run is on by default; untick it to send.
4. The workflow fills `queued:` with Buffer post ids, so re-running never double-posts.

Media are served to Buffer from `raw.githubusercontent.com`, which is why this repo is public. Nothing private goes in here.

The Buffer key lives only in the repo secret `BUFFER_API_KEY`.

LinkedIn posts (`li-*`) have no Buffer channel; post those by hand.

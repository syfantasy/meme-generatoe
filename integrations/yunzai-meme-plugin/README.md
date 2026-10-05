# Yunzai `meme.js` client for this image

Drop-in plugin that talks to this image's API and uses the pre-rendered static
list when it is available.

- Install: copy `meme.js` to `plugins/example/meme.js` of your Miao-Yunzai /
  Yunzai checkout (overwriting the upstream copy), then restart the bot.
- `baseUrl` at the top of the file points at the deployment
  (`https://meme-generatoe-sha.onrender.com`). Change it if you rename the
  service.
- Commands: `#meme列表`, `#meme列表 2`, `#meme搜索 <关键词>`, `#meme更新`,
  `#<表情名称>` (same as upstream, plus the static list fast path).

## How the list is served

1. `#meme列表` fetches `/memes/static/list/manifest.json`.
2. The manifest is used only when its `pageSize` matches the plugin's
   `MEME_LIST_PAGE_SIZE` (default `200`) **and** the meme key of every page adds
   up exactly to the keys the plugin sees in `/memes/static/infos.json`.
3. Each page is fetched from `/memes/static/list/p<N>-<version>.png` and cached
   locally under `data/memes/`, so the list renders instantly.
4. Anything that does not match — stale manifest, different page size, unreachable
   file, invalid image — falls back to `POST /memes/render_list`, i.e. the
   original on-demand behaviour. The command never hard-fails because of the
   static path.

# 前端接口使用说明（外网入口 http://114.119.188.177:8000）

## 0. 服务入口
- Flask 服务: `http://114.119.188.177:8000`
- REST 返回 JSON; 错误统一 `{ "error": "...", "message": "..." }`
- WebSocket:
  - `ws://114.119.188.177:8000/ws/hotlist`
  - 亦可运行 `python spider/hot_topics_ws.py --host 0.0.0.0 --port 8765`, 前端连 `ws://114.119.188.177:8765/?limit=30`
- 文档面向前端; 爬虫与调度逻辑由 backend/spider 自动执行。

## 1. 热榜（小时级）HTTP 接口
**URL** `GET /api/hot_topics/hourly`
**参数** `date`, `hour` (可选), `limit`(1-50)
- 响应始终含 `rank`, 即便原始 JSON 未写; 读取时已自动补齐。
- `source_path` 指向快照文件, 便于排查。
- 传入 `limit` 会附带 `requested_limit`。

```http
GET http://114.119.188.177:8000/api/hot_topics/hourly?date=2025-11-12&hour=7&limit=20
```

```json
{
  "date": "2025-11-12",
  "hour": 7,
  "generated_at": "2025-11-12T07:05:08+08:00",
  "total": 50,
  "topics": [
    {
      "rank": 1,
      "title": "示例话题 A",
      "category": "社会",
      "description": "官方摘要或抓取到的简介",
      "url": "https://s.weibo.com/weibo?q=%23%E7%A4%BA%E4%BE%8B%E8%AF%9D%E9%A2%98A%23",
      "hot": 812345,
      "ads": false,
      "readCount": 6312000,
      "discussCount": 9800,
      "origin": 214
    }
  ],
  "source_path": "data/hot_topics/hourly/2025-11-12/07.json",
  "requested_limit": 20
}
```

## 2. 热榜 WebSocket
- `ws://114.119.188.177:8000/ws/hotlist` 用于实时推送。
- `ws://114.119.188.177:8765/?limit=30` 为 spider/hot_topics_ws.py 独立服务。
- 消息类型: `snapshot` (初次或按需) 与 `update` (新快照)。
- payload 与 HTTP 相同, 含 `rank/date/hour/source_path`。
> 建议将 `date/hour/rank/title/slug` 缓存在每个行项里, 供后续调用帖子或 AI Card。

## 3. 话题帖子接口
**URL** `GET /api/hot_topics/posts`
- 必填: `date`
- 话题定位三选一: `slug` / `title` / `rank` (配合热榜 `date/hour`; 缺失时后端尝试推断)
- 可选: `hour`, `limit`(1-50)
- `items[].created_at` 统一为 `YY-MM-DD HH:MM` (北京时间)。
- 响应附带 `slug` 与 `source_path` (data/posts/<date>/<slug>.json)。
- 首次 404 代表帖子尚在抓取; 接口会触发补抓, 可提示稍后重试。

```http
GET http://114.119.188.177:8000/api/hot_topics/posts?date=2025-11-12&rank=1&limit=20
```

```json
{
  "date": "2025-11-12",
  "slug": "shili-huati-a",
  "title": "示例话题 A",
  "fetched_at": "2025-11-12T07:02:33+08:00",
  "total": 38,
  "items": [
    {
      "id": "weibo-1234567890",
      "url": "https://weibo.com/1234567890",
      "created_at": "25-11-12 06:58",
      "user_name": "微博用户A",
      "text": "正文内容...",
      "reposts": 120,
      "comments": 45,
      "likes": 560,
      "pics": [],
      "video": null,
      "score": 123.4
    }
  ],
  "source_path": "data/posts/2025-11-12/shili-huati-a.json",
  "requested_limit": 20
}
```

## 4. AI Card 接口
**URL** `GET /api/hot_topics/aicard`
- `date`: 必填或可推断
- 话题定位: `slug` / `title` / `rank`
- `hour`: 可选; 若无法推断则返回 400。
- 若归档中没有 AI Card, 接口会调用 ensure_aicard_snapshot 实时生成; 限流时返回 429, 响应包含 `retry_after`。
- 返回 `markdown` 与/或 `html`, 可按优先级渲染。

```http
GET http://114.119.188.177:8000/api/hot_topics/aicard?date=2025-11-12&rank=1
```

```json
{
  "date": "2025-11-12",
  "hour": 7,
  "slug": "shili-huati-a",
  "title": "示例话题 A",
  "markdown_path": "data/aicard/hourly/2025-11-12/07/shili-huati-a.md",
  "markdown": "# 示例 AI Card\n1. 亮点总结...",
  "html_path": null,
  "html": "<article><h1>示例话题 A</h1><p>...</p></article>",
  "meta": { "query": "#示例话题A#", "fetched_at": "2025-11-12T07:01:02+08:00" },
  "links": [],
  "media": [],
  "fetched_at": "2025-11-12T07:01:02+08:00",
  "first_seen": "2025-11-12T07:00:00+08:00",
  "last_seen": "2025-11-12T07:00:00+08:00"
}
```

## 5. 每日整合 (风险/BI)
**URL** `GET /api/hot_topics/daily_bundle`
- 参数: `date` (必填), `include_posts` (默认 true)
- 返回整日归档; `include_posts=true` 时 `topics[].latest_posts` 会附帖子。
- `source_path` 指向 `data/daily_bundles/<date>/topics_with_posts.json`

```http
GET http://114.119.188.177:8000/api/hot_topics/daily_bundle?date=2025-11-12&include_posts=true
```

## 6. 前端串联建议
1. 初始化请求 `/api/hot_topics/hourly` 或监听 WebSocket 渲染热榜。
2. 将 `date/hour/rank/title/slug` 缓存在每行对象里。
3. 点击话题后, 并发请求:
   - `/api/hot_topics/posts?date=...&rank=...`
   - `/api/hot_topics/aicard?date=...&rank=...`
4. 渲染 `posts.items` 作为帖文列表, 将 `aicard.markdown` (优先) 或 `aicard.html` 展示在 AI Card 面板。
5. 响应返回的 `slug` 记入前端状态, 供后续刷新或分享使用。
6. 如需整日数据, 后端可按需调用 `/api/hot_topics/daily_bundle`。

## 7. slug 处理
- 后端默认使用 slugify_title: 将标题中非字母数字替换成 `-` 并转小写; 若为空则退回 `topic-${md5(title)[:8]}`。
- 建议前端直接使用接口返回的 `slug`。若必须本地生成, 可复用下面逻辑:
  ```js
  function slugifyTitle(title) {
    const cleaned = (title || '')
      .replace(/[^0-9a-zA-Z]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .toLowerCase();
    if (cleaned) return cleaned;
    return `topic-${md5(title).slice(0, 8)}`; // 需要时实现 md5
  }
  ```

## 8. 常见错误码
- `400 invalid date/hour/rank`: 参数格式错误
- `400 slug, title, or rank must be provided`: 帖子/AI Card 缺定位
- `404 Hourly snapshot not available / Topic posts not available / AI card not available`: 数据尚未生成, 稍后重试
- `429 ai_card_cooldown / ai_card_rate_limited`: AI Card 平台限流, 根据 `retry_after` 重试
- 其他 5xx: 根据 `source_path` 检查对应 JSON, 或查看后台日志

## 9. 数据刷新与缓存提示
- 监控任务约每 10 分钟写入 `data/hot_topics/hourly/<date>/<hour>.json` 并推送 WebSocket。
- 帖子在归档阶段与接口调用时都会刷新; `needs_refresh=true` 会被后台自动补抓。
- AI Card 归档时生成; 缺失时接口会实时写入 `data/aicard/hourly/...`。
- 30 天热度趋势可调 `/api/hot_topics/daily_heat?limit=30`。

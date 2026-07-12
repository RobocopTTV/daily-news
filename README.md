# 新闻日报(News Digest)

每天自动抓取免费新闻源,按 **政治 / 经济 / 股票 / 科技 / AI / 体育** 六大主题(聚焦 **美国 / 中国 / 澳洲**)生成中文摘要 + 简评的 HTML 日报。

参考了 [TrendRadar](https://github.com/sansan0/TrendRadar) 的思路,但更轻量:单个 Python 脚本、零第三方依赖、全免费。

## 工作原理

```
Google News RSS + ABC News RSS  →  筛选去重(每主题 5 条,24-36 小时内)
        →  Gemini API(免费额度)翻译成中文、写摘要和简评、生成今日综述
        →  单文件 HTML 日报(reports/YYYY-MM-DD.html + latest.html)
        →  同时输出 reports/latest.json(结构化数据)
        →  GitHub Actions 每天悉尼时间 6:30 自动运行并提交
```

配合 Claude Cowork 的定时任务使用时:Actions 6:30 先抓好数据,Claude 7 点通过
GitHub API 拉取 `reports/latest.json`,自己撰写更高质量的中文摘要和解读,并把
日报存到本地 news 文件夹。此时 `GEMINI_API_KEY` 可以不配置(Claude 会自己写),
配置了则 GitHub Pages 上的 HTML 版本也带中文摘要。

## 部署到 GitHub(约 10 分钟,全程免费)

1. **建仓库**:GitHub 上新建一个仓库(private 即可),把本文件夹里的所有文件上传(注意 `.github/workflows/daily-digest.yml` 的目录结构要保留)。

2. **(可选)拿一个免费的 Gemini API Key**:
   - 打开 [Google AI Studio](https://aistudio.google.com/apikey),用 Google 账号登录,点 "Create API key"。
   - 免费额度对每天一次的摘要任务绰绰有余。
   - 只搭配 Claude 定时任务使用的话可跳过这步(Claude 会自己写摘要)。

3. **配置 Secret**(做了第 2 步才需要):仓库页面 → Settings → Secrets and variables → Actions → New repository secret,名称填 `GEMINI_API_KEY`,值填上一步的 key。

4. **启用并测试**:仓库页面 → Actions 标签页 → 启用 workflows → 选 "Daily news digest" → "Run workflow" 手动跑一次。成功后 `reports/` 目录下会出现当天的 HTML 和 latest.json。

5. **接回 Claude**:把仓库名(如 `felix/news-digest`)告诉 Claude,让它填进每日定时任务里。

之后每天悉尼时间 6:30 左右 Actions 自动抓数据(GitHub 的 cron 偶尔延迟几分钟到几十分钟,属正常现象),7 点 Claude 定时任务生成本地日报。

## 本地运行

```bash
# 完整运行(需要网络;不设 key 则只有英文标题没有中文摘要)
set GEMINI_API_KEY=你的key        # Windows CMD
python news_digest.py

# 离线冒烟测试(不联网,用内置样例数据验证 HTML 生成)
python news_digest.py --test
```

报告输出在 `reports/` 目录。

## 自定义

都在 `news_digest.py` 顶部的配置区:

| 配置项 | 说明 |
|---|---|
| `FEEDS` | 新闻源列表。可增删 RSS 地址,Google News 搜索源格式:`https://news.google.com/rss/search?q=关键词+when:1d&hl=en-US&gl=US&ceid=US:en` |
| `MAX_PER_CATEGORY` | 每个主题保留几条(默认 5) |
| `MAX_ITEM_AGE_HOURS` | 只保留多少小时内的新闻(默认 36) |
| `GEMINI_MODEL` | 默认 `gemini-2.0-flash`,免费额度内速度和质量的平衡之选 |
| `REPORT_TIMEZONE` | 报告日期使用的时区(默认悉尼) |

**改运行时间**:编辑 `.github/workflows/daily-digest.yml` 里的 cron。注意 GitHub 用 UTC:悉尼冬令时(4-10月)6:30 = `30 20 * * *`,夏令时(10-4月)6:30 = `30 19 * * *`。

## 用 GitHub Pages 在线看报告(可选)

仓库 → Settings → Pages → Source 选 `main` 分支根目录,保存。之后访问:

```
https://你的用户名.github.io/仓库名/reports/latest.html
```

即可随时在手机/电脑浏览器看最新日报(private 仓库需要 GitHub 付费版才能开 Pages,public 仓库免费)。

## 常见问题

- **某些新闻源偶尔失败?** 正常,脚本会跳过失败的源继续运行,不会中断。
- **没有中文摘要?** 检查 `GEMINI_API_KEY` secret 是否配置正确;报告顶部会有黄色提示条说明原因。
- **想收邮件推送?** 可以在 workflow 里加一步邮件 action(如 `dawidd6/action-send-mail`),或改用功能更全的 [TrendRadar](https://github.com/sansan0/TrendRadar)(支持微信/钉钉/Telegram 推送)。

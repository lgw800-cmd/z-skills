---
name: 1-web-pack
description: 当用户提供一个或多个同主题网页链接，并要求“采集网页素材”“把链接正文拿到本地”“正文相关链接也下载”“配图保存到本地”“视频也下载下来”“做成备用写作素材包”“1-web-pack”时必须使用。会逐个阅读入口链接正文和正文内相关链接，排除侧边栏、页脚、广告、社交分享等低价值区域，把 Markdown、链接清单、阅读地图、图片和视频资源保存到 Clippings/Reading；支持懒加载图片、srcset 高清档、防盗链 Referer；正文 <video> 直链视频默认下载，YouTube/B站等平台视频可用 yt-dlp 下载；常规抓取失败后才允许使用 r.jina.ai 兜底。
---

# 网页素材包采集

把若干同主题链接整理成可直接供后续写文章使用的本地资料包。

## 依赖说明

- 必须用 miniconda 的 Python 运行（系统 python3 缺 readability-lxml）：`/Users/zz/miniconda3/bin/python3`
- 本 skill 已随包携带基础采集模块：`scripts/collect_web_research_pack.py`
- 平台视频下载依赖 `yt-dlp`（已安装于 `/Users/zz/miniconda3/bin/yt-dlp`）

## 输出目录

默认保存到：

```bash
/Users/zz/Library/Mobile Documents/iCloud~md~obsidian/Documents/zhangAI/Clippings/Reading/
```

每次任务创建独立文件夹：

```text
YYYY-MM-DD-主题名/
├── README.md
├── 00-research-brief.md
├── 01-link-inventory.md
├── 02-image-inventory.md
├── 03-reading-map.md
├── 04-media-inventory.md
├── MAIN-01-入口正文.md
├── LINKED-02-正文相关链接.md
└── assets/
```

## 完成标准

1. 每个用户入口链接都要生成 `MAIN-*.md`
2. 入口正文里的相关链接要尽量展开成 `LINKED-*.md`
3. 只采正文、正文表格、正文图片、正文代码和正文里的相关链接
4. 跳过侧边栏、页脚、广告、登录、订阅、招聘、隐私政策、服务条款、社交分享链接
5. 图片下载到 `assets/`，Markdown 中使用本地相对路径；懒加载图取真实地址，srcset 取最大档，badge/tracking 像素自动跳过
6. 正文 `<video>` 与直链视频默认下载到 `assets/`（`--videos direct`）；平台视频在用户要求时用 `--videos all` 下载
7. 生成 `README.md`、`00-research-brief.md`、`01-link-inventory.md`、`02-image-inventory.md`、`03-reading-map.md`、`04-media-inventory.md`
8. 记录失败、受限、跳过和兜底情况
9. 收尾检查不得留下非 `r2blog.zhanglearning.com` / `r2.zhanglearning.com` 的外链图片；如果留下，必须调用 `1-upload-images-to-picgo`

## 推荐命令

```bash
/Users/zz/miniconda3/bin/python3 .agent/skills/1-web-pack/scripts/collect_web_pack.py \
  --out-root "/Users/zz/Library/Mobile Documents/iCloud~md~obsidian/Documents/zhangAI/Clippings/Reading" \
  --title "主题名" \
  --max-depth 1 \
  --max-pages 80 \
  "https://example.com/a" \
  "https://example.com/b"
```

注意：脚本路径是相对项目根的，运行前先 `cd` 到 zhangAI 目录，或改用绝对路径。

参数建议：

- `--max-depth 1`：入口链接 + 入口正文相关链接
- `--max-depth 2`：用户明确要求尽量深挖时使用
- `--max-pages 40`：普通主题
- `--max-pages 80`：多入口或资料密集主题
- `--same-domain-only`：只采同域资料时使用
- `--no-jina`：调试时禁用 `r.jina.ai` 兜底
- `--videos direct`：默认值，只下载 `<video>` 标签和正文直链视频（mp4/webm 等）
- `--videos all`：用户要求"视频也下载"时使用，YouTube/B站/Vimeo/X/抖音页面和 m3u8 走 yt-dlp
- `--videos off`：明确不要视频时使用
- `--max-video-mb 300`：单个视频大小上限，默认 300MB
- `--max-image-mb 20`：单张图片大小上限，默认 20MB
- `--browser-cookies chrome`：平台视频报"Sign in / 412 / bot 验证"时，从本机浏览器带 cookie 重试（可选 chrome / safari / edge / firefox）

## 抓取顺序

每个页面按这个顺序尝试：

1. GitHub repo/blob 链接优先走 GitHub API / raw / README（已内置实现）
2. 常规 HTTP 抓取正文（图片带 Referer 防盗链）
3. Markdown、JSON、纯文本资源直接保存
4. 如果失败、受限、正文明显为空或只抓到登录提示，再使用 `r.jina.ai` 兜底

`r.jina.ai` 只能作为兜底。不要一开始就用它。

## 图片采集能力

- 懒加载：自动识别 `data-src` / `data-original` / `data-lazy-src` / `data-actualsrc` / `data-echo`，跳过 base64 占位图
- 响应式：`srcset` / `<picture><source>` 自动选最大宽度档
- 防盗链：所有图片请求带页面 `Referer`
- 纠错：按文件魔数（magic bytes）纠正扩展名，CDN 给错 Content-Type 也能存对
- 去重：相同内容（sha256）的图片只存一份
- 过滤：1x1 tracking 像素、shields.io badge、favicon 等装饰图自动跳过

## 视频采集能力

- `direct`（默认）：页面 `<video>` / `<source>` / 正文直链 `.mp4/.webm/.mov` 等流式下载，超过 `--max-video-mb` 自动放弃
- `all`：入口或正文里的 YouTube / B站 / Vimeo / X / 抖音 / m3u8 用 yt-dlp 下载（1080p 封顶，合并为 mp4）
- 入口链接本身是视频页时（如直接给 B 站链接），即使正文抓取失败也会尝试下载视频
- 下载成功的视频会在对应 Markdown 末尾生成"本页视频"小节，正文中的直链同步替换为本地路径
- 平台风控（YouTube bot 验证、B站 412）→ 加 `--browser-cookies chrome` 重试；仍失败则在 `04-media-inventory.md` 记录原因，不阻塞整体任务

## 相关链接判断

优先展开：

- 官方文档、博客、论文、模型卡、仓库、README、release、issue、PR
- 与主题直接相关的 benchmark、评测、数据表、cookbook、示例代码
- 正文里用于支撑核心观点的数据源、图表源、产品页

跳过：

- 导航菜单、页脚、广告位、推荐阅读区里的泛链接
- 登录、注册、订阅、招聘、隐私、条款、Cookie
- 分享到 X / LinkedIn / Facebook 等社交分享链接
- logo、favicon、头像、装饰图、徽章

## 收尾检查

交付前至少运行：

```bash
find "资料包目录" -maxdepth 2 -type f | sort
rg -n '!\[[^]]*\]\(https?://' "资料包目录" || true
find "资料包目录" -maxdepth 1 -name 'MAIN-*.md' -print
test -f "资料包目录/03-reading-map.md" && sed -n '1,120p' "资料包目录/03-reading-map.md"
find "资料包目录/assets" -type f | wc -l
test -f "资料包目录/04-media-inventory.md" && grep -c '^| ' "资料包目录/04-media-inventory.md"
```

最终回复说明：

- `1-web-pack` skill 的路径
- 本次资料包路径
- Markdown 数量、主文数量、关联资料数量、图片数量、视频数量
- 失败、受限或使用 Jina 兜底的链接
- 平台视频未下载时给出原因与 `--browser-cookies` 提示

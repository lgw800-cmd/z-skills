---
name: 1-web-pack
description: 当用户提供一个或多个同主题网页链接，并要求“采集网页素材”“把链接正文拿到本地”“正文相关链接也下载”“配图保存到本地”“做成备用写作素材包”“1-web-pack”时必须使用。会逐个阅读入口链接正文和正文内相关链接，排除侧边栏、页脚、广告、社交分享等低价值区域，把 Markdown、链接清单、阅读地图和图片资源保存到 Clippings/Reading；常规抓取失败后才允许使用 r.jina.ai 兜底。
---

# 网页素材包采集

把若干同主题链接整理成可直接供后续写文章使用的本地资料包。

## 输出目录

默认保存到：

```bash
<你的项目路径>/Clippings/Reading/
```

每次任务创建独立文件夹：

```text
YYYY-MM-DD-主题名/
├── README.md
├── 00-research-brief.md
├── 01-link-inventory.md
├── 02-image-inventory.md
├── 03-reading-map.md
├── MAIN-01-入口正文.md
├── LINKED-02-正文相关链接.md
└── assets/
```

## 完成标准

1. 每个用户入口链接都要生成 `MAIN-*.md`
2. 入口正文里的相关链接要尽量展开成 `LINKED-*.md`
3. 只采正文、正文表格、正文图片、正文代码和正文里的相关链接
4. 跳过侧边栏、页脚、广告、登录、订阅、招聘、隐私政策、服务条款、社交分享链接
5. 图片下载到 `assets/`，Markdown 中使用本地相对路径
6. 生成 `README.md`、`00-research-brief.md`、`01-link-inventory.md`、`02-image-inventory.md`、`03-reading-map.md`
7. 记录失败、受限、跳过和兜底情况
8. 收尾检查不得留下非 `r2blog.zhanglearning.com` / `r2.zhanglearning.com` 的外链图片；如果留下，必须调用 `1-upload-images-to-picgo`

## 推荐命令

```bash
python3 .agent/skills/1-web-pack/scripts/collect_web_pack.py \
  --out-root "<你的项目路径>/Clippings/Reading" \
  --title "主题名" \
  --max-depth 1 \
  --max-pages 80 \
  "https://example.com/a" \
  "https://example.com/b"
```

参数建议：

- `--max-depth 1`：入口链接 + 入口正文相关链接
- `--max-depth 2`：用户明确要求尽量深挖时使用
- `--max-pages 40`：普通主题
- `--max-pages 80`：多入口或资料密集主题
- `--same-domain-only`：只采同域资料时使用
- `--no-jina`：调试时禁用 `r.jina.ai` 兜底

## 抓取顺序

每个页面按这个顺序尝试：

1. 常规 HTTP 抓取正文
2. GitHub 链接优先走 GitHub API / raw / README
3. Markdown、JSON、纯文本资源直接保存
4. 如果失败、受限、正文明显为空或只抓到登录提示，再使用 `r.jina.ai` 兜底

`r.jina.ai` 只能作为兜底。不要一开始就用它。

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
```

最终回复说明：

- `1-web-pack` skill 的路径
- 本次资料包路径
- Markdown 数量、主文数量、关联资料数量、图片数量
- 失败、受限或使用 Jina 兜底的链接

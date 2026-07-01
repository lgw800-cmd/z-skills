# z-skills

`z-skills` 是一组可复用的本地 Agent Skills，用来把常见工作流沉淀成稳定能力：网页素材采集、视频下载、视频学习网页、文档解析、邮件读取、表格处理、Markdown 转 Word、概念拆解，以及文章四格漫画配图

这些 skill 默认面向中文创作、知识管理和自动化任务，适合放到本地 `.agent/skills/` 或 Codex/Claude Code 等支持 Skills 的环境里使用

## Skills 一览

| Skill | 用途 | 典型触发 |
| --- | --- | --- |
| `z-web-pack` | 采集网页正文、链接、图片和视频链接，整理成本地写作素材包 | 采集网页素材、把链接正文拿到本地、做成备用写作素材包 |
| `z-video-downloader` | 下载 YouTube、B站、m3u8、mp4 直链等视频 | 下载视频、下载 B站、下载 YouTube、下载 m3u8 |
| `z-video-study-webpage-qwen` | 用转录、关键帧和 Qwen 多模态分析视频，生成图文学习网页 | 理解视频内容、视频学习总结网页、关键知识点匹配画面 |
| `z-smart-xparse` | 用 xparse-cli 把 PDF、图片、Office 等文档转成 Markdown 或结构化结果 | 解析 PDF、文档转 Markdown、读取扫描件 |
| `z-mail-reader` | 通过 IMAP 读取邮件、下载附件、摘要邮件内容、监听新邮件 | 读邮件、查收邮件、邮件摘要、监听邮件 |
| `z-md-to-word` | 把本地 Markdown 文章转换成 Word 文档，生成 `.docx` 和 `.doc` 并做打开检查 | 转成doc、Markdown转Word、md转doc、导出Word |
| `z-md-excel` | 把 Markdown 里的表格提取成 Excel 文件 | Markdown 表格转 Excel、导出 MD 表格 |
| `z-excel-editor` | 读取、编辑、清洗、格式化电子表格文件 | 修改 xlsx、清洗 csv、补公式、做表格 |
| `z-father-concept` | 给任意词语或概念寻找父概念、上位概念和更深层解释 | 父概念、给这个词找爸爸、上位概念 |
| `z-xkcd-panda-comic` | 把文章、主题或观点改写成黑白手绘四格熊猫梗图 | 四格漫画、金馆长熊猫表情、金教授熊猫脸、熊猫梗图、文章转四格漫画 |

## z-web-pack 与 z-video-downloader 边界

`z-web-pack` 负责网页素材包采集，包含正文、正文相关链接、图片、本地阅读地图和媒体链接清单

`z-video-downloader` 负责视频下载，包含 YouTube、Bilibili、Vimeo、X/Twitter、TikTok、抖音、Instagram、Facebook、m3u8 和常见视频直链

`z-web-pack` 发现视频时只写入 `04-media-inventory.md`。如果要把视频保存到本地，把清单中的 Source URL 交给 `z-video-downloader`

### 采集网页素材

```bash
/Users/zz/miniconda3/bin/python3 z-web-pack/scripts/collect_web_pack.py \
  --out-root "/Users/zz/Library/Mobile Documents/iCloud~md~obsidian/Documents/zhangAI/Clippings/Reading" \
  --title "主题名" \
  --max-depth 1 \
  --max-pages 80 \
  "https://example.com/article"
```

输出里重点看：

- `README.md`
- `00-research-brief.md`
- `01-link-inventory.md`
- `02-image-inventory.md`
- `03-reading-map.md`
- `04-media-inventory.md`

### 下载视频

```bash
/Users/zz/miniconda3/bin/python3 z-video-downloader/scripts/download_video.py \
  --title "主题名" \
  "https://www.bilibili.com/video/BV..."
```

如果视频链接来自网页素材包，使用 `04-media-inventory.md` 里的 Source URL

## 推荐安装方式

把需要的 skill 目录复制到本地 skills 目录：

```bash
cp -R z-xkcd-panda-comic "/path/to/your/.agent/skills/"
```

如果希望一次性安装全部：

```bash
cp -R z-* "/path/to/your/.agent/skills/"
```

安装后，新会话开始时，Agent 会根据每个 `SKILL.md` 的 `name` 和 `description` 自动匹配触发词

## 新增：Markdown 转 Word Skill

`z-md-to-word` 用来把本地 Markdown 文章转换成 Word 文档，适合公众号文章、Obsidian 笔记、商单稿件和需要交付 `.doc` / `.docx` 的场景。

它会自动处理几个常见问题：

- 保留 Markdown 标题、作者和日期
- 写入远程图片和本地图片
- 跳过空上传占位符
- 修正常见列表识别问题
- 生成后检查文档是否能打开和渲染

默认输出：

```text
output/doc/<原文件名>.docx
output/doc/<原文件名>.doc
```

## 熊猫四格漫画 Skill

`z-xkcd-panda-comic` 用来把文章或主题变成一张 2x2 四格漫画，默认风格是：

- 黑白手绘漫画
- 金馆长熊猫表情味 / 金教授熊猫脸
- 中文短对白
- xkcd 式冷幽默节奏
- 适合插入公众号、Obsidian、Markdown 文章

它会先提炼文章核心观点，再设计四格节奏：

1. 设定痛点或误会
2. 普通办法暴露荒诞
3. 熊猫角色给出解决动作
4. 用一句话收束核心观点

目录内包含一张参考风格图：

```text
z-xkcd-panda-comic/assets/reference-codex-computer-housekeeper-panda-comic.png
```

## 使用建议

- 每个 skill 都以自己的 `SKILL.md` 为准
- 有脚本的 skill 优先使用脚本，避免手工重复操作
- 处理外部文件、邮件、视频和网页时，先确认路径、链接和权限
- 生成文章或图片后，尽量做一次实际查看或运行验证
- 网页素材采集和视频下载分开维护，降低单个 skill 的复杂度
- 视频平台风控、cookie、画质、播放列表等逻辑统一放在 `z-video-downloader`
- `z-web-pack` 的媒体清单只做发现和转交提示，避免采集资料时意外下载大文件

## 目录结构

```text
z-skills/
  z-xkcd-panda-comic/
    SKILL.md
    assets/
    evals/
  z-md-to-word/
    SKILL.md
    README.md
    scripts/
    evals/
  z-video-downloader/
    SKILL.md
    scripts/
    tests/
  z-video-study-webpage-qwen/
    SKILL.md
    scripts/
    tests/
  ...
```

## 维护方式

新增 skill 时建议包含：

- `SKILL.md`：触发词、执行流程、输出规范
- `scripts/`：可复用脚本
- `assets/`：参考图、模板或固定素材
- `evals/`：示例输入和评估用例
- `README.md`：复杂 skill 可单独补充说明

保持触发词清楚、流程可执行、输出可验证，是这个库的核心原则

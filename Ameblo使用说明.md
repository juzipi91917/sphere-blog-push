# 四人 Ameblo 更新检查

脚本：`D:\data\sphere\ameblo_check.py`。与 SPHERE SPACE 的 `main.py` 分开运行，复用当前目录的配置、翻译、COS 上传和 QQ 发送模块，不需要登录 Sphere。

## 使用

```powershell
cd D:\data\sphere
```

检查四人最新博客并预览，不上传图片、不发送：

```powershell
.\.venv\Scripts\python.exe ameblo_check.py
```

检查并推送有更新的人：

```powershell
.\.venv\Scripts\python.exe ameblo_check.py --send
```

默认群号从 `config.json` 的 `qq_group_id` 读取，当前为 `1075662749`。可用 `--group-id 群号` 覆盖，使用 `--no-translation` 禁用翻译。

首次没有历史记录时，每人最新一篇各发送一条。以后有几人更新就发几条，无更新不发送；每次只推送每人的最新一篇，不追补检查间隔中可能遗漏的中间文章。每位作者独立处理，一人失败不影响其他人。程序按需运行一次，没有设置定时任务或后台轮询。

如果仅希望从当前文章建立基线、以后只推送新发表的文章，可以在首次发送前执行：

```powershell
.\.venv\Scripts\python.exe ameblo_check.py --init
```

## 消息内容与多图

每个人的消息包含作者、标题、发布日期、博客原文和可用的中文译文。原文和译文以空行分隔，不添加“正文”“原文”“中文翻译”等标签，末尾不附加文章链接；文章正文自身包含的链接仍保留。翻译失败时保留原文。正文中所有 `PhotoSwipeImage` 照片都按顺序下载原图、上传 COS，以多个图片 CQ 码拼进同一条消息。不限制为第一张，不按图片拆成多条消息。

去除页面中重复的 `noscript` 照片及重复地址，忽略头像、布局图片；表情图保留其替代文字。照片下载或上传失败时，该作者本次不发送正文，也不更新成功记录。Ameblo 的内嵌视频当前保留为链接；这里的单条消息推送范围是正文、译文和照片，Sphere 视频模块仍由 `main.py` 使用。

## 状态与输出

- `data/ameblo_sent.json`：按群和作者保存已成功发送或初始化的最新文章 ID，与 Sphere 缓存分开。列表意外返回更旧 ID 时也不会回退推送。
- `data/ameblo/<作者>/latest.json`：该作者最新抓取内容、译文、全部图片及 COS URL。
- `data/images/<文章ID>/`：下载的原图。
- COS 路径：`ameblo/<作者>/<文章ID>/`。
- `data/delivery.sqlite3`：复用持久化发送日志，幂等键为 `ameblo:<作者>:<文章ID>`，群号另外参与去重。

发送前先记录发送中状态。QQ 返回成功才更新作者缓存。如果送达结果不明确，状态为 `needs_review`，后续不自动重发该条，其他作者继续处理。只有确认未送达后才解除重试限制，例如：

```powershell
.\.venv\Scripts\python.exe delivery.py --group-id 1075662749 --key ameblo:kotobukiminako:文章ID --part 1 --not-sent
```

如果已在群里收到消息，则使用 `--message-id 真实消息ID` 代替 `--not-sent` 补记成功；下次运行会同步作者缓存，不再发一次。

输出每人状态：`sent` 已发送、`unchanged` 无更新、`preview` 预览、`initialized` 建立基线、`needs_review` 待核实、`error` 抓取或准备失败。出现 `error` / `needs_review` 时进程返回码为 1。


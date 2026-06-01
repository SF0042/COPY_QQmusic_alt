# QQMusic TUI

这是从 `qqmusic_for_pc/qqmusic_client.py` 移植出来的终端版客户端。核心逻辑保留了 QQ 音乐和网易云音乐的搜索、登录、播放链接、歌词、歌单、歌单编辑、下载和本地下载索引；新目录里的程序已经删除 GUI 代码，默认直接进入命令行交互。

## 说明

- 不绕过会员、DRM、地区限制、登录限制或付费限制。
- QQ 音乐搜索需要先登录。
- 网易云支持网页登录 Cookie 导入和手机号验证码登录；手机号验证码可能被网易云风控拦截。
- 登录信息保存在本目录的 `.qqmusic_auth.json` 或 `.netease_auth.json`，不要分享给别人。
- 设置保存在本目录的 `.qqmusic_settings.json`。

## 运行前准备

基础功能只依赖 Python 标准库。建议安装一个播放器：

```bash
sudo apt update
sudo apt install python3 vlc
```

也可以使用 `mpv` 或 `ffplay`。程序会按 `python-vlc`、`mpv`、`vlc`、`ffplay`、`xdg-open` 的顺序尝试播放；也可以用环境变量或参数指定播放器：

```bash
QQMUSIC_PLAYER=mpv python3 qqmusic_client.py
python3 qqmusic_client.py --player /usr/bin/mpv
```

网易云手机号验证码登录依赖系统 `openssl` 命令：

```bash
sudo apt install openssl
```

## 启动 TUI

```bash
cd qqmusic_for_tui
python3 qqmusic_client.py
```

进入后输入：

```text
help
```

退出：

```text
quit
```

终端支持用上下方向键切换本次启动后输入过的命令；每次启动都会清空历史。


## 交互命令

```text
推荐流程:
  login qq                       登录
  search 稻香                    搜索歌曲，然后 play 1 播放
  playlist                       查看我的歌单
  playlist 1                     打开第 1 个歌单，然后 play 1 播放
  play playlist 1                打开第 1 个歌单并立刻播放第一首

status                         显示当前平台、账号和播放器
platform [qqmusic|netease]     查看或切换平台
quality [128|320|flac]         查看或设置播放/下载音质
mode [order|random|repeat]      切换或设置播放顺序
login [qq|wx|cookie|phone]     登录
logout                         删除当前平台的本地登录信息
search <关键词> [-n 数量]       搜索歌曲
songs                          显示当前歌曲队列前 100 首
play [歌曲序号]                播放当前队列；不填序号时播放当前/第一首
play playlist <歌单序号>        打开歌单并播放第一首
next | prev                    手动切到下一首/上一首；播完也会自动切歌
pause | resume | stop          控制播放
lyric [序号]                   实时显示歌词；播放结束会自动切歌，Ctrl+C 返回命令行
playlist                       同步并显示我的歌单
playlist <歌单序号|id>         打开歌单到歌曲队列
open <歌单序号|id>             同 playlist <歌单序号|id>
add <歌曲序号> <歌单序号>       把歌曲加入歌单
remove <歌曲序号>              从当前歌单移除歌曲
create <歌单名>                新建歌单
rename <歌单序号> <新名字>     重命名歌单
delete <歌单序号>              删除歌单
download <歌曲序号>            下载歌曲
settings                       显示当前设置 JSON
```

## 登录

QQ 音乐扫码登录：

```text
login qq
```

程序会直接在终端里显示二维码；同时会保存一份 PNG 图片作为备用。

QQ 音乐微信扫码登录：

```text
login wx
```

QQ 音乐 Cookie 导入：

```text
login cookie
```

网易云切换平台后导入 Cookie：

```text
platform netease
login cookie
```

手机号验证码登录：

```text
login phone
```

## 保留的脚本命令

除了交互 TUI，也可以继续用一次性子命令：

```bash
python3 qqmusic_client.py login
python3 qqmusic_client.py search 周杰伦
python3 qqmusic_client.py play 稻香 --wait
python3 qqmusic_client.py lyric 稻香
python3 qqmusic_client.py sync-playlists
python3 qqmusic_client.py logout
```

切换平台：

```bash
python3 qqmusic_client.py --platform netease search 稻香
```

## 下载目录

默认下载到：

```text
~/音乐/Music
```

下载索引保存在下载目录下的 `.qqmusic_downloads.json`，TUI 的 `playlists` 命令会把本地下载列表作为“已下载的歌曲”显示。

# 月下方海（Moonlit Cube Sea）

`qqmusic_for_web_browser` 的另一个前端：月光下由三万个方块组成的海。每个方块是一首歌，选中、特写、播放、看海，全部在一个页面里。

后端直接复用 `qqmusic_for_web_browser/server.py`（登录、歌单、歌曲、歌词接口都没有改），这里的 `server.py` 只是把首页换成月下方海，并多加了两个接口：

- `/api/image`：转发专辑封面，页面才能读取封面颜色（封面贴在方块上、场景色调跟着封面变）。
- `/api/stream`：转发歌曲音频并支持 Range，页面才能拖动进度、分析节拍。

## 启动

Windows（PowerShell）：

```powershell
cd qqmusic_for_moon_cube_sea
python server.py
# 命令不存在时用：py server.py
# 换端口：$env:QQMUSIC_WEB_PORT = "8787"; python server.py
```

macOS / Linux：

```bash
cd qqmusic_for_moon_cube_sea
python3 server.py
# 换端口：QQMUSIC_WEB_PORT=8787 python3 server.py
```

然后打开 `http://127.0.0.1:8765`。

登录信息保存在本文件夹里（`.qqmusic_auth.json`、`.netease_auth.json`、`.qqmusic_settings.json`），不和 `qqmusic_for_web_browser` 共用，已被 `.gitignore` 排除。

## 使用

- 页面右上角的小光点展开菜单：歌单导入、全屏、退场。
- **账号歌单**（需要通过 `server.py` 打开）：QQ / 微信扫码登录，网易云用手机验证码或 Cookie；选一个歌单，歌曲就放到镜头前方的海面上。登录状态和歌单引用会保存在浏览器里，刷新后还在。
- **本地音乐**：导入电脑里的音乐文件或文件夹（mp3 / flac 等，读取标签、封面和同名 `.lrc` 歌词），不需要后端。
- 鼠标指向方块会浮起；左键锁定后镜头推近特写；倒计时两下后开始播放，周围方块随节拍起波纹。
- 特写时可长按拖动切换相邻方块；停留超过 10 秒或点「观海模式」回到全景，点月亮暂停 / 播放，月亮左右是上一首 / 下一首和进度条。
- 点月亮三下有一个小彩蛋。

不通过后端、直接双击 `static/index.html` 也能打开（三方库已内联），此时只有本地导入。

这里不会绕过会员、版权保护、地区或付费限制：账号没有权限播放的歌会直接失败，页面自动跳到下一首。

## 目录

- `server.py`：本地服务。
- `static/index.html`：构建好的单文件页面（已内联 three.js r128）。
- `src/sea.body.html`：页面源码，修改它。
- `tools/build_offline.py`、`vendor/`：重新构建用。

修改页面后重新构建：

```powershell
python tools/build_offline.py src/sea.body.html vendor/three.min.js static/index.html
```

（macOS / Linux 把 `python` 换成 `python3`。）

three.js 采用 MIT 许可证，见 `vendor/THREE_LICENSE`。

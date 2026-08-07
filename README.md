![:name](https://count.getloli.com/@astrbot_plugin_xiaoheihe?name=astrbot_plugin_xiaoheihe&theme=booru-r6gdrawfriends&padding=7&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

# astrbot_plugin_steaminfo_xiaoheihe

一个为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 设计的插件，通过小黑盒搜索游戏并截图返回详情页，同时支持自动解析聊天中的小黑盒链接并截图回复。

![Preview](https://github.com/xiaoruange39/Plugin-Preview-Image/blob/main/image/Image_1777518350540_9.png) 

## ✨ 功能

- 🎮 **游戏搜索截图**：通过指令搜索小黑盒上的游戏，自动导航到详情页并返回高清截图
- 📊 **游戏信息提取**：自动提取游戏标题和在线人数等信息，以文本形式一并返回
- 🔗 **链接自动解析**：监听聊天消息，自动识别小黑盒链接并截图回复（支持 QQ JSON 卡片消息）
- 🍪 **Cookie 支持**：配置 Cookie 后可正常搜索并访问需要登录才能查看的内容
- 📱 **扫码登录**：支持通过小黑盒 App 扫码直接登录，无需手动获取 Cookie

## 🚀 安装

### 通过 AstrBot 插件市场安装

1. 打开 AstrBot WebUI
2. 进入插件市场，搜索 `steaminfo-xiaoheihe`
3. 点击安装

### 手动安装

将本仓库克隆到 AstrBot 的插件目录下：

```bash
cd <AstrBot目录>/data/plugins
git clone https://github.com/xiaoruange39/astrbot_plugin_steaminfo_xiaoheihe.git
```

> [!IMPORTANT]
> 本插件依赖 [Playwright](https://playwright.dev/python/)。安装后需要在 AstrBot 运行环境中执行以下命令安装浏览器：
> ```bash
> playwright install chromium
> ```

## 📝 使用

### 指令

| 指令 | 说明 |
|------|------|
| `/小黑盒 <游戏名>` | 搜索游戏并返回详情页截图 |
| `/xiaoheihe <游戏名>` | 同上（英文别名） |
| `/小黑盒扫码登录` | 生成二维码，用小黑盒 App 扫码完成登录 |
| `/小黑盒退出登录` | 清除已保存的扫码登录凭证 |
| `/小黑盒登录状态` | 查看当前登录状态 |

**示例：**

```
/小黑盒 三角洲行动
/xiaoheihe Elden Ring
```

> **💡 提示**：在配置中关闭 `require_prefix` 后，无需前缀，直接发送 `小黑盒 三角洲行动` 或 `xiaoheihe Elden Ring` 也能直接触发搜索。

### 链接自动解析

当启用 `enable_link_preview` 配置后，插件会自动监听聊天中的小黑盒链接并截图回复。

如果同时启用了 `link_text_parse`，链接解析将改为「文字模式」：插件会提取正文文字与图片，并按原文顺序发送。

支持的链接格式：
- 纯文本链接：直接发送 `https://www.xiaoheihe.cn/...` 即可触发
- **QQ JSON 卡片分享**：通过 QQ 分享的小黑盒卡片消息也能自动识别

## ⚙️ 配置项

所有配置项均可在 AstrBot WebUI 的插件配置页面中修改。

### 基础设置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `require_prefix` | bool | `true` | 启用指令前缀触发。关闭后发送“小黑盒 游戏名”亦可即刻触发 |
| `cookies` | string | `""` | 小黑盒 Cookie（建议填写） |
| `login_timeout` | int | `120` | 扫码登录等待超时（秒），超时后需重新发起 |

### 截图设置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `wait_timeout` | int | `60000` | 页面加载超时时间（毫秒） |
| `render_delay` | int | `5000` | 额外渲染等待时间（毫秒） |
| `device_scale_factor` | float | `2` | 截图清晰度（设备缩放因子），取值 1~3 |
| `image_quality` | int | `95` | JPEG 图片质量，取值 1~100 |

### 显示设置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `show_game_title` | bool | `true` | 截图回复时是否显示游戏名称 |
| `show_online_count` | bool | `true` | 截图回复时是否显示在线人数 |
| `enable_link_preview` | bool | `true` | 是否自动解析小黑盒链接并截图 |
| `link_text_parse` | bool | `false` | 启用后，链接解析改为文字模式 |
| `link_text_send_mode` | string | `auto` | 文字模式发送方式。`auto` 支持合并转发的平台走合并转发、官方机器人等自动回退普通图文；`forward` 强制合并转发；`plain` 强制普通图文；`markdown` QQ 官方机器人用原生 markdown 图文（需机器人有 markdown 权限，失败自动回退普通图文） |

### 视频解析

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `link_text_include_video` | bool | `true` | 文字模式下，帖子含视频时额外下载并发送视频，超过体积上限则改发直链 |
| `link_video_size_limit` | int | `100` | 视频超过该体积（MB）时改发直链，设为 `0` 表示不限制 |
| `link_image_size_limit_kb` | int | `2048` | 单张图片超过该体积（KB）时自动压缩再发送，避免官方机器人 413，设为 `0` 表示不压缩 |

### 会话过滤（白/黑名单）

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `session_filter_mode` | string | `off` | 自动解析链接时按会话 UMO 过滤。`off` 不过滤；`whitelist` 仅名单内会话生效；`blacklist` 名单内会话不生效 |
| `session_whitelist` | list | `[]` | 过滤模式为 `whitelist` 时生效，每行一个会话 UMO（如 `aiocqhttp:GroupMessage:123456`），也可只填群号/会话 ID 做分段匹配 |
| `session_blacklist` | list | `[]` | 过滤模式为 `blacklist` 时生效，规则同上 |

### 调试

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `debug` | bool | `false` | 启用后在控制台输出详细运行日志 |

---

## 🍪 如何获取小黑盒 Cookie？

### 方式一：扫码登录（推荐）

直接在聊天中发送 `/小黑盒扫码登录`，插件会生成一张二维码图片。用小黑盒 App 扫码并确认后，插件会自动保存登录凭证，后续截图无需手动配置 Cookie。

扫码登录的凭证会保存在插件目录下的 `credentials.json` 中，重启后依然有效。如需切换账号，先发送 `/小黑盒退出登录`，再重新 `/小黑盒扫码登录` 即可。

### 方式二：手动获取 Cookie

1. 在浏览器（推荐 Chrome / Edge）中访问并登录 [小黑盒官网](https://www.xiaoheihe.cn/)
2. 登录成功后，按 `F12` 打开开发者工具
3. 切换到 **网络**（Network）面板
4. 按 `F5` 刷新页面，面板中会出现网络请求
5. 点击任意一个发往 `www.xiaoheihe.cn` 的请求
6. 在右侧找到 **请求标头**（Request Headers），找到 `cookie:` 一行
7. 右键点击，选择 **复制值**（Copy value）
8. 将复制的字符串粘贴到插件配置的 `cookies` 输入框中

## 🔧 技术细节

- 使用 **Playwright** 驱动 Chromium 无头浏览器进行页面渲染和截图
- 浏览器实例延迟初始化并共享复用，避免重复启动开销
- 游戏搜索采用多方案自动降级（Plan A/B/C），提高匹配成功率
- 截图采用三级降级策略（元素截图 → 全页截图 → 视口截图），确保始终能返回结果
- QQ JSON 卡片消息通过递归解析嵌套 JSON 结构提取链接

## 🙏 致谢

本插件基于 [WhiteBr1ck/koishi-plugin-steaminfo-xiaoheihe](https://github.com/WhiteBr1ck/koishi-plugin-steaminfo-xiaoheihe#readme) 移植而来，感谢原作者的创意和设计。
扫码登录的实现参考了 [674537331/astrbot_plugin_xiaoheihe_adapter](https://github.com/674537331/astrbot_plugin_xiaoheihe_adapter)，感谢其完整的 API 签名与登录流程设计。

## ⚠️ 免责声明

- 本插件通过模拟浏览器操作访问公开的网页信息，所有数据均来自小黑盒 (xiaoheihe.cn)。
- 本插件仅供学习和技术交流使用，用户应自觉遵守相关法律法规及网站的用户协议。
- 因滥用本插件或因小黑盒网站结构变更导致的任何问题，开发者不承担任何责任。
- 请勿将本插件用于任何商业或非法用途。

## 📄 License

本插件使用 [MIT License](./LICENSE) 授权。

## 👤 作者

- **xiaoruange39**
- **[QQ群](https://qm.qq.com/q/8kdJ2Bzf6S)**

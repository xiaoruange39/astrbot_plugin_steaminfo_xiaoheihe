# astrbot_plugin_steaminfo_xiaoheihe

一个为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 设计的插件，通过小黑盒搜索游戏并截图返回详情页，同时支持自动解析聊天中的小黑盒链接并截图回复。

## ✨ 功能

- 🎮 **游戏搜索截图**：通过指令搜索小黑盒上的游戏，自动导航到详情页并返回高清截图
- 📊 **游戏信息提取**：自动提取游戏标题和在线人数等信息，以文本形式一并返回
- 🔗 **链接自动解析**：监听聊天消息，自动识别小黑盒链接并截图回复（支持 QQ JSON 卡片消息）
- 🍪 **Cookie 支持**：配置 Cookie 后可正常搜索并访问需要登录才能查看的内容

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

**示例：**

```
/小黑盒 三角洲行动
/xiaoheihe Elden Ring
```

> **💡 提示**：在配置中关闭 `require_prefix` 后，无需前缀，直接发送 `小黑盒 三角洲行动` 或 `xiaoheihe Elden Ring` 也能直接触发搜索。

### 链接自动解析

当启用 `enable_link_preview` 配置后，插件会自动监听聊天中的小黑盒链接并截图回复。

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

### 调试

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `debug` | bool | `false` | 启用后在控制台输出详细运行日志 |

---

## 🍪 如何获取小黑盒 Cookie？

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

## ⚠️ 免责声明

- 本插件通过模拟浏览器操作访问公开的网页信息，所有数据均来自小黑盒 (xiaoheihe.cn)。
- 本插件仅供学习和技术交流使用，用户应自觉遵守相关法律法规及网站的用户协议。
- 因滥用本插件或因小黑盒网站结构变更导致的任何问题，开发者不承担任何责任。
- 请勿将本插件用于任何商业或非法用途。

## 📄 License

本插件使用 [MIT License](./LICENSE) 授权。

© 2026, xiaoruange39.

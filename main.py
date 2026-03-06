import re
import json
import asyncio

from playwright.async_api import async_playwright, Browser, BrowserContext

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger, AstrBotConfig


class XiaoheihePlugin(Star):
    """小黑盒游戏截图插件

    功能：
    1. /小黑盒 <游戏名> 指令：搜索并截图游戏详情页
    2. 自动解析消息中的小黑盒链接并截图回复
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 读取配置
        self.require_prefix: bool = config.get("require_prefix", True)
        self.cookies: str = config.get("cookies", "")
        self.wait_timeout: int = config.get("wait_timeout", 60000)
        self.render_delay: int = config.get("render_delay", 5000)
        self.device_scale_factor: float = config.get("device_scale_factor", 2)
        self.image_quality: int = config.get("image_quality", 95)
        self.show_game_title: bool = config.get("show_game_title", True)
        self.show_online_count: bool = config.get("show_online_count", True)
        self.enable_link_preview: bool = config.get("enable_link_preview", True)
        self.debug: bool = config.get("debug", False)

        # Playwright 实例（延迟初始化）
        self._playwright = None
        self._browser: Browser | None = None
        self._browser_lock = asyncio.Lock()

    def _log(self, message: str):
        """调试日志"""
        if self.debug:
            logger.info(f"[小黑盒] {message}")

    async def _get_browser(self) -> Browser:
        """获取共享的浏览器实例（延迟初始化）"""
        async with self._browser_lock:
            if self._browser is None or not self._browser.is_connected():
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(headless=True)
                self._log("Playwright 浏览器已启动")
            return self._browser

    async def _create_context(self) -> BrowserContext:
        """创建带配置的浏览器上下文"""
        browser = await self._get_browser()
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            device_scale_factor=self.device_scale_factor,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        # 注入 Cookie
        if self.cookies:
            cookie_list = []
            for pair in self.cookies.split(";"):
                pair = pair.strip()
                if not pair:
                    continue
                parts = pair.split("=", 1)
                if len(parts) == 2:
                    cookie_list.append(
                        {
                            "name": parts[0].strip(),
                            "value": parts[1].strip(),
                            "domain": ".xiaoheihe.cn",
                            "path": "/",
                        }
                    )
            if cookie_list:
                await context.add_cookies(cookie_list)
                self._log(f"已注入 {len(cookie_list)} 个 Cookie")

        return context

    # ==================== 指令 ====================

    @filter.command("xiaoheihe", alias={"小黑盒"})
    async def cmd_xiaoheihe(self, event: AstrMessageEvent, game: str = ""):
        """搜索小黑盒游戏并截图"""
        if not game.strip():
            yield event.plain_result("请输入要搜索的游戏名称。\n用法：/小黑盒 <游戏名>")
            return

        yield event.plain_result("请求已收到，正在为您生成游戏截图，请稍候... 🎮")
        self._log(f'收到截图请求，游戏名称: "{game}"')

        context = None
        try:
            context = await self._create_context()
            page = await context.new_page()

            # 搜索游戏
            search_url = f"https://www.xiaoheihe.cn/app/search?q={game}"
            self._log(f"导航到搜索页面: {search_url}")
            await page.goto(search_url, wait_until="load", timeout=self.wait_timeout)

            final_url = None
            navigation_completed = False

            # Plan A: 寻找列表页的游戏链接
            list_game_selector = 'a[href*="/app/topic/game/"]'
            self._log(f'[Plan A] 尝试寻找列表页的游戏链接: "{list_game_selector}"')
            try:
                await page.wait_for_selector(list_game_selector, timeout=5000)
                game_page_href = await page.get_attribute(list_game_selector, "href")
                final_url = f"https://www.xiaoheihe.cn{game_page_href}"
                self._log(f"[Plan A] 成功！获取到链接: {final_url}")
            except Exception:
                self._log("[Plan A] 失败。切换到 Plan B...")
                try:
                    # Plan B: 尝试社区中转策略
                    community_link_selector = ".search-topic__topic-name"
                    self._log(
                        f'[Plan B] 尝试寻找社区链接: "{community_link_selector}"'
                    )
                    await page.wait_for_selector(
                        community_link_selector, timeout=5000
                    )
                    async with page.expect_navigation(
                        wait_until="load", timeout=self.wait_timeout
                    ):
                        await page.click(community_link_selector)

                    game_tab_selector = ".slide-tab__tab-label"
                    await page.wait_for_selector(game_tab_selector, timeout=10000)
                    async with page.expect_navigation(
                        wait_until="load", timeout=self.wait_timeout
                    ):
                        await page.click(game_tab_selector)

                    navigation_completed = True
                    self._log(f"[Plan B] 成功到达最终游戏页面: {page.url}")
                except Exception:
                    self._log("[Plan B] 失败。切换到 Plan C...")
                    try:
                        # Plan C: 尝试点击独立游戏卡片
                        single_game_card_selector = (
                            ".search-result__game .game-rank__game-card"
                        )
                        self._log(
                            f'[Plan C] 尝试寻找并点击独立游戏卡片: "{single_game_card_selector}"'
                        )
                        await page.wait_for_selector(
                            single_game_card_selector, timeout=5000
                        )
                        await page.click(single_game_card_selector)
                        navigation_completed = True
                        self._log("[Plan C] 点击成功！")
                    except Exception:
                        self._log("所有方案均失败。")
                        # 截取当前搜索页作为反馈
                        screenshot_bytes = await page.screenshot(full_page=True)
                        screenshot_path = self._save_temp_image(screenshot_bytes)
                        yield event.plain_result("未能找到游戏专题链接。这是当前页面的截图：")
                        yield event.image_result(screenshot_path)
                        return

            # 导航到游戏详情页
            if not navigation_completed:
                self._log(f"正在导航到: {final_url}")
                await page.goto(
                    final_url, wait_until="load", timeout=self.wait_timeout
                )

            # 等待核心内容
            main_content_selector = ".game-detail-page-detail"
            self._log(f'等待核心内容 "{main_content_selector}" 出现...')
            await page.wait_for_selector(
                main_content_selector, timeout=self.wait_timeout
            )
            self._log("核心内容容器已出现！")

            # 提取游戏标题和在线人数
            extracted_title = game
            online_info = "获取失败"
            try:
                title_selector = ".game-name p.name"
                online_number_selector = ".data-list .data-item:first-child .editor p"
                online_label_selector = ".data-list .data-item:first-child > .p2"

                self._log("等待标题和数据项出现...")
                await asyncio.gather(
                    page.wait_for_selector(title_selector, timeout=10000),
                    page.wait_for_selector(online_number_selector, timeout=10000),
                    page.wait_for_selector(online_label_selector, timeout=10000),
                )
                self._log("标题和数据项均已出现，开始提取...")

                title_el = await page.query_selector(title_selector)
                title = await title_el.text_content() if title_el else game
                extracted_title = title.strip() if title else game

                number_el = await page.query_selector(online_number_selector)
                number = await number_el.text_content() if number_el else ""
                number = number.strip() if number else ""

                label_el = await page.query_selector(online_label_selector)
                label = await label_el.text_content() if label_el else ""
                label = label.strip() if label else ""

                # 尝试获取单位
                unit = ""
                online_unit_selector = (
                    ".data-list .data-item:first-child .editor p + p"
                )
                try:
                    unit_el = await page.query_selector(online_unit_selector)
                    if unit_el:
                        unit = (await unit_el.text_content() or "").strip()
                except Exception:
                    pass

                online_info = f"{label}：{number}{unit}"
                self._log(f'成功提取到标题: "{extracted_title}"')
                self._log(f'成功提取到在线信息: "{online_info}"')
            except Exception:
                self._log("无法从页面提取标题或在线人数，将使用用户输入的游戏名。")

            # 额外等待渲染
            self._log(f"额外等待 {self.render_delay} 毫秒以确保内容渲染完成...")
            await asyncio.sleep(self.render_delay / 1000)

            # 隐藏不需要的元素
            selectors_to_hide = [
                ".game-detail-section-comment",
                ".game-detail-section-similar-games",
                ".publish-score-wrapper",
            ]
            selector_to_modify = ".game-detail-section-footer"
            self._log(
                f"准备隐藏 {len(selectors_to_hide)} 个元素，并修正 1 个悬浮元素的位置..."
            )
            await page.evaluate(
                """([toHide, toModify]) => {
                    for (const selector of toHide) {
                        const element = document.querySelector(selector);
                        if (element) element.style.display = 'none';
                    }
                    const floatingElement = document.querySelector(toModify);
                    if (floatingElement) floatingElement.style.position = 'static';
                }""",
                [selectors_to_hide, selector_to_modify],
            )

            # 精准截图
            element = await page.query_selector(main_content_selector)
            if not element:
                raise RuntimeError("无法定位到已等待的核心内容元素")

            self._log("正在执行最终的精准截图...")
            image_bytes = await element.screenshot(
                type="jpeg", quality=self.image_quality
            )
            self._log("截图成功！")

            # 保存临时图片并发送
            image_path = self._save_temp_image(image_bytes)

            # 构建消息
            text_lines = []
            if self.show_game_title:
                text_lines.append(f"游戏名：{extracted_title}")
            if self.show_online_count and online_info != "获取失败":
                text_lines.append(online_info)

            if text_lines:
                yield event.plain_result("\n".join(text_lines))
            yield event.image_result(image_path)

        except Exception as e:
            error_msg = "截图失败，请检查控制台错误日志。"
            if "timeout" in str(e).lower() or "Timeout" in type(e).__name__:
                error_msg = "截图失败，页面加载超时。可能是小黑盒服务器繁忙或您的网络不稳定。"
            logger.error(f"截图过程中发生严重错误: {e}")
            yield event.plain_result(error_msg)
        finally:
            if context:
                await context.close()
                self._log("浏览器上下文已关闭。")

    # ==================== 链接解析 ====================

    def _extract_xiaoheihe_url(self, event: AstrMessageEvent) -> str | None:
        """从消息中提取小黑盒链接，支持纯文本和 QQ JSON 卡片消息"""
        url_pattern = re.compile(
            r"https?://(?:[a-z0-9.-]*\.)?xiaoheihe\.cn[^\s\"'<>]*", re.IGNORECASE
        )

        # 1. 先从纯文本中查找
        content = event.message_str or ""
        match = url_pattern.search(content)
        if match:
            return match.group(0)

        # 2. 遍历消息链，查找 JSON 卡片消息中的链接
        try:
            message_chain = getattr(event.message_obj, "message", None) or []
            for seg in message_chain:
                seg_type = getattr(seg, "type", None) or ""
                # OneBot JSON 消息段的 type 为 "json"
                if seg_type.lower() != "json":
                    continue

                # 提取 data 字段（可能是字符串或 dict）
                raw_data = getattr(seg, "data", None)
                if raw_data is None:
                    continue

                # 如果 data 本身是 dict，取其内部的 "data" 字段（OneBot 嵌套结构）
                if isinstance(raw_data, dict):
                    raw_data = raw_data.get("data", raw_data)

                # 将 JSON 字符串解析为 dict
                json_str = raw_data if isinstance(raw_data, str) else ""
                if not json_str:
                    # 如果无法取得字符串，尝试对整个 raw_data 做全文搜索
                    full_text = str(raw_data)
                    m = url_pattern.search(full_text)
                    if m:
                        return m.group(0)
                    continue

                try:
                    card = json.loads(json_str)
                except (json.JSONDecodeError, TypeError):
                    # 解析失败也做全文搜索
                    m = url_pattern.search(json_str)
                    if m:
                        return m.group(0)
                    continue

                # 在解析后的 JSON 中递归搜索 URL
                # 常见路径: meta.detail_1.qqdocurl, meta.news.jumpUrl 等
                def find_url_in_dict(d):
                    if isinstance(d, dict):
                        for v in d.values():
                            result = find_url_in_dict(v)
                            if result:
                                return result
                    elif isinstance(d, list):
                        for item in d:
                            result = find_url_in_dict(item)
                            if result:
                                return result
                    elif isinstance(d, str):
                        m = url_pattern.search(d)
                        if m:
                            return m.group(0)
                    return None

                found = find_url_in_dict(card)
                if found:
                    return found
        except Exception as e:
            self._log(f"解析 JSON 卡片消息时出错: {e}")

        return None

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有消息，处理无前缀指令和自动解析小黑盒链接"""
        content = event.message_str or ""

        # 0. 无前缀触发逻辑
        if not self.require_prefix:
            content_stripped = content.strip()
            # 匹配"小黑盒 xxx"或"xiaoheihe xxx"（支持空格或换行分隔）
            if content_stripped.startswith("小黑盒") or content_stripped.startswith("xiaoheihe"):
                # 提取纯文本前缀
                prefix = "小黑盒" if content_stripped.startswith("小黑盒") else "xiaoheihe"
                # 检查紧跟的是否为空白字符
                if len(content_stripped) > len(prefix) and content_stripped[len(prefix)].isspace():
                    game = content_stripped[len(prefix):].strip()
                    if game:
                        self._log("检测到无前缀触发指令")
                        async for result in self.cmd_xiaoheihe(event, game):
                            yield result
                        return

        if not self.enable_link_preview:
            return

        target_url = self._extract_xiaoheihe_url(event)
        if not target_url:
            return

        self._log(f"检测到小黑盒链接，开始截图: {target_url}")

        yield event.plain_result("检测到小黑盒链接，正在为您生成截图，请稍候...")

        context = None
        try:
            context = await self._create_context()
            page = await context.new_page()

            await page.goto(
                target_url, wait_until="load", timeout=self.wait_timeout
            )

            # 等待渲染
            await asyncio.sleep(self.render_delay / 1000)

            # 尝试截取主要内容
            candidates = [
                ".hb-bbs-post",
                ".hb-bbs-image-text",
                ".game-detail-page-detail",
                ".post-detail",
                ".topic-detail",
                "main",
                "#app",
            ]

            element = None
            found_selector = ""
            for selector in candidates:
                el = await page.query_selector(selector)
                if el:
                    element = el
                    found_selector = selector
                    self._log(f"找到主要内容区域 ({selector})，进行精准截图")
                    break

            if element:
                try:
                    image_bytes = await element.screenshot(
                        type="jpeg", quality=self.image_quality,
                        timeout=self.wait_timeout,
                    )
                except Exception as el_err:
                    self._log(f"元素截图失败 ({found_selector}): {el_err}，回退到全页截图")
                    try:
                        image_bytes = await page.screenshot(
                            full_page=True, type="jpeg", quality=self.image_quality
                        )
                    except Exception as fp_err:
                        self._log(f"全页截图也失败: {fp_err}，回退到视口截图")
                        image_bytes = await page.screenshot(
                            type="jpeg", quality=self.image_quality
                        )
            else:
                self._log("未找到任何主要内容区域，进行全页截图")
                try:
                    image_bytes = await page.screenshot(
                        full_page=True, type="jpeg", quality=self.image_quality
                    )
                except Exception as fp_err:
                    self._log(f"全页截图失败: {fp_err}，回退到视口截图")
                    image_bytes = await page.screenshot(
                        type="jpeg", quality=self.image_quality
                    )

            image_path = self._save_temp_image(image_bytes)
            yield event.image_result(image_path)
            self._log("链接解析截图完成")

        except Exception as e:
            logger.error(f"链接解析截图失败: {e}")
            yield event.plain_result("链接截图失败，请稍后再试。")
        finally:
            if context:
                await context.close()
                self._log("链接解析：浏览器上下文已关闭")

    # ==================== 工具方法 ====================

    def _save_temp_image(self, image_bytes: bytes) -> str:
        """保存临时截图并返回文件路径"""
        import tempfile
        import os

        temp_dir = tempfile.gettempdir()
        file_path = os.path.join(
            temp_dir, f"xiaoheihe_{id(image_bytes)}_{asyncio.get_event_loop().time()}.jpg"
        )
        with open(file_path, "wb") as f:
            f.write(image_bytes)
        self._log(f"临时截图已保存: {file_path}")
        return file_path

    # ==================== 生命周期 ====================

    async def terminate(self):
        """插件卸载/停用时调用"""
        if self._browser and self._browser.is_connected():
            await self._browser.close()
            self._log("浏览器已关闭")
        if self._playwright:
            await self._playwright.stop()
            self._log("Playwright 已停止")
        logger.info("小黑盒游戏截图插件已停用")

import re
import json
import asyncio
import os
import tempfile
import uuid
import sys
import time
import hashlib
import secrets
import html
import astrbot.api.message_components as Comp
from urllib.parse import quote, urljoin

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
        self.cookies: str = config.get("cookies", "")
        self.wait_timeout: int = int(config.get("wait_timeout", 60000))
        self.render_delay: int = int(config.get("render_delay", 5000))
        self.device_scale_factor: float = float(config.get("device_scale_factor", 2))
        self.image_quality: int = int(config.get("image_quality", 95))
        self.show_game_title: bool = config.get("show_game_title", True)
        self.show_online_count: bool = config.get("show_online_count", True)
        self.enable_link_preview: bool = config.get("enable_link_preview", True)
        self.link_text_parse: bool = config.get("link_text_parse", False)
        self.link_text_include_images: bool = config.get("link_text_include_images", True)
        self.link_text_include_video: bool = config.get("link_text_include_video", True)
        self.link_video_size_limit: int = int(config.get("link_video_size_limit", 100))
        self.link_text_fallback_screenshot: bool = config.get("link_text_fallback_screenshot", False)
        self.debug: bool = config.get("debug", False)

        # 会话（UMO）白名单/黑名单
        self.session_filter_mode: str = str(config.get("session_filter_mode", "off") or "off").strip().lower()
        self.session_whitelist: list[str] = self._parse_session_list(config.get("session_whitelist", []))
        self.session_blacklist: list[str] = self._parse_session_list(config.get("session_blacklist", []))

        # Playwright 实例（延迟初始化）
        self._playwright_manager = None
        self._playwright = None
        self._browser: Browser | None = None
        self._browser_lock = asyncio.Lock()
        
        # 限制并发截图数量，防止 OOM
        self._semaphore = asyncio.Semaphore(2)

        # 跟踪临时文件以便退出时统一清理
        self._temp_files = set()

        # 扫码登录状态
        self._credentials_path = self._resolve_credentials_path()
        self._login_cookies: dict[str, str] = {}
        self._login_uid: str = ""
        self._login_nickname: str = ""
        self._login_device_id: str = ""
        self._login_poll_interval: int = 3
        self.login_timeout: int = int(config.get("login_timeout", 120))
        self._load_credentials()

        self.game_detail_selectors = [
            ".game-detail-page-detail",
            ".game-detail-page",
            ".game-detail",
            ".game-page",
            ".game-topic-detail",
            ".game-home",
            "[class*='game-detail']",
            "[class*='GameDetail']",
        ]
        self.page_fallback_selectors = [
            "main",
            "#app",
        ]

    def _log(self, message: str):
        """调试日志"""
        if self.debug:
            logger.info(f"[小黑盒] {message}")

    async def _get_browser(self) -> Browser:
        """获取共享的浏览器实例（延迟初始化）"""
        async with self._browser_lock:
            if self._playwright_manager is None:
                self._playwright_manager = async_playwright()
                self._playwright = await self._playwright_manager.start()
            if self._browser is None or not self._browser.is_connected():
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
        cookie_list = self._build_cookie_list()
        if cookie_list:
            try:
                await context.add_cookies(cookie_list)
                self._log(f"已注入 {len(cookie_list)} 个 Cookie")
            except Exception as e:
                self._log(f"注入 Cookie 时发生异常，关闭上下文: {e}")
                await context.close()
                raise

        return context

    # ==================== 指令 ====================

    @filter.command("xiaoheihe", alias={"小黑盒"}, ignore_prefix=True)
    async def cmd_xiaoheihe(self, event: AstrMessageEvent, game: str = ""):
        """搜索小黑盒游戏并截图"""
        if not game.strip():
            yield event.plain_result("请输入要搜索的游戏名称。\n用法：/小黑盒 <游戏名>")
            return

        yield event.plain_result("请求已收到，正在为您生成游戏截图，请稍候... ")
        self._log(f'收到截图请求，游戏名称: "{game}"')

        async with self._semaphore:
            async for result in self._process_screenshot(event, game):
                yield result

    @filter.command("小黑盒扫码登录", ignore_prefix=True)
    async def cmd_login(self, event: AstrMessageEvent):
        """扫码登录小黑盒，自动获取 Cookie"""
        try:
            _plugin_dir = os.path.dirname(os.path.abspath(__file__))
            if _plugin_dir not in sys.path:
                sys.path.insert(0, _plugin_dir)
            from xiaoheihe_login import (
                XiaoheiheLoginClient,
                generate_qr_png,
                LoginState,
            )
        except ImportError as e:
            yield event.plain_result(
                f"扫码登录模块加载失败，请确保已安装依赖（aiohttp、qrcode）：{e}"
            )
            return

        yield event.plain_result("正在生成登录二维码，请稍候...")

        client = XiaoheiheLoginClient(device_id=self._login_device_id)
        try:
            qr_session = await client.request_qr()
        except Exception as e:
            logger.error(f"[小黑盒] 获取二维码失败: {e}")
            yield event.plain_result(f"获取二维码失败：{e}")
            return

        qr_bytes = generate_qr_png(qr_session.qr_content)
        qr_path = self._save_temp_image(qr_bytes, suffix=".png")
        yield event.chain_result([
            Comp.Plain("请使用小黑盒 App 扫描下方二维码完成登录"),
            Comp.Image.fromFileSystem(qr_path),
        ])
        self._schedule_cleanup(qr_path)

        last_state = None
        deadline = min(qr_session.expires_at, time.time() + self.login_timeout)
        while time.time() < deadline:
            await asyncio.sleep(self._login_poll_interval)
            try:
                result = await client.check_qr(qr_session)
            except Exception as e:
                self._log(f"轮询登录状态失败: {e}")
                continue
            if result.state != last_state:
                self._log(f"登录状态变更: {result.state.value}")
                last_state = result.state
                if result.state == LoginState.SCANNED_WAITING_CONFIRM:
                    yield event.plain_result("已扫描，请在手机上确认登录")
                elif result.state == LoginState.SUCCESS:
                    self._save_credentials(
                        result.cookies, result.uid, result.nickname, client.device_id
                    )
                    name = result.nickname or result.uid or "未知"
                    yield event.plain_result(f"登录成功！欢迎，{name}")
                    return
                elif result.state in (LoginState.EXPIRED, LoginState.FAILED):
                    msg = result.message or "二维码已过期或登录失败"
                    yield event.plain_result(
                        f"登录失败：{msg}，请重新发起 /小黑盒扫码登录"
                    )
                    return
        yield event.plain_result("二维码已过期，请重新发起 /小黑盒扫码登录")

    @filter.command("小黑盒退出登录", ignore_prefix=True)
    async def cmd_logout(self, event: AstrMessageEvent):
        """退出登录，清除保存的凭证"""
        if not self._login_cookies:
            yield event.plain_result("当前未通过扫码登录，无需退出。")
            return
        name = self._login_nickname or self._login_uid or "未知"
        self._clear_credentials()
        yield event.plain_result(f"已退出登录（{name}），保存的凭证已清除。")

    @filter.command("小黑盒登录状态", ignore_prefix=True)
    async def cmd_login_status(self, event: AstrMessageEvent):
        """查看当前登录状态"""
        if self._login_cookies:
            name = self._login_nickname or self._login_uid or "未知"
            yield event.plain_result(
                f"当前已通过扫码登录：{name}\n如需重新登录，请使用 /小黑盒扫码登录"
            )
        elif self.cookies:
            yield event.plain_result(
                "当前使用配置中的 Cookie（手动填写），未使用扫码登录。\n"
                "如需扫码登录，请使用 /小黑盒扫码登录"
            )
        else:
            yield event.plain_result(
                "当前未登录。\n请使用 /小黑盒扫码登录 或在配置中填写 Cookie。"
            )

    async def _process_screenshot(self, event: AstrMessageEvent, game: str):
        short_timeout = max(3000, self.wait_timeout // 12)
        mid_timeout = max(5000, self.wait_timeout // 6)
        target_url = self._extract_url_from_text(game)

        context = None
        try:
            context = await self._create_context()
            page = await context.new_page()

            if target_url:
                self._log(f"检测到直接链接，导航到详情页: {target_url}")
                await page.goto(target_url, wait_until="domcontentloaded", timeout=self.wait_timeout)
                navigation_success = True
            else:
                # 搜索游戏
                search_url = f"https://www.xiaoheihe.cn/app/search?q={quote(game)}"
                self._log(f"导航到搜索页面: {search_url}")
                await page.goto(search_url, wait_until="domcontentloaded", timeout=self.wait_timeout)

                # 多重降级策略寻找并导航到游戏详情页
                navigation_success = await self._navigate_to_game_page(page, game, short_timeout, mid_timeout)

            if not navigation_success:
                self._log("所有方案均失败。")
                # 截取当前搜索页作为反馈
                screenshot_bytes = await page.screenshot(full_page=True)
                screenshot_path = self._save_temp_image(screenshot_bytes)
                chain = [
                    Comp.Plain(f"未能找到“{game}”的游戏专题链接。这是当前搜索页面的截图："),
                    Comp.Image.fromFileSystem(screenshot_path)
                ]
                yield event.chain_result(chain)
                self._schedule_cleanup(screenshot_path)
                return

            # 等待核心内容。小黑盒页面 class 经常变化，不能只依赖旧的
            # .game-detail-page-detail，否则结构调整后会一直等到超时。
            main_content_selector = ""
            detail_timeout = min(self.wait_timeout, max(mid_timeout, 15000))
            element = await self._find_first_visible_element(
                page, self.game_detail_selectors, detail_timeout
            )
            if element:
                main_content_selector = await self._describe_element(element)
                self._log(f"核心内容容器已出现: {main_content_selector}")
            else:
                self._log("未找到稳定的游戏详情容器，将使用全页截图兜底。")

            # 提取游戏标题和在线人数
            extracted_title = game
            online_info = "获取失败"
            try:
                title_selector = ".game-name p.name"
                online_number_selector = ".data-list .data-item:first-child .editor div"
                online_label_selector = ".data-list .data-item:first-child > .p2"

                self._log("等待标题和数据项出现...")
                await asyncio.gather(
                    page.wait_for_selector(title_selector, timeout=mid_timeout),
                    page.wait_for_selector(online_number_selector, timeout=mid_timeout),
                    page.wait_for_selector(online_label_selector, timeout=mid_timeout),
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
                    ".data-list .data-item:first-child .editor div + div"
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
            if not element:
                element = await self._find_first_visible_element(
                    page, self.page_fallback_selectors, 1000
                )

            self._log("正在执行最终截图...")
            image_bytes = await self._take_screenshot_with_fallback(
                page, element, main_content_selector
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
                result_text = "\n".join(text_lines)
            else:
                result_text = ""
            chain = [
                Comp.Plain(result_text),
                Comp.Image.fromFileSystem(image_path)
            ]
            yield event.chain_result(chain)
            self._schedule_cleanup(image_path)

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

    async def _navigate_to_game_page(self, page, game: str, short_timeout: int, mid_timeout: int) -> bool:
        """尝试多种方案导航到游戏详情页"""
        # Plan A: 寻找列表页的游戏链接
        list_game_selector = 'a[href*="/app/topic/game/"]'
        self._log(f'[Plan A] 尝试寻找列表页的游戏链接: "{list_game_selector}"')
        try:
            await page.wait_for_selector(list_game_selector, timeout=short_timeout)
            game_page_href = await page.get_attribute(list_game_selector, "href")
            final_url = urljoin("https://www.xiaoheihe.cn", game_page_href)
            self._log(f"[Plan A] 成功！获取到链接: {final_url}")
            self._log(f"正在导航到: {final_url}")
            await page.goto(final_url, wait_until="load", timeout=self.wait_timeout)
            return True
        except Exception:
            self._log("[Plan A] 失败")

        # Plan B: 尝试社区中转策略
        self._log("尝试切换到 Plan B...")
        try:
            community_link_selector = ".search-topic__topic-name"
            self._log(f'[Plan B] 寻找社区链接: "{community_link_selector}"')
            await page.wait_for_selector(community_link_selector, timeout=short_timeout)
            async with page.expect_navigation(wait_until="load", timeout=self.wait_timeout):
                await page.click(community_link_selector)

            game_tab_selector = ".slide-tab__tab-label"
            await page.wait_for_selector(game_tab_selector, timeout=mid_timeout)
            async with page.expect_navigation(wait_until="load", timeout=self.wait_timeout):
                await page.click(game_tab_selector)
            self._log(f"[Plan B] 成功到达最终游戏页面: {page.url}")
            return True
        except Exception:
            self._log("[Plan B] 失败")

        # Plan C: 尝试点击独立游戏卡片
        self._log("尝试切换到 Plan C...")
        try:
            single_game_card_selector = ".search-result__game .game-rank__game-card"
            self._log(f'[Plan C] 寻找并点击独立游戏卡片: "{single_game_card_selector}"')
            await page.wait_for_selector(single_game_card_selector, timeout=short_timeout)
            await page.click(single_game_card_selector)
            self._log("[Plan C] 点击成功！")
            return True
        except Exception:
            self._log("[Plan C] 失败")

        # Plan D: 新搜索页可能不再使用旧 class，从 DOM 中扫描专题链接
        self._log("尝试切换到 Plan D...")
        try:
            href = await self._find_game_topic_href(page, game)
            if href:
                final_url = urljoin("https://www.xiaoheihe.cn", href)
                self._log(f"[Plan D] 成功！扫描到游戏专题链接: {final_url}")
                await page.goto(final_url, wait_until="domcontentloaded", timeout=self.wait_timeout)
                return True
        except Exception as e:
            self._log(f"[Plan D] 失败: {e}")

        # Plan E: 如果新版页面把跳转藏在前端事件里，直接点击匹配文本的结果项
        self._log("尝试切换到 Plan E...")
        try:
            clicked = await self._click_game_result_by_text(page, game)
            if clicked:
                await page.wait_for_load_state("domcontentloaded", timeout=mid_timeout)
                self._log(f"[Plan E] 点击搜索结果后当前页面: {page.url}")
                return True
        except Exception as e:
            self._log(f"[Plan E] 失败: {e}")

        return False

    # ==================== 链接解析 ====================

    def _extract_xiaoheihe_url(self, event: AstrMessageEvent) -> str | None:
        """从消息中提取小黑盒链接，支持纯文本和 QQ JSON 卡片消息"""
        from urllib.parse import unquote

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
                if seg_type and seg_type.lower() not in {"json", "xml", "text", "card"}:
                    continue

                raw_data = getattr(seg, "data", None)
                if raw_data is None:
                    continue

                for candidate in self._iter_url_text_candidates(raw_data):
                    for text in (candidate, unquote(candidate)):
                        m = url_pattern.search(text)
                        if m:
                            return m.group(0)
        except Exception as e:
            self._log(f"解析 JSON 卡片消息时出错: {e}")

        return None

    def _parse_session_list(self, value) -> list[str]:
        """把配置中的会话列表统一成去空白后的字符串列表。"""
        items = []
        if isinstance(value, str):
            raw = re.split(r"[\n,，;；]+", value)
        elif isinstance(value, (list, tuple, set)):
            raw = list(value)
        else:
            raw = []
        for item in raw:
            text = str(item or "").strip()
            if text:
                items.append(text)
        return items

    def _get_session_umo(self, event: AstrMessageEvent) -> str:
        """获取当前会话的 unified_msg_origin（UMO）。"""
        try:
            umo = event.unified_msg_origin
            if umo:
                return str(umo)
        except Exception:
            pass
        try:
            umo = getattr(getattr(event, "message_obj", None), "unified_msg_origin", None)
            if umo:
                return str(umo)
        except Exception:
            pass
        return ""

    def _is_session_allowed(self, event: AstrMessageEvent) -> bool:
        """按 UMO 白名单/黑名单判断当前会话是否允许自动解析。"""
        mode = self.session_filter_mode
        if mode not in {"whitelist", "blacklist"}:
            return True

        umo = self._get_session_umo(event)
        if not umo:
            # 拿不到 UMO 时，白名单模式默认拦截、黑名单模式默认放行。
            return mode != "whitelist"

        if mode == "whitelist":
            return any(self._session_matches(umo, rule) for rule in self.session_whitelist)
        return not any(self._session_matches(umo, rule) for rule in self.session_blacklist)

    def _session_matches(self, umo: str, rule: str) -> bool:
        """会话匹配：支持完整 UMO，也支持只填群号/会话 ID 的子串匹配。"""
        umo = str(umo or "").strip()
        rule = str(rule or "").strip()
        if not umo or not rule:
            return False
        if umo == rule:
            return True
        # 允许只填群号/用户号：按冒号分段后精确匹配任意一段。
        segments = re.split(r"[:：]", umo)
        if rule in segments:
            return True
        return False

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有消息，处理无前缀指令和自动解析小黑盒链接"""
        content = event.message_str or ""

        # 0. 无前缀触发逻辑已移除，由 @filter.command(ignore_prefix=True) 处理

        if not self.enable_link_preview:
            return

        if not self._is_session_allowed(event):
            self._log(f"当前会话不在允许列表内，跳过链接解析: {self._get_session_umo(event)}")
            return

        target_url = self._extract_xiaoheihe_url(event)
        if not target_url:
            return

        if self.link_text_parse:
            self._log(f"检测到小黑盒链接，开始解析内容: {target_url}")
            yield event.plain_result("检测到小黑盒链接，正在为您解析内容，请稍候...")
            async with self._semaphore:
                async for result in self._process_link_text(event, target_url):
                    yield result
        else:
            self._log(f"检测到小黑盒链接，开始截图: {target_url}")
            yield event.plain_result("检测到小黑盒链接，正在为您生成截图，请稍候...")
            async with self._semaphore:
                async for result in self._process_link_screenshot(event, target_url):
                    yield result

    async def _process_link_screenshot(self, event: AstrMessageEvent, target_url: str):
        context = None
        try:
            context = await self._create_context()
            page = await context.new_page()

            await page.goto(
                target_url, wait_until="domcontentloaded", timeout=self.wait_timeout
            )

            # 等待渲染
            await asyncio.sleep(self.render_delay / 1000)
            await self._prepare_link_page_for_screenshot(page)

            # 尝试截取主要内容
            element = await self._find_article_content_element(page)
            found_selector = await self._describe_element(element) if element else ""
            candidates = [
                ".hb-bbs-post",
                ".hb-bbs-image-text",
                ".hb-bbs-post-detail",
                ".article-detail",
                "article",
                *self.game_detail_selectors,
                ".post-detail",
                ".topic-detail",
                *self.page_fallback_selectors,
            ]

            if element:
                self._log(f"通过正文启发式找到主要内容区域 ({found_selector})，进行精准截图")
            else:
                for selector in candidates:
                    el = await page.query_selector(selector)
                    if el:
                        element = el
                        found_selector = selector
                        self._log(f"找到主要内容区域 ({selector})，进行精准截图")
                        break

            image_bytes = await self._take_screenshot_with_fallback(page, element, found_selector)
            
            if not image_bytes:
                raise RuntimeError("截图过程异常，未能获取到任何图像数据。")

            image_path = self._save_temp_image(image_bytes)
            yield event.image_result(image_path)
            self._schedule_cleanup(image_path)
            self._log("链接解析截图完成")

        except Exception as e:
            logger.error(f"链接解析截图失败: {e}")
            yield event.plain_result("链接截图失败，请稍后再试。")
        finally:
            if context:
                await context.close()
                self._log("链接解析：浏览器上下文已关闭")

    async def _process_link_text(self, event: AstrMessageEvent, target_url: str):
        """直接调用小黑盒 API 解析链接内容，以合并转发方式发送图文。"""
        try:
            parsed = self._parse_link_url(target_url)
            if not parsed:
                resolved_url = await self._resolve_xiaoheihe_url(target_url)
                if resolved_url and resolved_url != target_url:
                    self._log(f"链接重定向后解析: {resolved_url}")
                    parsed = self._parse_link_url(resolved_url)
            if not parsed:
                self._log(f"无法从链接中提取 link_id: {target_url}")
                if self.link_text_fallback_screenshot:
                    async for result in self._process_link_screenshot(event, target_url):
                        yield result
                else:
                    yield event.plain_result("未能从该链接中识别到可解析的 ID，已按文字模式跳过截图。")
                return

            link_type, link_id = parsed
            self._log(f"链接解析: type={link_type} id={link_id} url={target_url}")

            payload = await self._fetch_link_api(link_type, link_id)
            if link_type == "bbs":
                content = self._parse_bbs_content(payload, target_url)
            else:
                content = self._parse_game_content(payload, target_url)
            if link_type == "bbs":
                cover_url = self._extract_img_url(
                    payload.get("result", {}).get("link", {}).get("thumb")
                    or payload.get("result", {}).get("link", {}).get("video_thumb")
                )
                if cover_url:
                    cover_bytes = await self._download_image(cover_url)
                    if cover_bytes:
                        content["cover_bytes"] = cover_bytes
                if self.link_text_include_video:
                    video_url = self._extract_bbs_video_url(payload)
                    if video_url:
                        content["video_url"] = video_url
            content["blocks"] = self._dedupe_link_blocks(content.get("blocks") or [])
            blocks = content.get("blocks") or []

            for block in blocks:
                if self.link_text_include_images and block.get("type") == "image":
                    img_bytes = await self._download_image(block.get("url"))
                    if img_bytes:
                        block["image_bytes"] = img_bytes

            has_content = any(
                (b.get("type") == "text" and b.get("text"))
                or (self.link_text_include_images and b.get("type") == "image" and b.get("image_bytes"))
                for b in blocks
            ) or any(content.get(key) for key in ("title", "author", "publish_time", "video_url"))
            if not has_content:
                self._log("API 未解析到正文内容。")
                fallback_content = await self._fallback_parse_xiaoheihe_page(target_url)
                if fallback_content:
                    nodes = self._build_link_forward_nodes(fallback_content, self._get_forward_uin(event))
                    if nodes:
                        yield event.chain_result(nodes)
                        self._log("链接解析（网页兜底合并转发）完成")
                        return
                if self.link_text_fallback_screenshot:
                    async for result in self._process_link_screenshot(event, target_url):
                        yield result
                else:
                    yield event.plain_result("未能解析到正文内容，已按文字模式跳过截图。")
                return

            nodes = self._build_link_forward_nodes(content, self._get_forward_uin(event))
            if not nodes:
                fallback_content = await self._fallback_parse_xiaoheihe_page(target_url)
                if fallback_content:
                    nodes = self._build_link_forward_nodes(fallback_content, self._get_forward_uin(event))
                if not nodes:
                    yield event.plain_result("未能解析到任何内容，请稍后再试。")
                    return

            yield event.chain_result(nodes)
            self._log("链接解析（API 合并转发）完成")

            video_url = content.get("video_url")
            if self.link_text_include_video and video_url:
                async for result in self._send_link_video(event, video_url):
                    yield result

        except Exception as e:
            logger.error(f"链接文字解析失败: {e}")
            yield event.plain_result("内容解析失败，请稍后再试。")

    def _parse_link_url(self, url: str):
        """从小黑盒链接中提取 (类型, link_id)。

        支持：community/app/post/<id>、bbs 帖子分享链接、带 ?link_id= 的链接、游戏专题页等。
        """
        from urllib.parse import urlsplit, parse_qsl

        try:
            parts = urlsplit(url)
            path = (parts.path or "").strip("/")
            query = dict(parse_qsl(parts.query, keep_blank_values=True))

            topic_match = re.search(r"(?:^|/)app/topic/game/(\d+)", path)
            if topic_match:
                return ("pc", topic_match.group(1))

            for game_type, keys in {
                "pc": ("steam_appid", "steam_app_id", "steamappid", "steamid", "appid", "gameid", "game_id"),
                "console": ("console_appid", "console_id", "appid", "gameid", "game_id"),
                "mobile": ("mobile_appid", "mobile_id", "appid", "gameid", "game_id"),
            }.items():
                for key in keys:
                    value = str(query.get(key, "")).strip()
                    if value.isdigit():
                        return (game_type, value)

            for key in ("link_id", "linkid", "id", "post_id", "postid", "content_id", "contentid", "article_id", "articleid", "share_id", "shareid", "topic_id", "topicid", "bbs_id"):
                value = str(query.get(key, "")).strip()
                if self._is_link_id(value):
                    return ("bbs", value)

            segments = [s for s in path.split("/") if s]
            for seg in reversed(segments):
                clean = seg.split("?")[0].split("#")[0]
                if self._is_link_id(clean):
                    return ("bbs", clean)

            m = re.search(r"/([A-Za-z0-9_-]{6,})(?:/|$|\?)", url)
            if m:
                return ("bbs", m.group(1))
        except Exception as e:
            self._log(f"解析链接 id 失败: {e}")
        return None

    def _is_link_id(self, value: str) -> bool:
        """小黑盒分享 API 的 link_id 可能是数字，也可能是字母数字混合。"""
        return bool(re.fullmatch(r"[A-Za-z0-9_-]{6,}", str(value or "").strip()))

    async def _fallback_parse_xiaoheihe_page(self, target_url: str) -> dict | None:
        """当分享 API 没有正文时，直接从网页中提取可读正文作为兜底。"""
        context = None
        try:
            context = await self._create_context()
            page = await context.new_page()
            await page.goto(target_url, wait_until="domcontentloaded", timeout=self.wait_timeout)
            await asyncio.sleep(self.render_delay / 1000)

            page_html = await page.content()
            for candidate in self._iter_url_text_candidates(page_html):
                parsed = self._parse_link_url(candidate)
                if not parsed:
                    continue
                link_type, link_id = parsed
                if link_type != "bbs":
                    continue
                original = self._parse_link_url(target_url)
                if original and original == parsed:
                    continue
                try:
                    payload = await self._fetch_link_api(link_type, link_id)
                    content = self._parse_bbs_content(payload, candidate)
                    content["blocks"] = self._dedupe_link_blocks(content.get("blocks") or [])
                    if any(
                        block.get("type") == "text" and block.get("text")
                        for block in content.get("blocks") or []
                        if isinstance(block, dict)
                    ):
                        for block in content.get("blocks") or []:
                            if self.link_text_include_images and block.get("type") == "image":
                                img_bytes = await self._download_image(block.get("url"))
                                if img_bytes:
                                    block["image_bytes"] = img_bytes
                        return content
                except Exception as e:
                    self._log(f"网页中真实 link_id 二次解析失败: {e}")

            extracted = await page.evaluate(
                r"""() => {
                    const pick = selectors => {
                        for (const selector of selectors) {
                            const node = document.querySelector(selector);
                            if (node) return node;
                        }
                        return null;
                    };

                    const root = pick([
                        'article',
                        'main',
                        '[class*="post-detail" i]',
                        '[class*="bbs" i]',
                        '[class*="content" i]',
                        '[class*="article" i]',
                    ]) || document.body;

                    const text = (root.innerText || root.textContent || '').trim();
                    const imageUrls = [...root.querySelectorAll('img')]
                        .map(img => img.getAttribute('data-original') || img.currentSrc || img.src || img.getAttribute('src'))
                        .filter(Boolean)
                        .filter(url => !/avatar|icon|logo|emoji|sprite/i.test(url))
                        .slice(0, 12);
                    return { text, imageUrls };
                }"""
            )

            text = self._clean_html(str((extracted or {}).get("text") or ""))
            if not text:
                return None

            lines = [
                line.strip()
                for line in text.splitlines()
                if line.strip() and not self._is_xhh_boilerplate_line(line.strip())
            ]
            # 过滤掉小黑盒页脚/导航等样板文案后，正文往往所剩无几，
            # 说明网页是未渲染的单页外壳，此时不要把页脚当正文发出去。
            meaningful = [line for line in lines if len(line) >= 8]
            if len("".join(meaningful)) < 40:
                return None

            title = lines[0]
            body_lines = lines[1:]
            blocks = []
            if body_lines:
                blocks.append({"type": "text", "text": "\n\n".join(body_lines[:20])})

            for url in (extracted or {}).get("imageUrls") or []:
                image_url = self._extract_img_url(url)
                if not image_url:
                    continue
                image_bytes = await self._download_image(image_url)
                if image_bytes:
                    blocks.append({"type": "image", "url": image_url, "image_bytes": image_bytes})

            return {
                "title": title,
                "author": "",
                "publish_time": "",
                "blocks": blocks,
            }
        except Exception as e:
            self._log(f"网页兜底解析失败: {e}")
            return None
        finally:
            if context:
                await context.close()

    def _is_xhh_boilerplate_line(self, line: str) -> bool:
        """识别小黑盒网页外壳里的导航、页脚、备案等样板文字。"""
        line = str(line or "").strip()
        if not line:
            return True
        if line in {".", "·", "•"}:
            return True
        boilerplate_keywords = (
            "下载小黑盒",
            "steam玩家必备",
            "小黑盒加速器",
            "黑盒语音",
            "黑盒工坊",
            "加入我们",
            "联系我们",
            "关于我们",
            "京公网安备",
            "京ICP备",
            "京网文",
            "京ICP证",
            "违法和不良信息举报",
            "举报电话",
            "立即下载",
            "打开App",
            "打开小黑盒",
            "版权所有",
        )
        lowered = line.lower()
        for keyword in boilerplate_keywords:
            if keyword.lower() in lowered:
                return True
        return False

    async def _fetch_link_api(self, link_type: str, link_id: str) -> dict:
        """调用小黑盒 API 获取帖子正文数据（复用本地签名模块）。"""
        try:
            _plugin_dir = os.path.dirname(os.path.abspath(__file__))
            if _plugin_dir not in sys.path:
                sys.path.insert(0, _plugin_dir)
            from xiaoheihe_login import (
                generate_hkey,
                WEB_CLIENT_PARAMS,
                API_BASE_URL,
            )
        except ImportError as e:
            raise RuntimeError(f"签名模块加载失败: {e}")

        import aiohttp

        path_map = {
            "bbs": "/bbs/app/link/tree",
            "pc": "/game/get_game_detail",
            "console": "/game/console/get_game_detail",
            "mobile": "/game/mobile/get_game_detail",
        }
        api_path = path_map.get(link_type, path_map["bbs"])

        base = {
            **WEB_CLIENT_PARAMS,
            "web_version": "2.5",
            "x_client_type": "web",
            "x_app": "heybox_website",
            "x_os_type": "Android",
        }
        if link_type == "bbs":
            base["link_id"] = str(link_id)
            base["limit"] = "20"
        elif link_type == "pc":
            base["steam_appid"] = str(link_id)
        else:
            base["appid"] = str(link_id)

        params = self._sign_link_api_params(api_path, base, generate_hkey)
        url = f"{API_BASE_URL}{api_path}"

        headers = {
            "Accept": "application/json",
            "Referer": "https://www.xiaoheihe.cn/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }
        cookies = self._build_cookie_dict()

        timeout = aiohttp.ClientTimeout(total=max(20, self.wait_timeout // 1000))
        async with aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=True), timeout=timeout
        ) as session:
            async with session.get(
                url, params=params, headers=headers, cookies=cookies
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)

        self._log(f"API 响应: {str(data)[:200]}")
        if not isinstance(data, dict):
            raise RuntimeError("API 返回的数据格式无效")
        return data

    def _sign_link_api_params(self, api_path: str, base: dict, generate_hkey):
        """按小黑盒 Web 端规则签名链接解析接口请求。"""
        params = dict(base or {})
        timestamp = int(time.time())
        nonce = hashlib.md5(
            f"{timestamp}{secrets.token_hex(16)}".encode(), usedforsecurity=False
        ).hexdigest().upper()
        params["_time"] = str(timestamp)
        params["nonce"] = nonce
        params["hkey"] = generate_hkey(api_path, timestamp + 1, nonce)
        if self._login_device_id:
            params["device_id"] = self._login_device_id
        return params

    def _parse_bbs_content(self, payload: dict, source_url: str) -> dict:
        """从 bbs/app/link/tree 响应中提取标题/作者/时间与按原文顺序的图文块。"""
        result = {"title": "", "author": "", "publish_time": "", "blocks": []}

        body = payload.get("result") or payload.get("data") or {}
        if not isinstance(body, dict):
            return result

        post = body.get("link") or body.get("post") or body.get("topic") or body

        title = post.get("title") or post.get("name") or body.get("title") or ""
        result["title"] = str(title).strip()

        user = post.get("user") or post.get("author") or {}
        if isinstance(user, dict):
            result["author"] = str(
                user.get("name")
                or user.get("nickname")
                or user.get("username")
                or ""
            ).strip()
        elif isinstance(user, str):
            result["author"] = user.strip()

        ts = post.get("creation_time") or post.get("created_at") or post.get("time")
        if ts:
            try:
                from datetime import datetime
                result["publish_time"] = datetime.fromtimestamp(
                    int(ts)
                ).strftime("%Y-%m-%d %H:%M")
            except Exception:
                result["publish_time"] = str(ts)

        summary_text = (
            post.get("description")
            or post.get("summary")
            or post.get("share_desc")
            or body.get("description")
            or body.get("summary")
            or ""
        )
        summary_text = self._clean_html(summary_text) if isinstance(summary_text, str) else ""

        text_content = post.get("content") or post.get("text") or post.get("description") or ""
        if isinstance(text_content, str) and text_content.strip():
            self._collect_link_text_blocks(text_content, result)

        content_list = (
            post.get("content_list")
            or post.get("content")
            or body.get("content_list")
            or []
        )
        if isinstance(content_list, str):
            text = self._clean_html(content_list)
            if text.strip():
                result["blocks"].append({"type": "text", "text": text})
        elif isinstance(content_list, list):
            for item in content_list:
                if isinstance(item, dict):
                    self._collect_content_blocks(item, result["blocks"])

        if not result["blocks"]:
            text = self._clean_html(
                str(
                    post.get("text")
                    or post.get("content_text")
                    or post.get("markdown_content")
                    or ""
                )
            )
            if text.strip():
                result["blocks"].append({"type": "text", "text": text})

        has_text_block = any(
            isinstance(block, dict) and block.get("type") == "text" and str(block.get("text") or "").strip()
            for block in result["blocks"]
        )
        if not has_text_block and summary_text and summary_text != result["title"]:
            result["blocks"].insert(0, {"type": "text", "text": summary_text})

        if not any(b.get("type") == "image" for b in result["blocks"]):
            img_list = post.get("imgs") or post.get("pictures") or []
            if isinstance(img_list, list):
                for img in img_list[:10]:
                    url = self._extract_img_url(img)
                    if url:
                        result["blocks"].append({"type": "image", "url": url})

        return result

    def _collect_link_text_blocks(self, text_content: str, result: dict):
        """解析小黑盒 link.text 里的 JSON 图文数组。"""
        try:
            parsed = json.loads(text_content)
        except Exception:
            parsed = None

        if isinstance(parsed, list):
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "").lower()
                if item_type in {"text", "txt"}:
                    text = self._clean_html(str(item.get("text") or item.get("content") or ""))
                    if text.strip():
                        result["blocks"].append({"type": "text", "text": text})
                    continue
                if item_type in {"img", "image", "pic", "picture"}:
                    url = self._extract_img_url(item.get("url") or item.get("src") or item)
                    if url:
                        result["blocks"].append({"type": "image", "url": url})
                    continue
                if item_type == "html":
                    html_text = str(item.get("text") or item.get("content") or "")
                    if html_text.strip():
                        self._collect_xhh_html_blocks(html_text, result["blocks"])
                    continue
                if item_type in {"game_card", "gamecard"}:
                    appid = str(item.get("appid") or item.get("steam_appid") or item.get("gameid") or "").strip()
                    if appid:
                        result["blocks"].append({"type": "game_card", "appid": appid})
                    continue

        elif isinstance(parsed, dict):
            self._collect_content_blocks(parsed, result["blocks"])
        else:
            text = self._clean_html(str(text_content))
            if text.strip():
                result["blocks"].append({"type": "text", "text": text})

    def _collect_xhh_html_blocks(self, html_text: str, blocks: list):
        """参考 rconsole-plugin 的方式解析小黑盒 html 图文混排正文。"""
        parts = re.split(r"(<img\b[^>]*?/?>|<iframe\b.*?</iframe>)", html_text, flags=re.IGNORECASE | re.DOTALL)
        text_buffer = []

        def flush_text():
            if not text_buffer:
                return
            text = self._clean_xhh_html_text("".join(text_buffer))
            text_buffer.clear()
            if text:
                blocks.append({"type": "text", "text": text})

        for part in parts:
            if not part:
                continue
            lower = part.lower()
            if lower.startswith("<img"):
                flush_text()
                game_match = re.search(r'data-gameid=["\']?(\d+)', part, flags=re.IGNORECASE)
                if game_match:
                    blocks.append({"type": "game_card", "appid": game_match.group(1)})
                    continue
                img_match = re.search(
                    r'(?:data-original|origin|src|url)=["\']([^"\']+)',
                    part,
                    flags=re.IGNORECASE,
                )
                if img_match:
                    url = self._extract_img_url(html.unescape(img_match.group(1)).replace("\\/", "/"))
                    if url:
                        blocks.append({"type": "image", "url": url})
                    continue
                text_buffer.append(part)
                continue
            if lower.startswith("<iframe"):
                flush_text()
                src_match = re.search(r'src=["\']([^"\']+)', part, flags=re.IGNORECASE)
                if src_match:
                    src = html.unescape(src_match.group(1)).replace("\\/", "/").replace("\\", "")
                    if src.startswith("//"):
                        src = "https:" + src
                    if src:
                        blocks.append({"type": "text", "text": f"({src})"})
                continue
            text_buffer.append(part)

        flush_text()

    def _is_redundant_summary_text(self, summary_text: str, blocks: list) -> bool:
        """判断摘要是否已经被正文首段覆盖，避免在顶部重复显示。"""
        summary_norm = re.sub(r"\s+", " ", self._clean_html(summary_text)).strip()
        if not summary_norm:
            return True

        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "text":
                continue
            text_norm = re.sub(r"\s+", " ", self._clean_html(str(block.get("text") or ""))).strip()
            if not text_norm:
                continue
            if summary_norm == text_norm:
                return True
            if summary_norm in text_norm and len(text_norm) - len(summary_norm) <= 40:
                return True
            if text_norm.startswith(summary_norm) and len(summary_norm) >= 16:
                return True
            if summary_norm.startswith(text_norm) and len(text_norm) >= 16:
                return True

        return False

    def _clean_xhh_html_text(self, text: str) -> str:
        """清理小黑盒正文 HTML，保留链接可读信息与段落换行。"""
        if not text:
            return ""

        def replace_link(match):
            href = html.unescape(match.group(1)).replace("\\/", "/").replace("\\", "")
            label = self._clean_html(match.group(2))
            if not label:
                return ""
            formatted = f"『{label}』"
            try:
                decoded = html.unescape(href)
                protocol_match = re.search(r"heybox://(\{.*\})", decoded)
                if protocol_match:
                    link_data = json.loads(protocol_match.group(1))
                    protocol_type = link_data.get("protocol_type")
                    if protocol_type == "openUser" and link_data.get("user_id"):
                        return f"{formatted} (https://www.xiaoheihe.cn/app/user/profile/{link_data['user_id']})"
                    if protocol_type == "openGameDetail" and link_data.get("app_id"):
                        game_type = link_data.get("game_type") or "pc"
                        return f"{formatted} (https://www.xiaoheihe.cn/app/topic/game/{game_type}/{link_data['app_id']})"
                    link = link_data.get("link") or {}
                    if protocol_type == "openLink" and link.get("linkid"):
                        return f"{formatted} (https://www.xiaoheihe.cn/app/bbs/link/{link['linkid']})"
            except Exception:
                return formatted
            if href.startswith("http"):
                return f"{formatted} ({href})"
            return formatted

        text = re.sub(r'<a[^>]*?href=["\']([^"\']*)["\'][^>]*?>(.*?)</a>', replace_link, text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r'<span[^>]*?data-emoji=["\']([^"\']*)["\'][^>]*?>.*?</span>', lambda m: f"[{m.group(1)}]", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"</p>|</h[1-6]>|</blockquote>|<br\s*/?>", "\n\n", text, flags=re.IGNORECASE)
        text = self._clean_html(text)
        return text.strip()

    def _parse_game_content(self, payload: dict, source_url: str) -> dict:
        """从游戏详情 API 响应中提取可读的文字与媒体。"""
        result = {"title": "", "author": "", "publish_time": "", "blocks": []}

        body = payload.get("result") or payload.get("data") or {}
        if not isinstance(body, dict):
            return result

        name = self._first_text(body, "name", "title", "game_name")
        name_en = self._first_text(body, "name_en", "english_name", "subtitle")
        if name and name_en and name_en.casefold() != name.casefold():
            result["title"] = f"{name} / {name_en}"
        else:
            result["title"] = name or name_en or "小黑盒游戏详情"

        lines = []
        self._append_field(lines, "小黑盒评分", self._first_text(body, "score", "heybox_score", "impression_score"))
        self._append_field(lines, "评分说明", self._first_text(body, "score_desc"))
        self._append_field(lines, "关注人数", self._first_text(body, "follow_num_str", "follow_num", "user_num"))
        self._append_field(lines, "平台", self._format_value(body.get("platforms_list") or body.get("platforms") or body.get("platf")))
        self._append_field(lines, "标签", self._format_tags(body.get("common_tags") or body.get("tags")))
        self._append_field(lines, "价格", self._format_price(body))

        desc = self._clean_html(
            self._first_text(
                body,
                "about_the_game",
                "desc",
                "description",
                "share_desc",
                "brief",
            )
        )
        if desc:
            lines.append("简介：")
            lines.append(self._truncate_text(desc, 1000))

        if lines:
            result["blocks"].append({"type": "text", "text": "\n".join(lines)})

        for url in self._collect_game_image_urls(body):
            result["blocks"].append({"type": "image", "url": url})

        return result

    def _collect_game_image_urls(self, body: dict) -> list[str]:
        """收集游戏封面与截图，去重并限制数量。"""
        urls = []
        for key in ("image", "share_img"):
            url = self._extract_img_url(body.get(key))
            if url:
                urls.append(url)

        topic_detail = body.get("topic_detail") or {}
        if isinstance(topic_detail, dict):
            url = self._extract_img_url(topic_detail.get("pic_url"))
            if url:
                urls.append(url)

        screenshots = body.get("screenshots") or []
        if isinstance(screenshots, list):
            for item in screenshots:
                if not isinstance(item, dict):
                    continue
                if str(item.get("type") or "").lower() == "movie":
                    url = self._extract_img_url(item.get("thumbnail"))
                else:
                    url = self._extract_img_url(item.get("url") or item.get("thumbnail"))
                if url:
                    urls.append(url)

        deduped = []
        seen = set()
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            deduped.append(url)
            if len(deduped) >= 4:
                break
        return deduped

    def _first_text(self, data: dict, *keys: str) -> str:
        for key in keys:
            value = data.get(key)
            if value is not None and value != "":
                return self._format_value(value)
        return ""

    def _append_field(self, lines: list[str], label: str, value: str):
        value = str(value or "").strip()
        if value:
            lines.append(f"{label}：{value}")

    def _format_value(self, value) -> str:
        if value is None or value == "":
            return ""
        if isinstance(value, (list, tuple, set)):
            parts = [self._format_value(item) for item in value]
            return "、".join(part for part in parts if part)
        if isinstance(value, dict):
            for key in ("name", "title", "label", "text", "value"):
                text = value.get(key)
                if text is not None and text != "":
                    return self._format_value(text)
            return ""
        return str(value).strip()

    def _format_tags(self, value) -> str:
        if isinstance(value, list):
            tags = []
            for item in value:
                text = self._format_value(item)
                if text:
                    tags.append(text)
            return "、".join(tags[:12])
        return self._format_value(value)

    def _format_price(self, body: dict) -> str:
        price_text = self._first_text(body, "price_desc", "price", "price_rich_text")
        if price_text:
            return price_text
        if body.get("is_free") in (1, True, "1", "true", "True"):
            return "免费"
        return ""

    def _truncate_text(self, text: str, limit: int) -> str:
        text = str(text or "").strip()
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + "..."

    def _collect_content_blocks(self, item: dict, blocks: list):
        """递归收集 content_list 节点，保持原文图文顺序。"""
        node_type = str(item.get("type") or item.get("box_type") or "").lower()

        if node_type in ("text", "txt", "paragraph", "p"):
            text = self._clean_html(
                str(item.get("text") or item.get("content") or item.get("value") or "")
            )
            if text.strip():
                blocks.append({"type": "text", "text": text})
            return

        if node_type in ("image", "img", "pic", "picture"):
            url = self._extract_img_url(item)
            if url:
                blocks.append({"type": "image", "url": url})
            return

        url = self._extract_img_url(item)
        if url:
            blocks.append({"type": "image", "url": url})
            return

        text = self._clean_html(str(item.get("text") or item.get("content") or ""))
        if text.strip():
            blocks.append({"type": "text", "text": text})

        for key in ("content_list", "list", "children", "items"):
            children = item.get(key)
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, dict):
                        self._collect_content_blocks(child, blocks)

    def _extract_img_url(self, item) -> str:
        """从图片节点提取并优化为原图 URL。"""
        if isinstance(item, str):
            url = item
        elif isinstance(item, dict):
            url = (
                item.get("url")
                or item.get("src")
                or item.get("origin_url")
                or item.get("full")
                or item.get("value")
                or ""
            )
            if not url:
                origin = item.get("origin") or item.get("size_big") or {}
                if isinstance(origin, dict):
                    url = origin.get("url") or origin.get("src") or ""
        else:
            url = ""
        url = str(url).strip()
        if not url:
            return ""
        if url.startswith("//"):
            url = "https:" + url
        if not url.startswith("http"):
            return ""
        # 利用小黑盒 CDN 特性获取原图
        if "?" in url and not url.endswith("\\"):
            url = url + "\\"
        return url

    def _clean_html(self, text: str) -> str:
        """去除 HTML 标签，保留纯文本。"""
        if not text:
            return ""
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        text = (
            text.replace("&nbsp;", " ")
            .replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
        )
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    async def _download_image(self, url: str):
        """下载图片字节（携带登录态）。"""
        if not url:
            return None
        try:
            import aiohttp

            headers = {
                "Referer": "https://www.xiaoheihe.cn/",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            }
            cookies = self._build_cookie_dict()
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers, cookies=cookies) as resp:
                    if resp.ok:
                        body = await resp.read()
                        if body:
                            return bytes(body)
        except Exception as e:
            self._log(f"下载图片失败 {url}: {e}")
        return None

    def _extract_bbs_video_url(self, payload: dict) -> str:
        """从 bbs/app/link/tree 响应中提取帖子视频直链。

        参考 rconsole-plugin：仅在 has_video == 1 时才取 video_url。
        """
        try:
            link = (payload.get("result") or {}).get("link") or {}
        except Exception:
            return ""
        if not isinstance(link, dict):
            return ""
        has_video = link.get("has_video")
        video_url = link.get("video_url") or link.get("videoUrl") or ""
        if has_video not in (1, "1", True) and not video_url:
            return ""
        video_url = str(video_url).strip().replace("\\/", "/").replace("\\", "")
        if video_url.startswith("//"):
            video_url = "https:" + video_url
        if not video_url.startswith("http"):
            return ""
        return video_url

    async def _send_link_video(self, event: AstrMessageEvent, video_url: str):
        """下载并发送小黑盒帖子视频，超过体积上限时改发直链。"""
        try:
            video_bytes = await self._download_video(video_url)
            if not video_bytes:
                yield event.plain_result(f"视频解析成功，但下载失败，直链：\n{video_url}")
                return

            size_mb = len(video_bytes) / (1024 * 1024)
            if self.link_video_size_limit > 0 and size_mb > self.link_video_size_limit:
                self._log(
                    f"视频体积 {size_mb:.1f}MB 超过上限 {self.link_video_size_limit}MB，改发直链"
                )
                yield event.plain_result(
                    f"视频体积约 {size_mb:.0f}MB，超过发送上限，直链：\n{video_url}"
                )
                return

            video_path = self._save_temp_image(video_bytes, suffix=".mp4")
            try:
                yield event.chain_result([Comp.Video.fromFileSystem(video_path)])
            except Exception as e:
                self._log(f"以文件方式发送视频失败，尝试直链: {e}")
                yield event.plain_result(f"视频解析成功，直链：\n{video_url}")
            finally:
                self._schedule_cleanup(video_path, delay=60.0)
        except Exception as e:
            self._log(f"发送视频失败 {video_url}: {e}")
            yield event.plain_result(f"视频解析成功，但发送失败，直链：\n{video_url}")

    async def _download_video(self, url: str):
        """下载视频字节（携带登录态）。"""
        if not url:
            return None
        try:
            import aiohttp

            headers = {
                "Referer": "https://www.xiaoheihe.cn/",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            }
            cookies = self._build_cookie_dict()
            timeout = aiohttp.ClientTimeout(total=120)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers, cookies=cookies) as resp:
                    if resp.ok:
                        body = await resp.read()
                        if body:
                            return bytes(body)
                    else:
                        self._log(f"下载视频 HTTP 状态异常 {resp.status}: {url}")
        except Exception as e:
            self._log(f"下载视频失败 {url}: {e}")
        return None

    def _build_cookie_dict(self) -> dict:
        """构建用于 HTTP 请求的 cookie 字典（扫码登录优先）。"""
        if self._login_cookies:
            return dict(self._login_cookies)
        cookies = {}
        if self.cookies:
            for pair in self.cookies.split(";"):
                pair = pair.strip()
                if not pair:
                    continue
                kv = pair.split("=", 1)
                if len(kv) == 2:
                    cookies[kv[0].strip()] = kv[1].strip()
        return cookies

    def _dedupe_link_blocks(self, blocks: list) -> list:
        """去掉 API 中 description 与 text 首段重复造成的重复正文。"""
        deduped = []
        seen_text = set()
        seen_images = set()
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text = self._clean_html(str(block.get("text") or ""))
                key = re.sub(r"\s+", " ", text).strip()
                if not key or key in seen_text:
                    continue
                seen_text.add(key)
                item = dict(block)
                item["text"] = text
                deduped.append(item)
                continue
            if block_type == "image":
                url = str(block.get("url") or "").strip()
                if not url or url in seen_images:
                    continue
                seen_images.add(url)
                deduped.append(block)
                continue
            deduped.append(block)
        return deduped

    def _build_link_forward_nodes(self, content: dict, uin: str):
        """将解析结果按原文顺序组装为一个合并转发中的多条记录节点。"""
        components = []
        nickname = "小黑盒解析"

        pending_text = []

        cover_bytes = content.get("cover_bytes")
        if cover_bytes:
            try:
                components.append(Comp.Image.fromBytes(cover_bytes))
            except Exception as e:
                self._log(f"构造封面图片失败: {e}")

        header_lines = []
        if content.get("title"):
            header_lines.append(str(content["title"]).strip())
        meta_parts = []
        if content.get("author"):
            meta_parts.append(str(content["author"]).strip())
        if content.get("publish_time"):
            meta_parts.append(str(content["publish_time"]).strip())
        if meta_parts:
            header_lines.append(" · ".join(meta_parts))

        if header_lines:
            # 合并转发里相邻的 Plain 组件会直接拼接，末尾补空行避免作者名与正文首行黏在一起。
            components.append(Comp.Plain("\n\n".join(header_lines).strip() + "\n\n"))

        def flush_text_node():
            nonlocal pending_text
            if pending_text:
                components.append(Comp.Plain("\n\n".join(pending_text).strip()))
                pending_text = []

        for block in content.get("blocks") or []:
            if block.get("type") == "text" and block.get("text"):
                text = self._clean_html(str(block["text"]))
                if text:
                    pending_text.append(text)
                continue

            if block.get("type") == "image" and block.get("image_bytes"):
                flush_text_node()
                try:
                    components.append(Comp.Image.fromBytes(block["image_bytes"]))
                except Exception as e:
                    self._log(f"构造图片节点失败: {e}")
                continue

            if block.get("type") == "game_card":
                appid = str(block.get("appid") or "").strip()
                if appid:
                    flush_text_node()
                    components.append(Comp.Plain(f"相关游戏：{appid}"))

        flush_text_node()
        if not components:
            return []
        return [Comp.Node(components, uin=uin, name=nickname)]

    def _iter_url_text_candidates(self, value):
        """递归展开消息载荷中的所有字符串候选，尽量找出被转义或嵌套的 URL。"""
        if value is None:
            return
        if isinstance(value, dict):
            for item in value.values():
                yield from self._iter_url_text_candidates(item)
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                yield from self._iter_url_text_candidates(item)
            return

        text = str(value)
        if not text:
            return

        yield text

        normalized = self._normalize_url_text(text)
        if normalized and normalized != text:
            yield normalized

        if text[:1] in "[{":
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            if parsed is not None:
                yield from self._iter_url_text_candidates(parsed)

    def _normalize_url_text(self, text: str) -> str:
        """对 QQ 卡片里常见的转义形式做轻量反解，避免 URL 被转义层层包住。"""
        if not text:
            return ""

        text = html.unescape(text)
        text = text.replace("\\/", "/")
        text = text.replace(r"\/", "/")
        text = text.replace("\\u002F", "/").replace("\u002F", "/")
        text = text.replace("\\u003A", ":").replace("\u003A", ":")
        text = text.replace("\\u0026", "&").replace("\u0026", "&")
        text = text.replace("\\u003F", "?").replace("\u003F", "?")
        text = text.replace("\\u003D", "=").replace("\u003D", "=")
        text = text.replace("\\u0023", "#").replace("\u0023", "#")
        text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)
        return text

    def _get_forward_uin(self, event: AstrMessageEvent) -> str:
        """获取用于合并转发节点的发送者 ID。"""
        try:
            uin = event.get_self_id()
            if uin:
                return str(uin)
        except Exception:
            pass
        try:
            uin = getattr(getattr(event, "message_obj", None), "self_id", None)
            if uin:
                return str(uin)
        except Exception:
            pass
        return "10000"

    # ==================== 工具方法 ====================

    async def _prepare_link_page_for_screenshot(self, page):
        """隐藏帖子页评论、吸底输入框和浮动操作栏，避免长截图时遮挡正文。"""
        await page.evaluate(
            r"""() => {
                const hide = element => {
                    if (!element || element.dataset.codexHidden === '1') return;
                    element.dataset.codexHidden = '1';
                    element.style.setProperty('display', 'none', 'important');
                };

                const hideSelectors = [
                    '[class*="comment" i]',
                    '[class*="reply" i]',
                    '[class*="bottom" i][class*="bar" i]',
                    '[class*="footer" i]',
                    '[class*="action" i][class*="bar" i]',
                    '[placeholder*="评论"]',
                    '[placeholder*="回复"]',
                ];

                for (const selector of hideSelectors) {
                    document.querySelectorAll(selector).forEach(node => {
                        const text = node.innerText || node.textContent || node.getAttribute('placeholder') || '';
                        const rect = node.getBoundingClientRect();
                        if (/评论|回复|点赞|分享|收藏/.test(text) || rect.bottom > window.innerHeight * 0.55) {
                            hide(node.closest('section, footer, form, nav, div') || node);
                        }
                    });
                }

                document.querySelectorAll('body *').forEach(node => {
                    const style = window.getComputedStyle(node);
                    if (!['fixed', 'sticky'].includes(style.position)) return;

                    const text = node.innerText || node.textContent || '';
                    const rect = node.getBoundingClientRect();
                    const isBottomOverlay = rect.top > window.innerHeight * 0.45;
                    const isCommentOverlay = /评论|回复|点赞|分享|收藏/.test(text);
                    const isSmallToolbar = rect.height < 180 && rect.width > window.innerWidth * 0.25;

                    if ((isBottomOverlay && isSmallToolbar) || isCommentOverlay) {
                        hide(node);
                    }
                });

                document.documentElement.style.scrollBehavior = 'auto';
                window.scrollTo(0, 0);
            }"""
        )

    async def _find_article_content_element(self, page):
        """为帖子链接挑选正文容器，尽量避开评论区和整页根节点。"""
        handle = await page.evaluate_handle(
            r"""() => {
                const badText = /评论|回复|点赞|分享|收藏|去搜索|相关搜索/;
                const nodes = [...document.querySelectorAll('article, main, section, [class*="post" i], [class*="article" i], [class*="detail" i], [class*="content" i], [class*="bbs" i], div')];
                let best = null;
                let bestScore = 0;

                for (const node of nodes) {
                    if (['HTML', 'BODY', 'SCRIPT', 'STYLE'].includes(node.tagName)) continue;
                    const rect = node.getBoundingClientRect();
                    if (rect.width < 260 || rect.height < 220) continue;

                    const style = window.getComputedStyle(node);
                    if (style.display === 'none' || style.visibility === 'hidden') continue;

                    const text = (node.innerText || node.textContent || '').trim();
                    if (text.length < 80) continue;

                    const commentPenalty = badText.test(text) ? 450 : 0;
                    const rootPenalty = ['app', 'root'].includes(node.id || '') ? 800 : 0;
                    const navPenalty = node.querySelectorAll('nav, footer, input, textarea').length * 120;
                    const mediaScore = node.querySelectorAll('img, video').length * 80;
                    const textScore = Math.min(text.length, 2600);
                    const score = textScore + mediaScore - commentPenalty - rootPenalty - navPenalty;

                    if (score > bestScore) {
                        best = node;
                        bestScore = score;
                    }
                }

                return best;
            }"""
        )
        return handle.as_element()

    def _extract_url_from_text(self, text: str) -> str | None:
        """从指令参数中提取小黑盒链接，支持直接贴专题 URL。"""
        match = re.search(
            r"https?://(?:[a-z0-9.-]*\.)?xiaoheihe\.cn[^\s\"'<>]*",
            text or "",
            re.IGNORECASE,
        )
        if not match:
            return None
        return match.group(0).replace("https://xiaoheihe.cn", "https://www.xiaoheihe.cn")

    async def _find_game_topic_href(self, page, game: str) -> str | None:
        """从新版搜索页扫描游戏专题链接。"""
        normalized_game = re.sub(r"\s+", "", game or "").lower()
        candidates = await page.evaluate(
            r"""() => {
                const gameHref = /\/app\/topic\/game\//;
                const nodes = [...document.querySelectorAll('[href], [data-href], [data-url], [onclick]')];
                const results = [];

                for (const node of nodes) {
                    const values = [
                        node.getAttribute('href'),
                        node.getAttribute('data-href'),
                        node.getAttribute('data-url'),
                        node.getAttribute('onclick'),
                    ].filter(Boolean);

                    for (const value of values) {
                        const match = String(value).match(/(?:https?:\/\/[^\s'\"]+)?\/app\/topic\/game\/[^\s'\")]+/);
                        if (!match || !gameHref.test(match[0])) continue;
                        results.push({
                            href: match[0],
                            text: (node.innerText || node.textContent || '').trim(),
                        });
                    }
                }

                const seen = new Set();
                return results.filter(item => {
                    if (seen.has(item.href)) return false;
                    seen.add(item.href);
                    return true;
                });
            }"""
        )

        if not candidates:
            self._log("[Plan D] 未扫描到任何游戏专题链接")
            return None

        for item in candidates:
            text = re.sub(r"\s+", "", item.get("text") or "").lower()
            if normalized_game and normalized_game in text:
                return item.get("href")

        return candidates[0].get("href")

    async def _click_game_result_by_text(self, page, game: str) -> bool:
        """点击新版搜索页中与游戏名匹配的可见结果。"""
        normalized_game = re.sub(r"\s+", "", game or "").lower()
        if not normalized_game:
            return False

        handle = await page.evaluate_handle(
            r"""target => {
                const normalize = value => String(value || '').replace(/\s+/g, '').toLowerCase();
                const blockedTags = new Set(['HTML', 'BODY', 'SCRIPT', 'STYLE']);
                const nodes = [...document.querySelectorAll('a, button, [role="button"], [onclick], div, li')];

                for (const node of nodes) {
                    if (blockedTags.has(node.tagName)) continue;
                    const text = normalize(node.innerText || node.textContent);
                    if (!text || !text.includes(target)) continue;

                    const rect = node.getBoundingClientRect();
                    if (rect.width < 20 || rect.height < 20) continue;
                    const style = window.getComputedStyle(node);
                    if (style.visibility === 'hidden' || style.display === 'none') continue;

                    return node.closest('a, button, [role="button"], [onclick]') || node;
                }

                return null;
            }""",
            normalized_game,
        )
        element = handle.as_element()
        if not element:
            return False

        await element.click(timeout=5000)
        return True

    async def _find_first_visible_element(self, page, selectors: list[str], timeout: int = 0):
        """按候选选择器查找首个可见元素，适配小黑盒频繁变更的 class。"""
        deadline = timeout / 1000
        start = asyncio.get_running_loop().time()

        while True:
            for selector in selectors:
                try:
                    elements = await page.query_selector_all(selector)
                    for element in elements:
                        box = await element.bounding_box()
                        if box and box.get("width", 0) > 20 and box.get("height", 0) > 20:
                            return element
                except Exception as e:
                    self._log(f"检查候选容器 {selector} 时失败: {e}")

            if timeout <= 0 or asyncio.get_running_loop().time() - start >= deadline:
                return None
            await asyncio.sleep(0.5)

    async def _describe_element(self, element) -> str:
        """生成用于日志的元素描述。"""
        try:
            return await element.evaluate(
                """el => {
                    const id = el.id ? `#${el.id}` : '';
                    const classes = typeof el.className === 'string'
                        ? el.className.trim().split(/\\s+/).filter(Boolean).map(c => `.${c}`).join('')
                        : '';
                    return `${el.tagName.toLowerCase()}${id}${classes}`;
                }"""
            )
        except Exception:
            return "unknown element"

    async def _take_screenshot_with_fallback(self, page, element=None, found_selector="") -> bytes:
        """带降级策略的网页截图"""
        image_bytes = None
        
        if element:
            try:
                await element.scroll_into_view_if_needed(timeout=5000)
                image_bytes = await element.screenshot(
                    type="jpeg", quality=self.image_quality, timeout=self.wait_timeout,
                )
                return image_bytes
            except Exception as el_err:
                self._log(f"元素截图失败 ({found_selector}): {el_err}，回退到全页截图")
        else:
            self._log("未找到任何主要内容区域，进行全页截图")

        try:
            image_bytes = await page.screenshot(
                full_page=True, type="jpeg", quality=self.image_quality
            )
            return image_bytes
        except Exception as fp_err:
            reason = "元素截图后全页截图也失败" if element else "全页截图失败"
            self._log(f"{reason}: {fp_err}，回退到最后手段：视口截图")
        
        image_bytes = await page.screenshot(type="jpeg", quality=self.image_quality)
        return image_bytes

    def _save_temp_image(self, image_bytes: bytes, suffix: str = ".jpg") -> str:
        """保存临时截图并返回文件路径"""
        temp_dir = tempfile.gettempdir()
        file_path = os.path.join(
            temp_dir, f"xiaoheihe_{uuid.uuid4().hex}{suffix}"
        )
        with open(file_path, "wb") as f:
            f.write(image_bytes)
        self._temp_files.add(file_path)
        self._log(f"临时截图已保存: {file_path}")
        return file_path

    def _schedule_cleanup(self, file_path: str, delay: float = 10.0):
        """延迟清理临时文件，确保图片发送完成后再删除"""
        def cleanup():
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    self._temp_files.discard(file_path)
                    self._log(f"已清理临时截图: {file_path}")
            except Exception as e:
                self._log(f"清理临时截图失败 {file_path}: {e}")
        
        asyncio.get_running_loop().call_later(delay, cleanup)

    # ==================== 扫码登录 ====================

    def _build_cookie_list(self) -> list[dict[str, str]]:
        """构建注入浏览器的 Cookie 列表，扫码登录 Cookie 优先"""
        if self._login_cookies:
            self._log("使用扫码登录的 Cookie")
            return [
                {"name": k, "value": v, "domain": ".xiaoheihe.cn", "path": "/"}
                for k, v in self._login_cookies.items()
            ]
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
                else:
                    self._log(f"警告：跳过了格式无效的 Cookie 项 - {pair}")
            return cookie_list
        return []

    def _load_credentials(self):
        """加载保存的扫码登录凭证"""
        # 迁移旧版本存放在插件目录内的凭证文件，避免更新插件后掉登录。
        self._migrate_legacy_credentials()
        try:
            if os.path.exists(self._credentials_path):
                with open(self._credentials_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                cookies = data.get("cookies", {})
                if isinstance(cookies, dict) and cookies:
                    self._login_cookies = {
                        str(k): str(v) for k, v in cookies.items()
                    }
                    self._login_uid = str(data.get("uid", ""))
                    self._login_nickname = str(data.get("nickname", ""))
                    self._login_device_id = str(data.get("device_id", ""))
                    self._log(
                        f"已加载扫码登录凭证 "
                        f"(uid={self._login_uid}, 昵称={self._login_nickname})"
                    )
        except Exception as e:
            self._log(f"加载登录凭证失败: {e}")

    def _resolve_credentials_path(self) -> str:
        """确定凭证文件路径，优先放在 AstrBot 数据目录，随插件更新保留。

        插件更新会覆盖插件目录本身，因此把凭证写在插件目录内会掉登录。
        这里优先使用 StarTools.get_data_dir()（data/plugin_data/<插件名>），
        拿不到时退回到 AstrBot data 目录，最后才退回插件目录。
        """
        plugin_name = "astrbot_plugin_steaminfo_xiaoheihe"
        try:
            from astrbot.api.star import StarTools

            data_dir = StarTools.get_data_dir(plugin_name)
            if data_dir:
                os.makedirs(str(data_dir), exist_ok=True)
                return os.path.join(str(data_dir), "credentials.json")
        except Exception as e:
            self._log(f"获取插件数据目录失败，尝试其它路径: {e}")
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            base = os.path.join(
                get_astrbot_data_path(), "plugin_data", plugin_name
            )
            os.makedirs(base, exist_ok=True)
            return os.path.join(base, "credentials.json")
        except Exception as e:
            self._log(f"获取 AstrBot 数据目录失败，退回插件目录: {e}")
        return os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "credentials.json"
        )

    def _migrate_legacy_credentials(self):
        """把旧版本写在插件目录内的 credentials.json 迁移到数据目录。"""
        try:
            legacy_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "credentials.json"
            )
            if legacy_path == self._credentials_path:
                return
            if os.path.exists(legacy_path) and not os.path.exists(self._credentials_path):
                os.makedirs(os.path.dirname(self._credentials_path), exist_ok=True)
                with open(legacy_path, "r", encoding="utf-8") as f:
                    data = f.read()
                with open(self._credentials_path, "w", encoding="utf-8") as f:
                    f.write(data)
                try:
                    os.remove(legacy_path)
                except Exception:
                    pass
                self._log(f"已迁移旧登录凭证到: {self._credentials_path}")
        except Exception as e:
            self._log(f"迁移旧登录凭证失败: {e}")

    def _save_credentials(
        self, cookies: dict[str, str], uid: str, nickname: str, device_id: str
    ):
        """保存扫码登录凭证到文件"""
        from datetime import datetime

        data = {
            "cookies": cookies,
            "uid": uid,
            "nickname": nickname,
            "device_id": device_id,
            "logged_in_at": datetime.now().isoformat(),
        }
        try:
            os.makedirs(os.path.dirname(self._credentials_path), exist_ok=True)
        except Exception as e:
            self._log(f"创建凭证目录失败: {e}")
        with open(self._credentials_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        self._login_cookies = cookies
        self._login_uid = uid
        self._login_nickname = nickname
        self._login_device_id = device_id
        self._log(f"扫码登录凭证已保存 (uid={uid}, 昵称={nickname})")

    def _clear_credentials(self):
        """清除保存的扫码登录凭证"""
        self._login_cookies = {}
        self._login_uid = ""
        self._login_nickname = ""
        self._login_device_id = ""
        try:
            if os.path.exists(self._credentials_path):
                os.remove(self._credentials_path)
                self._log("已删除凭证文件")
        except Exception as e:
            self._log(f"删除凭证文件失败: {e}")

    # ==================== 生命周期 ====================

    async def terminate(self):
        """插件卸载/停用时调用"""
        async with self._browser_lock:
            if self._browser and self._browser.is_connected():
                await self._browser.close()
                self._log("浏览器已关闭")

            if self._playwright:
                await self._playwright.stop()
                self._log("Playwright 已停止")

            self._browser = None
            self._playwright = None
            self._playwright_manager = None
        
        # 卸载时彻底清理可能的残留文件
        for file_path in list(self._temp_files):
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    self._log(f"卸载时清理残留截图文件: {file_path}")
            except Exception:
                pass
        self._temp_files.clear()

        logger.info("小黑盒游戏截图插件已停用")

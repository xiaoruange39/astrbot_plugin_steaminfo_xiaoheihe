import re
import json
import asyncio
import os
import tempfile
import uuid
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
        self.debug: bool = config.get("debug", False)

        # Playwright 实例（延迟初始化）
        self._playwright_manager = None
        self._playwright = None
        self._browser: Browser | None = None
        self._browser_lock = asyncio.Lock()

        # 限制并发截图数量，防止 OOM
        self._semaphore = asyncio.Semaphore(2)

        # 跟踪临时文件以便退出时统一清理
        self._temp_files = set()

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
        if self.cookies:
            try:
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
                if cookie_list:
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

                # 尝试做全文正则搜索
                json_text = json.dumps(raw_data, ensure_ascii=False) if isinstance(raw_data, (dict, list)) else str(raw_data)
                m = url_pattern.search(json_text)
                if m:
                    return m.group(0)
        except Exception as e:
            self._log(f"解析 JSON 卡片消息时出错: {e}")

        return None

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有消息，处理无前缀指令和自动解析小黑盒链接"""
        content = event.message_str or ""

        # 0. 无前缀触发逻辑已移除，由 @filter.command(ignore_prefix=True) 处理

        if not self.enable_link_preview:
            return

        target_url = self._extract_xiaoheihe_url(event)
        if not target_url:
            return

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

    def _save_temp_image(self, image_bytes: bytes) -> str:
        """保存临时截图并返回文件路径"""
        temp_dir = tempfile.gettempdir()
        file_path = os.path.join(
            temp_dir, f"xiaoheihe_{uuid.uuid4().hex}.jpg"
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

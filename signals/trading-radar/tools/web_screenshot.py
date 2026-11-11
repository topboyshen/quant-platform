#!/usr/bin/env python3
"""
网页截图监控工具
支持 Playwright 和 Camoufox 浏览器

用法:
    # 截图单个网页
    python tools/web_screenshot.py https://quote.eastmoney.com/concept/sh600519.html

    # 使用 Camoufox （反检测）
    python tools/web_screenshot.py https://quote.eastmoney.com/concept/sh600519.html --camoufox

    # 监控多个页面（对比变化）
    python tools/web_screenshot.py https://example.com --monitor
"""

import os
import sys
import argparse
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Optional

# 确保在正确的环境
os.chdir(Path(__file__).parent.parent)


def ensure_dirs():
    """确保目录存在"""
    Path("data/screenshots").mkdir(parents=True, exist_ok=True)
    Path("data/screenshots/diff").mkdir(parents=True, exist_ok=True)


def take_screenshot_playwright(url: str, output_path: str, full_page: bool = True, wait_time: int = 3):
    """使用 Playwright 截图"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        )
        page = context.new_page()

        try:
            page.goto(url, wait_until='networkidle', timeout=30000)
            page.wait_for_timeout(wait_time * 1000)
            page.screenshot(path=output_path, full_page=full_page)
            print(f"✅ 截图已保存: {output_path}")
        except Exception as e:
            print(f"❌ 截图失败: {e}")
        finally:
            context.close()
            browser.close()


def take_screenshot_camoufox(url: str, output_path: str, full_page: bool = True, wait_time: int = 3):
    """使用 Camoufox 截图（反检测）"""
    try:
        from camoufox.sync_api import Camoufox

        with Camoufox(headless=True) as browser:
            page = browser.new_page()
            page.goto(url, wait_until='networkidle', timeout=30000)
            page.wait_for_timeout(wait_time * 1000)
            page.screenshot(path=output_path, full_page=full_page)
            print(f"✅ Camoufox 截图已保存: {output_path}")
    except ImportError:
        print("⚠️ Camoufox 未安装，回退到 Playwright")
        take_screenshot_playwright(url, output_path, full_page, wait_time)
    except Exception as e:
        print(f"❌ Camoufox 截图失败: {e}")


def compare_screenshots(old_path: str, new_path: str) -> bool:
    """比较两张截图是否相同"""
    if not os.path.exists(old_path):
        return True

    # 使用文件哈希比较
    def file_hash(path):
        with open(path, 'rb') as f:
            return hashlib.md5(f.read()).hexdigest()

    return file_hash(old_path) != file_hash(new_path)


def monitor_websites(urls: list, use_camoufox: bool = False):
    """监控多个网页变化"""
    ensure_dirs()
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    changes = []

    for url in urls:
        url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
        new_path = f"data/screenshots/{url_hash}_{timestamp}.png"
        latest_path = f"data/screenshots/{url_hash}_latest.png"

        print(f"🔍 监控: {url}")

        if use_camoufox:
            take_screenshot_camoufox(url, new_path)
        else:
            take_screenshot_playwright(url, new_path)

        if os.path.exists(new_path):
            if compare_screenshots(latest_path, new_path):
                print(f"  🔔 发现变化!")
                changes.append(url)

                # 保存历史版本
                if os.path.exists(latest_path):
                    backup_name = f"data/screenshots/diff/{url_hash}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                    os.rename(latest_path, backup_name)

                # 更新最新版本
                os.rename(new_path, latest_path)
            else:
                print(f"  ✅ 无变化")
                os.remove(new_path)

    return changes


def main():
    parser = argparse.ArgumentParser(description='网页截图监控工具')
    parser.add_argument('url', help='目标网页 URL')
    parser.add_argument('--camoufox', action='store_true', help='使用 Camoufox 反检测浏览器')
    parser.add_argument('--output', help='输出路径')
    parser.add_argument('--wait', type=int, default=3, help='等待时间（秒）')
    parser.add_argument('--monitor', action='store_true', help='监控模式（保存历史对比）')
    args = parser.parse_args()

    ensure_dirs()

    if args.monitor:
        changes = monitor_websites([args.url], args.camoufox)
        if changes:
            print(f"\n🔔 发现 {len(changes)} 个页面发生变化")
        else:
            print(f"\n✅ 页面未发生变化")
    else:
        # 单次截图
        if not args.output:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            url_hash = hashlib.md5(args.url.encode()).hexdigest()[:8]
            args.output = f"data/screenshots/{url_hash}_{timestamp}.png"

        if args.camoufox:
            take_screenshot_camoufox(args.url, args.output, wait_time=args.wait)
        else:
            take_screenshot_playwright(args.url, args.output, wait_time=args.wait)


if __name__ == "__main__":
    main()

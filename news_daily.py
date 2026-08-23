#!/usr/bin/env python3
"""
每日新闻邮件发送脚本
功能：收集当天全球重要新闻 + 中国新闻，AI生成详细总结和深度点评，通过 SMTP 邮箱发送
      邮件顶部附带次日天气（温度、湿度、天气状况、降雨时段预测）
      严格限定仅推送当天新闻，自动去重已推送内容
环境支持：本地运行（config.json）/ GitHub Actions 云端运行（环境变量 secrets）
"""

import json
import logging
import os
import re
import smtplib
import sys
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from typing import Optional

import requests
import feedparser

# ============================================================
# Windows 控制台 UTF-8 编码修复
# ============================================================
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ============================================================
# 路径和常量
# ============================================================
SCRIPT_DIR = Path(__file__).parent
LOG_FILE = SCRIPT_DIR / "news_daily.log"
SENT_NEWS_FILE = SCRIPT_DIR / "sent_news.json"

# 内置默认配置（config.json 可覆盖，环境变量再覆盖 secrets 部分）
DEFAULT_RSS_FEEDS = [
    {"name": "人民网-国内", "url": "http://www.people.com.cn/rss/politics.xml"},
    {"name": "人民网-国际", "url": "http://www.people.com.cn/rss/world.xml"},
    {"name": "人民网-社会", "url": "http://www.people.com.cn/rss/society.xml"},
    {"name": "人民网-财经", "url": "http://www.people.com.cn/rss/finance.xml"},
    {"name": "人民网-科技", "url": "http://www.people.com.cn/rss/scitech.xml"},
    {"name": "36氪", "url": "https://36kr.com/feed"},
]

DEFAULT_CONFIG = {
    "smtp": {
        "server": "smtp.qq.com",
        "port": 465,
        "sender": "",       # 必填：发件邮箱
        "password": "",     # 必填：SMTP 授权码
        "receiver": "",     # 必填：收件邮箱
    },
    "deepseek": {
        "api_key": "",      # 必填：DeepSeek API Key
        "api_url": "https://api.deepseek.com/v1/chat/completions",
        "model": "deepseek-v4-flash",
        "enable_search": True,
        "timeout": 120,
    },
    "rss_feeds": DEFAULT_RSS_FEEDS,
    "weather": {
        # 邮件顶部天气卡片的位置，可改为任意城市
        "lat": 39.90,
        "lon": 116.40,
        "location": "北京",
    },
}


# ============================================================
# 配置加载（三层合并：默认值 → config.json → 环境变量）
# ============================================================
def load_config() -> dict:
    """
    加载配置，按优先级合并三层来源：
    1. 内置默认值 DEFAULT_CONFIG（零配置可运行）
    2. config.json 覆盖（本地个性化：RSS 源、天气位置等）
    3. 环境变量覆盖 secrets（GitHub Actions 使用，敏感信息不入库）
    """
    config = {
        "smtp": dict(DEFAULT_CONFIG["smtp"]),
        "deepseek": dict(DEFAULT_CONFIG["deepseek"]),
        "rss_feeds": list(DEFAULT_CONFIG["rss_feeds"]),
        "weather": dict(DEFAULT_CONFIG["weather"]),
    }

    config_file = SCRIPT_DIR / "config.json"
    if config_file.exists():
        with open(config_file, "r", encoding="utf-8") as f:
            file_config = json.load(f)
        for section, values in file_config.items():
            if isinstance(values, dict) and isinstance(config.get(section), dict):
                config[section].update(values)
            else:
                config[section] = values

    env_map = {
        "SMTP_PASSWORD": ("smtp", "password"),
        "SMTP_SENDER": ("smtp", "sender"),
        "SMTP_RECEIVER": ("smtp", "receiver"),
        "DEEPSEEK_API_KEY": ("deepseek", "api_key"),
    }
    for env_name, (section, key) in env_map.items():
        value = os.environ.get(env_name, "")
        if value:
            config[section][key] = value

    missing = [k for k in ("sender", "receiver", "password") if not config["smtp"][k]]
    if not config["deepseek"]["api_key"]:
        missing.append("DEEPSEEK_API_KEY")
    if missing:
        raise ValueError(
            "缺少必要配置: " + ", ".join(missing) + "。"
            "请在 config.json 中填写，或设置环境变量 "
            "(SMTP_PASSWORD / SMTP_SENDER / SMTP_RECEIVER / DEEPSEEK_API_KEY)"
        )

    return config


# ============================================================
# 日志配置
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


# ============================================================
# 天气获取（Open-Meteo 免费 API，无需注册）
# ============================================================
WMO_CODES = {
    0:  ("晴 ☀️", "sunny"),
    1:  ("大部晴朗 🌤️", "mostly_clear"),
    2:  ("多云 ⛅", "partly_cloudy"),
    3:  ("阴天 ☁️", "overcast"),
    45: ("雾 🌫️", "fog"),
    48: ("雾凇 🌫️", "rime_fog"),
    51: ("小毛毛雨 🌧️", "light_drizzle"),
    53: ("毛毛雨 🌧️", "moderate_drizzle"),
    55: ("大毛毛雨 🌧️", "dense_drizzle"),
    56: ("冻毛毛雨 🌨️", "freezing_drizzle"),
    57: ("冻毛毛雨 🌨️", "freezing_drizzle"),
    61: ("小雨 🌧️", "slight_rain"),
    63: ("中雨 🌧️", "moderate_rain"),
    65: ("大雨 🌧️", "heavy_rain"),
    66: ("冻雨 🌨️", "freezing_rain"),
    67: ("冻雨 🌨️", "freezing_rain"),
    71: ("小雪 ❄️", "slight_snow"),
    73: ("中雪 ❄️", "moderate_snow"),
    75: ("大雪 ❄️", "heavy_snow"),
    77: ("雪粒 ❄️", "snow_grains"),
    80: ("阵雨 ⛈️", "rain_showers"),
    81: ("中阵雨 ⛈️", "moderate_rain_showers"),
    82: ("大阵雨 ⛈️", "violent_rain_showers"),
    85: ("阵雪 ❄️", "snow_showers"),
    86: ("大阵雪 ❄️", "heavy_snow_showers"),
    95: ("雷暴 ⚡", "thunderstorm"),
    96: ("雷暴+冰雹 ⚡", "thunderstorm_hail"),
    99: ("强雷暴+冰雹 ⚡", "severe_thunderstorm_hail"),
}


def fetch_weather(config: dict) -> Optional[dict]:
    """
    从 Open-Meteo 获取次日天气数据（温度、湿度、天气、降雨时段）
    位置取自 config 的 weather 节；返回 dict 或 None
    """
    weather_cfg = config.get("weather", DEFAULT_CONFIG["weather"])
    lat = weather_cfg["lat"]
    lon = weather_cfg["lon"]
    location = weather_cfg["location"]

    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max"
        f"&hourly=temperature_2m,relativehumidity_2m,precipitation_probability,weathercode"
        f"&timezone=Asia/Shanghai"
        f"&forecast_days=2"
    )

    try:
        logger.info("获取天气数据...")
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        raw = resp.json()

        daily = raw.get("daily", {})
        hourly = raw.get("hourly", {})

        idx = 1 if len(daily.get("time", [])) > 1 else 0

        wmo_code = daily["weathercode"][idx]
        weather_desc, _ = WMO_CODES.get(wmo_code, (f"未知({wmo_code})", "unknown"))

        temp_max = daily["temperature_2m_max"][idx]
        temp_min = daily["temperature_2m_min"][idx]
        precip_sum = daily["precipitation_sum"][idx]
        precip_prob = daily["precipitation_probability_max"][idx]

        rain_codes = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}
        is_rain_day = wmo_code in rain_codes or precip_prob >= 50

        hourly_times = hourly.get("time", [])
        hourly_precip_prob = hourly.get("precipitation_probability", [])
        hourly_rh = hourly.get("relativehumidity_2m", [])
        hourly_temp = hourly.get("temperature_2m", [])
        hourly_wmo = hourly.get("weathercode", [])

        tomorrow_str = daily["time"][idx]
        tomorrow_hours = [
            i for i, t in enumerate(hourly_times) if t.startswith(tomorrow_str)
        ]

        rh_values = [hourly_rh[i] for i in tomorrow_hours if i < len(hourly_rh)]
        avg_humidity = round(sum(rh_values) / len(rh_values)) if rh_values else None

        rain_periods = []
        if is_rain_day and tomorrow_hours:
            rain_slots = []
            for i in tomorrow_hours:
                prob = hourly_precip_prob[i] if i < len(hourly_precip_prob) else 0
                code = hourly_wmo[i] if i < len(hourly_wmo) else 0
                hour = int(hourly_times[i].split("T")[1].split(":")[0])
                if prob >= 40 or code in rain_codes:
                    rain_slots.append(hour)

            if rain_slots:
                periods = []
                start = rain_slots[0]
                end = rain_slots[0]
                for h in rain_slots[1:]:
                    if h == end + 1:
                        end = h
                    else:
                        periods.append((start, end))
                        start = h
                        end = h
                periods.append((start, end))
                rain_periods = [f"{s:02d}:00-{e+1:02d}:00" for s, e in periods]

        temp_values = [hourly_temp[i] for i in tomorrow_hours if i < len(hourly_temp)]
        avg_temp = round(sum(temp_values) / len(temp_values)) if temp_values else None

        result = {
            "location": location,
            "date": tomorrow_str,
            "weather_desc": weather_desc,
            "temp_max": temp_max,
            "temp_min": temp_min,
            "avg_temp": avg_temp,
            "humidity": avg_humidity,
            "precip_sum": precip_sum,
            "precip_prob": precip_prob,
            "rain_periods": rain_periods,
            "is_rain_day": is_rain_day,
        }

        logger.info(
            f"天气: {weather_desc} {temp_min}~{temp_max}°C "
            f"湿度{avg_humidity}% 降水概率{precip_prob}% "
            f"降雨时段:{rain_periods if rain_periods else '无'}"
        )
        return result

    except Exception as e:
        logger.error(f"天气获取失败: {e}")
        return None


# ============================================================
# RSS 新闻抓取（仅保留今天的新闻作为候选）
# ============================================================
def fetch_rss_news(config: dict) -> list[dict]:
    """从 RSS 源抓取候选新闻，仅保留今天发布的条目"""
    all_entries = []
    seen_urls = set()
    today_key = datetime.now().strftime("%Y-%m-%d")

    for feed_info in config.get("rss_feeds", []):
        name = feed_info["name"]
        url = feed_info["url"]
        try:
            logger.info(f"RSS: {name}")
            feed = feedparser.parse(url)
            if feed.bozo and not feed.entries:
                logger.warning(f"  {name}: 解析失败,跳过")
                continue

            count = 0
            today_count = 0
            for entry in feed.entries:
                link = entry.get("link", "")
                title = entry.get("title", "").strip()
                if not title or link in seen_urls:
                    continue
                seen_urls.add(link)

                published = None
                is_today = False
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    try:
                        published = datetime(*entry.published_parsed[:6])
                        is_today = published.strftime("%Y-%m-%d") == today_key
                    except (TypeError, ValueError):
                        pass

                summary = ""
                if hasattr(entry, "summary"):
                    summary = re.sub(r"<[^>]+>", "", entry.get("summary", "")).strip()[:300]

                all_entries.append({
                    "title": title, "link": link, "source": name,
                    "published": published.isoformat() if published else None,
                    "summary": summary,
                    "is_today": is_today,
                })
                count += 1
                if is_today:
                    today_count += 1

            logger.info(f"  {name}: +{count}条 (今日{today_count}条)")
        except Exception as e:
            logger.warning(f"  {name}: 异常 - {e}")

    # 今日新闻排最前，其余的放后面作为后备
    all_entries.sort(key=lambda e: (not e.get("is_today", False), e.get("published") or "0000"), reverse=False)
    logger.info(f"RSS候选: {len(all_entries)} 条")
    return all_entries[:200]


# ============================================================
# 已发送新闻去重缓存
# ============================================================
def load_sent_news() -> list[str]:
    """加载近期已推送的新闻标题列表"""
    if SENT_NEWS_FILE.exists():
        try:
            with open(SENT_NEWS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                titles = data.get("recent_titles", [])
                last_date = data.get("last_date", "")
                if len(titles) > 120:
                    titles = titles[-120:]
                logger.info(f"已发送新闻缓存: {len(titles)} 条 (最近日期: {last_date})")
                return titles
        except (json.JSONDecodeError, IOError):
            pass
    return []


def save_sent_news(titles: list[str]) -> None:
    """保存今日已发送的新闻标题"""
    unique = list(dict.fromkeys(titles))
    if len(unique) > 120:
        unique = unique[-120:]
    with open(SENT_NEWS_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"recent_titles": unique, "last_date": datetime.now().strftime("%Y-%m-%d")},
            f, ensure_ascii=False, indent=2,
        )
    logger.info(f"已保存 {len(unique)} 条新闻到去重缓存")


# ============================================================
# DeepSeek Prompt 构建（强化时效性+去重）
# ============================================================
def build_news_prompt(rss_candidates: list[dict], recent_titles: list[str] = None) -> str:
    """构建发送给 DeepSeek 的 prompt"""
    today_str = datetime.now().strftime("%Y年%m月%d日")

    dedup_text = ""
    if recent_titles:
        dedup_text = "\n--- 以下标题近期已推送过，今日请务必避免重复 ---\n"
        for i, title in enumerate(recent_titles[-60:], 1):
            dedup_text += f"  {i}. {title}\n"

    rss_text = ""
    if rss_candidates:
        rss_text = "\n--- 今日RSS新闻源参考 ---\n"
        for i, entry in enumerate(rss_candidates[:80], 1):
            today_mark = "[今日]" if entry.get("is_today") else ""
            rss_text += f"{i}. {today_mark}[{entry['source']}] {entry['title']}\n"

    prompt = (
        f"你是资深新闻评论员。请整理今天（{today_str}）最重要的新闻。\n\n"
        f"{rss_text}\n"
        f"{dedup_text}\n"
        f"!!! 时效性硬性要求（最重要，违反直接作废）!!!\n"
        f"每条新闻必须是 {today_str} 当天发生或首次报道的事件！\n"
        f"搜索时在关键词中加上日期过滤 \"{today_str}\"\n"
        f"输出前逐条自检：这是 {today_str} 的新闻吗？不是的话立即删除换一条！\n"
        f"绝对禁止：昨天的旧闻、上周回顾、\"近日\"\"日前\"类模糊时间新闻\n"
        f"如果某领域搜不到当天重要新闻，宁可减少条数，绝不旧闻凑数\n"
        f"!!!\n\n"
        f"任务：\n"
        f"1. 联网搜索 {today_str} 当天全球最重要新闻\n"
        f"2. 筛选全球10条（覆盖政治、经济、科技、社会、环境）\n"
        f"3. 筛选中国3条\n"
        f"4. 每条含：title(≤25字)、summary(100-180字)、comment(50-100字深度分析)、source\n\n"
        f"严格JSON输出：\n"
        f"{{\n"
        f"  \"global_news\": [\n"
        f"    {{\"title\": \"...\", \"summary\": \"...\", \"comment\": \"...\", \"source\": \"...\"}}\n"
        f"  ],\n"
        f"  \"china_news\": [\n"
        f"    {{\"title\": \"...\", \"summary\": \"...\", \"comment\": \"...\", \"source\": \"...\"}}\n"
        f"  ]\n"
        f"}}\n\n"
        f"global_news恰好10条，china_news恰好3条。summary和comment不重复。"
    )
    return prompt


# ============================================================
# DeepSeek API 调用
# ============================================================
def call_deepseek_api(config: dict, rss_candidates: list[dict], recent_titles: list[str] = None) -> Optional[dict]:
    """调用 DeepSeek API（主方案：联网搜索，强制当天新闻）"""
    api_key = config["deepseek"]["api_key"]
    api_url = config["deepseek"]["api_url"]
    model = config["deepseek"]["model"]
    timeout = config["deepseek"].get("timeout", 120)

    today_str = datetime.now().strftime("%Y年%m月%d日")
    prompt = build_news_prompt(rss_candidates, recent_titles)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": f"今天是{today_str}。你只报道当天发生或首次披露的新闻，绝不用旧闻。严格按JSON格式回复。",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 16384,
        "response_format": {"type": "json_object"},
    }

    enable_search = config["deepseek"].get("enable_search", True)

    def _do_request(p: dict, label: str) -> Optional[dict]:
        logger.info(f"调用 DeepSeek API（{label}）...")
        try:
            response = requests.post(api_url, headers=headers, json=p, timeout=timeout)
            if response.status_code == 400:
                body = response.text[:500]
                logger.warning(f"DeepSeek 400: {body}")
                return None
            response.raise_for_status()
            result = response.json()
            content = result["choices"][0]["message"]["content"]
            finish = result["choices"][0].get("finish_reason", "?")
            logger.info(f"DeepSeek 返回: {len(content)} 字符 (finish_reason={finish})")

            try:
                return json.loads(content)
            except json.JSONDecodeError as e:
                logger.warning("JSON解析失败(" + str(e) + ")，尝试正则修复...")
                match = re.search(r"\{.*\}", content, re.DOTALL)
                if match:
                    try:
                        return json.loads(match.group())
                    except json.JSONDecodeError:
                        logger.error(f"正则修复失败: {content[:300]}")
                else:
                    logger.error(f"JSON解析失败: {content[:300]}")
                return None
        except requests.exceptions.Timeout:
            logger.error("DeepSeek 超时")
        except requests.exceptions.RequestException as e:
            logger.error(f"DeepSeek 请求失败: {e}")
        except (KeyError, IndexError) as e:
            logger.error(f"DeepSeek 返回格式异常: {e}")
        return None

    if enable_search:
        payload["enable_search"] = True
        result = _do_request(payload, "联网搜索")
        if result is not None:
            return result
        logger.warning("联网搜索模式失败，尝试关闭 enable_search 重试...")
        del payload["enable_search"]

    return _do_request(payload, "普通模式")


# ============================================================
# 降级方案
# ============================================================
def fallback_with_rss(config: dict, rss_candidates: list[dict]) -> Optional[dict]:
    """当联网搜索不可用时，用 RSS 候选新闻 + DeepSeek 筛选点评"""
    if not rss_candidates:
        logger.error("无RSS候选，降级失败")
        return None

    logger.info("降级方案：RSS + DeepSeek 点评")
    candidates = rss_candidates[:50]

    text = ""
    for i, entry in enumerate(candidates, 1):
        text += f"{i}. [{entry['source']}] {entry['title']}\n"
        if entry["summary"]:
            text += f"   摘要: {entry['summary'][:200]}\n"

    today_str = datetime.now().strftime("%Y年%m月%d日")

    prompt = f"从以下今日（{today_str}）新闻中筛选最重要10条全球+3条中国，严格JSON输出：\n\n{text}\n\n{{'global_news':[...],'china_news':[...]}}"

    api_key = config["deepseek"]["api_key"]
    api_url = config["deepseek"]["api_url"]
    model = config["deepseek"]["model"]
    timeout = config["deepseek"].get("timeout", 120)

    try:
        response = requests.post(
            api_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": f"今天是{today_str}，只筛选当天新闻，严格JSON格式回复。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.7,
                "max_tokens": 16384,
            },
            timeout=timeout,
        )
        if response.status_code == 400:
            logger.warning(f"降级方案 400: {response.text[:500]}")
            return None
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        logger.info(f"降级方案返回: {len(content)} 字符")

        try:
            return json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if match:
                return json.loads(match.group())
    except Exception as e:
        logger.error(f"降级方案失败: {e}")

    return None


# ============================================================
# 天气卡片 HTML 构建
# ============================================================
def build_weather_card(weather: Optional[dict]) -> str:
    """根据天气数据生成邮件顶部的天气卡片 HTML"""
    if weather is None:
        return ""

    tomorrow_str = weather["date"]
    try:
        dt = datetime.strptime(tomorrow_str, "%Y-%m-%d")
        weekdays = ["一", "二", "三", "四", "五", "六", "日"]
        date_display = f"{dt.month}月{dt.day}日（周{weekdays[dt.weekday()]}）"
    except Exception:
        date_display = tomorrow_str

    if weather["is_rain_day"]:
        card_gradient = "linear-gradient(135deg, #1e3c72, #2a5298)"
    else:
        card_gradient = "linear-gradient(135deg, #2d5016, #4a7c2e)"

    rain_text = ""
    if weather["rain_periods"]:
        periods_str = "、".join(weather["rain_periods"])
        rain_text = f"""
            <tr>
              <td style="padding:6px 0 0 0;font-size:13px;color:#e2e8f0;">
                🌂 预计降雨时段：<span style="color:#fbd38d;font-weight:bold;">{periods_str}</span>
              </td>
            </tr>"""
    elif weather["is_rain_day"]:
        rain_text = """
            <tr>
              <td style="padding:6px 0 0 0;font-size:13px;color:#e2e8f0;">
                🌂 明日有降雨可能，建议随身带伞
              </td>
            </tr>"""

    humidity = weather["humidity"]
    humidity_note = ""
    if humidity is not None:
        if humidity > 80:
            humidity_note = "（偏潮湿）"
        elif humidity < 40:
            humidity_note = "（偏干燥）"

    html = f"""
  <tr>
    <td style="padding:0 0 18px 0;">
      <table width="100%" cellpadding="0" cellspacing="0" style="background:{card_gradient};border-radius:10px;overflow:hidden;">
        <tr>
          <td style="padding:18px 22px 6px 22px;">
            <p style="margin:0;font-size:13px;color:#a0c4e8;letter-spacing:1px;">
              📍 {weather["location"]}　|　明日天气　{date_display}
            </p>
          </td>
        </tr>
        <tr>
          <td style="padding:4px 22px 0 22px;">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td width="140" valign="middle" style="padding:6px 16px 6px 0;">
                  <p style="margin:0;font-size:28px;color:#ffffff;line-height:1.4;">
                    {weather["weather_desc"]}
                  </p>
                </td>
                <td width="130" valign="middle" style="padding:6px 16px 6px 0;">
                  <p style="margin:0;font-size:32px;font-weight:bold;color:#ffffff;line-height:1.2;">
                    {weather["temp_min"]}°<span style="font-size:16px;font-weight:normal;color:#cbd5e0;"> ~ {weather["temp_max"]}°C</span>
                  </p>
                </td>
                <td valign="middle" style="padding:6px 0;">
                  <table cellpadding="0" cellspacing="0">
                    <tr>
                      <td style="font-size:13px;color:#e2e8f0;padding-bottom:4px;">
                        💧 湿度：<span style="color:#ffffff;font-weight:bold;">{humidity if humidity else "--"}%</span>{humidity_note}
                      </td>
                    </tr>
                    <tr>
                      <td style="font-size:13px;color:#e2e8f0;">
                        🌧️ 降水概率：<span style="color:#ffffff;font-weight:bold;">{weather["precip_prob"]}%</span>
                        {"（降水量约 " + str(weather["precip_sum"]) + "mm）" if weather["precip_sum"] and weather["precip_sum"] > 0 else ""}
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>
            </table>
          </td>
        </tr>{rain_text}
        <tr><td style="height:12px;"></td></tr>
      </table>
    </td>
  </tr>"""
    return html


# ============================================================
# HTML 邮件生成
# ============================================================
def build_html_email(data: dict, weather: Optional[dict] = None) -> str:
    """生成排版精美的 HTML 邮件，顶部包含天气卡片"""
    today_str = datetime.now().strftime("%Y年%m月%d日")
    weekday = ["一", "二", "三", "四", "五", "六", "日"][datetime.now().weekday()]

    weather_card = build_weather_card(weather)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin:0;padding:0;background-color:#f5f5f5;font-family:'Microsoft YaHei','PingFang SC','Hiragino Sans GB',sans-serif;">

<table width="100%" cellpadding="0" cellspacing="0" style="background:linear-gradient(135deg,#0f0c29,#302b63,#24243e);padding:35px 0;">
  <tr>
    <td align="center">
      <h1 style="color:#ffffff;margin:0;font-size:26px;letter-spacing:2px;">每日新闻速递</h1>
      <p style="color:#a8b2d1;margin:10px 0 0 0;font-size:14px;">{today_str}　星期{weekday}</p>
      <p style="color:#8892b0;margin:6px 0 0 0;font-size:12px;">全球十大要闻 + 中国三大新闻　·　AI 深度点评</p>
    </td>
  </tr>
</table>

<table width="680" cellpadding="0" cellspacing="0" align="center" style="margin:24px auto;">

  <!-- ====== 天气卡片 ====== -->
{weather_card}

  <!-- ====== 全球新闻 ====== -->
  <tr>
    <td style="padding:12px 24px 0 24px;">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td style="font-size:20px;font-weight:bold;color:#1a1a2e;padding-bottom:10px;border-bottom:3px solid #e94560;">
            🌍 全球十大重要新闻
          </td>
        </tr>
      </table>
    </td>
  </tr>"""

    for i, item in enumerate(data.get("global_news", [])[:10], 1):
        title = item.get("title", "无标题")
        summary = item.get("summary", "")
        comment = item.get("comment", "")
        source = item.get("source", "")
        accent = "#e94560" if i <= 3 else "#5a6a7e"

        html += f"""
  <tr>
    <td style="padding:18px 24px;background-color:#ffffff;border-radius:6px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,0.06);">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td width="44" valign="top" style="font-size:24px;font-weight:800;color:{accent};padding-right:14px;line-height:1.2;">
            {i:02d}
          </td>
          <td valign="top">
            <p style="margin:0 0 10px 0;font-size:17px;font-weight:bold;color:#1a1a2e;line-height:1.6;">
              {title}
            </p>
            <p style="margin:0 0 10px 0;font-size:14px;color:#3d4f5f;line-height:1.85;">
              {summary}
            </p>
            <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f8f9fb;border-left:3px solid {accent};border-radius:0 4px 4px 0;">
              <tr>
                <td style="padding:10px 14px;">
                  <p style="margin:0;font-size:13px;color:#2d3748;line-height:1.75;">
                    <span style="color:{accent};font-weight:bold;">【点评】</span>{comment}
                    <span style="color:#a0aec0;font-size:12px;margin-left:10px;">— {source}</span>
                  </p>
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
    </td>
  </tr>
  <tr><td style="height:12px;"></td></tr>"""

    html += f"""
  <tr>
    <td style="padding:30px 24px 0 24px;">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td style="font-size:20px;font-weight:bold;color:#1a1a2e;padding-bottom:10px;border-bottom:3px solid #e94560;">
            🇨🇳 中国三大重要新闻
          </td>
        </tr>
      </table>
    </td>
  </tr>"""

    for i, item in enumerate(data.get("china_news", [])[:3], 1):
        title = item.get("title", "无标题")
        summary = item.get("summary", "")
        comment = item.get("comment", "")
        source = item.get("source", "")
        accent = "#c53030" if i == 1 else "#5a6a7e"

        html += f"""
  <tr>
    <td style="padding:18px 24px;background-color:#ffffff;border-radius:6px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,0.06);">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td width="44" valign="top" style="font-size:24px;font-weight:800;color:{accent};padding-right:14px;line-height:1.2;">
            {i:02d}
          </td>
          <td valign="top">
            <p style="margin:0 0 10px 0;font-size:17px;font-weight:bold;color:#1a1a2e;line-height:1.6;">
              {title}
            </p>
            <p style="margin:0 0 10px 0;font-size:14px;color:#3d4f5f;line-height:1.85;">
              {summary}
            </p>
            <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f8f9fb;border-left:3px solid {accent};border-radius:0 4px 4px 0;">
              <tr>
                <td style="padding:10px 14px;">
                  <p style="margin:0;font-size:13px;color:#2d3748;line-height:1.75;">
                    <span style="color:{accent};font-weight:bold;">【点评】</span>{comment}
                    <span style="color:#a0aec0;font-size:12px;margin-left:10px;">— {source}</span>
                  </p>
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
    </td>
  </tr>
  <tr><td style="height:12px;"></td></tr>"""

    html += f"""
  <tr>
    <td style="padding:30px 24px;text-align:center;">
      <p style="color:#a0aec0;font-size:12px;margin:0;">本邮件由 AI 自动生成 · 每日 20:00 发送 · {today_str}</p>
      <p style="color:#cbd5e0;font-size:11px;margin:5px 0 0 0;">新闻内容通过 DeepSeek 联网搜索与 RSS 聚合，仅供参考</p>
    </td>
  </tr>
</table>
</body>
</html>"""
    return html


# ============================================================
# 邮件发送
# ============================================================
def send_email(config: dict, html_content: str) -> bool:
    """通过 QQ 邮箱 SMTP 发送 HTML 邮件"""
    smtp_config = config["smtp"]
    today_str = datetime.now().strftime("%m月%d日")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(f"每日新闻速递 · {today_str}", "utf-8")
    msg["From"] = formataddr(("每日新闻速递", smtp_config["sender"]))
    msg["To"] = smtp_config["receiver"]
    msg.attach(MIMEText("请使用支持HTML的邮件客户端查看。\n\n每日新闻速递 - 全球十大要闻 + 中国三大新闻", "plain", "utf-8"))
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    try:
        logger.info(f"SMTP连接: {smtp_config['server']}:{smtp_config['port']}")
        with smtplib.SMTP_SSL(smtp_config["server"], smtp_config["port"], timeout=30) as server:
            server.login(smtp_config["sender"], smtp_config["password"])
            server.sendmail(smtp_config["sender"], smtp_config["receiver"], msg.as_string())
            logger.info("邮件发送成功")
            return True
    except smtplib.SMTPAuthenticationError:
        logger.error("SMTP认证失败，请检查授权码")
    except smtplib.SMTPConnectError:
        logger.error("SMTP连接失败，请检查网络")
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")
    return False


# ============================================================
# 主流程
# ============================================================
def main():
    logger.info("=" * 50)
    logger.info("每日新闻邮件服务启动")
    logger.info("=" * 50)

    try:
        config = load_config()
    except (FileNotFoundError, ValueError) as e:
        logger.error(str(e))
        return 1

    weather = fetch_weather(config)
    recent_titles = load_sent_news()
    rss_candidates = fetch_rss_news(config)

    data = call_deepseek_api(config, rss_candidates, recent_titles)

    if data is None:
        logger.warning("主方案失败，尝试降级...")
        data = fallback_with_rss(config, rss_candidates)

    if data is None:
        logger.error("所有方案均失败")
        return 1

    global_count = len(data.get("global_news", []))
    china_count = len(data.get("china_news", []))
    logger.info(f"获取新闻: 全球{global_count}条, 中国{china_count}条")

    if global_count < 5:
        logger.warning("全球新闻不足5条，质量可能不佳")

    html_content = build_html_email(data, weather)
    logger.info(f"HTML邮件: {len(html_content)} 字符")

    success = send_email(config, html_content)
    if success:
        logger.info("[OK] 邮件发送成功")
        today_titles = [item.get("title", "") for item in data.get("global_news", [])]
        today_titles += [item.get("title", "") for item in data.get("china_news", [])]
        if recent_titles:
            today_titles = recent_titles + today_titles
        save_sent_news(today_titles)
    else:
        logger.error("[FAIL] 发送失败")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

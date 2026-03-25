#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Daily Report - 每日 GitHub 热门项目推送
支持：DeepSeek / Gemini / Qwen / 任意 OpenAI 兼容接口 / 纯爬虫（无需 AI）
"""

import json, os, re, sys, time, random, base64
import requests
from bs4 import BeautifulSoup
from datetime import datetime

# ─────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(SCRIPT_DIR, "config.json"), encoding="utf-8") as f:
    CFG = json.load(f)

def _secret(env_key: str, cfg_key: str) -> str:
    """优先读取环境变量，其次读 config.json，config.json 里不应存真实密钥"""
    val = os.environ.get(env_key, "")
    if val:
        return val
    v = CFG.get(cfg_key, "")
    if v and not v.startswith("YOUR_"):
        return v
    return ""

# ── 敏感信息：从环境变量读取 ────────────────────────────
# 设置方式见 set_secrets.ps1
WECOM_WEBHOOK = _secret("WECOM_WEBHOOK_URL",  "wecom_webhook_url")
AI_API_KEY    = _secret("AI_API_KEY",          "ai_api_key")
GITHUB_TOKEN  = _secret("GITHUB_TOKEN",        "github_token")

# ── 非敏感配置：从 config.json 读取 ─────────────────────
OUTPUT_DIR     = CFG.get("output_dir", os.path.join(SCRIPT_DIR, "reports"))
TRENDING_COUNT = CFG.get("trending_count", 3)
LANG_FILTER    = CFG.get("language_filter", "")
AI_PROVIDER    = CFG.get("ai_provider", "gemini")
AI_BASE_URL    = CFG.get("ai_base_url", "")
AI_MODEL       = CFG.get("ai_model", "")

os.makedirs(OUTPUT_DIR, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/122.0.0.0 Safari/537.36")
HTTP_HEADERS = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
GH_HEADERS   = {**HTTP_HEADERS, "Accept": "application/vnd.github.v3+json",
                **({"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {})}


# ─────────────────────────────────────────────
# GitHub 数据抓取
# ─────────────────────────────────────────────
def fetch_trending(limit=3, language="", since="daily"):
    url = "https://github.com/trending" + (f"/{language}" if language else "")
    r = requests.get(url, params={"since": since}, headers=HTTP_HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    repos = []
    for art in soup.select("article.Box-row")[:limit]:
        h2 = art.select_one("h2 a")
        if not h2:
            continue
        full_name = h2["href"].strip("/")
        desc_el   = art.select_one("p.col-9")
        lang_el   = art.select_one("span[itemprop='programmingLanguage']")
        star_els  = art.select("a.Link--muted")
        today_el  = art.select_one("span.d-inline-block.float-sm-right")
        repos.append({
            "full_name":   full_name,
            "url":         f"https://github.com/{full_name}",
            "description": desc_el.get_text(strip=True) if desc_el else "",
            "language":    lang_el.get_text(strip=True) if lang_el else "",
            "total_stars": star_els[0].get_text(strip=True).replace(",","") if star_els else "",
            "forks":       star_els[1].get_text(strip=True).replace(",","") if len(star_els)>1 else "",
            "stars_today": today_el.get_text(strip=True) if today_el else "",
        })
    return repos


def fetch_repo_meta(full_name):
    try:
        r = requests.get(f"https://api.github.com/repos/{full_name}",
                         headers=GH_HEADERS, timeout=12)
        if r.status_code == 200:
            d = r.json()
            return {
                "topics":      d.get("topics", []),
                "homepage":    d.get("homepage", ""),
                "created_at":  d.get("created_at","")[:10],
                "pushed_at":   d.get("pushed_at","")[:10],
                "open_issues": d.get("open_issues_count", 0),
                "watchers":    d.get("watchers_count", 0),
            }
    except Exception:
        pass
    return {}


def fetch_readme(full_name, max_chars=2500):
    try:
        r = requests.get(f"https://api.github.com/repos/{full_name}/readme",
                         headers=GH_HEADERS, timeout=12)
        if r.status_code == 200:
            text = base64.b64decode(r.json()["content"]).decode("utf-8", errors="ignore")
            # 去掉纯图片行和 badge
            lines = [l for l in text.split("\n")
                     if not re.match(r"!\[.*?\]\(.*?\)", l.strip())]
            return "\n".join(lines)[:max_chars]
    except Exception:
        pass
    return ""


HISTORY_FILE = os.path.join(SCRIPT_DIR, "history.json")
HISTORY_DAYS = 30  # 30天内不重复推荐


def load_history() -> set:
    """加载历史推荐记录"""
    if not os.path.exists(HISTORY_FILE):
        return set()
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        # 只保留 30 天内的记录
        cutoff = datetime.now().timestamp() - HISTORY_DAYS * 86400
        return {entry["name"] for entry in data if entry.get("ts", 0) > cutoff}
    except Exception:
        return set()


def save_history(full_name: str):
    """把今日推荐存入历史"""
    data = []
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = []
    # 清理 30 天前的记录
    cutoff = datetime.now().timestamp() - HISTORY_DAYS * 86400
    data = [e for e in data if e.get("ts", 0) > cutoff]
    data.append({"name": full_name, "ts": datetime.now().timestamp()})
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_recommendation():
    """搜索近期活跃、star 高的项目作为今日推荐，30天内不重复"""
    seen = load_history()
    try:
        r = requests.get(
            "https://api.github.com/search/repositories",
            headers=GH_HEADERS,
            params={"q": "stars:>3000 pushed:>2025-06-01", "sort": "stars",
                    "order": "desc", "per_page": 30},
            timeout=15,
        )
        if r.status_code == 200:
            items = r.json().get("items", [])
            # 过滤掉近期推荐过的
            candidates = [i for i in items if i["full_name"] not in seen]
            if not candidates:
                candidates = items  # 全都推荐过了就不限制
                print("  [提示] 候选项目已全部推荐过，重新轮换")
            pick = random.choice(candidates[:10])
            save_history(pick["full_name"])
            return {
                "full_name":   pick["full_name"],
                "url":         pick["html_url"],
                "description": pick.get("description", ""),
                "language":    pick.get("language", "") or "",
                "total_stars": f"{pick['stargazers_count']:,}",
                "forks":       f"{pick['forks_count']:,}",
                "stars_today": "",
                "topics":      pick.get("topics", []),
            }
    except Exception as e:
        print(f"  [警告] 推荐项目获取失败：{e}")
    return None


# ─────────────────────────────────────────────
# AI 分析（支持多种接口，也可降级为无 AI）
# ─────────────────────────────────────────────
def call_ai(prompt: str) -> str:
    """统一 AI 调用入口，支持 DeepSeek / Gemini / OpenAI 兼容接口"""
    provider = AI_PROVIDER.lower()

    if provider == "none" or not AI_API_KEY or AI_API_KEY.startswith("YOUR_"):
        return ""

    # ── DeepSeek / 通义千问 / 任意 OpenAI 兼容 ──────────────────
    if provider in ("deepseek", "openai", "qwen", "openai_compat"):
        base = AI_BASE_URL or {
            "deepseek": "https://api.deepseek.com",
            "qwen":     "https://dashscope.aliyuncs.com/compatible-mode/v1",
        }.get(provider, "https://api.openai.com/v1")
        model = AI_MODEL or {
            "deepseek": "deepseek-chat",
            "qwen":     "qwen-plus",
        }.get(provider, "gpt-4o-mini")

        r = requests.post(
            f"{base.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {AI_API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": model,
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 1200},
            timeout=40,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

    # ── Google Gemini ──────────────────────────────────────────
    if provider == "gemini":
        model = AI_MODEL or "gemini-2.5-flash"
        for attempt in range(3):
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={AI_API_KEY}",
                    json={"contents": [{"parts": [{"text": prompt}]}]},
                    timeout=60,
                )
                r.raise_for_status()
                return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            except Exception as e:
                if attempt < 2:
                    print(f"  [重试 {attempt+1}/3] {e}")
                    time.sleep(5)
                else:
                    raise

    return ""


ANALYZE_PROMPT = """你是技术洞察专家，用**深入浅出、通俗易懂**的中文分析以下 GitHub 项目，受众是对技术感兴趣的非程序员。

项目信息：
名称：{full_name}
描述：{description}
语言：{language}
Stars：{stars}  Forks：{forks}  今日新增：{today}
Topics：{topics}

README 节选：
{readme}

请只输出 JSON，不要多余文字：
{{
  "one_line": "一句话定位（20字内）",
  "what_is_it": "它是什么？用类比解释，外行能懂（120字内）",
  "why_hot": "为什么火？（80字内）",
  "use_case": "2-3个具体使用场景",
  "prospect": "前景：🔥超热赛道 / ✅稳健增长 / ⚠️观望，附理由（80字内）",
  "target_user": "最适合哪类人",
  "tech_highlight": "最值得关注的亮点（1-2点）",
  "emoji": "最能代表它的一个 emoji"
}}"""


def auto_analyze(repo: dict, readme: str) -> dict:
    """无 AI 时根据现有数据自动生成分析摘要"""
    desc = repo.get("description", "") or "暂无描述"
    lang = repo.get("language", "") or "未知"
    topics = repo.get("topics", [])

    # 从 README 提取第一个非空段落
    first_para = ""
    for line in readme.split("\n"):
        line = line.strip()
        if len(line) > 40 and not line.startswith(("#", "|", ">")):
            first_para = line[:200]
            break

    # 根据语言和 stars 简单判断前景
    hot_langs = {"rust", "go", "typescript", "python", "zig"}
    prospect = "✅稳健增长，持续受到开发者关注"
    try:
        stars_num = int(repo.get("total_stars","0").replace(",",""))
        if stars_num > 20000:
            prospect = "🔥超热赛道，star 数量反映出极高的社区认可度"
    except Exception:
        pass
    if lang.lower() in hot_langs:
        prospect = "🔥超热赛道，" + lang + " 生态持续高速增长"

    return {
        "one_line":        desc[:30],
        "what_is_it":      first_para or desc,
        "why_hot":         f"在 GitHub Trending 上排名靠前，今日新增 {repo.get('stars_today','若干')} 个 Star",
        "use_case":        f"主要应用于 {', '.join(topics[:3]) if topics else lang} 相关领域",
        "prospect":        prospect,
        "target_user":     f"主要使用 {lang} 的开发者和技术团队",
        "tech_highlight":  f"使用 {lang} 构建，{('涵盖话题：' + ', '.join(topics[:3])) if topics else '详见 README'}",
        "emoji":           _lang_emoji(lang),
    }


def _lang_emoji(lang: str) -> str:
    m = {"python":"🐍","typescript":"🔷","javascript":"🟨","rust":"🦀",
         "go":"🐹","java":"☕","c++":"⚡","c":"⚙️","swift":"🍎",
         "kotlin":"🎯","ruby":"💎","shell":"🐚"}
    return m.get(lang.lower(), "⭐")


def analyze_repo(repo: dict, readme: str) -> dict:
    prompt = ANALYZE_PROMPT.format(
        full_name=repo["full_name"],
        description=repo.get("description",""),
        language=repo.get("language",""),
        stars=repo.get("total_stars",""),
        forks=repo.get("forks",""),
        today=repo.get("stars_today",""),
        topics=", ".join(repo.get("topics",[])),
        readme=readme or "（无法获取）",
    )
    raw = call_ai(prompt)
    if raw:
        try:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                return json.loads(m.group())
        except Exception:
            pass
        print("  [警告] AI 返回格式异常，降级为自动摘要")

    return auto_analyze(repo, readme)


# ─────────────────────────────────────────────
# 生成 HTML
# ─────────────────────────────────────────────
def build_html(trending: list, recommended: tuple) -> str:
    today_str = datetime.now().strftime("%Y年%m月%d日")

    def repo_card(repo, an, rank=None, is_rec=False):
        badge    = f'<span class="rank-badge">#{rank}</span>' if rank else ""
        rec_lbl  = '<span class="rec-label">⭐ 今日推荐</span>' if is_rec else ""
        p_cls    = "hot" if "🔥" in an.get("prospect","") else (
                   "warn" if "⚠️" in an.get("prospect","") else "good")
        topics   = "".join(f'<span class="topic">{t}</span>'
                           for t in repo.get("topics",[])[:6])
        return f"""
<div class="card {'rec-card' if is_rec else ''}">
  <div class="card-header">
    {badge}{rec_lbl}
    <span class="emoji-big">{an.get('emoji','⭐')}</span>
    <div class="repo-title">
      <a href="{repo['url']}" target="_blank">{repo['full_name']}</a>
      <span class="one-line">{an.get('one_line','')}</span>
    </div>
    <div class="stats">
      <span class="stat">⭐ {repo.get('total_stars','')}</span>
      <span class="stat">🍴 {repo.get('forks','')}</span>
      {'<span class="stat today">📈 ' + repo["stars_today"] + ' 今日</span>' if repo.get("stars_today") else ""}
      {'<span class="stat lang">' + repo.get("language","") + '</span>' if repo.get("language") else ""}
    </div>
  </div>
  {('<div class="topics">' + topics + '</div>') if topics else ""}
  <div class="grid2">
    <div class="section">
      <div class="stitle">💡 是什么？</div>
      <div class="sbody">{an.get('what_is_it','')}</div>
    </div>
    <div class="section">
      <div class="stitle">🔥 为什么火？</div>
      <div class="sbody">{an.get('why_hot','')}</div>
    </div>
    <div class="section">
      <div class="stitle">🎯 使用场景</div>
      <div class="sbody">{an.get('use_case','')}</div>
    </div>
    <div class="section">
      <div class="stitle">✨ 技术亮点</div>
      <div class="sbody">{an.get('tech_highlight','')}</div>
    </div>
  </div>
  <div class="prospect {p_cls}"><span class="ptitle">前景评估：</span>{an.get('prospect','')}</div>
  <div class="target">👥 最适合：{an.get('target_user','')}</div>
</div>"""

    t_cards = "".join(repo_card(r, a, rank=i+1) for i,(r,a) in enumerate(trending))
    r_repo, r_an = recommended
    r_card = repo_card(r_repo, r_an, is_rec=True)

    ai_badge = ("" if AI_PROVIDER == "none" or not AI_API_KEY or AI_API_KEY.startswith("YOUR_")
                else f'<span class="ai-badge">🤖 {AI_PROVIDER.upper()} 分析</span>')

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GitHub 每日精选 · {today_str}</title>
<style>
:root{{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;
  --blue:#58a6ff;--green:#3fb950;--orange:#d29922;--red:#f85149;--purple:#bc8cff;--gold:#ffd700}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,
  'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;line-height:1.65}}
.hero{{background:linear-gradient(135deg,#0d1117,#161b22,#1a1f2e);
  border-bottom:1px solid var(--border);padding:44px 24px 36px;text-align:center;position:relative}}
.hero::before{{content:'';position:absolute;inset:0;
  background:radial-gradient(ellipse at 50% 0%,rgba(88,166,255,.08),transparent 65%);pointer-events:none}}
.hero-date{{color:var(--blue);font-size:12px;letter-spacing:3px;text-transform:uppercase;margin-bottom:8px}}
.hero h1{{font-size:34px;font-weight:800;
  background:linear-gradient(90deg,#58a6ff,#bc8cff,#3fb950);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;margin-bottom:6px}}
.hero-sub{{color:var(--muted);font-size:14px;display:flex;align-items:center;
  justify-content:center;gap:10px;flex-wrap:wrap}}
.ai-badge{{background:rgba(188,140,255,.15);color:var(--purple);
  font-size:12px;padding:2px 10px;border-radius:20px;border:1px solid rgba(188,140,255,.3)}}
.container{{max-width:880px;margin:0 auto;padding:28px 16px 60px}}
.sec-header{{display:flex;align-items:center;gap:12px;margin:36px 0 16px;
  font-size:17px;font-weight:700}}
.sec-header::after{{content:'';flex:1;height:1px;background:var(--border)}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:14px;
  padding:22px;margin-bottom:20px;transition:.2s}}
.card:hover{{border-color:var(--blue);transform:translateY(-2px);
  box-shadow:0 8px 30px rgba(88,166,255,.1)}}
.rec-card{{border-color:rgba(255,215,0,.3);
  background:linear-gradient(135deg,#161b22,#1b1e2d)}}
.rec-card:hover{{border-color:var(--gold);box-shadow:0 8px 30px rgba(255,215,0,.12)}}
.card-header{{display:flex;align-items:flex-start;gap:12px;margin-bottom:14px;flex-wrap:wrap}}
.rank-badge{{background:linear-gradient(135deg,#58a6ff,#bc8cff);color:#fff;
  font-size:12px;font-weight:800;padding:2px 10px;border-radius:20px;align-self:center}}
.rec-label{{background:linear-gradient(135deg,#ffd700,#ffaa00);color:#1a1200;
  font-size:12px;font-weight:700;padding:2px 10px;border-radius:20px;align-self:center}}
.emoji-big{{font-size:34px;line-height:1;flex-shrink:0}}
.repo-title{{flex:1;min-width:0}}
.repo-title a{{font-size:17px;font-weight:700;color:var(--blue);text-decoration:none;
  display:block;word-break:break-all}}
.repo-title a:hover{{text-decoration:underline}}
.one-line{{font-size:13px;color:var(--muted);display:block;margin-top:3px}}
.stats{{display:flex;flex-wrap:wrap;gap:7px;align-self:center}}
.stat{{background:rgba(255,255,255,.05);padding:3px 10px;border-radius:20px;
  font-size:12px;color:var(--muted)}}
.stat.today{{background:rgba(63,185,80,.15);color:var(--green);font-weight:600}}
.stat.lang{{background:rgba(188,140,255,.15);color:var(--purple)}}
.topics{{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px}}
.topic{{background:rgba(88,166,255,.1);color:var(--blue);font-size:11px;
  padding:2px 8px;border-radius:12px;border:1px solid rgba(88,166,255,.2)}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px}}
@media(max-width:580px){{.grid2{{grid-template-columns:1fr}}}}
.section{{background:rgba(255,255,255,.025);border-radius:8px;padding:12px}}
.stitle{{font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;
  letter-spacing:.6px;margin-bottom:5px}}
.sbody{{font-size:14px;line-height:1.7}}
.prospect{{margin-top:14px;padding:12px 16px;border-radius:8px;font-size:14px;line-height:1.65}}
.prospect.hot{{background:rgba(248,81,73,.1);border-left:3px solid var(--red)}}
.prospect.good{{background:rgba(63,185,80,.1);border-left:3px solid var(--green)}}
.prospect.warn{{background:rgba(210,153,34,.1);border-left:3px solid var(--orange)}}
.ptitle{{font-weight:700}}
.target{{margin-top:10px;font-size:13px;color:var(--muted)}}
.footer{{text-align:center;padding:24px;color:var(--muted);font-size:13px;
  border-top:1px solid var(--border)}}
.footer a{{color:var(--blue);text-decoration:none}}
</style>
</head>
<body>
<div class="hero">
  <div class="hero-date">📅 {today_str} · GitHub Daily Picks</div>
  <h1>每日 GitHub 精选</h1>
  <div class="hero-sub">
    今日 Trending 深度解读
    {ai_badge}
  </div>
</div>
<div class="container">
  <div class="sec-header">🔥 今日 Trending Top {len(trending)}</div>
  {t_cards}
  <div class="sec-header">💎 今日重点推荐</div>
  {r_card}
</div>
<div class="footer">
  自动生成 · {today_str} ·
  <a href="https://github.com/trending" target="_blank">查看完整 Trending →</a>
</div>
</body>
</html>"""


# ─────────────────────────────────────────────
# 推送到 GitHub Pages
# ─────────────────────────────────────────────
GITHUB_USER = "frankee0920-rgb"
GITHUB_REPO = "github-daily-report"

def push_to_github_pages(filename: str, html_content: str) -> str:
    """把 HTML 推送到 GitHub 仓库，返回 Pages 访问 URL"""
    if not GITHUB_TOKEN:
        print("  [跳过] 未配置 GITHUB_TOKEN")
        return ""

    api_url = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{filename}"
    encoded = base64.b64encode(html_content.encode("utf-8")).decode("utf-8")

    # 查询文件是否已存在（获取 sha）
    sha = None
    r = requests.get(api_url, headers=GH_HEADERS, timeout=15)
    if r.status_code == 200:
        sha = r.json().get("sha")

    payload = {
        "message": f"Daily report {datetime.now().strftime('%Y-%m-%d')}",
        "content": encoded,
    }
    if sha:
        payload["sha"] = sha

    r = requests.put(api_url, headers=GH_HEADERS, json=payload, timeout=30)
    if r.status_code in (200, 201):
        pages_url = f"https://{GITHUB_USER}.github.io/{GITHUB_REPO}/{filename}"
        print(f"  GitHub Pages：{pages_url}")
        return pages_url
    else:
        print(f"  [警告] GitHub 推送失败：{r.status_code} {r.text[:100]}")
        return ""


# ─────────────────────────────────────────────
# 企业微信推送
# ─────────────────────────────────────────────
def send_wecom(webhook: str, trending: list, rec: tuple, pages_url: str):
    date_str = datetime.now().strftime("%m月%d日")
    lines = [f"## 📊 GitHub 每日精选 · {date_str}\n"]
    for i, (repo, an) in enumerate(trending):
        emoji = an.get("emoji", "⭐")
        today = f"📈+{repo['stars_today']}  " if repo.get('stars_today') else ""
        lines.append(
            f"**{emoji} #{i+1} [{repo['full_name']}]({repo['url']})**\n"
            f"> {an.get('one_line','')}\n"
            f"⭐{repo.get('total_stars','')}  {today}{repo.get('language','')}\n"
            f"{an.get('what_is_it','')[:80]}\n"
            f"**前景：**{an.get('prospect','')[:40]}\n"
        )
    r_repo, r_an = rec
    lines.append(
        f"---\n**💎 今日推荐：[{r_repo['full_name']}]({r_repo['url']})**\n"
        f"> {r_an.get('one_line','')}\n"
        f"⭐{r_repo.get('total_stars','')}  {r_repo.get('language','')}\n"
        f"{r_an.get('what_is_it','')[:80]}\n"
        f"**前景：**{r_an.get('prospect','')[:40]}\n"
    )
    if pages_url:
        lines.append(f"\n[📄 点击查看完整报告（含详细分析）]({pages_url})")

    payload = {"msgtype": "markdown", "markdown": {"content": "\n".join(lines)}}
    try:
        print(f"  [调试] 使用 Webhook key: ...{webhook[-8:]}")
        r = requests.post(webhook, json=payload, timeout=10, proxies={"http": None, "https": None})
        res = r.json()
        if res.get("errcode") == 0:
            print("  [OK] 企业微信推送成功")
        else:
            print(f"  [警告] 企业微信推送失败：{res}")
    except Exception as e:
        print(f"  [错误] 推送异常：{e}")


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────
def main():
    print(f"\n{'='*52}")
    print(f"  GitHub Daily Report  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    ai_mode = AI_PROVIDER if (AI_API_KEY and not AI_API_KEY.startswith("YOUR_")) else "无 AI（自动摘要）"
    print(f"  AI 模式：{ai_mode}")
    print(f"{'='*52}")

    print(f"\n[1/4] 抓取 GitHub Trending (top {TRENDING_COUNT})...")
    repos = fetch_trending(TRENDING_COUNT, LANG_FILTER)
    if not repos:
        print("  [错误] 未抓取到数据，请检查网络")
        sys.exit(1)
    print(f"  获取 {len(repos)} 个项目")

    print("\n[2/4] 获取今日推荐...")
    rec = fetch_recommendation() or repos[0]
    print(f"  推荐：{rec['full_name']}")

    print("\n[3/4] 分析项目...")
    analyzed = {}
    for repo in repos + ([rec] if rec["full_name"] not in [r["full_name"] for r in repos] else []):
        if repo["full_name"] in analyzed:
            continue
        print(f"  → {repo['full_name']}")
        meta = fetch_repo_meta(repo["full_name"])
        repo.update(meta)
        readme = fetch_readme(repo["full_name"])
        analyzed[repo["full_name"]] = analyze_repo(repo, readme)
        time.sleep(0.3)

    trending_pairs = [(r, analyzed[r["full_name"]]) for r in repos]
    rec_pair       = (rec, analyzed[rec["full_name"]])

    print("\n[4/5] 生成 HTML...")
    fname = f"github_daily_{datetime.now().strftime('%Y%m%d')}.html"
    html_path = os.path.join(OUTPUT_DIR, fname)
    html_content = build_html(trending_pairs, rec_pair)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"  HTML：{html_path}")

    print("\n[5/5] 推送 GitHub Pages 并发送企业微信...")
    pages_url = push_to_github_pages(fname, html_content)

    if WECOM_WEBHOOK and not WECOM_WEBHOOK.startswith("YOUR_"):
        send_wecom(WECOM_WEBHOOK, trending_pairs, rec_pair, pages_url)
    else:
        print("  [跳过] 未配置企业微信 Webhook")

    print(f"\n完成！")


if __name__ == "__main__":
    main()

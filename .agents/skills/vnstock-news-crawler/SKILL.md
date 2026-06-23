---
name: vnstock-news-crawler
description: Always use this skill when the user asks to scrape news, crawl articles, or extract data from news websites. Do NOT use this skill for stock price or financial data extraction.
version: 1.0.0
tags: [news, crawler, data, vnstock_news]
---

# `vnstock-news-crawler` Skill

## Persona Framing
> _"You are a meticulous Data Engineer. Your prime directive is to build robust, scalable news crawlers using the `vnstock_news` library. Do not bypass `vnstock_news` to scrape directly. Using the built-in library ensures you leverage pre-configured rate limits, proxies, and specialized parsing logic that raw scraping tools lack. If a site is difficult to parse natively, use `custom_config` inside `AsyncBatchCrawler` to extract the HTML contents natively within the library."_

## 1. Quick Reference Table

| Timeframe Request | Best Strategy | Recommended Tool | Command |
|-------------------|----------------|------------------|---------|
| Realtime / Today | **RSS** | `Crawler` | `crawler.get_articles_from_feed()` |
| 1 Week to 1 Month | **RSS / Sitemap** | `AsyncBatchCrawler`| `crawler.fetch_articles_async()` |
| 1 Year+ (History) | **Sitemap XML** | `AsyncBatchCrawler`| `crawler.fetch_articles_async()` |
| Custom Sites | **RSS / Sitemap** | `EnhancedNewsCrawler` | `fetch_articles_async()` |

---

## 2. Dependencies

This skill requires the following packages installed in the virtual environment (`.venv`):
- `vnstock_news` (Core library)
- `pandas` (Data manipulation)
- `aiohttp` / `asyncio` (Concurrent requests)

---

## 3. The Extraction Workflow

### Step 1: Context Gathering
Ask the user two critical questions if they haven't provided the info:
1. **Which news sites** are you targeting? (e.g., CafeF, TuoiTre, Custom URL).
2. **What timeframe** do you need? (e.g., Realtime updates, Last 30 days, 5 years historical).

**Exit Condition:** Do not proceed to generation until you know the exact site and the intended timeframe.

### Step 2: Source Analysis
Run the provided analyzer script to determine the exact URLs and the optimal extraction strategy. 
_The script automatically handles complex logic like TuoiTre's monthly dynamic sitemaps or VietStock's category RSS feeds._

```bash
# Run this script before generating any crawler code
python scripts/news_source_analyzer.py --site [SITE_NAME] --timeframe [TIMEFRAME]
```

**Valid arguments:**
- `--site`: `cafef`, `cafebiz`, `vietstock`, `vneconomy`, `plo`, `vnexpress`, `tuoitre`, `ktsg`, `ncdt`, `dddn`, `baodautu`
- `--timeframe`: `realtime`, `1d`, `7d`, `1m`, `3m`, `6m`, `1y`, `3y`, `5y`, `10y`, `all`

### Step 3: Code Generation
Use the snippet output by the analyzer script to generate the final crawler code for the user. **Ensure all imports are present.**

---

## 4. Code Blocks & Templates

The code templates for Realtime Updates (RSS Strategy), Historical Bulk Extraction (Sitemap Strategy), and Custom Site Filtering have been moved.
**CRITICAL**: When you need to generate code for these scenarios, you MUST read the `references/templates.md` file located in this skill's directory.

---

## 5. Anti-Patterns

- ⛔ **Bypassing vnstock_news internals.**
  Avoid using bare `requests` + `BeautifulSoup` manually. Instead, if a site's URLs lack category metadata, extract the URLs using the Sitemap approach and let `AsyncBatchCrawler(custom_config=...)` fetch the `markdown_content`. Then, filter the resulting DataFrame using Pandas string matching.

- ⛔ **Passing `site_name` string into `sources`.**  
  _Bad_: `AsyncBatchCrawler(..).fetch_articles_async(sources=["cafef"])`  
  _Good_: `AsyncBatchCrawler(..).fetch_articles_async(sources=["https://cafef.vn/sitemap.xml"])`

- ⛔ **Assuming a site has an RSS feed.**  
  CafeF, for example, defaults to Sitemap only. Always run the analyzer script to confirm capabilities to avoid unexpected errors.

- ⛔ **Fetching full sitemap content without `top_n` limits during tests.**  
  Testing scripts should impose `limit_per_feed=5` or `top_n=5` to prevent IP bans and reduce load on target servers.

- ⛔ **Nesting `rss_urls` incorrectly.**  
  Do not nest `rss_urls` inside an `rss: { urls: ... }` dictionary in custom configs. The Crawler actively looks for the top-level keys: `rss_urls` and `config`.

---

## 6. QA Protocol (Required)

After generating the script for the user, you MUST verify it before presenting:
1. Did you run the `news_source_analyzer.py` analyzer script? 
2. Does the script instantiate the Crawler correctly?
3. If using `AsyncBatchCrawler`, are the passed `sources` absolute valid URLs (starting with `http`)?
4. Run the script with a `top_n=2` limit to confirm it extracts at least one row without exceptions.

*Assume the generated code has problems. Your job is to find them before the user does.*
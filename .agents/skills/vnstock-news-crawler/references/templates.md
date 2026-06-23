### 📝 Template A: Realtime Updates (RSS Strategy)
Use this when the user demands up-to-the-minute news. 
**CRITICAL:** Do NOT use `custom_config` when initializing `Crawler(site_name="...")` unless you explicitly omit `site_name`.

```python
from vnstock_news import Crawler
import pandas as pd

# IMPORTANT: Passing site_name automatically loads predefined RSS/Sitemap configs
crawler = Crawler(site_name="cafebiz") 
articles = crawler.get_articles_from_feed(limit_per_feed=20)

df = pd.DataFrame(articles)
print(f"Extracted {len(df)} real-time articles")
```

### 📝 Template B: Historical Bulk Extraction (Sitemap Strategy)
Use when the user wants data spanning months or years. 

```python
import asyncio
from vnstock_news import AsyncBatchCrawler
import pandas as pd

async def fetch_historical():
    # max_concurrency should be between 2-5 to prevent rate limits
    crawler = AsyncBatchCrawler(site_name="tuoitre", max_concurrency=3)
    
    # ⚠️ CRITICAL: The `sources` array MUST be an array of absolute URLs, NOT site names.
    sources = [
        "https://tuoitre.vn/StaticSitemaps/sitemaps-2023-1.xml",
        "https://tuoitre.vn/StaticSitemaps/sitemaps-2023-2.xml"
    ]
    
    articles = await crawler.fetch_articles_async(
        sources=sources,
        top_n=500, # Limit per feed to avoid OOM
        within="1y"
    )
    return articles

if __name__ == "__main__":
    df = asyncio.run(fetch_historical())
    print(df.head())
```

### 📝 Template C: Custom Site with Content Filtering (No category in URL)
Use when the site is not natively supported and its sitemap URLs are flat (e.g., `...post123.html`), requiring you to fetch the articles first and filter by keywords in their content.

```python
import asyncio
import pandas as pd
from vnstock_news import AsyncBatchCrawler

custom_config = {
    "name": "Custom Site",
    "domain": "customsite.vn",
    "sitemap": {
        "pattern_type": "monthly",
        "base_url": "https://customsite.vn/sitemaps/news",
        "format": "-{year}-{month}",
        "extension": "xml"
    },
    "config": {
        "title_selector": {"tag": "h1", "class": "title"},
        "content_selector": {"tag": "div", "class": "article-body"}
    }
}

async def fetch_filtered_news():
    # ⚠️ CRITICAL: site_name MUST be omitted when using custom_config
    crawler = AsyncBatchCrawler(custom_config=custom_config, max_concurrency=5)
    
    # Generate explicit sitemap URL matching the pattern
    sitemap_url = "https://customsite.vn/sitemaps/news-2026-3.xml"
    
    df = await crawler.fetch_articles_async(
        sources=[sitemap_url],
        top_n=50,
        within="1d"
    )
    
    if not df.empty and 'markdown_content' in df.columns:
        # Filter natively extracted markdown content
        df_filtered = df[df['markdown_content'].str.contains('keyword1|keyword2', case=False, na=False)]
        return df_filtered
    return pd.DataFrame()

if __name__ == "__main__":
    df = asyncio.run(fetch_filtered_news())
    print(f"Extracted {len(df)} filtered articles")
```

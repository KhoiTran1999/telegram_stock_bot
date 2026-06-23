# 💻 Code Examples for Premium Charts

Use these examples as a baseline when generating code for the user. Do not just copy-paste them; adapt them to the user's specific data, but retain the aesthetic configurations.

## 1. Premium Static Chart (`matplotlib`)

This example demonstrates how to create a highly polished, aesthetic static chart with matplotlib.

```python
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd

def plot_premium_static_chart(df, symbol):
    # Ensure date is datetime
    df['time'] = pd.to_datetime(df['time'])
    
    # Setup the figure with high resolution and premium background
    plt.style.use('seaborn-v0_8-darkgrid')
    fig, ax = plt.subplots(figsize=(14, 7), dpi=150)
    fig.patch.set_facecolor('#f8f9fa')
    ax.set_facecolor('#ffffff')

    # Plot the closing price with a vibrant line and area fill
    line_color = '#0984e3'
    ax.plot(df['time'], df['close'], color=line_color, linewidth=2.5, label='Close Price')
    ax.fill_between(df['time'], df['close'], color=line_color, alpha=0.1)

    # Highlight trends (e.g., 20-day SMA)
    if len(df) >= 20:
        df['SMA_20'] = df['close'].rolling(window=20).mean()
        ax.plot(df['time'], df['SMA_20'], color='#d63031', linewidth=1.5, linestyle='--', label='20-Day SMA')

    # Typography & Labels
    ax.set_title(f'{symbol} - Historical Price Action', fontsize=18, fontweight='bold', color='#2d3436', pad=15)
    ax.set_xlabel('Date', fontsize=12, color='#636e72')
    ax.set_ylabel('Price (VND)', fontsize=12, color='#636e72')

    # Grid and Spines styling
    ax.grid(True, linestyle='--', alpha=0.5, color='#b2bec3')
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['left'].set_color('#dfe6e9')
    ax.spines['bottom'].set_color('#dfe6e9')

    # Formatter for dates
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
    plt.xticks(rotation=45)
    
    # Legend & Watermark
    ax.legend(loc='upper left', frameon=True, facecolor='white', edgecolor='#dfe6e9')
    
    # Disclaimer (MANDATORY)
    fig.subplots_adjust(bottom=0.15)
    plt.figtext(0.5, 0.02, 'Dữ liệu công khai trích xuất qua vnstock/vnstock_data', ha='center', fontsize=9, color='#94a3b8', style='italic')

    plt.tight_layout(rect=[0, 0.05, 1, 1]) # Adjust layout to not cut off figtext
    plt.show()
```

## 2. Interactive/High-Level Excellence (`vnstock_ezchart`)

Use the `Chart` class from `vnstock_ezchart` for rapid, beautiful plotting. Always remember to disable the logo and add the disclaimer manually.

```python
from vnstock_ezchart import Chart
import matplotlib.pyplot as plt
import pandas as pd

def plot_premium_ezchart(df, symbol):
    # Ensure df has a datetime index or a 'time'/'date' column
    
    # 1. Disable the vnstock logo to comply with branding rules
    Chart.set_theme(logo_path=None, theme_name='vnstock')
    
    # 2. Draw candlestick chart with volume
    fig, axes = Chart.candle(
        data=df, 
        title=f"{symbol} Price Action", 
        volume=True, 
        figsize=(12, 8)
    )
    
    # 3. Add explicit data source disclaimer
    fig.subplots_adjust(bottom=0.15) 
    plt.figtext(0.5, 0.01, 'Dữ liệu công khai trích xuất qua vnstock/vnstock_data', ha='center', fontsize=9, color='#94a3b8', style='italic')
    
    plt.show()
```

import asyncio
import pandas as pd
import numpy as np
from vnstock import Quote
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import datetime

# ==========================================
# 1. HÀM VẼ CHART NGÀY (CHO /info & /report)
# ==========================================
async def generate_chart_html(symbol: str) -> str:
    """Vẽ chart ngày cho /info"""
    return await _create_daily_chart(symbol, height=380)

async def generate_mini_chart(symbol: str) -> str:
    """Vẽ chart ngày mini cho /report"""
    return await _create_daily_chart(symbol, height=320)

async def _create_daily_chart(symbol: str, height: int) -> str:
    try:
        def _get_data():
            end = datetime.datetime.now()
            start = end - datetime.timedelta(days=180)
            q = Quote(symbol=symbol, source='VCI')
            return q.history(start=start.strftime('%Y-%m-%d'), end=end.strftime('%Y-%m-%d'), interval='1D')

        df = await asyncio.to_thread(_get_data)
        if df is None or df.empty: return ""
            
        df.columns = df.columns.str.lower().str.strip()
        if 'time' in df.columns: df['time'] = pd.to_datetime(df['time'])
        df['close'] = pd.to_numeric(df['close'], errors='coerce')
        df['volume'] = pd.to_numeric(df['volume'], errors='coerce')
        df = df.dropna(subset=['close'])
        if df['close'].mean() < 500: df['close'] *= 1000

        mn, mx = df['close'].min(), df['close'].max()
        padding = (mx - mn) * 0.1 if (mx - mn) > 0 else mx * 0.05
        y_min, y_max = mn - padding, mx + padding

        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.02, row_heights=[0.75, 0.25])

        fig.add_trace(go.Scatter(
            x=df['time'].tolist(), y=df['close'].tolist(), mode='lines', name='Giá', 
            line=dict(color='#2962ff', width=2), fill='tozeroy', fillcolor='rgba(41, 98, 255, 0.1)',
            showlegend=False, hoverinfo='skip'
        ), row=1, col=1)

        fig.add_trace(go.Bar(
            x=df['time'].tolist(), y=df['volume'].tolist(), name='Vol', 
            marker_color='rgba(128, 128, 128, 0.3)', showlegend=False, hoverinfo='skip'
        ), row=2, col=1)

        fig.update_layout(
            paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
            xaxis_rangeslider_visible=False, height=height,
            margin=dict(l=0, r=0, t=10, b=10), font=dict(size=9, family='Manrope, sans-serif'),
            hovermode=False, dragmode=False
        )
        fig.update_yaxes(range=[y_min, y_max], showgrid=True, gridcolor='rgba(128,128,128,0.1)', fixedrange=True, showticklabels=False, row=1, col=1)
        fig.update_yaxes(showgrid=False, showticklabels=False, fixedrange=True, row=2, col=1)
        fig.update_xaxes(fixedrange=True)

        return fig.to_html(full_html=False, include_plotlyjs=False, config={'displayModeBar': False, 'responsive': True, 'staticPlot': False})
    except: return ""

# ==========================================
# 2. HÀM VẼ CHART INTRADAY (FLASH VIEW)
# ==========================================

def draw_line_chart_fixed_ui(x_list, y_list, v_list):
    """
    Vẽ Line Chart 5 phút (Premium Style).
    Tên hàm này khớp với alert_bot.py import.
    """
    if not y_list: return ""
    mn, mx = min(y_list), max(y_list)
    pad = (mx - mn) * 0.1 if mx != mn else mx * 0.05
    
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.0, row_heights=[0.8, 0.2])

    # Line
    fig.add_trace(go.Scatter(
        x=x_list, y=y_list, mode='lines', line=dict(color='#2962ff', width=2.5),
        fill='tozeroy', fillcolor='rgba(41, 98, 255, 0.08)',
        showlegend=False, hoverinfo='skip'
    ), row=1, col=1)

    # Volume Color
    colors = ['#089981' if (i==0 or y_list[i]>=y_list[i-1]) else '#f23645' for i in range(len(y_list))]
    fig.add_trace(go.Bar(x=x_list, y=v_list, marker_color=colors, marker_opacity=0.6, showlegend=False, hoverinfo='skip'), row=2, col=1)

    fig.update_layout(
        paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
        margin=dict(l=10, r=50, t=10, b=10), 
        height=300, dragmode=False, hovermode=False,
        font=dict(family="Manrope, sans-serif", size=10, color='#787b86')
    )

    # Trục Giá
    fig.update_yaxes(range=[mn-pad, mx+pad], showgrid=True, gridcolor='rgba(0,0,0,0.04)', fixedrange=True, showticklabels=True, side='right', row=1, col=1)
    # Trục Vol
    fig.update_yaxes(showgrid=False, showticklabels=False, fixedrange=True, row=2, col=1)
    fig.update_xaxes(fixedrange=True, showgrid=False)

    return fig.to_html(full_html=False, include_plotlyjs=False, config={'displayModeBar': False, 'staticPlot': False, 'responsive': True})

def draw_orderbook_fixed_ui(price_depth, ref_price):
    """
    Vẽ biểu đồ khớp lệnh (Bo tròn).
    Tên hàm này khớp với alert_bot.py import.
    """
    if not price_depth: return ""
    data = sorted(price_depth, key=lambda x: x['volume'], reverse=True)[:6]
    data.sort(key=lambda x: x['price'], reverse=True)

    prices = [f"{x['price']:,.0f}" for x in data]
    volumes = [x['volume'] for x in data]
    raw_prices = [x['price'] for x in data]
    
    colors = []
    for p in raw_prices:
        if p > ref_price: colors.append('#089981')
        elif p < ref_price: colors.append('#f23645')
        else: colors.append('#f0b90b')

    fig = go.Figure(go.Bar(
        x=volumes, y=prices, orientation='h',
        marker_color=colors, marker_line_width=0,
        text=volumes, textposition='auto', texttemplate='%{value:.2s}',
        hoverinfo='none', marker=dict(color=colors, cornerradius=5)
    ))

    fig.update_layout(
        paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
        margin=dict(l=0, r=0, t=0, b=0), height=200, bargap=0.35,
        xaxis=dict(showgrid=False, showticklabels=False, fixedrange=True),
        yaxis=dict(showgrid=False, fixedrange=True, tickfont=dict(size=12, color='#131722', weight='bold')),
        dragmode=False, hovermode=False, font=dict(family="Manrope, sans-serif")
    )
    return fig.to_html(full_html=False, include_plotlyjs=False, config={'displayModeBar': False, 'staticPlot': False})

# ==========================================
# 3. HÀM LẤY DATA TỔNG HỢP
# ==========================================
async def get_flash_view_data(symbol: str):
    """Lấy toàn bộ dữ liệu Intraday, Resample và Tính toán chỉ số"""
    q = Quote(symbol=symbol, source='VCI')
    try:
        df = await asyncio.to_thread(lambda: q.intraday(symbol=symbol, page_size=10000))
    except: return None

    if df is None or df.empty: return None

    # Clean Data
    df.columns = df.columns.str.lower().str.strip()
    df['time'] = pd.to_datetime(df['time'])
    df['price'] = pd.to_numeric(df['price'], errors='coerce')
    df['volume'] = pd.to_numeric(df['volume'], errors='coerce')
    
    if df['price'].mean() < 500: df['price'] *= 1000
    
    df = df.sort_values('time').set_index('time')
    
    # Resample 5M
    df_5m = df['price'].resample('5min').last().ffill().dropna()
    vol_5m = df['volume'].resample('5min').sum().fillna(0).reindex(df_5m.index).fillna(0)
    
    # Convert Lists (No Timezone)
    x_list = df_5m.index.strftime('%H:%M').tolist()
    y_list = df_5m.tolist()
    v_list = vol_5m.tolist()
    
    current = y_list[-1]
    ref_price = df['price'].iloc[0]
    change_val = current - ref_price
    change_pct = (change_val / ref_price) * 100
    
    # Order Flow
    df_raw = df.reset_index()
    price_depth = df_raw.groupby('price')['volume'].sum().reset_index().to_dict('records')
    
    df_raw['match_type'] = df_raw['match_type'].str.title()
    buy_vol = df_raw[df_raw['match_type'] == 'Buy']['volume'].sum()
    total_vol = df_raw['volume'].sum()
    buy_pct = int(buy_vol/total_vol*100) if total_vol else 50
    sell_pct = 100 - buy_pct
    
    # Metrics
    high = df['price'].max()
    low = df['price'].min()
    range_pct = ((current - low) / (high - low)) * 100 if high > low else 50
    range_pct = max(0, min(100, range_pct))
    
    delta = pd.Series(y_list).diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs)).iloc[-1]
    if np.isnan(rsi): rsi = 50

    return {
        "symbol": symbol,
        "chart_data": (x_list, y_list, v_list),
        "price_depth": price_depth,
        "ref_price": ref_price,
        "current": current,
        "change_str": f"{'+' if change_val>=0 else ''}{change_pct:.2f}%",
        "bg_cls": "bg-up" if change_val >= 0 else "bg-down",
        "cls_color": "t-up" if change_val >= 0 else "t-down",
        "buy_pct": buy_pct,
        "sell_pct": sell_pct,
        "buy_vol_str": f"{buy_vol/1e6:.1f}M",
        "sell_vol_str": f"{(total_vol-buy_vol)/1e6:.1f}M",
        "low": f"{low:,.0f}",
        "high": f"{high:,.0f}",
        "range_pct": range_pct,
        "rsi_val": f"{rsi:.1f}",
        "rsi_color": "#089981" if rsi < 30 else "#f23645" if rsi > 70 else "#131722",
        "rsi_msg": "Vùng Quá Bán" if rsi < 30 else "Vùng Quá Mua" if rsi > 70 else "Trung Tính",
        "volume_str": f"{total_vol/1e6:.1f}M"
    }
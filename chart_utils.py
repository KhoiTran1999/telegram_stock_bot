import asyncio
import pandas as pd
import numpy as np
from vnstock import Quote, Trading
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
    Vẽ biểu đồ phân bổ dòng tiền (Style Soft & Modern).
    [ĐÃ SỬA] Giảm cornerradius xuống 4 cho tinh tế hơn.
    """
    if not price_depth: return ""
    
    # 1. Lọc và Sắp xếp
    data = [x for x in price_depth if x['volume'] > 0]
    data.sort(key=lambda x: x['price'], reverse=True)
    data = data[:16] 

    prices = [f"{x['price']:,.0f}" for x in data]
    volumes = [x['volume'] for x in data]
    raw_prices = [x['price'] for x in data]
    
    # 2. Logic màu sắc
    colors = []
    
    for p in raw_prices:
        if p > ref_price: colors.append('#089981') 
        elif p < ref_price: colors.append('#f23645') 
        else: colors.append('#f0b90b') 

    # 3. Format Volume
    text_labels = []
    for v in volumes:
        val_str = ""
        if v >= 1_000_000: val_str = f"{v/1_000_000:.2f}M"
        elif v >= 1_000: val_str = f"{v/1_000:.0f}K"
        else: val_str = str(int(v))
        text_labels.append(f" {val_str} ") 

    # 4. Vẽ Chart
    fig = go.Figure(go.Bar(
        x=volumes, 
        y=prices, 
        orientation='h',
        marker_color=colors, 
        marker_line_width=0,
        text=text_labels, 
        textposition='inside', 
        insidetextanchor='end', 
        textfont=dict(size=10, family="Manrope, sans-serif", color='white', weight='bold'),
        hoverinfo='none',
        opacity=0.9,
        # --- [FIX] Giảm bo tròn từ 10 xuống 4 cho tinh tế ---
        marker=dict(cornerradius=4) 
    ))

    chart_height = max(200, len(data) * 26) 

    fig.update_layout(
        paper_bgcolor='rgba(0,0,0,0)', 
        plot_bgcolor='rgba(0,0,0,0)',
        margin=dict(l=0, r=0, t=0, b=0), 
        height=chart_height, 
        bargap=0.35,
        xaxis=dict(showgrid=False, showticklabels=False, fixedrange=True, zeroline=False),
        yaxis=dict(showgrid=False, fixedrange=True, tickfont=dict(size=11, color='#9ca3af', family="Manrope, sans-serif"), type='category'),
        dragmode=False, 
        hovermode=False,
        font=dict(family="Manrope, sans-serif")
    )
    
    return fig.to_html(full_html=False, include_plotlyjs=False, config={'displayModeBar': False, 'staticPlot': False})

# ==========================================
# 3. HÀM LẤY DATA TỔNG HỢP
# ==========================================
async def get_flash_view_data(symbol: str):
    """
    Lấy toàn bộ dữ liệu Intraday, Resample và Tính toán chỉ số.
    [ĐÃ SỬA] Fix lỗi màu RSI khi ở vùng Trung tính (Neutral) trong Dark Mode.
    """
    q = Quote(symbol=symbol, source='VCI')
    try:
        df = await asyncio.to_thread(lambda: q.intraday())
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
    
    x_list = df_5m.index.strftime('%H:%M').tolist()
    y_list = df_5m.tolist()
    v_list = vol_5m.tolist()
    
    current = y_list[-1]
    
    # Lấy Ref Price chuẩn
    ref_price = df['price'].iloc[0]
    try:
        def _get_true_ref():
            t = Trading(symbol=symbol, source='VCI')
            board = t.price_board([symbol])
            if board is not None and not board.empty:
                row = board.iloc[0]
                return float(row.get('reference_price', 0))
            return None

        true_ref = await asyncio.to_thread(_get_true_ref)
        if true_ref and true_ref > 0:
            if true_ref < 500: true_ref *= 1000
            ref_price = true_ref
    except Exception: pass

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

    # --- [FIX START] RSI COLOR LOGIC ---
    # Nếu RSI > 70 (Quá Mua) -> Đỏ
    # Nếu RSI < 30 (Quá Bán) -> Xanh
    # Nếu Trung tính -> Dùng biến CSS var(--text-primary) để tự động theo Dark/Light mode
    if rsi < 30:
        rsi_color = "#089981"
    elif rsi > 70:
        rsi_color = "#f23645"
    else:
        rsi_color = "var(--text-primary)" # Thay vì fix cứng màu đen (#131722)
    # --- [FIX END] ---

    rsi_msg = "Vùng Quá Bán" if rsi < 30 else "Vùng Quá Mua" if rsi > 70 else "Trung Tính"

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
        "rsi_color": rsi_color, # Sử dụng màu đã fix
        "rsi_msg": rsi_msg,
        "volume_str": f"{total_vol/1e6:.1f}M"
    }

# ==========================================
# 4. HÀM VẼ CHART HIỆU SUẤT NGÀNH
# ==========================================
def draw_sector_performance_chart(sector_data, period='12w'):
    """
    Vẽ biểu đồ hiệu suất ngành (Horizontal Bar Chart).
    period: '12w' hoặc '6m'
    """
    if not sector_data: return ""
    
    # 1. Prepare Data
    items = []
    for name, metrics in sector_data.items():
        val = metrics.get(f'change_{period}')
        if val is not None:
            items.append((name, val))
            
    # Sort by performance
    items.sort(key=lambda x: x[1]) # Sort ascending for horizontal bar (bottom to top)
    
    names = [x[0] for x in items]
    values = [x[1] for x in items]

    # [NEW] Shorten names for Mobile UI
    display_names = []
    for n in names:
        # Mapping tên dài thành ngắn
        n = n.replace("Hàng & Dịch vụ Công nghiệp", "Hàng & DV CN")
        n = n.replace("Điện, nước & xăng dầu khí đốt", "Điện, Nước, Xăng")
        n = n.replace("Thực phẩm và đồ uống", "Thực phẩm & ĐU")
        n = n.replace("Xây dựng và Vật liệu", "XD & Vật liệu")
        n = n.replace("Tài nguyên Cơ bản", "Tài nguyên CB")
        n = n.replace("Dịch vụ tài chính", "DV Tài chính")
        n = n.replace("Công nghệ Thông tin", "CNTT")
        n = n.replace("Hàng cá nhân & Gia dụng", "Hàng CN & GD")
        n = n.replace("Du lịch và Giải trí", "Du lịch & GT")
        n = n.replace("Ô tô và phụ tùng", "Ô tô & PT")
        n = n.replace("Truyền thông", "Truyền thông")
        n = n.replace("Bảo hiểm", "Bảo hiểm")
        n = n.replace("Bất động sản", "BĐS")
        n = n.replace("Hóa chất", "Hóa chất")
        n = n.replace("Ngân hàng", "Ngân hàng")
        n = n.replace("Bán lẻ", "Bán lẻ")
        n = n.replace("Y tế", "Y tế")
        
        # Cắt ngắn nếu vẫn quá dài
        if len(n) > 18:
            n = n[:16] + ".."
        display_names.append(n)
    
    # 2. Colors
    colors = ['#089981' if v >= 0 else '#f23645' for v in values]
    
    # 3. Draw Chart (Simplified UI - No Text Labels)
    fig = go.Figure(go.Bar(
        x=values,
        y=display_names,
        orientation='h',
        marker_color=colors,
        hoverinfo='x+y',
        marker=dict(cornerradius=4)
    ))
    
    chart_height = max(300, len(items) * 30)
    
    fig.update_layout(
        autosize=True,
        paper_bgcolor='rgba(0,0,0,0)',
        plot_bgcolor='rgba(0,0,0,0)',
        margin=dict(l=5, r=5, t=10, b=10), # Minimal margins
        height=chart_height,
        xaxis=dict(
            showgrid=True, 
            gridcolor='rgba(128,128,128,0.1)', 
            zeroline=True, 
            zerolinecolor='rgba(128,128,128,0.3)',
            tickfont=dict(color='#9ca3af', family="Manrope, sans-serif", size=10)
        ),
        yaxis=dict(
            showgrid=False, 
            tickfont=dict(size=11, family="Manrope, sans-serif", color='#9ca3af'),
            automargin=True
        ),
        dragmode=False,
        hovermode=False,
        font=dict(family="Manrope, sans-serif")
    )
    
    return fig.to_html(full_html=False, include_plotlyjs=False, config={'displayModeBar': False, 'staticPlot': False, 'responsive': True})

def generate_sector_table_html(sector_data):
    """
    Tạo HTML bảng hiệu suất ngành (6 Tháng vs 12 Tuần).
    Style matches SCREENER_WEBAPP_TEMPLATE.
    """
    if not sector_data: return ""

    # 1. Prepare Data
    rows = []
    for name, metrics in sector_data.items():
        c6m = metrics.get('change_6m')
        c12w = metrics.get('change_12w')
        
        if c6m is None and c12w is None: continue
        
        rows.append({
            "name": name,
            "c6m": c6m if c6m is not None else 0,
            "c12w": c12w if c12w is not None else 0
        })

    # Sort by 6M performance descending
    rows.sort(key=lambda x: x['c6m'], reverse=True)

    # 2. Build HTML
    # Using inline styles to match the theme variables
    table_style = "width: 100%; border-collapse: collapse; font-size: 13px;"
    th_style = "text-align: left; padding: 10px; color: var(--hint-color); font-size: 11px; text-transform: uppercase; border-bottom: 1px solid var(--border-color);"
    td_style = "padding: 12px 10px; border-bottom: 1px solid var(--border-color); color: var(--text-color); font-weight: 600;"
    
    html = f"""
    <div style="overflow-x: auto;">
        <table style="{table_style}">
            <thead>
                <tr>
                    <th style="{th_style}">Ngành</th>
                    <th style="{th_style} text-align: right;">6 Tháng</th>
                    <th style="{th_style} text-align: right;">12 Tuần</th>
                </tr>
            </thead>
            <tbody>
    """

    for row in rows:
        def _fmt(val):
            color = "var(--success-text)" if val >= 0 else "var(--danger-text)"
            sign = "+" if val >= 0 else ""
            return f'<span style="color: {color}; font-weight: 700;">{sign}{val:.1f}%</span>'

        html += f"""
                <tr>
                    <td style="{td_style}">
                        {row['name']}
                    </td>
                    <td style="{td_style} text-align: right;">
                        {_fmt(row['c6m'])}
                    </td>
                    <td style="{td_style} text-align: right;">
                        {_fmt(row['c12w'])}
                    </td>
                </tr>
        """

    html += """
            </tbody>
        </table>
    </div>
    """
    return html
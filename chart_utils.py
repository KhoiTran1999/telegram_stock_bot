# chart_utils.py
import asyncio
import pandas as pd
from vnstock import Quote
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import datetime

async def generate_chart_html(symbol: str) -> str:
    """
    [Dùng cho /info]
    Vẽ biểu đồ Line Chart Transparent.
    FIX: Tắt hoàn toàn tương tác (Zoom, Pan, Hover Tooltip).
    """
    try:
        # 1. Lấy dữ liệu
        def _get_data():
            end_date = datetime.datetime.now()
            start_date = end_date - datetime.timedelta(days=180)
            q = Quote(symbol=symbol, source='VCI')
            return q.history(
                start=start_date.strftime('%Y-%m-%d'), 
                end=end_date.strftime('%Y-%m-%d'), 
                interval='1D'
            )

        df = await asyncio.to_thread(_get_data)
        
        if df is None or df.empty:
            return '<div style="padding:20px; text-align:center; color:var(--text-color);">Không có dữ liệu biểu đồ</div>'
            
        # 2. Xử lý dữ liệu
        df.columns = df.columns.str.lower().str.strip()
        if 'time' in df.columns: df['time'] = pd.to_datetime(df['time'])
        
        df['close'] = pd.to_numeric(df['close'], errors='coerce')
        df['volume'] = pd.to_numeric(df['volume'], errors='coerce')
        df = df.dropna(subset=['close'])
        
        if df['close'].mean() < 500:
            df['close'] = df['close'] * 1000

        # 3. Tính Scale
        min_price = df['close'].min()
        max_price = df['close'].max()
        delta = max_price - min_price
        padding = max_price * 0.05 if delta == 0 else delta * 0.2
        y_min = min_price - padding
        y_max = max_price + padding

        # 4. Vẽ
        fig = make_subplots(
            rows=2, cols=1, 
            shared_xaxes=True, 
            vertical_spacing=0.02, 
            row_heights=[0.75, 0.25] 
        )

        # Line Chart
        fig.add_trace(go.Scatter(
            x=df['time'].tolist(), 
            y=df['close'].tolist(), 
            mode='lines', 
            name='Giá', 
            line=dict(color='#007aff', width=2.5),
            fill='tozeroy',
            fillcolor='rgba(0, 122, 255, 0.15)', 
            showlegend=False,
            hoverinfo='skip'  # <--- TẮT HOVER TRÊN ĐƯỜNG GIÁ
        ), row=1, col=1)

        # Volume
        fig.add_trace(go.Bar(
            x=df['time'].tolist(), 
            y=df['volume'].tolist(), 
            name='Vol', 
            marker_color='rgba(128, 128, 128, 0.5)', 
            showlegend=False,
            hoverinfo='skip'  # <--- TẮT HOVER TRÊN VOL
        ), row=2, col=1)

        # 5. Layout Transparent + TẮT CỬ CHỈ + TẮT HOVER MODE
        fig.update_layout(
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            xaxis_rangeslider_visible=False,
            height=380,
            margin=dict(l=10, r=10, t=10, b=10),
            font=dict(size=10, family='Inter, sans-serif'),
            hovermode=False, # <--- TẮT CHẾ ĐỘ HOVER CHUNG (Quan trọng nhất)
            dragmode=False   # Tắt kéo thả
        )

        # Trục Y: Fixed Range (Không cho Zoom)
        fig.update_yaxes(range=[y_min, y_max], showgrid=True, fixedrange=True, row=1, col=1)
        fig.update_yaxes(showticklabels=False, showgrid=False, fixedrange=True, row=2, col=1)
        
        # Trục X: Fixed Range
        fig.update_xaxes(fixedrange=True, row=1, col=1)
        fig.update_xaxes(fixedrange=True, row=2, col=1)

        return fig.to_html(
            full_html=False, 
            include_plotlyjs=False, 
            config={'displayModeBar': False, 'responsive': True, 'staticPlot': True} # staticPlot=True làm biểu đồ thành ảnh tĩnh hoàn toàn
        )

    except Exception as e:
        print(f"Chart Error: {e}")
        return f'<div style="padding:20px;">Lỗi hiển thị: {e}</div>'


async def generate_mini_chart(symbol: str) -> str:
    """
    [Dùng cho /report]
    Vẽ biểu đồ Mini.
    FIX: Tắt hoàn toàn tương tác (Zoom, Pan, Hover Tooltip).
    """
    try:
        def _get_data():
            end = datetime.datetime.now()
            start = end - datetime.timedelta(days=180)
            q = Quote(symbol=symbol, source='VCI')
            return q.history(start=start.strftime('%Y-%m-%d'), end=end.strftime('%Y-%m-%d'), interval='1D')

        df = await asyncio.to_thread(_get_data)
        
        if df is None or df.empty: 
            return ""
            
        df.columns = df.columns.str.lower().str.strip()
        if 'time' in df.columns: df['time'] = pd.to_datetime(df['time'])
        
        df['close'] = pd.to_numeric(df['close'], errors='coerce')
        df['volume'] = pd.to_numeric(df['volume'], errors='coerce')
        df = df.dropna(subset=['close'])
        
        if df['close'].mean() < 500: 
            df['close'] = df['close'] * 1000

        mn, mx = df['close'].min(), df['close'].max()
        padding = (mx - mn) * 0.2 if (mx - mn) > 0 else mx * 0.05
        y_min, y_max = mn - padding, mx + padding

        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.02, row_heights=[0.75, 0.25])

        fig.add_trace(go.Scatter(
            x=df['time'].tolist(), y=df['close'].tolist(), 
            mode='lines', name='Giá', 
            line=dict(color='#007aff', width=2),
            fill='tozeroy', fillcolor='rgba(0, 122, 255, 0.1)',
            showlegend=False,
            hoverinfo='skip' # <--- TẮT HOVER
        ), row=1, col=1)

        fig.add_trace(go.Bar(
            x=df['time'].tolist(), y=df['volume'].tolist(), 
            name='Vol', marker_color='rgba(128, 128, 128, 0.3)',
            showlegend=False,
            hoverinfo='skip' # <--- TẮT HOVER
        ), row=2, col=1)

        # Layout Mini
        fig.update_layout(
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            xaxis_rangeslider_visible=False,
            height=320,
            margin=dict(l=0, r=0, t=10, b=10),
            font=dict(size=9, family='Inter, sans-serif'),
            hovermode=False, # <--- TẮT HOVER MODE
            dragmode=False
        )

        fig.update_yaxes(range=[y_min, y_max], showgrid=True, gridcolor='rgba(128,128,128,0.1)', fixedrange=True, row=1, col=1)
        fig.update_yaxes(showticklabels=False, showgrid=False, fixedrange=True, row=2, col=1)
        
        fig.update_xaxes(fixedrange=True, row=1, col=1)
        fig.update_xaxes(fixedrange=True, row=2, col=1)

        return fig.to_html(full_html=False, include_plotlyjs=False, config={'displayModeBar': False, 'responsive': True, 'staticPlot': True})

    except Exception as e:
        print(f"Mini Chart Error {symbol}: {e}")
        return ""
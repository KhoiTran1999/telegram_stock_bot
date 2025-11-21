import asyncio
import pandas as pd
from vnstock import Quote
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import datetime

async def generate_chart_html(symbol: str) -> str:
    """
    Vẽ biểu đồ Line Chart Transparent (để hỗ trợ Dark/Light Mode).
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
            # Màu fill cũng phải hơi trong suốt để hợp dark mode
            fillcolor='rgba(0, 122, 255, 0.15)', 
            showlegend=False
        ), row=1, col=1)

        # Volume
        fig.add_trace(go.Bar(
            x=df['time'].tolist(), 
            y=df['volume'].tolist(), 
            name='Vol', 
            marker_color='rgba(128, 128, 128, 0.5)', # Màu xám trung tính
            showlegend=False
        ), row=2, col=1)

        # 5. Layout Transparent (Quan trọng)
        fig.update_layout(
            paper_bgcolor='rgba(0,0,0,0)', # Trong suốt
            plot_bgcolor='rgba(0,0,0,0)',  # Trong suốt
            xaxis_rangeslider_visible=False,
            height=380, # Giảm chiều cao chút cho gọn
            margin=dict(l=10, r=10, t=10, b=10),
            font=dict(size=10, family='Inter, sans-serif'), # Font chữ mặc định
            hovermode='x unified'
        )

        # Trục Y
        fig.update_yaxes(range=[y_min, y_max], showgrid=True, row=1, col=1)
        fig.update_yaxes(showticklabels=False, showgrid=False, row=2, col=1)

        # Xuất HTML div
        return fig.to_html(
            full_html=False, 
            include_plotlyjs=False, 
            config={'displayModeBar': False, 'responsive': True} 
        )

    except Exception as e:
        print(f"Chart Error: {e}")
        return f'<div style="padding:20px;">Lỗi hiển thị: {e}</div>'
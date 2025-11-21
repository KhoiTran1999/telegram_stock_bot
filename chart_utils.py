# chart_utils.py
import asyncio
import pandas as pd
from vnstock import Quote
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import datetime

# Hàm vẽ cho /info (Giữ nguyên logic, chỉ sửa config cuối)
async def generate_chart_html(symbol: str) -> str:
    return await _create_chart(symbol, height=380)

# Hàm vẽ cho /report và EOD (Giữ nguyên logic, chỉ sửa config cuối)
async def generate_mini_chart(symbol: str) -> str:
    return await _create_chart(symbol, height=320)

# --- HÀM CORE DÙNG CHUNG ĐỂ TRÁNH LẶP CODE ---
async def _create_chart(symbol: str, height: int) -> str:
    try:
        # 1. Lấy dữ liệu
        def _get_data():
            end = datetime.datetime.now()
            start = end - datetime.timedelta(days=180)
            q = Quote(symbol=symbol, source='VCI')
            return q.history(start=start.strftime('%Y-%m-%d'), end=end.strftime('%Y-%m-%d'), interval='1D')

        df = await asyncio.to_thread(_get_data)
        
        if df is None or df.empty: 
            return ""
            
        # 2. Làm sạch dữ liệu
        df.columns = df.columns.str.lower().str.strip()
        if 'time' in df.columns: df['time'] = pd.to_datetime(df['time'])
        
        df['close'] = pd.to_numeric(df['close'], errors='coerce')
        df['volume'] = pd.to_numeric(df['volume'], errors='coerce')
        df = df.dropna(subset=['close'])
        
        # Logic Fix đơn vị: Nếu giá TB < 500 (tức là nghìn đồng) -> nhân 1000
        # VNINDEX (~1200) > 500 -> Giữ nguyên
        # HPG (~27) < 500 -> Nhân 1000
        if df['close'].mean() < 500: 
            df['close'] = df['close'] * 1000

        # 3. Tính Scale trục Y
        mn, mx = df['close'].min(), df['close'].max()
        padding = (mx - mn) * 0.1 if (mx - mn) > 0 else mx * 0.05
        y_min, y_max = mn - padding, mx + padding

        # 4. Vẽ
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.02, row_heights=[0.75, 0.25])

        # Đường giá
        fig.add_trace(go.Scatter(
            x=df['time'].tolist(), y=df['close'].tolist(), 
            mode='lines', name='Giá', 
            line=dict(color='#007aff', width=2),
            fill='tozeroy', fillcolor='rgba(0, 122, 255, 0.1)',
            showlegend=False, hoverinfo='skip'
        ), row=1, col=1)

        # Volume
        fig.add_trace(go.Bar(
            x=df['time'].tolist(), y=df['volume'].tolist(), 
            name='Vol', marker_color='rgba(128, 128, 128, 0.3)',
            showlegend=False, hoverinfo='skip'
        ), row=2, col=1)

        # 5. Layout: TẮT TƯƠNG TÁC (QUAN TRỌNG)
        fig.update_layout(
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            xaxis_rangeslider_visible=False,
            height=height,
            margin=dict(l=0, r=0, t=10, b=10),
            font=dict(size=9, family='Inter, sans-serif'),
            
            # ⚠️ CHÌA KHÓA: Tắt các chế độ tương tác chuột/tay
            hovermode=False, 
            dragmode=False,  
            clickmode='none'
        )

        # Khóa cứng các trục (Fixed Range)
        fig.update_yaxes(range=[y_min, y_max], showgrid=True, gridcolor='rgba(128,128,128,0.1)', fixedrange=True, showticklabels=False, row=1, col=1)
        fig.update_yaxes(showticklabels=False, showgrid=False, fixedrange=True, row=2, col=1)
        fig.update_xaxes(fixedrange=True, row=1, col=1)
        fig.update_xaxes(fixedrange=True, row=2, col=1)

        # 6. Xuất HTML: QUAN TRỌNG NHẤT LÀ staticPlot: False
        # Để JS có thể resize lại khi Modal mở ra
        return fig.to_html(
            full_html=False, 
            include_plotlyjs=False, 
            config={
                'displayModeBar': False, 
                'staticPlot': False, # <--- PHẢI LÀ FALSE ĐỂ JS RESIZE ĐƯỢC
                'responsive': True,
                'scrollZoom': False
            }
        )

    except Exception as e:
        print(f"Chart Error {symbol}: {e}")
        return ""
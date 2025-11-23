DIGEST_HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>StockBot Digest</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: var(--tg-theme-bg-color, #f2f2f7);
            --text-color: var(--tg-theme-text-color, #000);
            --hint-color: var(--tg-theme-hint-color, #8e8e93);
            --card-bg: var(--tg-theme-secondary-bg-color, #ffffff);
            --accent-color: #007aff;
        }
        
        body { 
            font-family: 'Inter', sans-serif; background-color: var(--bg-color); color: var(--text-color); 
            margin: 0; padding: 20px 16px 40px 16px; font-size: 14px; line-height: 1.5;
            visibility: hidden; opacity: 0; transition: opacity 0.3s ease-in-out;
        }
        body.loaded { visibility: visible; opacity: 1; }

        .header { text-align: center; margin-bottom: 32px; }
        .date-badge { display: inline-flex; align-items: center; gap: 6px; background-color: var(--card-bg); padding: 6px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; color: var(--hint-color); box-shadow: 0 2px 6px rgba(0,0,0,0.03); margin-bottom: 12px; }
        .header-title { font-size: 32px; font-weight: 800; margin: 0; background: linear-gradient(135deg, #007aff 0%, #af52de 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .pro-badge { background: linear-gradient(135deg, #FFD700 0%, #FFA500 100%); color: white; font-size: 11px; font-weight: 800; padding: 4px 8px; border-radius: 8px; display: inline-flex; transform: translateY(-2px); }
        
        .section-card { background-color: var(--card-bg); border-radius: 16px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.04); overflow: hidden; }
        .card-header { padding: 16px 16px 10px 16px; display: flex; align-items: center; gap: 10px; border-bottom: 1px solid rgba(0,0,0,0.05); }
        .card-icon { width: 28px; height: 28px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 16px; background: rgba(0,122,255,0.1); color: #007aff; }
        .card-title { font-size: 17px; font-weight: 700; }
        
        /* Item có hiệu ứng click */
        .list-item { padding: 14px 16px; border-bottom: 1px solid rgba(0,0,0,0.05); display: block; text-decoration: none; color: inherit; cursor: pointer; }
        .list-item:active { background-color: rgba(0,0,0,0.05); }
        .list-item:last-child { border-bottom: none; }
        
        .item-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
        .badge { background: rgba(0,0,0,0.05); font-size: 11px; font-weight: 700; padding: 4px 8px; border-radius: 6px; }
        .item-title { font-size: 15px; font-weight: 500; line-height: 1.4; }
        .item-meta { font-size: 12px; color: var(--hint-color); margin-top: 4px; }
        .hidden-item { display: none; }
        .action-area { padding: 10px; text-align: center; border-top: 1px solid rgba(0,0,0,0.05); }
        .btn-toggle { background: none; border: none; color: var(--accent-color); font-size: 13px; font-weight: 600; cursor: pointer; padding: 6px 12px; display: inline-flex; align-items: center; gap: 4px; }
        
        .stock-table { width: 100%; border-collapse: collapse; font-size: 13px; }
        .stock-table th { text-align: left; padding: 10px 16px; color: var(--hint-color); font-weight: 600; font-size: 11px; }
        .stock-table td { padding: 12px 16px; border-bottom: 1px solid rgba(0,0,0,0.05); }
        .score-badge { background: #5856d6; color: white; padding: 4px 8px; border-radius: 8px; font-weight: 700; font-size: 12px; }
        
        .premium-card { background: linear-gradient(135deg, #007aff 0%, #af52de 100%); border-radius: 24px; padding: 24px; color: white; text-align: center; margin-top: 32px; }
        .premium-btn { display: block; width: 100%; padding: 15px; background-color: #fff; color: #007aff; border: none; border-radius: 14px; font-size: 16px; font-weight: 700; cursor: pointer; margin-top: 16px; }
        
        .list-item.locked { position: relative; background: repeating-linear-gradient(45deg, var(--card-bg), var(--card-bg) 10px, #f9f9f9 10px, #f9f9f9 20px); cursor: default; }
        .blur-content { filter: blur(4px); opacity: 0.6; user-select: none; pointer-events: none; }
        .lock-overlay { position: absolute; top: 0; left: 0; width: 100%; height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; background: rgba(255, 255, 255, 0.6); z-index: 2; }
        .lock-btn { background: var(--text-color); color: var(--bg-color); border: none; padding: 6px 16px; border-radius: 20px; font-size: 12px; font-weight: 700; cursor: pointer; box-shadow: 0 4px 10px rgba(0,0,0,0.1); display: flex; align-items: center; gap: 4px; transform: translateY(2px); }
    </style>
</head>
<body>
    <div class="header">
        <div class="date-badge"><span>🗓️</span> {{ date_str }}</div>
        <div class="header-title">Daily Digest</div>
        {% if data.is_pro %}<span class="pro-badge">PRO MEMBER 👑</span>{% endif %}
    </div>

    {% if data.ai_news %}
    <div class="section-card">
        <div class="card-header">
            <div class="card-icon">🧠</div>
            <div class="card-title">AI Market Briefing</div>
            {% if data.ai_news.sentiment_score >= 7 %}
                <span class="st-badge act-buy" style="margin-left:auto">Tích cực 🟢</span>
            {% elif data.ai_news.sentiment_score <= 4 %}
                <span class="st-badge act-sell" style="margin-left:auto">Tiêu cực 🔴</span>
            {% else %}
                <span class="st-badge act-hold" style="margin-left:auto">Thận trọng 🟡</span>
            {% endif %}
        </div>
        
        <div style="padding: 16px;">
            <div style="background: rgba(0,122,255,0.05); border-radius: 12px; padding: 12px; margin-bottom: 16px;">
                <div style="font-weight:700; color:#007aff; margin-bottom:8px; font-size:12px; text-transform: uppercase;">⚡ Tiêu điểm nóng</div>
                {% for item in data.ai_news.headline %}
                <div style="margin-bottom:8px; font-size:14px; font-weight:600; line-height:1.4;">
                    <a href="{{ item.link }}" style="text-decoration:none; color:inherit;">
                        • {{ item.text }} <span style="color:#007aff; font-size:12px;">↗</span>
                    </a>
                </div>
                {% endfor %}
            </div>

            {% if data.ai_news.macro %}
            <div style="font-weight:700; color:#555; margin:16px 0 8px 0; font-size:12px; text-transform: uppercase;">🌊 Vĩ mô & Chính sách</div>
            {% for item in data.ai_news.macro %}
            <div style="margin-bottom:6px; font-size:13px;">
                <a href="{{ item.link }}" style="text-decoration:none; color:#333;">• {{ item.text }}</a>
            </div>
            {% endfor %}
            {% endif %}

            {% if data.ai_news.corporate %}
            <div style="margin-top: 20px;">
                <div style="font-weight:800; color:#555; margin-bottom:12px; font-size:13px; text-transform: uppercase; border-bottom: 2px solid #eee; padding-bottom: 6px; display:flex; align-items:center; gap:6px;">
                    <span>🏢</span> TIN DOANH NGHIỆP
                </div>
                
                <div class="list-container">
                    {% for item in data.ai_news.corporate %}
                    <div class="list-item" onclick="viewNews('{{ item.link }}')" style="padding: 10px 0; border-bottom: 1px solid #f0f0f0; display: flex; align-items: flex-start; gap: 10px;">
                        <div style="margin-top: 4px; min-width: 6px; height: 6px; background-color: #007aff; border-radius: 50%;"></div>
                        
                        <div style="flex: 1;">
                            <div style="font-size:14px; line-height: 1.5; color: var(--text-color);">
                                {% if item.ticker %}
                                    <span style="background-color: #eef2ff; color: #007aff; font-weight: 700; font-size: 11px; padding: 2px 6px; border-radius: 4px; margin-right: 4px; border: 1px solid rgba(0,122,255,0.2);">{{ item.ticker }}</span>
                                {% endif %}
                                {{ item.text }}
                            </div>
                        </div>
                    </div>
                    {% endfor %}
                </div>
            </div>
            {% endif %}

            <div style="margin-top: 16px; font-style: italic; color: #666; font-size: 13px; border-top: 1px solid #eee; padding-top: 12px; background:#fafafa; padding:12px; border-radius:8px;">
                "{{ data.ai_news.comment }}"
            </div>
        </div>
    </div>
    {% endif %}

    {% if data.bctc %}
    <div class="section-card" id="bctc-card">
        <div class="card-header"><div class="card-icon" style="color:#34c759; background:rgba(52,199,89,0.1)">📊</div><div class="card-title">Báo Cáo Tài Chính</div></div>
        <div class="list-container">
            {% for item in data.bctc %}
                <div class="{% if loop.index > 3 %}hidden-item{% endif %}">
                    {% if item.is_locked %}
                    <div class="list-item locked">
                        <div class="blur-content">
                            <div class="item-header"><span class="badge">{{ item.symbol }}</span> <b>Q{{ item.quarter }}/{{ item.year }}</b></div>
                            <div class="item-meta">Lợi nhuận tăng trưởng đột biến...</div>
                        </div>
                        <div class="lock-overlay"><button class="lock-btn" onclick="Telegram.WebApp.close()">🔒 Nâng cấp để xem</button></div>
                    </div>
                    {% else %}
                    <div class="list-item">
                        <div class="item-header"><span class="badge">{{ item.symbol }}</span> <b>Q{{ item.quarter }}/{{ item.year }}</b></div>
                        <div class="item-meta">🕒 Công bố lúc {{ item.time }}</div>
                    </div>
                    {% endif %}
                </div>
            {% endfor %}
        </div>
        {% if data.bctc|length > 3 %}
        <div class="action-area">
            <button class="btn-toggle" onclick="toggleSection('bctc-card', this, {{ data.bctc|length }}, 3)">Xem thêm {{ data.bctc|length - 3 }} mục ↓</button>
        </div>
        {% endif %}
    </div>
    {% endif %}

    {% if data.reports %}
    <div class="section-card" id="reports-card">
        <div class="card-header"><div class="card-icon" style="color:#007aff; background:rgba(0,122,255,0.1)">📑</div><div class="card-title">Góc Nhìn Chuyên Gia</div></div>
        <div class="list-container">
            {% for item in data.reports %}
                <div class="{% if loop.index > 3 %}hidden-item{% endif %}">
                    {% if item.is_locked %}
                    <div class="list-item locked">
                         <div class="blur-content">
                            <div class="item-header"><span class="badge">{{ item.symbol }}</span></div>
                            <div class="item-title">{{ item.title }}</div>
                        </div>
                        <div class="lock-overlay"><button class="lock-btn" onclick="Telegram.WebApp.close()">🔒 Mở khóa {{ item.symbol }}</button></div>
                    </div>
                    {% else %}
                    <div class="list-item" onclick="viewNews('{{ item.link }}')">
                        <div class="item-header"><span class="badge">{{ item.symbol }}</span></div>
                        <div class="item-title">{{ item.title }}</div>
                        {% if item.time %}<div class="item-meta">🕒 {{ item.time }}</div>{% endif %}
                    </div>
                    {% endif %}
                </div>
            {% endfor %}
        </div>
        {% if data.reports|length > 3 %}
        <div class="action-area">
            <button class="btn-toggle" onclick="toggleSection('reports-card', this, {{ data.reports|length }}, 3)">Xem thêm {{ data.reports|length - 3 }} báo cáo ↓</button>
        </div>
        {% endif %}
    </div>
    {% endif %}

    {% if data.specialized %}
    <div class="section-card" id="specialized-card">
        <div class="card-header"><div class="card-icon" style="color:#ff9500; background:rgba(255,149,0,0.1)">🏢</div><div class="card-title">Tin Doanh Nghiệp</div></div>
        <div class="list-container">
            {% for item in data.specialized %}
            <div class="list-item {% if loop.index > 3 %}hidden-item{% endif %}" onclick="viewNews('{{ item.link }}')">
                <div class="item-title">{{ item.title }}</div>
            </div>
            {% endfor %}
        </div>
        {% if data.specialized|length > 3 %}
        <div class="action-area">
            <button class="btn-toggle" onclick="toggleSection('specialized-card', this, {{ data.specialized|length }}, 3)">Xem thêm {{ data.specialized|length - 3 }} tin ↓</button>
        </div>
        {% endif %}
    </div>
    {% endif %}

    {% if data.value_stocks %}
    <div class="section-card" id="stocks-card">
        <div class="card-header"><div class="card-icon">💎</div><div class="card-title">Top Value Hôm Nay</div></div>
        <table class="stock-table">
            <thead><tr><th>Mã</th><th style="text-align:right">Chỉ Số</th><th style="text-align:right">Điểm</th></tr></thead>
            <tbody>
                {% for item in data.value_stocks %}
                <tr class="{% if loop.index > 5 %}hidden-item{% endif %}">
                    <td><b>{{ item.symbol }}</b><br><span style="font-size:11px; color:#8e8e93;">{{ item.industry }}</span></td>
                    <td style="text-align:right">P/E: {{ item.pe }}<br>ROE: {{ item.roe }}%</td>
                    <td style="text-align:right"><span class="score-badge">{{ item.score }}</span></td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
        {% if data.value_stocks|length > 5 %}
        <div class="action-area">
            <button class="btn-toggle" onclick="toggleSection('stocks-card', this, {{ data.value_stocks|length }}, 5)">Xem thêm {{ data.value_stocks|length - 5 }} mã ↓</button>
        </div>
        {% endif %}
    </div>
    {% endif %}

    {% if data.macro %}
    <div class="section-card" id="macro-card">
        <div class="card-header"><div class="card-icon" style="color:#af52de; background:rgba(175,82,222,0.1)">🌍</div><div class="card-title">Vĩ Mô & Chính Sách</div></div>
        <div class="list-container">
            {% for item in data.macro %}
            <div class="list-item {% if loop.index > 3 %}hidden-item{% endif %}" onclick="viewNews('{{ item.link }}')">
                <div class="item-title">{{ item.title }}</div>
            </div>
            {% endfor %}
        </div>
        {% if data.macro|length > 3 %}
        <div class="action-area">
            <button class="btn-toggle" onclick="toggleSection('macro-card', this, {{ data.macro|length }}, 3)">Xem thêm {{ data.macro|length - 3 }} tin ↓</button>
        </div>
        {% endif %}
    </div>
    {% endif %}

    {% if not data.is_pro %}
    <div class="premium-card">
        <div style="font-size:18px; font-weight:800; margin-bottom:10px;">Mở khóa toàn bộ sức mạnh 🚀</div>
        <div style="font-size:13px; opacity:0.9; margin-bottom:20px;">
            • Xem chi tiết BCTC ngay khi công bố<br>
            • Đọc báo cáo phân tích chuyên sâu<br>
            • Sử dụng Bộ lọc Value Realtime
        </div>
        <button class="premium-btn" onclick="Telegram.WebApp.close()">🔥 Gõ /upgrade ngay</button>
    </div>
    {% else %}
    <div style="text-align:center; margin-top:30px;">
        <button style="padding:12px 40px; background:var(--text-color); color:var(--bg-color); border:none; border-radius:12px; font-weight:600;" onclick="Telegram.WebApp.close()">Đóng</button>
    </div>
    {% endif %}

    <script>
        Telegram.WebApp.expand();
        window.addEventListener('load', function() {
            document.body.classList.add('loaded');
            Telegram.WebApp.ready();
        });

        // --- HÀM MỞ LINK CƠ BẢN NHẤT ---
        function viewNews(url) {
            // Mở link bằng trình duyệt mặc định (Safari/Chrome)
            // Đảm bảo hoạt động 100% trên mọi thiết bị
            Telegram.WebApp.openLink(url);
        }

        // Hàm toggle view cũ
        function toggleSection(cardId, btn, total, limit) {
            const card = document.getElementById(cardId);
            const hiddenItems = card.querySelectorAll('.hidden-item');
            const isExpanded = btn.getAttribute('data-expanded') === 'true';
            const hiddenCount = total - limit;
            let unit = 'mục';
            if (cardId.includes('stocks')) unit = 'mã';
            else if (cardId.includes('specialized') || cardId.includes('macro')) unit = 'tin';
            else if (cardId.includes('reports')) unit = 'báo cáo';

            if (!isExpanded) {
                hiddenItems.forEach(item => {
                    if (item.tagName === 'TR') item.style.display = 'table-row';
                    else item.style.display = 'block';
                    item.style.animation = 'fadeIn 0.3s ease';
                });
                btn.innerHTML = 'Thu gọn ↑';
                btn.setAttribute('data-expanded', 'true');
            } else {
                hiddenItems.forEach(item => { item.style.display = 'none'; });
                btn.innerHTML = `Xem thêm ${hiddenCount} ${unit} ↓`;
                btn.setAttribute('data-expanded', 'false');
                card.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        }

        const style = document.createElement('style');
        style.innerHTML = `
            @keyframes fadeIn { from { opacity: 0; transform: translateY(-5px); } to { opacity: 1; transform: translateY(0); } }
        `;
        document.head.appendChild(style);
    </script>
</body>
</html>
"""

DIGEST_404_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Liên kết hết hạn</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: var(--tg-theme-bg-color, #fff);
            --text-color: var(--tg-theme-text-color, #000);
            --hint-color: var(--tg-theme-hint-color, #999);
            --button-color: var(--tg-theme-button-color, #007aff);
            --button-text-color: var(--tg-theme-button-text-color, #fff);
        }
        body {
            font-family: 'Inter', sans-serif;
            background-color: var(--bg-color);
            color: var(--text-color);
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            height: 100vh;
            margin: 0;
            text-align: center;
            padding: 20px;
        }
        .icon { font-size: 64px; margin-bottom: 20px; animation: pulse 2s infinite; }
        h2 { font-size: 22px; margin: 0 0 10px 0; font-weight: 700; }
        p { color: var(--hint-color); font-size: 15px; line-height: 1.6; margin-bottom: 30px; max-width: 320px; }
        button {
            padding: 14px 32px;
            background-color: var(--button-color);
            color: var(--button-text-color);
            border: none;
            border-radius: 14px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            width: 100%;
            max-width: 200px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.1);
        }
        @keyframes pulse { 0% { transform: scale(1); } 50% { transform: scale(1.1); } 100% { transform: scale(1); } }
    </style>
</head>
<body>
    <div class="icon">⏳</div>
    <h2>Bản tin đã hết hạn</h2>
    <p>
        Nội dung bản tin Daily Digest chỉ được lưu trữ trong 24 giờ để đảm bảo tính cập nhật và bảo mật.
        <br><br>
        Vui lòng xem các tin nhắn mới nhất từ bot.
    </p>
    <button onclick="Telegram.WebApp.close()">Đóng</button>
    <script>Telegram.WebApp.ready(); Telegram.WebApp.expand();</script>
</body>
</html>
"""

#--------------------------------

# digest_template.py

PROFILE_HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Hồ sơ doanh nghiệp {{ symbol }}</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            /* Biến màu mặc định (sẽ được override bởi Telegram Theme) */
            --bg-color: var(--tg-theme-bg-color, #f2f2f7);
            --text-color: var(--tg-theme-text-color, #000);
            --hint-color: var(--tg-theme-hint-color, #8e8e93);
            --card-bg: var(--tg-theme-secondary-bg-color, #ffffff);
            --accent: var(--tg-theme-button-color, #007aff);
            --chart-grid: #e5e5ea; /* Màu lưới mặc định sáng */
        }
        
        /* Dark Mode Overrides (Telegram tự inject class hoặc biến, ta dùng media query fallback) */
        @media (prefers-color-scheme: dark) {
            :root {
                --chart-grid: #3a3a3c; /* Màu lưới tối */
            }
        }

        * { box-sizing: border-box; }
        
        body {
            margin: 0; padding: 16px;
            font-family: 'Inter', sans-serif;
            background-color: var(--bg-color);
            color: var(--text-color);
            visibility: hidden; opacity: 0; transition: opacity 0.3s ease-in-out;
        }
        body.loaded { visibility: visible; opacity: 1; }
        
        .page { max-width: 720px; margin: 0 auto; }
        
        /* Header */
        .header { margin-bottom: 12px; }
        .chip-row { display: flex; justify-content: space-between; margin-bottom: 8px; }
        .chip { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 20px; background: rgba(128,128,128,0.15); font-size: 11px; color: var(--hint-color); font-weight: 600; text-transform: uppercase; }
        .pro-badge { display: inline-flex; padding: 3px 8px; border-radius: 8px; background: linear-gradient(135deg, #FFD700, #FFA500); color: white; font-size: 10px; font-weight: 800; text-transform: uppercase; }
        
        .symbol { display: flex; align-items: baseline; gap: 8px; margin-top: 4px; }
        .symbol-main { font-size: 28px; font-weight: 800; letter-spacing: -0.5px; color: var(--accent); }
        .symbol-sub { font-size: 13px; color: var(--hint-color); font-weight: 500; }
        .meta { margin-top: 4px; font-size: 11px; color: var(--hint-color); }

        /* Card Text */
        .card {
            background-color: var(--card-bg); border-radius: 16px; padding: 16px; margin-top: 16px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.03); 
        }
        .card-title-row { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; border-bottom: 1px solid rgba(128,128,128,0.1); padding-bottom: 8px; }
        .card-title { font-size: 15px; font-weight: 700; text-transform: uppercase; color: var(--text-color); }
        .profile-text { font-size: 14px; line-height: 1.6; color: var(--text-color); white-space: pre-line; }

        /* Chart Card */
        .chart-card {
            background-color: var(--card-bg); border-radius: 16px;
            padding: 16px 0; margin-top: 20px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.05); overflow: hidden;
        }
        .chart-header { 
            padding: 0 16px; margin-bottom: 4px; 
            font-size: 12px; font-weight: 700; text-transform: uppercase; 
            color: var(--text-color); opacity: 0.8;
        }

        /* Footer */
        .close-btn { width: 100%; padding: 12px; margin-top: 30px; border-radius: 12px; background: var(--card-bg); border: none; color: var(--text-color); font-weight: 600; cursor: pointer; box-shadow: 0 4px 10px rgba(0,0,0,0.05); }
    </style>
</head>
<body>
    <div class="page">
        <div class="header">
            <div class="chip-row">
                <div class="chip">Hồ sơ doanh nghiệp</div>
                {% if is_pro %}<div class="pro-badge">PRO 👑</div>{% endif %}
            </div>
            <div class="symbol">
                <div class="symbol-main">{{ symbol }}</div>
                <div class="symbol-sub">Hồ sơ chi tiết</div>
            </div>
            {% if generated_at %}<div class="meta">Cập nhật lúc: {{ generated_at }}</div>{% endif %}
        </div>

        {% for sec in sections %}
        <div class="card">
            <div class="card-title-row">
                <span>{{ sec.icon }}</span>
                <div class="card-title">{{ sec.title }}</div>
            </div>
            <div class="profile-text">{{ sec.body }}</div>
        </div>
        {% endfor %}

        {% if chart_html %}
        <div class="chart-card" id="chart-container">
            <div class="chart-header">📉 Biến động giá (6 tháng)</div>
            {{ chart_html | safe }}
        </div>
        {% endif %}

        <button class="close-btn" onclick="Telegram.WebApp.close()">Đóng Hồ Sơ</button>
    </div>

    <script>
        Telegram.WebApp.expand();

        window.addEventListener('load', function() {
            document.body.classList.add('loaded');
            
            // Format text bold
            const textElements = document.querySelectorAll('.profile-text');
            textElements.forEach(el => {
                el.innerHTML = el.innerHTML.replace(/\*\*(.*?)\*\*/g, '<b>$1</b>');
            });

            // --- [THEME ADAPTION LOGIC] ---
            applyChartTheme();
            
            // Lắng nghe sự kiện đổi theme của Telegram (nếu có)
            Telegram.WebApp.onEvent('themeChanged', applyChartTheme);
        });

        function applyChartTheme() {
            // Tìm thẻ div của Plotly (nó thường có class 'plotly-graph-div')
            const chartDiv = document.querySelector('.plotly-graph-div');
            if (!chartDiv) return;

            // Lấy màu từ biến CSS của Telegram
            const style = getComputedStyle(document.body);
            const textColor = style.getPropertyValue('--tg-theme-text-color').trim() || '#000000';
            const hintColor = style.getPropertyValue('--tg-theme-hint-color').trim() || '#8e8e93';
            
            // Xác định màu Grid dựa trên theme (Telegram.WebApp.colorScheme)
            const scheme = Telegram.WebApp.colorScheme; // 'light' or 'dark'
            const gridColor = (scheme === 'dark') ? 'rgba(255,255,255,0.1)' : 'rgba(0,0,0,0.05)';

            // Update layout Plotly
            const update = {
                'font.color': textColor,     // Đổi màu chữ
                'xaxis.gridcolor': gridColor, // Đổi màu lưới X
                'yaxis.gridcolor': gridColor, // Đổi màu lưới Y
                'xaxis.tickfont.color': hintColor,
                'yaxis.tickfont.color': hintColor
            };

            Plotly.relayout(chartDiv, update);
        }
    </script>
</body>
</html>
"""

PROFILE_404_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Hồ sơ không tìm thấy</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: var(--tg-theme-bg-color, #fff);
            --text-color: var(--tg-theme-text-color, #000);
            --hint-color: var(--tg-theme-hint-color, #999);
            --button-color: var(--tg-theme-button-color, #007aff);
            --button-text-color: var(--tg-theme-button-text-color, #fff);
        }
        body {
            font-family: 'Inter', sans-serif;
            background-color: var(--bg-color);
            color: var(--text-color);
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            height: 100vh;
            margin: 0;
            padding: 16px;
            text-align: center;
        }
        .icon {
            font-size: 40px;
            margin-bottom: 14px;
        }
        h2 {
            font-size: 22px;
            margin: 0 0 10px 0;
            font-weight: 700;
        }
        p {
            color: var(--hint-color);
            font-size: 14px;
            line-height: 1.6;
            margin-bottom: 24px;
            max-width: 320px;
        }
        button {
            padding: 10px 22px;
            background-color: var(--button-color);
            color: var(--button-text-color);
            border: none;
            border-radius: 999px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            width: 100%;
            max-width: 200px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.1);
        }
    </style>
</head>
<body>
    <div class="icon">📄</div>
    <h2>Hồ sơ không tìm thấy</h2>
    <p>
        Hồ sơ cho mã <strong>{{ symbol }}</strong> đã hết hạn hoặc chưa được tạo.
        <br><br>
        Vui lòng quay lại Telegram và gõ lệnh <code>/info {{ symbol }}</code> để tạo mới hồ sơ.
    </p>
    <button onclick="Telegram.WebApp.close()">Đóng</button>
    <script>
        Telegram.WebApp.ready();
        Telegram.WebApp.expand();
    </script>
</body>
</html>
"""

#--------------------------------

REPORT_HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Báo cáo Danh mục</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: var(--tg-theme-bg-color, #f2f2f7);
            --text-color: var(--tg-theme-text-color, #000);
            --hint-color: var(--tg-theme-hint-color, #8e8e93);
            --card-bg: var(--tg-theme-secondary-bg-color, #ffffff);
            --accent-color: var(--tg-theme-button-color, #007aff);
            
            --success-bg: rgba(52, 199, 89, 0.15); --success-text: #34c759;
            --warning-bg: rgba(255, 204, 0, 0.15); --warning-text: #d48806;
            --danger-bg: rgba(255, 59, 48, 0.15);  --danger-text: #ff3b30;
            --info-bg: rgba(0, 122, 255, 0.1);     --info-text: #007aff;
        }
        
        /* --- SMOOTH LOADING --- */
        body { 
            font-family: 'Inter', sans-serif; 
            background-color: var(--bg-color); 
            color: var(--text-color); 
            margin: 0; padding: 16px; 
            -webkit-font-smoothing: antialiased; 
            visibility: hidden; opacity: 0; transition: opacity 0.3s ease-in-out;
        }
        body.loaded { visibility: visible; opacity: 1; }
        
        /* Header */
        .header-row { text-align: center; margin-bottom: 20px; }
        .header-title { font-size: 20px; font-weight: 800; margin: 0; display: inline-flex; align-items: center; gap: 6px; color: var(--text-color); }
        .pro-badge { background: linear-gradient(135deg, #FFD700 0%, #FFA500 100%); color: white; font-size: 10px; font-weight: 800; padding: 3px 8px; border-radius: 8px; text-transform: uppercase; transform: translateY(-1px); }
        .header-time { font-size: 12px; color: var(--hint-color); margin-top: 4px; font-weight: 500; }

        /* Score & Market Cards (Giữ nguyên) */
        .score-card { background: linear-gradient(135deg, #007aff, #5856d6); color: white; border-radius: 20px; padding: 24px; text-align: center; margin-bottom: 20px; box-shadow: 0 8px 20px rgba(0,122,255,0.25); position: relative; overflow: hidden; }
        .score-val { font-size: 48px; font-weight: 800; line-height: 1; letter-spacing: -2px; }
        .score-label { font-size: 14px; font-weight: 500; opacity: 0.9; margin-top: 4px; text-transform: uppercase; letter-spacing: 0.5px; }
        .score-sub { font-size: 12px; opacity: 0.8; margin-top: 8px; }

        .market-card { background-color: var(--card-bg); border-radius: 16px; padding: 16px; margin-bottom: 24px; border-left: 4px solid var(--accent-color); box-shadow: 0 2px 8px rgba(0,0,0,0.04); }
        .market-title { font-size: 12px; font-weight: 700; text-transform: uppercase; color: var(--hint-color); margin-bottom: 8px; letter-spacing: 0.5px; }
        .market-text { font-size: 14px; line-height: 1.6; font-weight: 400; }

        /* Stock List */
        .stock-card { background-color: var(--card-bg); border-radius: 16px; padding: 16px; margin-bottom: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.04); }
        .st-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
        .st-symbol { font-size: 18px; font-weight: 800; color: var(--text-color); }
        .st-industry { font-size: 12px; color: var(--hint-color); font-weight: 500; margin-left: 6px; }
        
        .st-badge { font-size: 11px; font-weight: 700; padding: 4px 10px; border-radius: 20px; text-transform: uppercase; }
        .act-buy { background-color: var(--success-bg); color: var(--success-text); }
        .act-hold { background-color: var(--warning-bg); color: var(--warning-text); }
        .act-sell { background-color: var(--danger-bg); color: var(--danger-text); }
        .act-neutral { background-color: var(--bg-color); color: var(--hint-color); }

        /* --- CHART MINI WRAPPER --- */
        .chart-mini-wrapper {
            border: 1px solid rgba(0,0,0,0.05); border-radius: 12px;
            padding: 4px 0; margin-bottom: 16px;
            background: rgba(128,128,128,0.02); /* Nền siêu nhẹ */
            overflow: hidden;
        }

        .st-analysis { font-size: 14px; line-height: 1.6; margin-bottom: 12px; color: var(--text-color); white-space: pre-line; }
        .st-metrics { background-color: var(--bg-color); border-radius: 10px; padding: 10px; font-size: 12px; color: var(--hint-color); display: flex; align-items: center; gap: 6px; }
        .st-metrics-icon { font-size: 14px; }

        .footer { text-align: center; margin-top: 30px; font-size: 12px; color: var(--hint-color); padding-bottom: 40px; }
        .btn-close { display: block; width: 100%; padding: 14px; background-color: var(--card-bg); color: var(--text-color); border: none; border-radius: 14px; font-size: 15px; font-weight: 600; margin-top: 20px; cursor: pointer; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }
    </style>
</head>
<body>
    <div class="header-row">
        <div class="header-title">Báo Cáo Danh Mục {% if is_pro %}<span class="pro-badge">PRO 👑</span>{% endif %}</div>
        <div class="header-time">Cập nhật lúc: {{ generated_at }}</div>
    </div>

    <div class="score-card">
        <div class="score-val">{{ data.portfolio_health_score }}</div>
        <div class="score-label">Điểm Sức Khỏe</div>
        <div class="score-sub">Đánh giá tiềm năng danh mục</div>
    </div>

    <div class="market-card">
        <div class="market-title">NHẬN ĐỊNH THỊ TRƯỜNG</div>
        <div class="market-text">{{ data.general_market_comment }}</div>
    </div>

    <div style="margin-bottom: 8px; font-size: 13px; font-weight: 600; color: var(--hint-color); text-transform: uppercase; letter-spacing: 0.5px;">Chi tiết cổ phiếu</div>
    
    {% for stock in data.stocks %}
    <div class="stock-card">
        <div class="st-header">
            <div>
                <span class="st-symbol">{{ stock.symbol }}</span>
                <span class="st-industry">{{ stock.industry }}</span>
            </div>
            {% set act = stock.action | lower %}
            {% set badge_class = 'act-neutral' %}
            {% if 'mua' in act or 'tăng' in act %} {% set badge_class = 'act-buy' %}
            {% elif 'giữ' in act or 'nắm' in act %} {% set badge_class = 'act-hold' %}
            {% elif 'bán' in act or 'hạ' in act or 'giảm' in act %} {% set badge_class = 'act-sell' %}
            {% endif %}
            <div class="st-badge {{ badge_class }}">{{ stock.action }}</div>
        </div>
        
        {% if stock.chart_html %}
        <div class="chart-mini-wrapper">
            {{ stock.chart_html | safe }}
        </div>
        {% endif %}
        
        <div class="st-analysis">{{ stock.analysis }}</div>
        
        {% if stock.key_metrics %}
        <div class="st-metrics">
            <span class="st-metrics-icon">📊</span> 
            <span><b>Metrics:</b> {{ stock.key_metrics }}</span>
        </div>
        {% endif %}
    </div>
    {% endfor %}

    <div class="footer">Dữ liệu được phân tích tự động bởi AI (Gemini).<br>Không phải khuyến nghị đầu tư tài chính.</div>
    <button class="btn-close" onclick="Telegram.WebApp.close()">Đóng Báo Cáo</button>

    <script>
        Telegram.WebApp.expand();
        window.addEventListener('load', function() {
            document.body.classList.add('loaded');
            applyChartTheme();
            Telegram.WebApp.onEvent('themeChanged', applyChartTheme);
            Telegram.WebApp.ready();
        });

        function applyChartTheme() {
            // Logic đổi màu biểu đồ theo Theme Telegram
            const chartDivs = document.querySelectorAll('.plotly-graph-div');
            if (!chartDivs.length) return;

            const scheme = Telegram.WebApp.colorScheme;
            const style = getComputedStyle(document.body);
            const textColor = style.getPropertyValue('--text-color').trim();
            const hintColor = style.getPropertyValue('--hint-color').trim();
            const gridColor = (scheme === 'dark') ? 'rgba(255,255,255,0.1)' : 'rgba(0,0,0,0.05)';

            const update = {
                'font.color': textColor,
                'xaxis.gridcolor': gridColor,
                'yaxis.gridcolor': gridColor,
                'xaxis.tickfont.color': hintColor,
                'yaxis.tickfont.color': hintColor
            };

            chartDivs.forEach(div => Plotly.relayout(div, update));
        }
    </script>
</body>
</html>
"""

REPORT_404_TEMPLATE = """
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Báo cáo không tìm thấy</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: var(--tg-theme-bg-color, #f2f2f7);
            --text-color: var(--tg-theme-text-color, #000);
            --hint-color: var(--tg-theme-hint-color, #8e8e93);
            --button-color: var(--tg-theme-button-color, #007aff);
            --button-text-color: var(--tg-theme-button-text-color, #fff);
        }
        body {
            font-family: 'Inter', sans-serif;
            background-color: var(--bg-color);
            color: var(--text-color);
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            height: 100vh;
            margin: 0;
            padding: 20px;
            text-align: center;
            box-sizing: border-box;
        }
        .icon { 
            font-size: 64px; 
            margin-bottom: 24px; 
            animation: float 3s ease-in-out infinite; 
        }
        h2 { 
            font-size: 20px; 
            margin: 0 0 12px 0; 
            font-weight: 700; 
        }
        p { 
            color: var(--hint-color); 
            font-size: 15px; 
            line-height: 1.5; 
            margin-bottom: 32px; 
            max-width: 300px; 
        }
        button {
            padding: 14px 32px;
            background-color: var(--button-color);
            color: var(--button-text-color);
            border: none;
            border-radius: 14px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            width: 100%;
            max-width: 240px;
            box-shadow: 0 8px 20px rgba(0,0,0,0.1);
            transition: transform 0.1s;
        }
        button:active { transform: scale(0.98); }
        
        @keyframes float { 
            0% { transform: translateY(0px); } 
            50% { transform: translateY(-10px); } 
            100% { transform: translateY(0px); } 
        }
    </style>
</head>
<body>
    <div class="icon">📉</div>
    <h2>Không tìm thấy báo cáo</h2>
    <p>
        Báo cáo phân tích này có thể đã hết hạn (lưu trữ 7 ngày) hoặc đường dẫn không hợp lệ.
        <br><br>
        Vui lòng quay lại bot và gõ lệnh <b>/report</b> để tạo báo cáo mới nhất.
    </p>
    <button onclick="Telegram.WebApp.close()">Đóng</button>
    <script>
        Telegram.WebApp.ready();
        Telegram.WebApp.expand();
    </script>
</body>
</html>
"""

#---------------------------------

SCREENER_HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="vi" data-theme="light">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Định Giá Cổ Phiếu</title>
    <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Oswald:wght@700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #f8f9fa; --card-bg: #ffffff; --text-primary: #111827; --text-secondary: #6b7280;
            --metric-bg: rgba(249, 250, 251, 1); --metric-border: rgba(0,0,0,0.04);
            --r1-bg: linear-gradient(160deg, #ffffff 40%, #fffbeb 100%); --r1-border: #fbbf24; --r1-shadow: rgba(245, 158, 11, 0.25);
            --r2-bg: linear-gradient(160deg, #ffffff 40%, #f3f4f6 100%); --r2-border: #9ca3af; --r2-shadow: rgba(156, 163, 175, 0.2);
            --r3-bg: linear-gradient(160deg, #ffffff 40%, #fff7ed 100%); --r3-border: #fdba74; --r3-shadow: rgba(234, 88, 12, 0.15);
            --success-text: #059669; --success-bg: #d1fae5;
            --danger-text: #dc2626;  --danger-bg: #fee2e2;
            --warning-text: #d97706; --warning-bg: #fef3c7;
            --brand-gradient: linear-gradient(135deg, #2563eb 0%, #7c3aed 100%);
            --pro-gradient: linear-gradient(135deg, #F59E0B 0%, #FCD34D 100%);
        }
        [data-theme="dark"] {
            --bg-color: #121212; --card-bg: #1e1e1e; --text-primary: #f9fafb; --text-secondary: #9ca3af;
            --metric-bg: rgba(255, 255, 255, 0.05); --metric-border: rgba(255, 255, 255, 0.1);
            --r1-bg: linear-gradient(160deg, #1e1e1e 40%, #2d2606 100%); --r1-border: #b45309; --r1-shadow: rgba(245, 158, 11, 0.1);
            --r2-bg: linear-gradient(160deg, #1e1e1e 40%, #27272a 100%); --r2-border: #4b5563; --r2-shadow: rgba(255, 255, 255, 0.05);
            --r3-bg: linear-gradient(160deg, #1e1e1e 40%, #2c1509 100%); --r3-border: #7c2d12; --r3-shadow: rgba(234, 88, 12, 0.1);
            --success-text: #34d399; --success-bg: rgba(5, 150, 105, 0.2);
            --danger-text: #f87171;  --danger-bg: rgba(220, 38, 38, 0.2);
            --warning-text: #fbbf24; --warning-bg: rgba(217, 119, 6, 0.2);
        }
        body { font-family: 'Manrope', sans-serif; background-color: var(--bg-color); color: var(--text-primary); margin: 0; padding: 20px 16px 40px 16px; transition: all 0.3s ease; -webkit-font-smoothing: antialiased; }
        .theme-toggle { position: absolute; top: 20px; right: 20px; background: var(--card-bg); border: 1px solid var(--text-secondary); color: var(--text-primary); padding: 6px 12px; border-radius: 20px; font-size: 11px; font-weight: 700; cursor: pointer; opacity: 0.7; }
        .header { text-align: center; margin-bottom: 28px; margin-top: 10px;}
        .header-title { font-size: 26px; font-weight: 800; margin: 0; letter-spacing: -0.5px; background: var(--brand-gradient); -webkit-background-clip: text; -webkit-text-fill-color: transparent; display:inline-block; }
        .pro-badge { background: var(--pro-gradient); color: #fff; font-size: 10px; font-weight: 800; padding: 4px 8px; border-radius: 12px; display: inline-flex; align-items: center; transform: translateY(-4px); box-shadow: 0 4px 10px rgba(245, 158, 11, 0.3); margin-left: 6px; }
        .header-subtitle { font-size: 13px; color: var(--text-secondary); font-weight: 500; margin-top: 4px; }
        .timestamp-badge { display: inline-block; margin-top: 8px; padding: 4px 10px; background: var(--metric-bg); border-radius: 12px; font-size: 11px; font-weight: 600; color: var(--text-secondary); border: 1px solid var(--metric-border); }
        .card { background: var(--card-bg); border-radius: 20px; padding: 18px; margin-bottom: 16px; position: relative; border: 1px solid var(--metric-border); box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.02); transition: transform 0.2s; }
        .card-rank-1 { background: var(--r1-bg); border: 1px solid var(--r1-border); box-shadow: 0 10px 25px -5px var(--r1-shadow); transform: scale(1.02); z-index: 10; }
        .card-rank-1 .rank-badge { background: linear-gradient(135deg, #FFD700, #F59E0B); box-shadow: 0 4px 12px rgba(245, 158, 11, 0.5); font-size: 18px; width: 36px; height: 36px; border: 2px solid #fff; }
        .card-rank-1::before { content: '👑'; position: absolute; top: -14px; left: 10px; font-size: 24px; transform: rotate(-15deg); z-index: 20; filter: drop-shadow(0 2px 4px rgba(0,0,0,0.1)); }
        .card-rank-2 { background: var(--r2-bg); border: 1px solid var(--r2-border); box-shadow: 0 10px 25px -5px var(--r2-shadow); }
        .card-rank-2 .rank-badge { background: linear-gradient(135deg, #E5E7EB, #9CA3AF); box-shadow: 0 4px 10px rgba(156, 163, 175, 0.4); font-size: 16px; width: 32px; height: 32px; border: 2px solid #fff; color: #374151; }
        .card-rank-3 { background: var(--r3-bg); border: 1px solid var(--r3-border); box-shadow: 0 10px 25px -5px var(--r3-shadow); }
        .card-rank-3 .rank-badge { background: linear-gradient(135deg, #fdba74, #c2410c); box-shadow: 0 4px 10px rgba(194, 65, 12, 0.3); font-size: 16px; width: 32px; height: 32px; border: 2px solid #fff; }
        .rank-other { background: var(--metric-bg); color: var(--text-secondary); font-size: 12px; border-radius: 8px; width: 24px; height: 24px; font-family: 'Manrope', sans-serif; font-weight: 800; }
        .card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; padding-bottom: 12px; border-bottom: 1px dashed var(--text-secondary); opacity: 0.9; }
        .symbol-wrap { display: flex; align-items: center; gap: 12px; }
        .rank-badge { display: flex; align-items: center; justify-content: center; border-radius: 10px; font-family: 'Oswald', sans-serif; font-weight: 700; color: #fff; }
        .symbol { font-size: 20px; font-weight: 800; color: var(--text-primary); letter-spacing: -0.5px; }
        .signal-badge { font-size: 11px; font-weight: 700; padding: 5px 12px; border-radius: 20px; text-transform: uppercase; letter-spacing: 0.5px; }
        .sig-cheap { background: var(--success-bg); color: var(--success-text); }
        .sig-expensive { background: var(--danger-bg); color: var(--danger-text); }
        .sig-fair { background: var(--warning-bg); color: var(--warning-text); }
        .metrics-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
        .metric-box { background: var(--metric-bg); padding: 10px; border-radius: 12px; border: 1px solid var(--metric-border); }
        .m-label { font-size: 10px; font-weight: 700; color: var(--text-secondary); text-transform: uppercase; margin-bottom: 2px; opacity: 0.8; }
        .m-curr { font-size: 16px; font-weight: 800; color: var(--text-primary); }
        .m-avg { font-size: 11px; color: var(--text-secondary); font-weight: 500; }
        .m-diff { font-size: 11px; font-weight: 700; margin-top: 2px; display: flex; align-items: center; gap: 3px; }
        .diff-good { color: var(--success-text); } .diff-bad { color: var(--danger-text); }
        .hidden-item { display: none; }
        .btn-expand { display: block; width: 100%; padding: 14px; background: var(--card-bg); color: var(--text-secondary); border: 1px solid var(--text-secondary); border-radius: 16px; font-size: 13px; font-weight: 700; margin-top: 24px; cursor: pointer; opacity: 0.6; }
        .algo-explain { margin-top: 40px; padding: 24px; background: var(--card-bg); border-radius: 20px; border: 1px solid var(--metric-border); }
        .algo-title { font-size: 15px; font-weight: 800; margin-bottom: 12px; color: var(--text-primary); }
        .algo-text { font-size: 13px; line-height: 1.7; color: var(--text-secondary); }
        .algo-formula { background: var(--metric-bg); padding: 10px 14px; border-radius: 10px; font-family: monospace; font-weight: 700; font-size: 12px; color: var(--text-primary); margin: 12px 0; display: block; border-left: 4px solid #3b82f6; }
    </style>
</head>
<body>
    <div class="header">
        <div><span class="header-title">Định Giá Cổ Phiếu</span><span class="pro-badge">PRO 👑</span></div>
        <div class="header-subtitle">Top cổ phiếu định giá Rẻ nhất so với Lịch sử</div>
        <div class="timestamp-badge">🕒 Cập nhật: {{ generated_time }}</div>
    </div>

    {% for item in items %}
    <div class="card {% if loop.index == 1 %}card-rank-1{% elif loop.index == 2 %}card-rank-2{% elif loop.index == 3 %}card-rank-3{% endif %} {% if loop.index > 10 %}hidden-item{% endif %}">
        <div class="card-header">
            <div class="symbol-wrap">
                <div class="rank-badge {% if loop.index > 3 %}rank-other{% endif %}">{{ loop.index }}</div>
                <div class="symbol">{{ item.symbol }}</div>
            </div>
            <div class="signal-badge {{ item.signal_class }}">{{ item.signal_text }}</div>
        </div>
        <div class="metrics-grid">
            <div class="metric-box">
                <div class="m-label">P/E Ratio</div>
                <div class="m-row"><span class="m-curr">{{ "%.1f"|format(item.pe_cur) }}x</span><span class="m-avg">TB: {{ "%.1f"|format(item.pe_avg) }}</span></div>
                <div class="m-diff {{ item.pe_class }}">{{ item.pe_diff_str }}</div>
            </div>
            <div class="metric-box">
                <div class="m-label">P/B Ratio</div>
                <div class="m-row"><span class="m-curr">{{ "%.1f"|format(item.pb_cur) }}x</span><span class="m-avg">TB: {{ "%.1f"|format(item.pb_avg) }}</span></div>
                <div class="m-diff {{ item.pb_class }}">{{ item.pb_diff_str }}</div>
            </div>
        </div>
    </div>
    {% endfor %}

    {% if items|length > 10 %}
    <button id="btnToggle" class="btn-expand" onclick="toggleExpand()">Xem thêm {{ items|length - 10 }} mã ↓</button>
    {% endif %}

    <div class="algo-explain">
        <div class="algo-title">ℹ️ Nguyên lý xếp hạng (Mean Reversion)</div>
        <div class="algo-text">
            Hệ thống tự động tìm kiếm các cổ phiếu đang có định giá <b>Rẻ hơn so với chính nó</b> trong quá khứ (trung bình 5 năm).
            <span class="algo-formula">Discount = (Giá trị hiện tại - Trung bình 5 năm) / Trung bình</span>
            Xếp hạng dựa trên <b>Trung bình cộng</b> của độ lệch P/E và P/B (Trọng số 50:50).
        </div>
    </div>

    <script>
        Telegram.WebApp.expand();
        function toggleExpand() {
            document.querySelectorAll('.hidden-item').forEach(i => { i.style.display = 'block'; i.style.animation = 'fadeIn 0.5s ease'; });
            document.getElementById('btnToggle').style.display = 'none';
        }
        function toggleTheme() {
            const html = document.documentElement;
            html.setAttribute('data-theme', html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
        }
        const style = document.createElement('style');
        style.innerHTML = `@keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }`;
        document.head.appendChild(style);
    </script>
</body>
</html>
"""

#---------------------------------

LOCKED_FEATURE_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Tính năng Pro</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-body: #F5F7FA;
            --text-primary: #111827;
            --text-secondary: #6B7280;
            --brand-gold: #D97706;
            --brand-gold-bg: #FFFBEB;
            --btn-gradient: linear-gradient(135deg, #007aff 0%, #af52de 100%);
        }
        
        /* --- SMOOTH LOADING --- */
        body { 
            font-family: 'Manrope', sans-serif; 
            background-color: var(--bg-body); 
            color: var(--text-primary);
            display: flex; flex-direction: column; align-items: center; justify-content: center; 
            height: 100vh; margin: 0; padding: 24px; text-align: center;
            visibility: hidden; opacity: 0; transition: opacity 0.3s ease-in-out;
        }
        body.loaded { visibility: visible; opacity: 1; }
        
        .lock-icon-wrapper { position: relative; margin-bottom: 24px; }
        .lock-icon { font-size: 64px; z-index: 2; position: relative; animation: float 3s ease-in-out infinite; }
        .blur-bg {
            position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
            width: 120px; height: 120px; background: rgba(217, 119, 6, 0.2);
            filter: blur(40px); z-index: 1; border-radius: 50%;
        }

        .pro-badge {
            background-color: var(--brand-gold-bg); color: var(--brand-gold);
            font-size: 11px; font-weight: 800; text-transform: uppercase;
            padding: 6px 12px; border-radius: 20px; margin-bottom: 16px;
            letter-spacing: 1px; display: inline-block;
        }

        .title { font-size: 24px; font-weight: 800; margin: 0 0 12px 0; line-height: 1.3; }
        .desc { font-size: 15px; color: var(--text-secondary); line-height: 1.6; margin-bottom: 40px; max-width: 320px; }

        .btn { 
            background: var(--btn-gradient); color: white; border: none; 
            padding: 16px 32px; border-radius: 16px; 
            font-weight: 700; font-size: 16px; cursor: pointer; 
            width: 100%; max-width: 280px; 
            box-shadow: 0 8px 25px rgba(0,122,255,0.25);
            transition: transform 0.1s;
        }
        .btn:active { transform: scale(0.98); opacity: 0.9; }
        
        @keyframes float { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-10px); } }
    </style>
</head>
<body>
    <div class="pro-badge">Tính Năng Cao Cấp</div>
    
    <div class="lock-icon-wrapper">
        <div class="blur-bg"></div>
        <div class="lock-icon">{{ icon }}</div>
    </div>
    
    <div class="title">{{ title }}</div>
    <div class="desc">{{ desc }}</div>
    
    <button class="btn" onclick="Telegram.WebApp.close()">🔥 Nâng cấp Pro ngay</button>
    
    <script>
        Telegram.WebApp.expand();
        window.addEventListener('load', function() {
            document.body.classList.add('loaded');
            Telegram.WebApp.ready();
        });
    </script>
</body>
</html>
"""

#----------------------------------

# digest_template.py

EOD_HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Tổng Kết Cuối Phiên</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: var(--tg-theme-bg-color, #f2f2f7);
            --text-color: var(--tg-theme-text-color, #000);
            --hint-color: var(--tg-theme-hint-color, #8e8e93);
            --card-bg: var(--tg-theme-secondary-bg-color, #ffffff);
            --up-color: #34c759; --down-color: #ff3b30; --ref-color: #ffcc00;
        }
        body { font-family: 'Inter', sans-serif; background-color: var(--bg-color); color: var(--text-color); margin: 0; padding: 20px 16px 40px 16px; font-size: 14px; line-height: 1.5; }
        
        .header { text-align: center; margin-bottom: 20px; }
        .header-title { font-size: 20px; font-weight: 800; margin-bottom: 4px; }
        .header-sub { font-size: 12px; color: var(--hint-color); }

        .ai-card { background: linear-gradient(135deg, #e0f2fe 0%, #f0f9ff 100%); border-left: 4px solid #007aff; border-radius: 16px; padding: 16px; margin-bottom: 20px; box-shadow: 0 4px 12px rgba(0,122,255,0.1); }
        .ai-title { font-size: 13px; font-weight: 700; color: #007aff; margin-bottom: 6px; text-transform: uppercase; }
        .ai-content { font-size: 14px; color: #334155; white-space: pre-line; }

        .market-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 20px; }
        .m-card { background-color: var(--card-bg); border-radius: 16px; padding: 12px; text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,0.04); cursor: pointer; transition: transform 0.1s; }
        .m-card:active { transform: scale(0.96); }
        .m-label { font-size: 11px; font-weight: 700; color: var(--hint-color); display: flex; align-items: center; justify-content: center; gap: 4px; }
        .m-val { font-size: 17px; font-weight: 800; margin: 4px 0; }
        .m-change { font-size: 12px; font-weight: 600; }
        .t-up { color: var(--up-color); } .t-down { color: var(--down-color); } .t-ref { color: var(--ref-color); }

        .section-card { background-color: var(--card-bg); border-radius: 16px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.04); padding: 0; }
        .p-item { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; border-bottom: 1px solid rgba(0,0,0,0.05); }
        .p-item:last-child { border-bottom: none; }
        .p-sym { font-size: 16px; font-weight: 700; }
        .p-market { font-size: 11px; color: var(--hint-color); }
        .p-right { text-align: right; display: flex; flex-direction: column; align-items: flex-end; }
        .p-price-row { display: flex; align-items: center; gap: 8px; }
        .p-price { font-size: 16px; font-weight: 600; }
        .p-badge { font-size: 12px; font-weight: 700; padding: 2px 6px; border-radius: 4px; min-width: 50px; text-align: center; }
        .val-row { font-size: 11px; color: var(--hint-color); margin-top: 2px; display: flex; align-items: center; gap: 4px; }
        .bg-up { background: rgba(52,199,89,0.15); color: var(--up-color); }
        .bg-down { background: rgba(255,59,48,0.15); color: var(--down-color); }
        .bg-ref { background: rgba(255,204,0,0.15); color: #d4a017; }

        .btn-chart { background: none; border: none; padding: 4px 0 4px 8px; cursor: pointer; font-size: 16px; opacity: 0.7; }

        /* MODAL */
        .modal-overlay {
            position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(0, 0, 0, 0.6); z-index: 1000;
            display: flex; align-items: center; justify-content: center;
            opacity: 0; visibility: hidden; transition: opacity 0.3s ease, visibility 0.3s ease;
            backdrop-filter: blur(3px);
        }
        .modal-overlay.active { opacity: 1; visibility: visible; }

        .modal-box {
            background: var(--card-bg); width: 90%; max-width: 400px; border-radius: 20px; padding: 20px 16px;
            box-shadow: 0 20px 40px rgba(0,0,0,0.3);
            transform: scale(0.95); transition: transform 0.3s cubic-bezier(0.175, 0.885, 0.32, 1.275);
        }
        .modal-overlay.active .modal-box { transform: scale(1); }

        .modal-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
        .modal-title { font-size: 17px; font-weight: 800; }
        .modal-close { background: rgba(0,0,0,0.05); border: none; width: 30px; height: 30px; border-radius: 50%; font-weight: bold; color: var(--hint-color); cursor: pointer; font-size: 16px; }
        
        .chart-container { width: 100%; height: 300px; border-radius: 12px; overflow: hidden; border: 1px solid rgba(0,0,0,0.05); }
        .chart-note { text-align: center; font-size: 11px; color: var(--hint-color); margin-top: 10px; }

        .footer-btn { display: block; width: 100%; padding: 14px; background-color: var(--text-color); color: var(--bg-color); border: none; border-radius: 14px; font-size: 15px; font-weight: 700; margin-top: 24px; cursor: pointer; }
    </style>
</head>
<body>
    <div class="header">
        <div class="header-title">Tổng Kết Phiên</div>
        <div class="header-sub">Dữ liệu chốt phiên {{ generated_at }}</div>
    </div>

    {% if market_data.ai_comment %}
    <div class="ai-card">
        <div class="ai-title">🧠 AI Insight</div>
        <div class="ai-content">{{ market_data.ai_comment }}</div>
    </div>
    {% endif %}

    <div class="market-grid">
        <div class="m-card" onclick="openModal('VNINDEX')">
            <div class="m-label">VN-INDEX 🔍</div>
            <div class="m-val {{ market_data.vnindex.cls }}">{{ market_data.vnindex.price }}</div>
            <div class="m-change {{ market_data.vnindex.cls }}">{{ market_data.vnindex.change_str }}</div>
        </div>
        <div class="m-card" onclick="openModal('VN30')">
            <div class="m-label">VN30 🔍</div>
            <div class="m-val {{ market_data.vn30.cls }}">{{ market_data.vn30.price }}</div>
            <div class="m-change {{ market_data.vn30.cls }}">{{ market_data.vn30.change_str }}</div>
        </div>
    </div>

    {% if market_data.vnindex.chart_html %}
    <div id="modal-VNINDEX" class="modal-overlay" onclick="closeModal(event, 'VNINDEX')">
        <div class="modal-box">
            <div class="modal-header"><div class="modal-title">VN-INDEX</div><button class="modal-close" onclick="closeModalById('VNINDEX')">✕</button></div>
            <div class="chart-container">{{ market_data.vnindex.chart_html | safe }}</div>
            <div class="chart-note">Biến động 6 tháng</div>
        </div>
    </div>
    {% endif %}
    {% if market_data.vn30.chart_html %}
    <div id="modal-VN30" class="modal-overlay" onclick="closeModal(event, 'VN30')">
        <div class="modal-box">
            <div class="modal-header"><div class="modal-title">VN30</div><button class="modal-close" onclick="closeModalById('VN30')">✕</button></div>
            <div class="chart-container">{{ market_data.vn30.chart_html | safe }}</div>
            <div class="chart-note">Biến động 6 tháng</div>
        </div>
    </div>
    {% endif %}

    <div class="section-card">
        {% for s in user_stocks %}
        <div class="p-item">
            <div class="p-left">
                <div class="p-sym">{{ s.symbol }}</div>
                <div class="p-market">HOSE</div>
            </div>
            <div class="p-right">
                <div class="p-price-row">
                    <span class="p-price {{ s.text_cls }}">{{ s.price }}</span>
                    <span class="p-badge {{ s.bg_cls }}">{{ s.pct }}%</span>
                    {% if s.chart_html %}<button class="btn-chart" onclick="openModal('{{ s.symbol }}')">📉</button>{% endif %}
                </div>
                <div class="val-row">Vol: {{ s.vol_str }} • 💰 {{ s.val_str }}</div>
            </div>
        </div>
        
        {% if s.chart_html %}
        <div id="modal-{{ s.symbol }}" class="modal-overlay" onclick="closeModal(event, '{{ s.symbol }}')">
            <div class="modal-box">
                <div class="modal-header"><div class="modal-title">{{ s.symbol }}</div><button class="modal-close" onclick="closeModalById('{{ s.symbol }}')">✕</button></div>
                <div class="chart-container">{{ s.chart_html | safe }}</div>
                <div class="chart-note">Giá: {{ s.price }} | Vol: {{ s.vol_str }}</div>
            </div>
        </div>
        {% endif %}
        {% endfor %}
    </div>

    <button class="footer-btn" onclick="Telegram.WebApp.close()">Đóng</button>

    <script>
        Telegram.WebApp.expand();
        window.addEventListener('load', function() { document.body.classList.add('loaded'); });

        function openModal(id) {
            const modal = document.getElementById('modal-' + id);
            if (modal) {
                modal.classList.add('active');
                
                // 🔥 FIX CHÍNH: Trigger resize cho Plotly khi modal hiện ra
                // Delay 50ms để đảm bảo CSS transition đã bắt đầu và container có kích thước
                setTimeout(() => {
                    const chartDiv = modal.querySelector('.plotly-graph-div');
                    if (chartDiv && window.Plotly) {
                        Plotly.Plots.resize(chartDiv);
                    }
                }, 50);
            }
        }

        function closeModal(event, id) {
            if (event.target === event.currentTarget) closeModalById(id);
        }
        
        function closeModalById(id) {
            document.getElementById('modal-' + id).classList.remove('active');
        }
    </script>
</body>
</html>
"""

EOD_404_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Link hết hạn</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        body { font-family: sans-serif; display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100vh; margin: 0; text-align: center; color: #555; }
        h2 { margin-bottom: 10px; }
        p { font-size: 14px; max-width: 300px; line-height: 1.5; }
        button { margin-top: 20px; padding: 10px 25px; border: none; background: #007aff; color: white; border-radius: 8px; font-weight: bold; cursor: pointer; }
    </style>
</head>
<body>
    <div style="font-size: 50px; margin-bottom: 20px;">🌙</div>
    <h2>Bản tin đã cũ</h2>
    <p>Bản tin cuối ngày chỉ có giá trị trong ngày giao dịch.</p>
    <button onclick="Telegram.WebApp.close()">Đóng</button>
    <script>Telegram.WebApp.ready(); Telegram.WebApp.expand();</script>
</body>
</html>
"""


#------------------------------------

FLASH_VIEW_HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Flash View {{ symbol }}</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-body: #f8f9fa; --card-bg: #ffffff; 
            --text-primary: #111827; --text-secondary: #6b7280;
            --up-color: #089981; --down-color: #f23645; --ref-color: #f0b90b;
            --accent-blue: #2962ff;
            --radius: 16px; --shadow: 0 4px 24px rgba(0,0,0,0.06);
        }
        body { margin: 0; padding: 16px; font-family: 'Manrope', sans-serif; background-color: var(--bg-body); color: var(--text-primary); -webkit-font-smoothing: antialiased; }
        
        .header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 16px; }
        .sym-name { font-size: 28px; font-weight: 800; margin: 0; line-height: 1; }
        .sym-sub { font-size: 12px; color: var(--text-secondary); font-weight: 600; margin-top: 4px; }
        .price-main { font-size: 28px; font-weight: 800; display: block; line-height: 1; }
        .price-change { font-size: 13px; font-weight: 700; padding: 4px 10px; border-radius: 8px; display: inline-flex; margin-top: 6px; }
        .btn-refresh { background: rgba(41, 98, 255, 0.1); border: none; cursor: pointer; color: var(--accent-blue); font-size: 14px; font-weight: 700; display: flex; align-items: center; gap: 4px; padding: 6px 12px; border-radius: 20px; }
        .btn-refresh:active { transform: scale(0.95); }

        .t-up { color: var(--up-color); } .t-down { color: var(--down-color); }
        .bg-up { background: rgba(8, 153, 129, 0.12); color: var(--up-color); }
        .bg-down { background: rgba(242, 54, 69, 0.12); color: var(--down-color); }

        .card { background: var(--card-bg); border-radius: var(--radius); box-shadow: var(--shadow); margin-bottom: 16px; overflow: hidden; padding: 16px; border: 1px solid rgba(0,0,0,0.03); }
        
        .chart-wrapper { height: 300px; width: 100%; margin-left: -5px; margin-right: -5px; }

        .flow-bar-wrapper { display: flex; height: 8px; width: 100%; border-radius: 4px; overflow: hidden; background: #f0f3fa; margin-bottom: 8px; }
        .fb-buy { background-color: var(--up-color); height: 100%; } .fb-sell { background-color: var(--down-color); height: 100%; }
        .flow-labels { display: flex; justify-content: space-between; font-size: 13px; font-weight: 700; margin-top: 8px; }
        .lbl-buy { color: var(--up-color); } .lbl-sell { color: var(--down-color); }
        .flow-sub { display: flex; justify-content: space-between; font-size: 11px; color: var(--text-secondary); font-weight: 500; }

        .range-track { height: 4px; background: #e0e3eb; border-radius: 2px; position: relative; margin: 14px 0; }
        .range-fill { position: absolute; height: 100%; border-radius: 2px; background: linear-gradient(90deg, var(--down-color), var(--up-color)); opacity: 0.3; width: 100%; }
        .range-thumb { position: absolute; top: -6px; width: 16px; height: 16px; background: #fff; border: 3px solid var(--text-primary); border-radius: 50%; transform: translateX(-50%); box-shadow: 0 2px 5px rgba(0,0,0,0.15); }
        .range-meta { display: flex; justify-content: space-between; font-size: 12px; font-weight: 600; color: var(--text-primary); }
        .range-lbl { font-size: 10px; color: var(--text-secondary); font-weight: 500; text-transform: uppercase; }

        .metrics-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 20px; }
        .m-box { background: var(--card-bg); padding: 14px; border-radius: var(--radius); text-align: center; box-shadow: var(--shadow); }
        .m-lbl { font-size: 11px; font-weight: 700; color: var(--text-secondary); text-transform: uppercase; }
        .m-val { font-size: 18px; font-weight: 800; margin-top: 6px; color: var(--text-primary); letter-spacing: -0.5px; }
        .rsi-status { font-size: 11px; font-weight: 600; margin-top: 4px; }

        .btn-close { width: 100%; padding: 16px; background: var(--text-primary); color: #fff; border: none; border-radius: var(--radius); font-weight: 700; font-size: 15px; cursor: pointer; box-shadow: 0 4px 12px rgba(0,0,0,0.1); }
    </style>
</head>
<body>
    <div class="header">
        <div class="sym-box"><h1 class="sym-name">{{ symbol }}</h1><div style="display:flex;align-items:center;gap:8px;margin-top:4px;"><span class="sym-sub">HOSE • Intraday</span><button class="btn-refresh" onclick="window.location.reload()">🔄 Làm mới</button></div></div>
        <div style="text-align:right"><div class="price-main {{ cls_color }}">{{ current_price }}</div><div class="price-change {{ bg_cls }}">{{ change_str }}</div></div>
    </div>

    <div class="card" style="padding: 10px 0 0 0;">
        <div class="chart-wrapper">{{ chart_html | safe }}</div>
    </div>

    <div class="card">
        <div style="font-size:12px;font-weight:700;text-transform:uppercase;color:var(--text-secondary);margin-bottom:12px;">⚡ Phân bổ dòng tiền (Chủ động)</div>
        <div class="flow-bar-wrapper"><div class="fb-buy" style="width: {{ buy_pct }}%"></div><div class="fb-sell" style="width: {{ sell_pct }}%"></div></div>
        <div class="flow-labels"><span class="lbl-buy">{{ buy_pct }}% Mua</span><span class="lbl-sell">{{ sell_pct }}% Bán</span></div>
        <div class="flow-sub"><span>{{ buy_vol_str }}</span><span>{{ sell_vol_str }}</span></div>
        <div style="height: 220px; margin-top: 20px;">{{ orderbook_html | safe }}</div>
    </div>

    <div class="card">
        <div style="font-size:12px;font-weight:700;text-transform:uppercase;color:var(--text-secondary);margin-bottom:12px;">🎯 Vị thế giá</div>
        <div class="range-meta"><span>Thấp: {{ low }}</span><span>Cao: {{ high }}</span></div>
        <div class="range-track"><div class="range-fill"></div><div class="range-thumb" style="left: {{ range_pct }}%;"></div></div>
    </div>

    <div class="metrics-grid">
        <div class="m-box"><div class="m-lbl">RSI (5M)</div><div class="m-val" style="color:{{ rsi_color }}">{{ rsi_val }}</div><div class="rsi-status" style="color:{{ rsi_color }}">{{ rsi_msg }}</div></div>
        <div class="m-box"><div class="m-lbl">Tổng KL</div><div class="m-val">{{ volume_str }}</div><div class="rsi-status" style="color:var(--text-secondary)">Cổ phiếu</div></div>
    </div>

    <button class="btn-close" onclick="Telegram.WebApp.close()">Đóng</button>
    <script>Telegram.WebApp.expand();</script>
</body>
</html>
"""



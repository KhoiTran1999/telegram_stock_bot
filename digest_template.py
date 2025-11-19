
DIGEST_HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bản Tin Sáng StockBot</title>
    <style>
        :root {
            --bg-color: #f5f5f7;
            --card-bg: #ffffff;
            --text-primary: #1c1c1e;
            --text-secondary: #8e8e93;
            --accent-color: #007aff;
            --danger-color: #ff3b30;
            --success-color: #34c759;
            --border-color: #e5e5ea;
            --font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }

        /* Dark Mode Support cho Telegram */
        @media (prefers-color-scheme: dark) {
            :root {
                --bg-color: #000000;
                --card-bg: #1c1c1e;
                --text-primary: #ffffff;
                --text-secondary: #98989d;
                --border-color: #38383a;
            }
        }

        body {
            background-color: var(--bg-color);
            color: var(--text-primary);
            font-family: var(--font-family);
            margin: 0;
            padding: 16px;
            line-height: 1.5;
            font-size: 14px;
        }

        .header {
            margin-bottom: 20px;
            text-align: center;
        }
        .header h1 {
            margin: 0;
            font-size: 22px;
            font-weight: 700;
        }
        .header p {
            margin: 4px 0 0;
            color: var(--text-secondary);
            font-size: 13px;
        }

        .card {
            background-color: var(--card-bg);
            border-radius: 12px;
            padding: 16px;
            margin-bottom: 16px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.04);
        }

        .section-title {
            font-size: 16px;
            font-weight: 600;
            margin-bottom: 12px;
            display: flex;
            align-items: center;
            color: var(--text-primary);
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 8px;
        }
        .section-title span { margin-right: 6px; }

        /* LIST STYLE (News & Reports) */
        .list-item {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            padding: 10px 0;
            border-bottom: 1px solid var(--border-color);
        }
        .list-item:last-child { border-bottom: none; }
        
        .item-content {
            flex: 1;
            margin-right: 12px;
        }
        
        .item-title {
            text-decoration: none;
            color: var(--text-primary);
            font-weight: 500;
            display: block;
            margin-bottom: 4px;
        }
        .item-title:hover { color: var(--accent-color); }

        .item-meta {
            font-size: 11px;
            color: var(--text-secondary);
            display: flex;
            align-items: center;
            white-space: nowrap;
            flex-shrink: 0;
        }

        .stock-badge {
            background-color: var(--accent-color);
            color: white;
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 10px;
            font-weight: 700;
            margin-right: 6px;
            display: inline-block;
            vertical-align: middle;
        }

        /* TABLE STYLE (Value Stocks) */
        .table-wrapper { overflow-x: auto; }
        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
        }
        th {
            text-align: left;
            color: var(--text-secondary);
            font-weight: 500;
            font-size: 11px;
            padding-bottom: 8px;
            border-bottom: 1px solid var(--border-color);
        }
        td {
            padding: 8px 0;
            border-bottom: 1px solid var(--border-color);
        }
        tr:last-child td { border-bottom: none; }
        
        .score-high { color: var(--success-color); font-weight: bold; }

        .empty-state {
            text-align: center;
            color: var(--text-secondary);
            font-style: italic;
            padding: 10px 0;
        }

        .footer {
            text-align: center;
            margin-top: 30px;
            font-size: 11px;
            color: var(--text-secondary);
        }
    </style>
</head>
<body>

    <div class="header">
        <h1>🌅 Bản Tin Sáng</h1>
        <p>{{ date_str }}</p>
    </div>

    {% if data.is_pro and data.value_stocks %}
    <div class="card">
        <div class="section-title">💎 Top Cổ Phiếu Giá Trị (Hôm nay)</div>
        <div class="table-wrapper">
            <table>
                <thead>
                    <tr>
                        <th>Mã</th>
                        <th>Ngành</th>
                        <th>P/E</th>
                        <th>ROE</th>
                        <th>Score</th>
                    </tr>
                </thead>
                <tbody>
                    {% for stock in data.value_stocks %}
                    <tr>
                        <td><span class="stock-badge">{{ stock.symbol }}</span></td>
                        <td style="max-width: 100px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">{{ stock.industry }}</td>
                        <td>{{ stock.pe }}</td>
                        <td>{{ stock.roe }}%</td>
                        <td class="score-high">{{ stock.score }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>
    {% endif %}

    {% if data.bctc %}
    <div class="card">
        <div class="section-title">📊 BCTC Mới Công Bố</div>
        {% for item in data.bctc %}
        <div class="list-item">
            <div class="item-content">
                <div>
                    <span class="stock-badge">{{ item.symbol }}</span>
                    <span>Báo cáo Quý {{ item.quarter }}/{{ item.year }}</span>
                </div>
            </div>
            <div class="item-meta">{{ item.time }}</div>
        </div>
        {% endfor %}
    </div>
    {% endif %}

    {% if data.reports %}
    <div class="card">
        <div class="section-title">📑 Báo Cáo Phân Tích Mới</div>
        {% for item in data.reports %}
        <div class="list-item">
            <div class="item-content">
                <a href="{{ item.link }}" target="_blank" class="item-title">
                    <span class="stock-badge">{{ item.symbol }}</span>
                    {{ item.title }}
                </a>
            </div>
            {% if item.time %}
            <div class="item-meta">{{ item.time }}</div>
            {% endif %}
        </div>
        {% endfor %}
    </div>
    {% endif %}

    {% if data.specialized %}
    <div class="card">
        <div class="section-title">💡 Góc Nhìn Chuyên Gia (Theo Danh Mục)</div>
        {% for item in data.specialized %}
        <div class="list-item">
            <div class="item-content">
                <a href="{{ item.link }}" target="_blank" class="item-title">{{ item.title }}</a>
            </div>
            {% if item.time %}
            <div class="item-meta">{{ item.time }}</div>
            {% endif %}
        </div>
        {% endfor %}
    </div>
    {% endif %}

    {% if data.macro %}
    <div class="card">
        <div class="section-title">🌍 Tin Vĩ Mô Nổi Bật</div>
        {% for item in data.macro %}
        <div class="list-item">
            <div class="item-content">
                <a href="{{ item.link }}" target="_blank" class="item-title">{{ item.title }}</a>
            </div>
        </div>
        {% endfor %}
    </div>
    {% endif %}

    {% if not data.value_stocks and not data.bctc and not data.reports and not data.specialized and not data.macro %}
    <div class="empty-state">
        Hôm nay thị trường khá yên ắng, chưa có tin tức nổi bật liên quan đến danh mục của bạn.
    </div>
    {% endif %}

    <div class="footer">
        Tổng hợp tự động bởi KT StockBot 🤖
    </div>

</body>
</html>
"""

DIGEST_404_TEMPLATE = """
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
PROFILE_HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Hồ sơ doanh nghiệp {{ symbol }}</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: var(--tg-theme-bg-color, #f2f2f7);
            --text-color: var(--tg-theme-text-color, #000);
            --hint-color: var(--tg-theme-hint-color, #8e8e93);
            --card-bg: var(--tg-theme-secondary-bg-color, #ffffff);
            --border-color: rgba(0, 0, 0, 0.06);
            --accent: var(--tg-theme-button-color, #007aff);
        }
        * { box-sizing: border-box; }
        
        body {
            margin: 0; padding: 16px;
            font-family: 'Inter', sans-serif;
            background-color: var(--bg-color);
            color: var(--text-color);
            -webkit-font-smoothing: antialiased;
        }
        
        .page { max-width: 720px; margin: 0 auto; }
        
        /* Header Styles */
        .header { margin-bottom: 12px; }
        
        .chip-row {
            display: flex; align-items: center; justify-content: space-between;
            gap: 8px; margin-bottom: 8px;
        }
        .chip {
            display: inline-flex; align-items: center; gap: 6px;
            padding: 4px 10px; border-radius: 20px;
            background: rgba(0,0,0,0.05);
            font-size: 11px; color: var(--hint-color);
            font-weight: 600; text-transform: uppercase;
        }
        
        /* PRO Badge Style */
        .pro-badge {
            display: inline-flex; align-items: center; gap: 4px;
            padding: 3px 8px; border-radius: 8px;
            background: linear-gradient(135deg, #FFD700, #FFA500);
            color: white; font-size: 10px; font-weight: 800;
            box-shadow: 0 3px 8px rgba(255,165,0,0.3);
            text-transform: uppercase; letter-spacing: 0.5px;
            text-shadow: 0 1px 1px rgba(0,0,0,0.1);
        }

        .symbol { display: flex; align-items: baseline; gap: 8px; margin-top: 4px; }
        .symbol-main { font-size: 28px; font-weight: 800; letter-spacing: -0.5px; color: var(--accent); }
        .symbol-sub { font-size: 13px; color: var(--hint-color); font-weight: 500; }
        
        .meta { margin-top: 4px; font-size: 11px; color: var(--hint-color); }

        /* Table of Contents (Chips) */
        .toc {
            margin-top: 16px; display: flex; gap: 8px;
            overflow-x: auto; padding-bottom: 8px;
            -webkit-overflow-scrolling: touch; scrollbar-width: none;
        }
        .toc::-webkit-scrollbar { display: none; }
        
        .toc-chip {
            flex: 0 0 auto; display: inline-flex; align-items: center; gap: 6px;
            padding: 8px 12px; border-radius: 20px;
            background: var(--card-bg); border: 1px solid rgba(0,0,0,0.08);
            font-size: 12px; font-weight: 600; color: var(--text-color);
            cursor: pointer; transition: all 0.2s ease;
            box-shadow: 0 2px 4px rgba(0,0,0,0.02);
        }
        .toc-chip:active { transform: scale(0.96); opacity: 0.8; }

        /* Content Card */
        .card {
            background-color: var(--card-bg);
            border-radius: 16px;
            padding: 16px; margin-top: 16px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.03);
            border: 1px solid rgba(0,0,0,0.03);
            scroll-margin-top: 16px; /* Để khi scroll tới không bị che */
        }
        
        .card-title-row {
            display: flex; align-items: center; gap: 8px;
            margin-bottom: 12px; padding-bottom: 8px;
            border-bottom: 1px solid rgba(0,0,0,0.05);
        }
        .card-title-icon { font-size: 18px; }
        .card-title { font-size: 15px; font-weight: 700; text-transform: uppercase; color: var(--text-color); letter-spacing: 0.5px; }

        /* QUAN TRỌNG: Xử lý hiển thị văn bản JSON */
        .profile-text {
            font-size: 14px;
            line-height: 1.6;
            color: var(--text-color);
            white-space: pre-line; /* Tự động xuống dòng khi gặp \n */
            font-weight: 400;
        }

        /* Footer */
        .meta-footer { margin-top: 24px; font-size: 11px; color: var(--hint-color); text-align: center; line-height: 1.5; }
        
        .footer-btn-container { margin-top: 20px; display: flex; justify-content: center; padding-bottom: 30px; }
        .close-btn {
            padding: 12px 32px; border-radius: 12px;
            background: var(--card-bg); border: none;
            color: var(--text-color); font-size: 14px; font-weight: 600;
            cursor: pointer; box-shadow: 0 4px 10px rgba(0,0,0,0.05);
        }
        .close-btn:active { transform: scale(0.98); }

        /* Animation */
        .card { animation: fadeInUp 0.4s ease-out; animation-fill-mode: backwards; }
        .card:nth-child(1) { animation-delay: 0.05s; }
        .card:nth-child(2) { animation-delay: 0.1s; }
        .card:nth-child(3) { animation-delay: 0.15s; }
        
        @keyframes fadeInUp {
            from { opacity: 0; transform: translateY(10px); }
            to { opacity: 1; transform: translateY(0); }
        }
    </style>
</head>
<body>
    <div class="page">
        <div class="header">
            <div class="chip-row">
                <div class="chip">Hồ sơ doanh nghiệp</div>
                {% if is_pro %}
                <div class="pro-badge">PRO 👑</div>
                {% endif %}
            </div>
            
            <div class="symbol">
                <div class="symbol-main">{{ symbol }}</div>
                <div class="symbol-sub">Hồ sơ chi tiết</div>
            </div>
            
            {% if generated_at %}
            <div class="meta">Cập nhật lúc: {{ generated_at }}</div>
            {% endif %}

            {% if sections %}
            <div class="toc">
                {% for sec in sections %}
                <div class="toc-chip" onclick="scrollToId('{{ sec.id }}')">
                    <span>{{ sec.icon }} {{ sec.title }}</span>
                </div>
                {% endfor %}
            </div>
            {% endif %}
        </div>

        {% for sec in sections %}
        <div class="card" id="{{ sec.id }}">
            <div class="card-title-row">
                <div class="card-title-icon">{{ sec.icon }}</div>
                <div class="card-title">{{ sec.title }}</div>
            </div>
            <div class="profile-text">
                {{ sec.body }}
            </div>
        </div>
        {% endfor %}

        <div class="meta-footer">
            <div>Nguồn: Dữ liệu thị trường & BCTC, tổng hợp bởi Gemini AI.</div>
            {% if report_code %}
            <div>Ref ID: {{ report_code }}</div>
            {% endif %}
        </div>

        <div class="footer-btn-container">
            <button class="close-btn" onclick="Telegram.WebApp.close()">Đóng Hồ Sơ</button>
        </div>
    </div>

    <script>
        Telegram.WebApp.ready();
        Telegram.WebApp.expand();

        function scrollToId(id) {
            const el = document.getElementById(id);
            if (el) {
                el.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        }
    </script>
</body>
</html>
"""

PROFILE_404_TEMPLATE = """
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


REPORT_HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Báo cáo Danh mục</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
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
        
        body { font-family: 'Inter', sans-serif; background-color: var(--bg-color); color: var(--text-color); margin: 0; padding: 16px; -webkit-font-smoothing: antialiased; }
        
        /* Header Title & Badge */
        .header-row { text-align: center; margin-bottom: 20px; animation: fadeInDown 0.5s ease; }
        .header-title { font-size: 20px; font-weight: 800; margin: 0; display: inline-flex; align-items: center; gap: 6px; color: var(--text-color); }
        
        /* PRO BADGE STYLE (Giống Digest) */
        .pro-badge { 
            background: linear-gradient(135deg, #FFD700 0%, #FFA500 100%); 
            color: white; 
            font-size: 10px; 
            font-weight: 800; 
            padding: 3px 8px; 
            border-radius: 8px; 
            letter-spacing: 0.5px; 
            box-shadow: 0 3px 8px rgba(255, 165, 0, 0.3); 
            text-shadow: 0 1px 1px rgba(0,0,0,0.1); 
            text-transform: uppercase;
            transform: translateY(-1px);
        }

        .header-time { font-size: 12px; color: var(--hint-color); margin-top: 4px; font-weight: 500; }

        /* Header Score */
        .score-card {
            background: linear-gradient(135deg, #007aff, #5856d6);
            color: white;
            border-radius: 20px;
            padding: 24px;
            text-align: center;
            margin-bottom: 20px;
            box-shadow: 0 8px 20px rgba(0,122,255,0.25);
            position: relative; overflow: hidden;
        }
        .score-card::before { content: ''; position: absolute; top: -50%; left: -50%; width: 200%; height: 200%; background: radial-gradient(circle, rgba(255,255,255,0.2) 0%, transparent 60%); pointer-events: none; }
        
        .score-val { font-size: 48px; font-weight: 800; line-height: 1; letter-spacing: -2px; }
        .score-label { font-size: 14px; font-weight: 500; opacity: 0.9; margin-top: 4px; text-transform: uppercase; letter-spacing: 0.5px; }
        .score-sub { font-size: 12px; opacity: 0.8; margin-top: 8px; }

        /* Market Comment */
        .market-card {
            background-color: var(--card-bg);
            border-radius: 16px;
            padding: 16px;
            margin-bottom: 24px;
            border-left: 4px solid var(--accent-color);
            box-shadow: 0 2px 8px rgba(0,0,0,0.04);
        }
        .market-title { font-size: 12px; font-weight: 700; text-transform: uppercase; color: var(--hint-color); margin-bottom: 8px; letter-spacing: 0.5px; }
        .market-text { font-size: 14px; line-height: 1.6; font-weight: 400; }

        /* Stock List */
        .stock-card {
            background-color: var(--card-bg);
            border-radius: 16px;
            padding: 16px;
            margin-bottom: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.04);
            transition: transform 0.1s;
        }
        .stock-card:active { transform: scale(0.98); }
        
        .st-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
        .st-symbol { font-size: 18px; font-weight: 800; color: var(--text-color); }
        .st-industry { font-size: 12px; color: var(--hint-color); font-weight: 500; margin-left: 6px; }
        
        .st-badge {
            font-size: 11px; font-weight: 700; padding: 4px 10px; border-radius: 20px; text-transform: uppercase;
        }
        /* Badge logic Colors */
        .act-buy { background-color: var(--success-bg); color: var(--success-text); }
        .act-hold { background-color: var(--warning-bg); color: var(--warning-text); }
        .act-sell { background-color: var(--danger-bg); color: var(--danger-text); }
        .act-neutral { background-color: var(--bg-color); color: var(--hint-color); }

        .st-analysis { 
            font-size: 14px; 
            line-height: 1.6;
            margin-bottom: 12px; 
            color: var(--text-color);
            white-space: pre-line;
        }
        
        .st-metrics {
            background-color: var(--bg-color);
            border-radius: 10px;
            padding: 10px;
            font-size: 12px;
            color: var(--hint-color);
            display: flex; align-items: center; gap: 6px;
        }
        .st-metrics-icon { font-size: 14px; }

        .footer { text-align: center; margin-top: 30px; font-size: 12px; color: var(--hint-color); padding-bottom: 40px; }
        
        .btn-close {
            display: block; width: 100%; padding: 14px; 
            background-color: var(--card-bg); color: var(--text-color); 
            border: none; border-radius: 14px; 
            font-size: 15px; font-weight: 600; margin-top: 20px; 
            cursor: pointer; box-shadow: 0 2px 8px rgba(0,0,0,0.05);
        }

        @keyframes fadeInDown { from { opacity: 0; transform: translateY(-10px); } to { opacity: 1; transform: translateY(0); } }
    </style>
</head>
<body>
    <div class="header-row">
        <div class="header-title">
            Báo Cáo Danh Mục
            {% if is_pro %}
            <span class="pro-badge">PRO 👑</span>
            {% endif %}
        </div>
        <div class="header-time">Cập nhật lúc: {{ generated_at }}</div>
    </div>

    <div class="score-card">
        <div class="score-val">{{ data.portfolio_health_score }}</div>
        <div class="score-label">Điểm Sức Khỏe Danh Mục</div>
        <div class="score-sub">Đánh giá dựa trên tiềm năng tăng trưởng</div>
    </div>

    <div class="market-card">
        <div class="market-title">NHẬN ĐỊNH THỊ TRƯỜNG & CHIẾN LƯỢC</div>
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
            {% if 'mua' in act or 'tăng' in act %}
                {% set badge_class = 'act-buy' %}
            {% elif 'giữ' in act or 'nắm' in act %}
                {% set badge_class = 'act-hold' %}
            {% elif 'bán' in act or 'hạ' in act or 'giảm' in act %}
                {% set badge_class = 'act-sell' %}
            {% endif %}
            
            <div class="st-badge {{ badge_class }}">{{ stock.action }}</div>
        </div>
        
        <div class="st-analysis">
            {{ stock.analysis }}
        </div>
        
        {% if stock.key_metrics %}
        <div class="st-metrics">
            <span class="st-metrics-icon">📊</span> 
            <span><b>Key Metrics:</b> {{ stock.key_metrics }}</span>
        </div>
        {% endif %}
    </div>
    {% endfor %}

    <div class="footer">
        Dữ liệu được phân tích tự động bởi AI (Gemini).<br>
        Không phải khuyến nghị đầu tư tài chính.
    </div>

    <button class="btn-close" onclick="Telegram.WebApp.close()">Đóng Báo Cáo</button>

    <script>
        Telegram.WebApp.ready();
        Telegram.WebApp.expand();
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
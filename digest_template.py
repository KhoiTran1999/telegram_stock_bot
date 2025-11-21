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
            font-family: 'Inter', sans-serif; 
            background-color: var(--bg-color); 
            color: var(--text-color); 
            margin: 0; padding: 20px 16px 40px 16px; 
            font-size: 14px; line-height: 1.5;
            visibility: hidden; opacity: 0; transition: opacity 0.3s ease-in-out;
        }
        body.loaded { visibility: visible; opacity: 1; }

        /* CSS Cũ giữ nguyên (rút gọn để tập trung vào phần mới) */
        .header { text-align: center; margin-bottom: 32px; }
        .date-badge { display: inline-flex; align-items: center; gap: 6px; background-color: var(--card-bg); padding: 6px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; color: var(--hint-color); box-shadow: 0 2px 6px rgba(0,0,0,0.03); margin-bottom: 12px; }
        .header-title { font-size: 32px; font-weight: 800; margin: 0; background: linear-gradient(135deg, #007aff 0%, #af52de 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .pro-badge { background: linear-gradient(135deg, #FFD700 0%, #FFA500 100%); color: white; font-size: 11px; font-weight: 800; padding: 4px 8px; border-radius: 8px; display: inline-flex; transform: translateY(-2px); }
        
        .section-card { background-color: var(--card-bg); border-radius: 16px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.04); overflow: hidden; }
        .card-header { padding: 16px 16px 10px 16px; display: flex; align-items: center; gap: 10px; border-bottom: 1px solid rgba(0,0,0,0.05); }
        .card-icon { width: 28px; height: 28px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 16px; background: rgba(0,122,255,0.1); color: #007aff; }
        .card-title { font-size: 17px; font-weight: 700; }
        
        /* --- SỬA ĐỔI: List Item giờ là div có cursor pointer --- */
        .list-item { padding: 14px 16px; border-bottom: 1px solid rgba(0,0,0,0.05); display: block; text-decoration: none; color: inherit; cursor: pointer; }
        .list-item:active { background-color: rgba(0,0,0,0.05); }
        
        .list-item:last-child { border-bottom: none; }
        .item-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
        .badge { background: rgba(0,0,0,0.05); font-size: 11px; font-weight: 700; padding: 4px 8px; border-radius: 6px; }
        .item-title { font-size: 15px; font-weight: 500; line-height: 1.4; }
        .item-meta { font-size: 12px; color: var(--hint-color); margin-top: 4px; }

        /* ... (Giữ nguyên CSS Locked, Table, Utilities cũ) ... */
        .list-item.locked { position: relative; background: repeating-linear-gradient(45deg, var(--card-bg), var(--card-bg) 10px, #f9f9f9 10px, #f9f9f9 20px); }
        .blur-content { filter: blur(4px); opacity: 0.6; user-select: none; pointer-events: none; }
        .lock-overlay { position: absolute; top: 0; left: 0; width: 100%; height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; background: rgba(255, 255, 255, 0.6); z-index: 2; }
        .lock-btn { background: var(--text-color); color: var(--bg-color); border: none; padding: 6px 16px; border-radius: 20px; font-size: 12px; font-weight: 700; cursor: pointer; box-shadow: 0 4px 10px rgba(0,0,0,0.1); display: flex; align-items: center; gap: 4px; transform: translateY(2px); }
        .stock-table { width: 100%; border-collapse: collapse; font-size: 13px; }
        .stock-table th { text-align: left; padding: 10px 16px; color: var(--hint-color); font-weight: 600; font-size: 11px; }
        .stock-table td { padding: 12px 16px; border-bottom: 1px solid rgba(0,0,0,0.05); }
        .score-badge { background: #5856d6; color: white; padding: 4px 8px; border-radius: 8px; font-weight: 700; font-size: 12px; }
        .premium-card { background: linear-gradient(135deg, #007aff 0%, #af52de 100%); border-radius: 24px; padding: 24px; color: white; text-align: center; margin-top: 32px; }
        .premium-btn { display: block; width: 100%; padding: 15px; background-color: #fff; color: #007aff; border: none; border-radius: 14px; font-size: 16px; font-weight: 700; cursor: pointer; margin-top: 16px; }
        .hidden-item { display: none; }
        .action-area { padding: 10px; text-align: center; border-top: 1px solid rgba(0,0,0,0.05); }
        .btn-toggle { background: none; border: none; color: var(--accent-color); font-size: 13px; font-weight: 600; cursor: pointer; padding: 6px 12px; display: inline-flex; align-items: center; gap: 4px; }

        /* --- CSS MỚI CHO NEWS READER (MODAL) --- */
        .news-modal {
            position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background: #fff; z-index: 9999;
            display: flex; flex-direction: column;
            transform: translateY(100%); transition: transform 0.3s cubic-bezier(0.16, 1, 0.3, 1);
        }
        .news-modal.active { transform: translateY(0); }
        
        .modal-header {
            padding: 12px 16px; display: flex; justify-content: space-between; align-items: center;
            background: rgba(255,255,255,0.95); border-bottom: 1px solid rgba(0,0,0,0.1);
            backdrop-filter: blur(10px);
        }
        .close-news-btn {
            background: rgba(0,0,0,0.05); border: none; width: 32px; height: 32px; border-radius: 50%;
            font-size: 18px; display: flex; align-items: center; justify-content: center; cursor: pointer;
        }
        .open-ext-btn {
            color: var(--accent-color); font-weight: 600; font-size: 13px; text-decoration: none;
        }
        .news-iframe { flex: 1; border: none; width: 100%; height: 100%; background: #fff; }
        
    </style>
</head>
<body>
    <div class="header">
        <div class="date-badge"><span>🗓️</span> {{ date_str }}</div>
        <div class="header-title">Daily Digest</div>
        {% if data.is_pro %}<span class="pro-badge">PRO MEMBER 👑</span>{% endif %}
    </div>

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

    <div id="newsModal" class="news-modal">
        <div class="modal-header">
            <button class="close-news-btn" onclick="closeNews()">✕</button>
            <span style="font-weight:600; font-size:14px;">Xem tin tức</span>
            <a id="extLink" href="#" target="_blank" class="open-ext-btn">Mở Web ↗️</a>
        </div>
        <iframe id="newsFrame" class="news-iframe" src=""></iframe>
    </div>

    <script>
        Telegram.WebApp.expand();
        window.addEventListener('load', function() {
            document.body.classList.add('loaded');
            Telegram.WebApp.ready();
        });

        // --- HÀM XEM TIN MỚI ---
        function viewNews(url) {
            const modal = document.getElementById('newsModal');
            const frame = document.getElementById('newsFrame');
            const extLink = document.getElementById('extLink');
            
            // Set link cho iframe và nút mở ngoài
            frame.src = url;
            extLink.href = url;
            extLink.onclick = function() { Telegram.WebApp.openLink(url); return false; }; // Dùng SDK để mở ngoài an toàn hơn
            
            // Hiện modal
            modal.classList.add('active');
            Telegram.WebApp.BackButton.show();
            Telegram.WebApp.BackButton.onClick(closeNews);
        }

        function closeNews() {
            const modal = document.getElementById('newsModal');
            const frame = document.getElementById('newsFrame');
            
            modal.classList.remove('active');
            // Xóa src để dừng load/âm thanh nếu có
            setTimeout(() => { frame.src = ''; }, 300);
            
            Telegram.WebApp.BackButton.hide();
            Telegram.WebApp.BackButton.offClick(closeNews);
        }

        // Hàm toggle cũ
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
            @keyframes fadeIn {
                from { opacity: 0; transform: translateY(-5px); }
                to { opacity: 1; transform: translateY(0); }
            }
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
PROFILE_HTML_TEMPLATE = r"""
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
        
        /* --- SMOOTH LOADING --- */
        body {
            margin: 0; padding: 16px;
            font-family: 'Inter', sans-serif;
            background-color: var(--bg-color);
            color: var(--text-color);
            -webkit-font-smoothing: antialiased;
            
            visibility: hidden; opacity: 0; transition: opacity 0.3s ease-in-out;
        }
        body.loaded { visibility: visible; opacity: 1; }
        
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

        /* Table of Contents */
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
            scroll-margin-top: 16px; 
            /* Animation cho card */
            animation: fadeInUp 0.4s ease-out; animation-fill-mode: backwards;
        }
        .card:nth-child(1) { animation-delay: 0.05s; }
        .card:nth-child(2) { animation-delay: 0.1s; }
        .card:nth-child(3) { animation-delay: 0.15s; }
        
        .card-title-row {
            display: flex; align-items: center; gap: 8px;
            margin-bottom: 12px; padding-bottom: 8px;
            border-bottom: 1px solid rgba(0,0,0,0.05);
        }
        .card-title-icon { font-size: 18px; }
        .card-title { font-size: 15px; font-weight: 700; text-transform: uppercase; color: var(--text-color); letter-spacing: 0.5px; }

        .profile-text {
            font-size: 14px; line-height: 1.6; color: var(--text-color);
            white-space: pre-line; font-weight: 400;
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
        Telegram.WebApp.expand();

        // --- LOGIC SMOOTH LOADING ---
        window.addEventListener('load', function() {
            document.body.classList.add('loaded');
            
            // Format lại text đậm
            const textElements = document.querySelectorAll('.profile-text');
            textElements.forEach(el => {
                let content = el.innerHTML;
                content = content.replace(/\*\*(.*?)\*\*/g, '<b style="font-weight: 700; color: var(--text-color);">$1</b>');
                el.innerHTML = content;
            });
            
            Telegram.WebApp.ready();
        });

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
        
        /* Header Title & Badge */
        .header-row { text-align: center; margin-bottom: 20px; animation: fadeInDown 0.5s ease; }
        .header-title { font-size: 20px; font-weight: 800; margin: 0; display: inline-flex; align-items: center; gap: 6px; color: var(--text-color); }
        
        .pro-badge { 
            background: linear-gradient(135deg, #FFD700 0%, #FFA500 100%); 
            color: white; font-size: 10px; font-weight: 800; 
            padding: 3px 8px; border-radius: 8px; 
            text-transform: uppercase; transform: translateY(-1px);
        }

        .header-time { font-size: 12px; color: var(--hint-color); margin-top: 4px; font-weight: 500; }

        /* Header Score */
        .score-card {
            background: linear-gradient(135deg, #007aff, #5856d6);
            color: white; border-radius: 20px; padding: 24px;
            text-align: center; margin-bottom: 20px;
            box-shadow: 0 8px 20px rgba(0,122,255,0.25);
            position: relative; overflow: hidden;
        }
        .score-card::before { content: ''; position: absolute; top: -50%; left: -50%; width: 200%; height: 200%; background: radial-gradient(circle, rgba(255,255,255,0.2) 0%, transparent 60%); pointer-events: none; }
        
        .score-val { font-size: 48px; font-weight: 800; line-height: 1; letter-spacing: -2px; }
        .score-label { font-size: 14px; font-weight: 500; opacity: 0.9; margin-top: 4px; text-transform: uppercase; letter-spacing: 0.5px; }
        .score-sub { font-size: 12px; opacity: 0.8; margin-top: 8px; }

        /* Market Comment */
        .market-card {
            background-color: var(--card-bg); border-radius: 16px;
            padding: 16px; margin-bottom: 24px;
            border-left: 4px solid var(--accent-color);
            box-shadow: 0 2px 8px rgba(0,0,0,0.04);
        }
        .market-title { font-size: 12px; font-weight: 700; text-transform: uppercase; color: var(--hint-color); margin-bottom: 8px; letter-spacing: 0.5px; }
        .market-text { font-size: 14px; line-height: 1.6; font-weight: 400; }

        /* Stock List */
        .stock-card {
            background-color: var(--card-bg); border-radius: 16px;
            padding: 16px; margin-bottom: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.04);
            transition: transform 0.1s;
        }
        .stock-card:active { transform: scale(0.98); }
        
        .st-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
        .st-symbol { font-size: 18px; font-weight: 800; color: var(--text-color); }
        .st-industry { font-size: 12px; color: var(--hint-color); font-weight: 500; margin-left: 6px; }
        
        .st-badge { font-size: 11px; font-weight: 700; padding: 4px 10px; border-radius: 20px; text-transform: uppercase; }
        
        .act-buy { background-color: var(--success-bg); color: var(--success-text); }
        .act-hold { background-color: var(--warning-bg); color: var(--warning-text); }
        .act-sell { background-color: var(--danger-bg); color: var(--danger-text); }
        .act-neutral { background-color: var(--bg-color); color: var(--hint-color); }

        .st-analysis { font-size: 14px; line-height: 1.6; margin-bottom: 12px; color: var(--text-color); white-space: pre-line; }
        
        .st-metrics {
            background-color: var(--bg-color); border-radius: 10px;
            padding: 10px; font-size: 12px; color: var(--hint-color);
            display: flex; align-items: center; gap: 6px;
        }
        .st-metrics-icon { font-size: 14px; }

        .footer { text-align: center; margin-top: 30px; font-size: 12px; color: var(--hint-color); padding-bottom: 40px; }
        
        .btn-close {
            display: block; width: 100%; padding: 14px; 
            background-color: var(--card-bg); color: var(--text-color); 
            border: none; border-radius: 14px; font-size: 15px; font-weight: 600; 
            margin-top: 20px; cursor: pointer; box-shadow: 0 2px 8px rgba(0,0,0,0.05);
        }

        @keyframes fadeInDown { from { opacity: 0; transform: translateY(-10px); } to { opacity: 1; transform: translateY(0); } }
    </style>
</head>
<body>
    <div class="header-row">
        <div class="header-title">
            Báo Cáo Danh Mục
            {% if is_pro %}<span class="pro-badge">PRO 👑</span>{% endif %}
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
        Telegram.WebApp.expand();
        window.addEventListener('load', function() {
            document.body.classList.add('loaded');
            Telegram.WebApp.ready();
        });
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
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Bộ Lọc Cổ Phiếu</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: var(--tg-theme-bg-color, #f2f2f7);
            --text-color: var(--tg-theme-text-color, #000);
            --hint-color: var(--tg-theme-hint-color, #8e8e93);
            --card-bg: var(--tg-theme-secondary-bg-color, #ffffff);
            --button-color: var(--tg-theme-button-color, #007aff);
            
            --brand-gradient: linear-gradient(135deg, #007aff 0%, #af52de 100%);
            --pro-gradient: linear-gradient(135deg, #FFD700 0%, #FFA500 100%);
            
            --color-success: #34c759; --color-success-bg: rgba(52, 199, 89, 0.1);
            --color-danger: #ff3b30;
            
            --border-radius: 16px;
            --shadow-sm: 0 2px 8px rgba(0,0,0,0.04);
        }

        /* --- SMOOTH LOADING --- */
        body { 
            font-family: 'Inter', sans-serif; 
            background-color: var(--bg-color); 
            color: var(--text-color); 
            margin: 0; padding: 16px 16px 40px 16px;
            -webkit-font-smoothing: antialiased; 
            visibility: hidden; opacity: 0; transition: opacity 0.3s ease-in-out;
        }
        body.loaded { visibility: visible; opacity: 1; }
        
        /* HEADER */
        .header { text-align: center; margin-bottom: 24px; }
        
        .header-title-row { display: flex; align-items: center; justify-content: center; gap: 8px; margin-bottom: 4px; }
        
        .header-title { 
            font-size: 28px; font-weight: 800; margin: 0; letter-spacing: -1px; line-height: 1.2;
            background: var(--brand-gradient); 
            -webkit-background-clip: text; -webkit-text-fill-color: transparent; 
            background-clip: text; color: var(--button-color);
        }
        
        .pro-badge { 
            background: var(--pro-gradient); color: white; font-size: 11px; font-weight: 800; 
            padding: 4px 8px; border-radius: 8px; display: inline-flex; align-items: center; 
            transform: translateY(-2px); 
        }

        .header-desc { font-size: 13px; color: var(--hint-color); margin-top: 6px; font-weight: 500; }
        .header-time { font-size: 11px; color: var(--hint-color); margin-top: 4px; font-weight: 500; opacity: 0.8; }

        /* TABS */
        .tabs-wrapper { overflow-x: auto; scrollbar-width: none; margin: 0 -8px 20px -8px; padding: 0 8px; text-align: center; }
        .tabs-wrapper::-webkit-scrollbar { display: none; }
        
        .tabs { display: inline-flex; gap: 8px; background: rgba(118, 118, 128, 0.12); padding: 4px; border-radius: 12px; }
        
        .tab { 
            padding: 6px 16px; border-radius: 8px; font-size: 13px; font-weight: 600; 
            color: var(--text-color); text-decoration: none; transition: all 0.2s; border: none; white-space: nowrap;
        }
        .tab.active { background: var(--card-bg); color: var(--button-color); box-shadow: 0 3px 8px rgba(0,0,0,0.12); }

        /* CARDS */
        .section-card { 
            background-color: var(--card-bg); border-radius: var(--border-radius); 
            margin-bottom: 16px; box-shadow: var(--shadow-sm); overflow: hidden; 
            animation: fadeInUp 0.4s ease;
        }

        .card-header { padding: 14px 16px; border-bottom: 1px solid rgba(0,0,0,0.05); display: flex; justify-content: space-between; align-items: center; }
        .industry-title { font-size: 15px; font-weight: 700; color: var(--text-color); }
        .industry-stat { font-size: 11px; color: var(--hint-color); font-weight: 500; background: var(--bg-color); padding: 4px 8px; border-radius: 6px; }

        /* TABLE */
        .stock-table { width: 100%; border-collapse: collapse; font-size: 13px; }
        .stock-table th { text-align: right; color: var(--hint-color); font-weight: 600; font-size: 11px; padding: 10px 12px 8px 4px; text-transform: uppercase; }
        .stock-table th:first-child { text-align: left; padding-left: 16px; }
        .stock-table th:last-child { padding-right: 16px; }

        .stock-table td { padding: 12px 12px 12px 4px; border-bottom: 1px solid rgba(0,0,0,0.05); vertical-align: middle; color: var(--text-color); }
        .stock-table tr:last-child td { border-bottom: none; }
        .stock-table td:first-child { padding-left: 16px; }
        .stock-table td:last-child { padding-right: 16px; }

        .sym-box { font-weight: 700; font-size: 14px; color: var(--button-color); }
        .val-good { color: var(--color-success); font-weight: 600; }
        .val-bad { color: var(--color-danger); }
        
        .score-badge { background: var(--color-success-bg); color: var(--color-success); padding: 4px 8px; border-radius: 6px; font-weight: 700; font-size: 12px; min-width: 32px; display: inline-block; text-align: center; }

        /* EXPAND BUTTON */
        .row-hidden { display: none; }
        .action-area { padding: 10px; text-align: center; border-top: 1px solid rgba(0,0,0,0.05); }
        .btn-toggle { background: none; border: none; color: var(--hint-color); font-size: 12px; font-weight: 600; cursor: pointer; padding: 6px 12px; display: inline-flex; align-items: center; gap: 4px; }
        .btn-toggle:active { opacity: 0.7; }

        /* FOOTER */
        .footer-btn { display: block; width: 100%; padding: 14px; background: var(--brand-gradient); color: #fff; text-align: center; border-radius: 16px; border: none; font-size: 15px; font-weight: 700; margin-top: 24px; cursor: pointer; box-shadow: 0 8px 20px rgba(0,122,255, 0.25); }

        @keyframes fadeInUp { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
    </style>
</head>
<body>
    <div class="header">
        <div class="header-title-row">
            <h1 class="header-title">Bộ Lọc Cổ Phiếu</h1>
            <span class="pro-badge">PRO 👑</span>
        </div>
        <div class="header-desc">Dữ liệu realtime từ thị trường</div>
        {% if data.as_of %}
        <div class="header-time">🕒 Cập nhật: {{ data.as_of }}</div>
        {% endif %}
    </div>

    <div class="tabs-wrapper">
        <div class="tabs">
            <a href="?type=all&chat_id={{ chat_id }}" class="tab {% if current_type == 'all' %}active{% endif %}">Tổng hợp</a>
            <a href="?type=pe&chat_id={{ chat_id }}" class="tab {% if current_type == 'pe' %}active{% endif %}">P/E Thấp</a>
            <a href="?type=pb&chat_id={{ chat_id }}" class="tab {% if current_type == 'pb' %}active{% endif %}">P/B Thấp</a>
            <a href="?type=roe&chat_id={{ chat_id }}" class="tab {% if current_type == 'roe' %}active{% endif %}">ROE Cao</a>
        </div>
    </div>

    {% if error %}
        <div style="text-align:center; color: var(--hint-color); margin-top: 60px;">
            <div style="font-size: 48px; margin-bottom: 10px; opacity: 0.5;">📉</div>
            <b>{{ error }}</b>
        </div>
    {% else %}
        {% for industry in data.industries %}
        <div class="section-card" id="card-{{ loop.index }}">
            <div class="card-header">
                <div class="industry-title">{{ industry.industry }}</div>
                {% if current_type == 'all' %}
                    <div class="industry-stat">P/E Ngành: {{ "%.1f"|format(industry.rows[0].pe_industry) }}</div>
                {% endif %}
            </div>
            
            <table class="stock-table">
                <thead>
                    <tr>
                        <th>Mã</th>
                        <th>P/E</th>
                        <th>P/B</th>
                        <th>ROE</th>
                        {% if current_type == 'all' %}<th>Điểm</th>{% endif %}
                    </tr>
                </thead>
                <tbody>
                    {% for stock in industry.rows %}
                    <tr class="stock-row-expandable {% if loop.index > 5 %}row-hidden{% endif %}">
                        <td><div class="sym-box">{{ stock.symbol }}</div></td>
                        <td style="text-align:right">
                            {% if stock.pe < 10 %}<span class="val-good">{{ "%.1f"|format(stock.pe) }}</span>
                            {% else %}{{ "%.1f"|format(stock.pe) }}{% endif %}
                        </td>
                        <td style="text-align:right">
                            {% if stock.pb < 1.5 %}<span class="val-good">{{ "%.1f"|format(stock.pb) }}</span>
                            {% else %}{{ "%.1f"|format(stock.pb) }}{% endif %}
                        </td>
                        <td style="text-align:right">
                            {% if stock.roe > 0.15 %}<span class="val-good">{{ "%.0f"|format(stock.roe * 100) }}%</span>
                            {% else %}{{ "%.0f"|format(stock.roe * 100) }}%{% endif %}
                        </td>
                        {% if current_type == 'all' %}
                        <td style="text-align:right"><span class="score-badge">{{ "%.1f"|format(stock.value_score) }}</span></td>
                        {% endif %}
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            
            {% if industry.rows|length > 5 %}
            <div class="action-area">
                <button class="btn-toggle" 
                        data-expanded="false" 
                        data-total="{{ industry.rows|length }}"
                        onclick="toggleRows('card-{{ loop.index }}', this)">
                    Xem thêm {{ industry.rows|length - 5 }} mã ↓
                </button>
            </div>
            {% endif %}
        </div>
        {% endfor %}
    {% endif %}

    <div style="padding: 0 16px;">
        <button class="footer-btn" onclick="Telegram.WebApp.close()">Đóng</button>
    </div>

    <script>
        Telegram.WebApp.expand();
        
        window.addEventListener('load', function() {
            document.body.classList.add('loaded');
            Telegram.WebApp.ready();
        });

        function toggleRows(cardId, btn) {
            const card = document.getElementById(cardId);
            const hiddenRows = card.querySelectorAll('.stock-row-expandable');
            const isExpanded = btn.getAttribute('data-expanded') === 'true';
            const total = btn.getAttribute('data-total');
            const hiddenCount = parseInt(total) - 5;

            if (!isExpanded) {
                hiddenRows.forEach((row, index) => {
                    row.classList.remove('row-hidden');
                    if (index >= 5) row.style.animation = 'fadeInUp 0.3s ease';
                });
                btn.innerHTML = 'Thu gọn ↑';
                btn.setAttribute('data-expanded', 'true');
            } else {
                hiddenRows.forEach((row, index) => {
                    if (index >= 5) row.classList.add('row-hidden');
                });
                btn.innerHTML = 'Xem thêm ' + hiddenCount + ' mã ↓';
                btn.setAttribute('data-expanded', 'false');
                card.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        }
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

EOD_HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Tổng Kết Cuối Phiên</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: var(--tg-theme-bg-color, #f2f2f7);
            --text-color: var(--tg-theme-text-color, #000);
            --hint-color: var(--tg-theme-hint-color, #8e8e93);
            --card-bg: var(--tg-theme-secondary-bg-color, #ffffff);
            
            /* 🎨 BẢNG MÀU CHỨNG KHOÁN */
            --up-color: #34c759;   
            --down-color: #ff3b30; 
            --ref-color: #ffcc00;  
            --ceil-color: #ce23ff; 
            --floor-color: #00c5c5;
        }
        
        body { 
            font-family: 'Inter', sans-serif; 
            background-color: var(--bg-color); 
            color: var(--text-color); 
            margin: 0; padding: 20px 16px 40px 16px; 
            font-size: 14px; line-height: 1.5; 
            visibility: hidden; opacity: 0; transition: opacity 0.3s ease-in-out;
        }
        body.loaded { visibility: visible; opacity: 1; }

        .header { text-align: center; margin-bottom: 24px; }
        .date-badge { display: inline-flex; align-items: center; gap: 6px; background-color: var(--card-bg); padding: 6px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; color: var(--hint-color); box-shadow: 0 2px 6px rgba(0,0,0,0.03); margin-bottom: 8px; }
        .header-title { 
            font-size: 28px; font-weight: 800; margin: 0; 
            background: linear-gradient(135deg, #007aff 0%, #af52de 100%); 
            -webkit-background-clip: text; -webkit-text-fill-color: transparent; 
        }

        .ai-card { 
            background: linear-gradient(135deg, #e0f2fe 0%, #f0f9ff 100%); 
            border-left: 4px solid #007aff;
            border-radius: 16px; padding: 16px; margin-bottom: 20px; 
            box-shadow: 0 4px 12px rgba(0,122,255,0.1);
        }
        .ai-header { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
        .ai-icon { font-size: 20px; }
        .ai-title { font-size: 14px; font-weight: 700; color: #007aff; text-transform: uppercase; letter-spacing: 0.5px; }
        .ai-content { font-size: 14px; color: #334155; line-height: 1.6; white-space: pre-line; }

        /* GRID 2 CỘT CHO VNINDEX & VN30 */
        .market-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 24px; }
        .m-card { background-color: var(--card-bg); border-radius: 16px; padding: 16px; text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,0.04); }
        .m-label { font-size: 12px; color: var(--hint-color); font-weight: 700; text-transform: uppercase; margin-bottom: 6px; }
        .m-val { font-size: 20px; font-weight: 800; line-height: 1.2; margin-bottom: 4px; }
        .m-change { font-size: 13px; font-weight: 600; }
        
        /* Khối lượng giao dịch */
        .m-vol { margin-top: 12px; padding-top: 8px; border-top: 1px solid rgba(0,0,0,0.05); font-size: 12px; color: var(--hint-color); }
        .m-vol-val { font-weight: 700; color: var(--text-color); }
        
        .text-up { color: var(--up-color); }
        .text-down { color: var(--down-color); }
        .text-ref { color: var(--ref-color); }
        
        .section-title { font-size: 15px; font-weight: 700; margin-bottom: 12px; padding-left: 4px; display: flex; align-items: center; gap: 8px; }
        .p-list { background-color: var(--card-bg); border-radius: 16px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.04); }
        .p-item { display: flex; justify-content: space-between; align-items: center; padding: 14px 16px; border-bottom: 1px solid rgba(0,0,0,0.05); }
        .p-item:last-child { border-bottom: none; }
        
        .p-sym { font-size: 16px; font-weight: 700; }
        .p-price { font-size: 15px; font-weight: 600; text-align: right; }
        .p-change { font-size: 12px; font-weight: 600; padding: 4px 8px; border-radius: 6px; min-width: 50px; text-align: center; display: inline-block; margin-left: 8px;}

        .bg-up { background-color: rgba(52, 199, 89, 0.1); color: var(--up-color); }
        .bg-down { background-color: rgba(255, 59, 48, 0.1); color: var(--down-color); }
        .bg-ref { background-color: rgba(255, 204, 0, 0.15); color: #d4a017; }
        .bg-ceil { background-color: rgba(206, 35, 255, 0.1); color: var(--ceil-color); }
        .bg-floor { background-color: rgba(0, 197, 197, 0.1); color: var(--floor-color); }

        .footer-btn { display: block; width: 100%; padding: 14px; background-color: var(--text-color); color: var(--bg-color); border: none; border-radius: 14px; font-size: 15px; font-weight: 700; margin-top: 30px; cursor: pointer; }
        .meta-time { text-align: center; margin-top: 16px; color: var(--hint-color); font-size: 11px; }
    </style>
</head>
<body>
    <div class="header">
        <div class="date-badge">📅 {{ generated_at }}</div>
        <div class="header-title">Tổng Kết Phiên</div>
    </div>

    {% if market_data.ai_comment %}
    <div class="ai-card">
        <div class="ai-header">
            <div class="ai-icon">🧠</div>
            <div class="ai-title">Góc nhìn AI (Gemini)</div>
        </div>
        <div class="ai-content">{{ market_data.ai_comment }}</div>
    </div>
    {% endif %}

    <div class="market-grid">
        <div class="m-card">
            <div class="m-label">VN-INDEX</div>
            <div class="m-val {{ market_data.vnindex.cls }}">{{ market_data.vnindex.price }}</div>
            <div class="m-change {{ market_data.vnindex.cls }}">{{ market_data.vnindex.change_str }}</div>
            <div class="m-vol">KL: <span class="m-vol-val">{{ market_data.vnindex.vol_str }}</span></div>
        </div>
        <div class="m-card">
            <div class="m-label">VN30</div>
            <div class="m-val {{ market_data.vn30.cls }}">{{ market_data.vn30.price }}</div>
            <div class="m-change {{ market_data.vn30.cls }}">{{ market_data.vn30.change_str }}</div>
            <div class="m-vol">KL: <span class="m-vol-val">{{ market_data.vn30.vol_str }}</span></div>
        </div>
    </div>

    {% if user_stocks %}
    <div class="section-title">👤 Danh mục của bạn</div>
    <div class="p-list">
        {% for stock in user_stocks %}
        <div class="p-item">
            <div class="p-sym">{{ stock.symbol }}</div>
            <div>
                <span class="p-price">{{ stock.price }}</span>
                
                {% set badge_cls = 'bg-ref' %}
                {% set val_sign = '' %}
                
                {% if stock.pct >= 6.9 %}
                    {% set badge_cls = 'bg-ceil' %}
                    {% set val_sign = '+' %}
                {% elif stock.pct <= -6.9 %}
                    {% set badge_cls = 'bg-floor' %}
                {% elif stock.pct == 0 %}
                    {% set badge_cls = 'bg-ref' %}
                {% elif stock.pct > 0 %}
                    {% set badge_cls = 'bg-up' %}
                    {% set val_sign = '+' %}
                {% elif stock.pct < 0 %}
                    {% set badge_cls = 'bg-down' %}
                {% endif %}

                <span class="p-change {{ badge_cls }}">{{ val_sign }}{{ stock.pct }}%</span>
            </div>
        </div>
        {% endfor %}
    </div>
    {% else %}
    <div style="text-align:center; padding:20px; color:var(--hint-color);">
        Bạn chưa theo dõi mã nào. <br>Gõ <b>/add MÃ</b> để thêm.
    </div>
    {% endif %}

    <button class="footer-btn" onclick="Telegram.WebApp.close()">Đóng Bản Tin</button>
    <div class="meta-time">Dữ liệu được cập nhật cuối phiên</div>

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




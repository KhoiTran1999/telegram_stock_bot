DIGEST_HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>StockBot Digest</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Oswald:wght@700&display=swap" rel="stylesheet">
    <style>
        :root {
            /* --- Biến Theme Chung (Light Mode Mặc định) --- */
            --bg-color: var(--tg-theme-bg-color, #f2f2f7);
            --text-color: var(--tg-theme-text-color, #000);
            --hint-color: var(--tg-theme-hint-color, #8e8e93);
            --card-bg: var(--tg-theme-secondary-bg-color, #ffffff);
            --accent-color: var(--tg-theme-button-color, #007aff);
            --border-color: rgba(0,0,0,0.05);

            /* Badge Colors */
            --success-bg: rgba(52, 199, 89, 0.15); --success-text: #34c759;
            --warning-bg: rgba(255, 204, 0, 0.15); --warning-text: #d48806;
            --danger-bg: rgba(255, 59, 48, 0.15);  --danger-text: #ff3b30;
            --info-bg-light: rgba(0,122,255,0.05); 
            
            /* --- Màu Định giá (Rankings) - Light Mode --- */
            --metric-bg: #f9fafb; 
            
            /* Rank 1 (Gold) */
            --r1-bg: linear-gradient(160deg, #ffffff 40%, #fffbeb 100%); 
            --r1-border: #fbbf24; 
            --r1-shadow: rgba(245, 158, 11, 0.25);
            
            /* Rank 2 (Silver) */
            --r2-bg: linear-gradient(160deg, #ffffff 40%, #f3f4f6 100%); 
            --r2-border: #9ca3af; 
            --r2-shadow: rgba(156, 163, 175, 0.2);
            
            /* Rank 3 (Bronze) */
            --r3-bg: linear-gradient(160deg, #ffffff 40%, #fff7ed 100%); 
            --r3-border: #fdba74; 
            --r3-shadow: rgba(234, 88, 12, 0.15);

            /* Rank 4+ */
            --rank-other-bg: #e5e7eb;
            --rank-other-text: #374151;
        }
        
        /* --- Dark Mode Overrides --- */
        @media (prefers-color-scheme: dark) {
            :root {
                --border-color: rgba(255,255,255,0.15);
                --info-bg-light: rgba(10, 132, 255, 0.15); 

                --success-text: #6ee7b7; --success-bg: rgba(16, 185, 129, 0.2);
                --warning-text: #fcd34d; --warning-bg: rgba(250, 204, 21, 0.2);
                --danger-text: #f87171;  --danger-bg: rgba(248, 113, 113, 0.2);

                /* Nền ô chỉ số sáng hơn nền card một chút để tạo độ nổi */
                --metric-bg: #2c2c2e; 

                /* Rank 1 (Gold Dark) */
                --r1-bg: linear-gradient(135deg, #42330b 0%, #1c1c1e 100%);
                --r1-border: #fbbf24; --r1-shadow: rgba(251, 191, 36, 0.1); 

                /* Rank 2 (Silver Dark) */
                --r2-bg: linear-gradient(135deg, #374151 0%, #1c1c1e 100%);
                --r2-border: #9ca3af; --r2-shadow: rgba(255,255,255,0.05);

                /* Rank 3 (Bronze Dark) */
                --r3-bg: linear-gradient(135deg, #431407 0%, #1c1c1e 100%);
                --r3-border: #fb923c; --r3-shadow: rgba(251, 146, 60, 0.1);

                /* Rank 4+ (Dark) */
                --rank-other-bg: rgba(255,255,255,0.1);
                --rank-other-text: #d1d5db;
            }
        }

        body { 
            font-family: 'Inter', sans-serif; background-color: var(--bg-color); color: var(--text-color); 
            margin: 0; padding: 20px 16px 40px 16px; font-size: 14px; line-height: 1.5;
            visibility: hidden; opacity: 0; transition: opacity 0.3s ease-in-out;
            -webkit-font-smoothing: antialiased;
        }
        body.loaded { visibility: visible; opacity: 1; }

        /* HEADER Styles */
        .header { text-align: center; margin-bottom: 32px; }
        .date-badge { display: inline-flex; align-items: center; gap: 6px; background-color: var(--card-bg); padding: 6px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; color: var(--hint-color); box-shadow: 0 2px 6px rgba(0,0,0,0.03); margin-bottom: 12px; }
        .header-title { font-size: 32px; font-weight: 800; margin: 0; background: linear-gradient(135deg, #007aff 0%, #af52de 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .pro-badge { background: linear-gradient(135deg, #FFD700 0%, #FFA500 100%); color: white; font-size: 11px; font-weight: 800; padding: 4px 8px; border-radius: 8px; display: inline-flex; transform: translateY(-2px); }
        
        /* CARD Styles */
        .section-card { background-color: var(--card-bg); border-radius: 16px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.04); overflow: hidden; }
        .card-header { padding: 16px 16px 10px 16px; display: flex; align-items: center; gap: 10px; border-bottom: 1px solid var(--border-color); }
        .card-icon { width: 28px; height: 28px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 16px; background: rgba(0,122,255,0.1); color: #007aff; }
        .card-title { font-size: 17px; font-weight: 700; }

        /* BADGES */
        .st-badge { font-size: 11px; font-weight: 700; padding: 4px 10px; border-radius: 20px; text-transform: uppercase; }
        .st-badge.act-buy { background: var(--success-bg); color: var(--success-text); }
        .st-badge.act-sell { background: var(--danger-bg); color: var(--danger-text); }
        .st-badge.act-hold { background: var(--warning-bg); color: var(--warning-text); }
        
        /* AI INSIGHT */
        .ai-hot-topic { background: var(--info-bg-light); border-radius: 12px; padding: 12px; margin-bottom: 16px; }
        .ai-hot-title { font-weight:700; color:var(--accent-color); margin-bottom:8px; font-size:12px; text-transform: uppercase; }
        .ai-hot-text a { text-decoration:none; color:inherit; }
        .ai-hot-text a:hover { text-decoration: underline; }
        
        .ai-macro-title { font-weight:700; color:var(--hint-color); margin:16px 0 8px 0; font-size:12px; text-transform: uppercase; }
        .ai-macro-text a { text-decoration:none; color:inherit; } 
        
        .ai-corp-header { font-weight:800; color:var(--hint-color); margin-bottom:12px; font-size:13px; text-transform: uppercase; border-bottom: 2px solid var(--border-color); padding-bottom: 6px; display:flex; align-items:center; gap:6px; }
        .ai-comment-box { margin-top: 16px; font-style: italic; color: var(--hint-color); font-size: 13px; border-top: 1px solid var(--border-color); padding-top: 12px; background:var(--bg-color); padding:12px; border-radius:8px; }
        .ai-corp-item { padding: 10px 0; border-bottom: 1px solid var(--border-color); display: flex; align-items: flex-start; gap: 10px; }
        .ai-corp-item:last-child { border-bottom: none; }
        .ai-corp-ticker { background-color: rgba(0,122,255,0.1); color: #007aff; font-weight: 700; font-size: 11px; padding: 2px 6px; border: 1px solid rgba(0,122,255,0.2); }
        
        /* LIST ITEMS */
        .list-item { padding: 14px 16px; border-bottom: 1px solid var(--border-color); display: block; text-decoration: none; color: inherit; cursor: pointer; }
        .list-item:active { background-color: rgba(0,0,0,0.05); }
        .list-item:last-child { border-bottom: none; }
        
        .item-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
        .badge { background: var(--metric-bg); font-size: 11px; font-weight: 700; padding: 4px 8px; border-radius: 6px; }
        .item-title { font-size: 15px; font-weight: 500; line-height: 1.4; }
        .item-meta { font-size: 12px; color: var(--hint-color); margin-top: 4px; }
        .hidden-item { display: none; }
        .action-area { padding: 10px; text-align: center; border-top: 1px solid var(--border-color); }
        .btn-toggle { background: none; border: none; color: var(--accent-color); font-size: 13px; font-weight: 600; cursor: pointer; padding: 6px 12px; display: inline-flex; align-items: center; gap: 4px; }
        
        /* LOCKED CONTENT */
        .list-item.locked { position: relative; background: repeating-linear-gradient(45deg, var(--card-bg), var(--card-bg) 10px, var(--bg-color) 10px, var(--bg-color) 20px); cursor: default; }
        .blur-content { filter: blur(4px); opacity: 0.6; user-select: none; pointer-events: none; }
        .lock-overlay { position: absolute; top: 0; left: 0; width: 100%; height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; background: rgba(255, 255, 255, 0.6); z-index: 2; }
        @media (prefers-color-scheme: dark) { .lock-overlay { background: rgba(0, 0, 0, 0.7); } }
        .lock-btn { background: var(--text-color); color: var(--bg-color); border: none; padding: 6px 16px; border-radius: 20px; font-size: 12px; font-weight: 700; cursor: pointer; box-shadow: 0 4px 10px rgba(0,0,0,0.1); display: flex; align-items: center; gap: 4px; }

        /* --- SCREENER CARD (Fix Darkmode & Layout) --- */
        .screener-card {
            border-radius: 16px; padding: 16px; margin-bottom: 12px; position: relative;
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            box-shadow: 0 2px 4px rgba(0,0,0,0.02);
        }
        
        /* Rank 1 */
        .card-rank-1 { background: var(--r1-bg); border: 1px solid var(--r1-border); box-shadow: 0 4px 15px var(--r1-shadow); z-index: 2; }
        .card-rank-1 .rank-badge { background: linear-gradient(135deg, #FFD700, #F59E0B); box-shadow: 0 4px 12px rgba(245, 158, 11, 0.4); border: 2px solid var(--card-bg); color: #fff;}
        .card-rank-1::before { content: '👑'; position: absolute; top: -12px; left: 8px; font-size: 20px; transform: rotate(-15deg); z-index: 3; filter: drop-shadow(0 2px 2px rgba(0,0,0,0.1)); }
        
        /* Rank 2 */
        .card-rank-2 { background: var(--r2-bg); border: 1px solid var(--r2-border); box-shadow: 0 4px 15px var(--r2-shadow); }
        .card-rank-2 .rank-badge { background: linear-gradient(135deg, #E5E7EB, #9CA3AF); box-shadow: 0 4px 10px rgba(156, 163, 175, 0.3); border: 2px solid var(--card-bg); color: var(--text-color); }
        
        /* Rank 3 */
        .card-rank-3 { background: var(--r3-bg); border: 1px solid var(--r3-border); box-shadow: 0 4px 15px var(--r3-shadow); }
        .card-rank-3 .rank-badge { background: linear-gradient(135deg, #fdba74, #c2410c); box-shadow: 0 4px 10px rgba(194, 65, 12, 0.3); border: 2px solid var(--card-bg); color: #fff;}
        
        /* Rank 4+ (Fix số hiển thị) */
        .rank-other { 
            background: var(--rank-other-bg); 
            color: var(--rank-other-text); 
            font-size: 13px; border-radius: 8px; width: 26px; height: 26px; 
            font-family: 'Manrope', sans-serif; font-weight: 800; 
            display: flex; align-items: center; justify-content: center;
        }
        
        .screener-card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; padding-bottom: 8px; border-bottom: 1px dashed var(--border-color); opacity: 0.9; }
        .symbol-wrap { display: flex; align-items: center; gap: 10px; }
        .rank-badge { display: flex; align-items: center; justify-content: center; border-radius: 8px; font-family: 'Oswald', sans-serif; font-weight: 700; width: 28px; height: 28px; font-size: 14px;}
        .symbol-name { font-size: 18px; font-weight: 800; color: var(--text-color); letter-spacing: -0.5px; }
        .signal-badge { font-size: 10px; font-weight: 700; padding: 4px 10px; border-radius: 20px; text-transform: uppercase; }
        .sig-cheap { background: var(--success-bg); color: var(--success-text); }
        .sig-expensive { background: var(--danger-bg); color: var(--danger-text); }
        .sig-fair { background: var(--warning-bg); color: var(--warning-text); }

        .metrics-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
        .metric-box { background: var(--metric-bg); padding: 10px; border-radius: 12px; border: 1px solid var(--border-color); }
        .m-label { font-size: 10px; font-weight: 700; color: var(--hint-color); text-transform: uppercase; margin-bottom: 2px; opacity: 0.8; }
        .m-row { display: flex; justify-content: space-between; align-items: baseline; }
        .m-curr { font-size: 15px; font-weight: 800; color: var(--text-color); }
        .m-avg { font-size: 11px; color: var(--hint-color); font-weight: 500; }
        .m-diff { font-size: 11px; font-weight: 700; margin-top: 2px; display: flex; align-items: center; gap: 3px; }
        .diff-good { color: var(--success-text); } .diff-bad { color: var(--danger-text); }

        /* PREMIUM & FOOTER */
        .premium-card { background: linear-gradient(135deg, var(--accent-color) 0%, #af52de 100%); border-radius: 24px; padding: 24px; color: white; text-align: center; margin-top: 32px; }
        .premium-btn { display: block; width: 100%; padding: 15px; background-color: var(--card-bg); color: var(--accent-color); border: none; border-radius: 14px; font-size: 16px; font-weight: 700; cursor: pointer; margin-top: 16px; }
        .btn-close-simple { padding: 12px 40px; background: var(--text-color); color: var(--bg-color); border: none; border-radius: 12px; font-weight: 600; cursor: pointer; }
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
            <div class="ai-hot-topic">
                <div class="ai-hot-title">⚡ Tiêu điểm nóng</div>
                {% for item in data.ai_news.headline %}
                <div style="margin-bottom:8px; font-size:14px; font-weight:600; line-height:1.4;" class="ai-hot-text">
                    <a href="{{ item.link }}">
                        • {{ item.text }} <span style="color:var(--accent-color); font-size:12px;">↗</span>
                    </a>
                </div>
                {% endfor %}
            </div>

            {% if data.ai_news.macro %}
            <div class="ai-macro-title">🌊 Vĩ mô & Chính sách</div>
            {% for item in data.ai_news.macro %}
            <div style="margin-bottom:6px; font-size:13px;" class="ai-macro-text">
                <a href="{{ item.link }}">• {{ item.text }}</a>
            </div>
            {% endfor %}
            {% endif %}

            {% if data.ai_news.corporate %}
            <div style="margin-top: 20px;">
                <div class="ai-corp-header">
                    <span>🏢</span> TIN DOANH NGHIỆP
                </div>
                
                <div class="list-container">
                    {% for item in data.ai_news.corporate %}
                    <div class="ai-corp-item" onclick="viewNews('{{ item.link }}')">
                        <div style="margin-top: 4px; min-width: 6px; height: 6px; background-color: var(--accent-color); border-radius: 50%;"></div>
                        
                        <div style="flex: 1;">
                            <div style="font-size:14px; line-height: 1.5; color: var(--text-color);">
                                {% if item.ticker %}
                                    <span class="ai-corp-ticker">{{ item.ticker }}</span>
                                {% endif %}
                                {{ item.text }}
                            </div>
                        </div>
                    </div>
                    {% endfor %}
                </div>
            </div>
            {% endif %}

            <div class="ai-comment-box">
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

    {% if data.value_stocks %}
    <div class="section-card">
        <div class="card-header">
            <div class="card-icon">💎</div>
            <div class="card-title">Top Định Giá Rẻ (Mean Reversion)</div>
        </div>
        
        <div style="padding: 0 16px 16px 16px;">
            {% for item in data.value_stocks %}
            <div class="screener-card {% if loop.index == 1 %}card-rank-1{% elif loop.index == 2 %}card-rank-2{% elif loop.index == 3 %}card-rank-3{% endif %}">
                <div class="screener-card-header">
                    <div class="symbol-wrap">
                        <div class="rank-badge {% if loop.index > 3 %}rank-other{% endif %}">{{ loop.index }}</div>
                        <div class="symbol-name">{{ item.symbol }}</div>
                    </div>
                    <div class="signal-badge {{ item.signal_class }}">{{ item.signal_text }}</div>
                </div>
                <div class="metrics-grid">
                    <div class="metric-box">
                        <div class="m-label">P/E Ratio</div>
                        <div class="m-row"><span class="m-curr">{{ item.pe_cur }}x</span><span class="m-avg">TB: {{ item.pe_avg }}</span></div>
                        <div class="m-diff {{ item.pe_class }}">{{ item.pe_diff_str }}</div>
                    </div>
                    <div class="metric-box">
                        <div class="m-label">P/B Ratio</div>
                        <div class="m-row"><span class="m-curr">{{ item.pb_cur }}x</span><span class="m-avg">TB: {{ item.pb_avg }}</span></div>
                        <div class="m-diff {{ item.pb_class }}">{{ item.pb_diff_str }}</div>
                    </div>
                </div>
            </div>
            {% endfor %}
        </div>
        
        <div style="padding: 12px; font-size: 11px; color: var(--hint-color); text-align: center; background: var(--bg-color); border-bottom-left-radius: 20px; border-bottom-right-radius: 20px; opacity: 0.8;">
            *Xếp hạng dựa trên trung bình độ lệch P/E & P/B so với lịch sử 5 năm.
        </div>
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
        <button class="btn-close-simple" onclick="Telegram.WebApp.close()">Đóng</button>
    </div>
    {% endif %}

    <script>
        Telegram.WebApp.expand();
        window.addEventListener('load', function() {
            document.body.classList.add('loaded');
            Telegram.WebApp.ready();
        });

        function viewNews(url) {
            Telegram.WebApp.openLink(url);
        }

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
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Oswald:wght@700&display=swap" rel="stylesheet">
    <style>
        :root {
            /* --- Light Mode Defaults --- */
            --bg-color: #f8f9fa; 
            --card-bg: #ffffff; 
            --text-primary: #111827; 
            --text-secondary: #6b7280;
            --metric-border: rgba(0,0,0,0.04);
            --metric-bg: #f9fafb;
            
            /* Rank Colors (Light) */
            --r1-bg: linear-gradient(160deg, #ffffff 40%, #fffbeb 100%); --r1-border: #fbbf24; --r1-shadow: rgba(245, 158, 11, 0.25);
            --r2-bg: linear-gradient(160deg, #ffffff 40%, #f3f4f6 100%); --r2-border: #9ca3af; --r2-shadow: rgba(156, 163, 175, 0.2);
            --r3-bg: linear-gradient(160deg, #ffffff 40%, #fff7ed 100%); --r3-border: #fdba74; --r3-shadow: rgba(234, 88, 12, 0.15);
            
            /* Rank 4+ (Light fix) */
            --rank-other-bg: #e5e7eb;
            --rank-other-text: #374151;

            /* Signal Colors */
            --success-text: #059669; --success-bg: #d1fae5;
            --danger-text: #dc2626;  --danger-bg: #fee2e2;
            --warning-text: #d97706; --warning-bg: #fef3c7;
            --brand-gradient: linear-gradient(135deg, #2563eb 0%, #7c3aed 100%);
            --pro-gradient: linear-gradient(135deg, #F59E0B 0%, #FCD34D 100%);
        }

        /* --- Dark Mode Overrides --- */
        [data-theme="dark"] {
            --bg-color: #121212; 
            --card-bg: #1e1e1e; 
            --text-primary: #f9fafb; 
            --text-secondary: #9ca3af;
            --metric-border: rgba(255, 255, 255, 0.15);
            --metric-bg: #2c2c2e; /* Nền ô chỉ số sáng hơn nền card */
            
            --success-text: #34d399; --success-bg: rgba(5, 150, 105, 0.2);
            --danger-text: #f87171;  --danger-bg: rgba(220, 38, 38, 0.2);
            --warning-text: #fbbf24; --warning-bg: rgba(217, 119, 6, 0.2);

            --r1-bg: linear-gradient(135deg, #42330b 0%, #1c1c1e 100%); --r1-border: #b45309; --r1-shadow: rgba(251, 191, 36, 0.1);
            --r2-bg: linear-gradient(135deg, #374151 0%, #1c1c1e 100%); --r2-border: #4b5563; --r2-shadow: rgba(255, 255, 255, 0.05);
            --r3-bg: linear-gradient(135deg, #431407 0%, #1c1c1e 100%); --r3-border: #7c2d12; --r3-shadow: rgba(251, 146, 60, 0.1);
            
            /* Rank 4+ (Dark fix) */
            --rank-other-bg: rgba(255,255,255,0.1);
            --rank-other-text: #d1d5db;
        }
        
        body { font-family: 'Manrope', sans-serif; background-color: var(--bg-color); color: var(--text-primary); margin: 0; padding: 20px 16px 40px 16px; transition: all 0.3s ease; -webkit-font-smoothing: antialiased; }
        
        /* Header */
        .header { text-align: center; margin-bottom: 28px; margin-top: 10px;}
        .header-title { font-size: 26px; font-weight: 800; margin: 0; letter-spacing: -0.5px; background: var(--brand-gradient); -webkit-background-clip: text; -webkit-text-fill-color: transparent; display:inline-block; }
        .pro-badge { background: var(--pro-gradient); color: #fff; font-size: 10px; font-weight: 800; padding: 4px 8px; border-radius: 12px; display: inline-flex; align-items: center; transform: translateY(-4px); box-shadow: 0 4px 10px rgba(245, 158, 11, 0.3); margin-left: 6px; }
        .header-subtitle { font-size: 13px; color: var(--text-secondary); font-weight: 500; margin-top: 4px; }
        .timestamp-badge { display: inline-block; margin-top: 8px; padding: 4px 10px; background: var(--metric-bg); border-radius: 12px; font-size: 11px; font-weight: 600; color: var(--text-secondary); border: 1px solid var(--metric-border); }
        
        /* Card Styles */
        .card { 
            border-radius: 20px; padding: 18px; margin-bottom: 16px; position: relative; 
            border: 1px solid var(--metric-border); 
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.02); 
            transition: transform 0.2s; 
            background: var(--card-bg); 
        }
        
        .card-rank-1 { background: var(--r1-bg); border: 1px solid var(--r1-border); box-shadow: 0 10px 25px -5px var(--r1-shadow); transform: scale(1.02); z-index: 10; }
        .card-rank-1 .rank-badge { background: linear-gradient(135deg, #FFD700, #F59E0B); box-shadow: 0 4px 12px rgba(245, 158, 11, 0.5); font-size: 18px; width: 36px; height: 36px; border: 2px solid var(--card-bg); color: #fff; }
        .card-rank-1::before { content: '👑'; position: absolute; top: -14px; left: 10px; font-size: 24px; transform: rotate(-15deg); z-index: 20; filter: drop-shadow(0 2px 4px rgba(0,0,0,0.1)); }
        
        .card-rank-2 { background: var(--r2-bg); border: 1px solid var(--r2-border); box-shadow: 0 10px 25px -5px var(--r2-shadow); }
        .card-rank-2 .rank-badge { background: linear-gradient(135deg, #E5E7EB, #9CA3AF); box-shadow: 0 4px 10px rgba(156, 163, 175, 0.4); font-size: 16px; width: 32px; height: 32px; border: 2px solid var(--card-bg); color: #374151; }
        
        .card-rank-3 { background: var(--r3-bg); border: 1px solid var(--r3-border); box-shadow: 0 10px 25px -5px var(--r3-shadow); }
        .card-rank-3 .rank-badge { background: linear-gradient(135deg, #fdba74, #c2410c); box-shadow: 0 4px 10px rgba(194, 65, 12, 0.3); font-size: 16px; width: 32px; height: 32px; border: 2px solid var(--card-bg); color: #fff; }
        
        /* Style cho Rank 4+ (Đã fix màu) */
        .rank-other { 
            background: var(--rank-other-bg); 
            color: var(--rank-other-text); 
            font-size: 13px; 
            border-radius: 8px; 
            width: 26px; height: 26px; 
            font-family: 'Manrope', sans-serif; 
            font-weight: 800; 
            display: flex; align-items: center; justify-content: center;
        }
        
        .card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; padding-bottom: 12px; border-bottom: 1px dashed var(--text-secondary); opacity: 0.9; }
        .symbol-wrap { display: flex; align-items: center; gap: 12px; }
        .rank-badge { display: flex; align-items: center; justify-content: center; border-radius: 10px; font-family: 'Oswald', sans-serif; font-weight: 700; }
        
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
        
        function applyTheme() {
            const scheme = Telegram.WebApp.colorScheme;
            const body = document.body;
            const html = document.documentElement;
            
            // Set attribute data-theme cho cả html để CSS selector bắt được
            html.setAttribute('data-theme', scheme);
            body.setAttribute('data-theme', scheme);
        }
        
        Telegram.WebApp.onEvent('themeChanged', applyTheme);
        window.addEventListener('load', applyTheme);
        
        function toggleExpand() {
            document.querySelectorAll('.hidden-item').forEach(i => { i.style.display = 'block'; i.style.animation = 'fadeIn 0.5s ease'; });
            document.getElementById('btnToggle').style.display = 'none';
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
        .desc { 
            font-size: 15px; 
            color: var(--text-secondary); 
            line-height: 1.6; 
            margin-bottom: 32px; 
            max-width: 320px;
            
            /* 🔥 CÁC THAY ĐỔI QUAN TRỌNG: */
            white-space: pre-line;       /* Để hiểu ký tự xuống dòng \n */
            text-align: left;            /* Căn trái để các dấu tích ✅ thẳng hàng */
            margin-left: auto;           /* Tự động căn giữa khối div */
            margin-right: auto;          /* Tự động căn giữa khối div */
            background: rgba(0,0,0,0.03);/* (Tuỳ chọn) Thêm nền nhẹ cho nổi bật */
            padding: 16px;               /* (Tuỳ chọn) Đệm lề nếu thêm nền */
            border-radius: 12px;         /* (Tuỳ chọn) Bo góc */
        }
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
    <script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: var(--tg-theme-bg-color, #f2f2f7);
            --text-color: var(--tg-theme-text-color, #000);
            --hint-color: var(--tg-theme-hint-color, #8e8e93);
            --card-bg: var(--tg-theme-secondary-bg-color, #ffffff);
            --up-color: #34c759; --down-color: #ff3b30; --ref-color: #ffcc00;
            --ai-bg: #e0f2fe; --ai-border: #007aff;
            --border-color: rgba(0,0,0,0.05);
        }
        
        @media (prefers-color-scheme: dark) {
            :root {
                --ai-bg: #1c273b;
                --ai-border: #007aff;
                --text-color: var(--tg-theme-text-color, #fff);
                --ai-content-color: #f0f9ff;
                --border-color: rgba(255,255,255,0.1);
            }
            .ai-card { background: var(--ai-bg); }
            .ai-content { color: var(--ai-content-color); }
        }

        body { font-family: 'Inter', sans-serif; background-color: var(--bg-color); color: var(--text-color); margin: 0; padding: 20px 16px 40px 16px; font-size: 14px; line-height: 1.5; -webkit-font-smoothing: antialiased; }
        
        .header { text-align: center; margin-bottom: 20px; }
        .header-title { font-size: 20px; font-weight: 800; margin-bottom: 4px; }
        .header-sub { font-size: 12px; color: var(--hint-color); }

        .ai-card { 
            background: var(--ai-bg); 
            border-left: 4px solid var(--ai-border); 
            border-radius: 16px; padding: 16px; margin-bottom: 20px; 
            box-shadow: 0 4px 12px rgba(0,122,255,0.1); 
        }
        .ai-title { font-size: 13px; font-weight: 700; color: var(--ai-border); margin-bottom: 6px; text-transform: uppercase; }
        .ai-content { font-size: 14px; color: var(--text-color); white-space: pre-line; }

        .market-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 20px; }
        .m-card { background-color: var(--card-bg); border-radius: 16px; padding: 12px; text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,0.04); cursor: pointer; transition: transform 0.1s; }
        .m-card:active { transform: scale(0.96); }
        .m-label { font-size: 11px; font-weight: 700; color: var(--hint-color); display: flex; align-items: center; justify-content: center; gap: 4px; }
        .m-val { font-size: 17px; font-weight: 800; margin: 4px 0; }
        .m-change { font-size: 12px; font-weight: 600; }
        .t-up { color: var(--up-color); } .t-down { color: var(--down-color); } .t-ref { color: var(--ref-color); }

        .section-card { background-color: var(--card-bg); border-radius: 16px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.04); padding: 0; }
        .p-item { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; border-bottom: 1px solid var(--border-color); }
        .p-item:last-child { border-bottom: none; }
        .p-sym { font-size: 16px; font-weight: 700; }
        .p-market { font-size: 11px; color: var(--hint-color); }
        .p-right { text-align: right; display: flex; flex-direction: column; align-items: flex-end; }
        .p-price-row { display: flex; align-items: center; gap: 8px; }
        .p-price { font-size: 16px; font-weight: 600; }
        .p-badge { font-size: 12px; font-weight: 700; padding: 2px 6px; border-radius: 4px; min-width: 50px; text-align: center; }
        .val-row { font-size: 11px; color: var(--hint-color); margin-top: 2px; display: flex; align-items: center; gap: 4px; }
        
        /* Màu badge */
        .bg-up { background: rgba(52,199,89,0.15); color: var(--up-color); }
        .bg-down { background: rgba(255,59,48,0.15); color: var(--down-color); }
        .bg-ref { background: rgba(255,204,0,0.15); color: #d4a017; } /* Màu vàng cố định vì ref không đổi */

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
        window.addEventListener('load', function() { 
            document.body.classList.add('loaded'); 
            // Cần resize lại Plotly khi mở modal.
            Telegram.WebApp.onEvent('themeChanged', applyChartTheme);
        });

        function applyChartTheme() {
            // Logic đổi màu Plotly (đã có trong _create_daily_chart)
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

        function openModal(id) {
            const modal = document.getElementById('modal-' + id);
            if (modal) {
                modal.classList.add('active');
                
                setTimeout(() => {
                    const chartDiv = modal.querySelector('.plotly-graph-div');
                    if (chartDiv && window.Plotly) {
                        Plotly.Plots.resize(chartDiv);
                        // Trigger theme update on resize as well
                        applyChartTheme(); 
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

# ... (Giữ nguyên các template khác: DIGEST_HTML_TEMPLATE, REPORT_HTML_TEMPLATE, v.v...)

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
            /* Light Mode (Mặc định) */
            --bg-body: var(--tg-theme-bg-color, #f8f9fa);
            --card-bg: var(--tg-theme-secondary-bg-color, #ffffff);
            --text-primary: var(--tg-theme-text-color, #111827);
            --text-secondary: var(--tg-theme-hint-color, #6b7280);
            --border-color: rgba(0,0,0,0.05);
            
            --up-color: #089981; 
            --down-color: #f23645; 
            --ref-color: #f0b90b;
            --accent-blue: var(--tg-theme-button-color, #2962ff);
            
            --radius: 16px; 
            --shadow: 0 4px 24px rgba(0,0,0,0.06);
            
            /* Track bar color */
            --track-bg: #e0e3eb;
            --thumb-border: #ffffff;
        }

        /* Dark Mode Overrides */
        @media (prefers-color-scheme: dark) {
            :root {
                --bg-body: #18181b; /* Zinc 900 */
                --card-bg: #27272a; /* Zinc 800 */
                --text-primary: #f4f4f5;
                --text-secondary: #a1a1aa;
                --border-color: rgba(255,255,255,0.1);
                --shadow: none; /* Dark mode ít dùng shadow */
                
                --track-bg: #3f3f46;
                --thumb-border: #27272a;
            }
        }
        
        /* Telegram Theme Overrides (Ưu tiên cao nhất) */
        body[data-theme="dark"] {
            --bg-body: var(--tg-theme-bg-color, #18181b);
            --card-bg: var(--tg-theme-secondary-bg-color, #27272a);
            --text-primary: var(--tg-theme-text-color, #f4f4f5);
            --text-secondary: var(--tg-theme-hint-color, #a1a1aa);
            --border-color: rgba(255,255,255,0.1);
            --shadow: none;
            --track-bg: #3f3f46;
            --thumb-border: var(--tg-theme-secondary-bg-color, #27272a);
        }

        * { box-sizing: border-box; }
        body { margin: 0; padding: 16px; font-family: 'Manrope', sans-serif; background-color: var(--bg-body); color: var(--text-primary); -webkit-font-smoothing: antialiased; transition: background-color 0.3s, color 0.3s; }
        
        .header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 16px; }
        .sym-name { font-size: 28px; font-weight: 800; margin: 0; line-height: 1; }
        .sym-sub { font-size: 12px; color: var(--text-secondary); font-weight: 600; margin-top: 4px; }
        .price-main { font-size: 28px; font-weight: 800; display: block; line-height: 1; }
        .price-change { font-size: 13px; font-weight: 700; padding: 4px 10px; border-radius: 8px; display: inline-flex; margin-top: 6px; }
        .btn-refresh { background: rgba(41, 98, 255, 0.1); border: none; cursor: pointer; color: var(--accent-blue); font-size: 14px; font-weight: 700; display: flex; align-items: center; gap: 4px; padding: 6px 12px; border-radius: 20px; transition: transform 0.1s; }
        .btn-refresh:active { transform: scale(0.95); }

        .t-up { color: var(--up-color); } .t-down { color: var(--down-color); }
        .bg-up { background: rgba(8, 153, 129, 0.12); color: var(--up-color); }
        .bg-down { background: rgba(242, 54, 69, 0.12); color: var(--down-color); }

        .card { background: var(--card-bg); border-radius: var(--radius); box-shadow: var(--shadow); margin-bottom: 16px; overflow: hidden; padding: 16px; border: 1px solid var(--border-color); }
        
        .chart-wrapper { height: 300px; width: 100%; margin-left: -5px; margin-right: -5px; }

        .flow-bar-wrapper { display: flex; height: 8px; width: 100%; border-radius: 4px; overflow: hidden; background: var(--track-bg); margin-bottom: 8px; }
        .fb-buy { background-color: var(--up-color); height: 100%; } .fb-sell { background-color: var(--down-color); height: 100%; }
        .flow-labels { display: flex; justify-content: space-between; font-size: 13px; font-weight: 700; margin-top: 8px; }
        .lbl-buy { color: var(--up-color); } .lbl-sell { color: var(--down-color); }
        .flow-sub { display: flex; justify-content: space-between; font-size: 11px; color: var(--text-secondary); font-weight: 500; }

        .range-track { height: 4px; background: var(--track-bg); border-radius: 2px; position: relative; margin: 14px 0; }
        .range-fill { position: absolute; height: 100%; border-radius: 2px; background: linear-gradient(90deg, var(--down-color), var(--up-color)); opacity: 0.3; width: 100%; }
        .range-thumb { position: absolute; top: -6px; width: 16px; height: 16px; background: #fff; border: 3px solid var(--text-primary); border-radius: 50%; transform: translateX(-50%); box-shadow: 0 2px 5px rgba(0,0,0,0.15); z-index: 2; }
        
        .range-meta { display: flex; justify-content: space-between; font-size: 12px; font-weight: 600; color: var(--text-primary); }
        .range-lbl { font-size: 10px; color: var(--text-secondary); font-weight: 500; text-transform: uppercase; }

        .metrics-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 20px; }
        .m-box { background: var(--card-bg); padding: 14px; border-radius: var(--radius); text-align: center; box-shadow: var(--shadow); border: 1px solid var(--border-color); }
        .m-lbl { font-size: 11px; font-weight: 700; color: var(--text-secondary); text-transform: uppercase; }
        .m-val { font-size: 18px; font-weight: 800; margin-top: 6px; color: var(--text-primary); letter-spacing: -0.5px; }
        .rsi-status { font-size: 11px; font-weight: 600; margin-top: 4px; }

        .btn-close { width: 100%; padding: 16px; background: var(--text-primary); color: var(--card-bg); border: none; border-radius: var(--radius); font-weight: 700; font-size: 15px; cursor: pointer; box-shadow: 0 4px 12px rgba(0,0,0,0.1); transition: opacity 0.2s; }
        .btn-close:active { opacity: 0.8; }
    </style>
</head>
<body>
    <div class="header">
        <div class="sym-box"><h1 class="sym-name">{{ symbol }}</h1><div style="display:flex;align-items:center;gap:8px;margin-top:4px;"><span class="sym-sub">HOSE • Intraday</span><button class="btn-refresh" onclick="window.location.reload()">🔄</button></div></div>
        <div style="text-align:right"><div class="price-main {{ cls_color }}">{{ current_price }}</div><div class="price-change {{ bg_cls }}">{{ change_str }}</div></div>
    </div>

    <div class="card" style="padding: 10px 0 0 0;">
        <div id="main-chart" class="chart-wrapper">{{ chart_html | safe }}</div>
    </div>

    <div class="card">
        <div style="font-size:12px;font-weight:700;text-transform:uppercase;color:var(--text-secondary);margin-bottom:12px;">⚡ Phân bổ dòng tiền (Chủ động)</div>
        <div class="flow-bar-wrapper"><div class="fb-buy" style="width: {{ buy_pct }}%"></div><div class="fb-sell" style="width: {{ sell_pct }}%"></div></div>
        <div class="flow-labels"><span class="lbl-buy">{{ buy_pct }}% Mua</span><span class="lbl-sell">{{ sell_pct }}% Bán</span></div>
        <div class="flow-sub"><span>{{ buy_vol_str }}</span><span>{{ sell_vol_str }}</span></div>
        <div style="margin-top: 16px; min-height: 200px;">{{ orderbook_html | safe }}</div>
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
    
    <script>
        Telegram.WebApp.expand();

        function applyTheme() {
            // 1. Lấy theme
            const tgScheme = Telegram.WebApp.colorScheme; 
            const sysScheme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
            const scheme = tgScheme || sysScheme;

            // 2. Set attribute cho CSS
            document.body.setAttribute('data-theme', scheme);

            // 3. Cập nhật Plotly
            const chartDivs = document.querySelectorAll('.plotly-graph-div');
            if (chartDivs.length > 0) {
                
                const isDark = (scheme === 'dark');
                const textColor = isDark ? '#e4e4e7' : '#111827'; 
                const tickColor = isDark ? '#9ca3af' : '#6b7280'; // Màu xám sáng hơn cho label trục
                const gridColor = isDark ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.06)';
                
                const update = {
                    'font.color': textColor,
                    
                    // Update Trục 1 (Giá)
                    'xaxis.gridcolor': gridColor,
                    'yaxis.gridcolor': gridColor,
                    'xaxis.tickfont.color': tickColor,
                    'yaxis.tickfont.color': tickColor,

                    // Update Trục 2 (Volume - Nơi hiển thị giờ) - QUAN TRỌNG
                    'xaxis2.gridcolor': gridColor,
                    'yaxis2.gridcolor': gridColor,
                    'xaxis2.tickfont.color': tickColor, // <-- Dòng này sẽ fix màu đen
                    'yaxis2.tickfont.color': tickColor
                };

                chartDivs.forEach(div => {
                    Plotly.relayout(div, update);
                });
            }
        }

        window.addEventListener('load', applyTheme);
        Telegram.WebApp.onEvent('themeChanged', applyTheme);
    </script>
</body>
</html>
"""

#-------------------------------------

ADMIN_MOBILE_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Admin StockBot</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.13.3/dist/cdn.min.js"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        body { font-family: 'Manrope', sans-serif; background-color: #F8FAFC; -webkit-tap-highlight-color: transparent; }
        [x-cloak] { display: none !important; }
        .no-scrollbar::-webkit-scrollbar { display: none; }
        .no-scrollbar { -ms-overflow-style: none; scrollbar-width: none; }
    </style>
</head>
<body class="text-slate-800" x-data="mobileApp()">

    <div x-show="isLoading" class="fixed inset-0 z-50 flex items-center justify-center bg-white/80 backdrop-blur-sm" x-cloak>
        <div class="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600"></div>
    </div>

    <div class="sticky top-0 z-20 bg-white/90 backdrop-blur-md border-b border-slate-100 pb-2">
        <div class="px-4 pt-4 pb-2 flex justify-between items-center">
            <h1 class="text-xl font-extrabold text-slate-800">StockBot<span class="text-blue-600">.Admin</span></h1>
            <div class="text-xs bg-blue-50 text-blue-600 px-2 py-1 rounded font-mono" x-text="'Admin: ' + adminId"></div>
        </div>
        
        <div class="px-4 mt-1">
            <div class="relative">
                <i class="fa-solid fa-magnifying-glass absolute left-3.5 top-1/2 -translate-y-1/2 text-slate-400"></i>
                <input type="text" x-model="searchQuery" placeholder="Tìm tên, ID, SĐT..." 
                       class="w-full pl-10 pr-4 py-2.5 bg-slate-100 rounded-xl text-sm font-medium focus:bg-white focus:ring-2 focus:ring-blue-500 outline-none transition-all shadow-sm">
            </div>
        </div>

        <div class="mt-3 pl-4 flex gap-2 overflow-x-auto no-scrollbar pb-1">
            <template x-for="tab in tabs">
                <button @click="filterStatus = tab.id"
                        class="whitespace-nowrap px-4 py-1.5 rounded-full text-xs font-bold transition-all border flex items-center gap-1"
                        :class="filterStatus === tab.id ? 'bg-slate-800 text-white scale-105 shadow-md' : 'bg-white text-slate-500 border-slate-200'">
                    <span x-text="tab.label"></span>
                    <span class="text-[10px] opacity-80" x-text="'(' + getCount(tab.id) + ')'"></span>
                </button>
            </template>
        </div>
    </div>

    <div class="p-4 pb-24 space-y-3 min-h-screen">
        
        <div class="flex gap-3 overflow-x-auto no-scrollbar pb-2 -mx-4 px-4 snap-x">
            <div class="snap-center shrink-0 w-36 p-3 bg-gradient-to-br from-blue-500 to-blue-600 rounded-2xl text-white shadow-lg shadow-blue-200">
                <div class="text-xs opacity-80 mb-1 font-medium">Tổng User</div>
                <div class="text-2xl font-bold" x-text="users.length"></div>
                <div class="text-[10px] bg-white/20 inline-block px-1.5 rounded mt-1">+ Active</div>
            </div>
            <div class="snap-center shrink-0 w-36 p-3 bg-white border border-slate-100 rounded-2xl shadow-sm">
                <div class="text-xs text-slate-400 mb-1 font-bold">Doanh thu</div>
                <div class="text-lg font-extrabold text-slate-800">{{ total_revenue }}</div>
                <div class="text-[10px] text-green-500 mt-1 font-bold">↑ từ bảng bot_orders</div>
            </div>
            <div class="snap-center shrink-0 w-36 p-3 bg-white border border-slate-100 rounded-2xl shadow-sm">
                <div class="text-xs text-slate-400 mb-1 font-bold">Sắp hết hạn</div>
                <div class="text-xl font-bold text-red-500" x-text="getCount('expiring')"></div>
            </div>
        </div>

        <template x-for="user in filteredUsers" :key="user.id">
            <div @click="openSheet(user)" class="bg-white p-4 rounded-2xl shadow-sm border border-slate-100 active:scale-[0.98] transition-transform cursor-pointer relative overflow-hidden group">
                <div x-show="user.is_pro && !user.is_expired" class="absolute left-0 top-0 bottom-0 w-1 bg-yellow-400"></div>
                <div class="flex justify-between items-start">
                    <div class="flex gap-3 w-full">
                        <div class="relative shrink-0">
                            <img :src="`https://ui-avatars.com/api/?name=${user.name}&background=random&size=64`" class="w-11 h-11 rounded-full object-cover border border-slate-100">
                            <div x-show="user.is_pro && !user.is_expired" class="absolute -bottom-1 -right-1 bg-yellow-400 text-white text-[8px] p-0.5 rounded-full border-2 border-white">
                                <i class="fa-solid fa-crown"></i>
                            </div>
                        </div>
                        <div class="flex-1 min-w-0">
                            <h3 class="font-bold text-sm truncate flex items-center gap-2">
                                <span x-text="user.name" :class="user.is_banned ? 'line-through text-red-500' : 'text-slate-800'"></span>
                                <span x-show="user.is_banned" class="text-[10px] bg-red-100 text-red-600 px-1.5 rounded">BANNED</span>
                            </h3>
                            <div class="flex items-center gap-2 mt-0.5">
                                <div class="text-xs text-slate-400 font-mono" x-text="user.id"></div>
                                <button @click.stop="copyId(user.id)" class="text-slate-300 hover:text-blue-500 transition active:scale-90 p-1">
                                    <i class="fa-regular fa-copy text-xs"></i>
                                </button>
                            </div>
                            <div class="flex gap-1.5 mt-2 flex-wrap">
                                <span class="px-2 py-0.5 rounded text-[10px] font-bold tracking-wide"
                                      :class="getStatusClass(user)" x-text="getStatusLabel(user)"></span>
                                <span x-show="user.days_left > 0 && user.days_left <= 3" 
                                      class="px-2 py-0.5 rounded text-[10px] font-bold bg-red-50 text-red-500 border border-red-100 flex items-center animate-pulse">
                                    <i class="fa-regular fa-clock mr-1"></i>Còn <span x-text="user.days_left"></span> ngày
                                </span>
                                <span x-show="user.has_used_trial && (!user.is_pro || user.is_expired) && (user.plan_name === 'trial' || !user.plan_name)" 
                                    class="px-2 py-0.5 rounded text-[10px] font-bold bg-purple-50 text-purple-600 border border-purple-100">
                                    <i class="fa-solid fa-flask mr-1"></i>Hết Trial
                                </span>

                                <span x-show="(!user.is_pro || user.is_expired) && user.plan_name === 'pro'" 
                                    class="px-2 py-0.5 rounded text-[10px] font-bold bg-orange-50 text-orange-600 border border-orange-100">
                                    <i class="fa-solid fa-gem mr-1"></i>Hết hạn Pro
                                </span>
                            </div>
                        </div>
                    </div>
                    <div class="flex items-center h-full"><i class="fa-solid fa-chevron-right text-xs text-slate-300"></i></div>
                </div>
            </div>
        </template>
        
        <div x-show="filteredUsers.length === 0" class="py-10 text-center" x-cloak>
            <div class="w-16 h-16 bg-slate-50 rounded-full flex items-center justify-center mx-auto mb-3">
                <i class="fa-solid fa-filter text-slate-300 text-xl"></i>
            </div>
            <p class="text-slate-400 text-sm">Không tìm thấy user nào.</p>
        </div>
    </div>

    <div x-show="sheetOpen" class="relative z-50" x-cloak>
        <div class="fixed inset-0 bg-black/60 backdrop-blur-sm transition-opacity duration-300"
             x-show="sheetOpen" x-transition.opacity @click="sheetOpen = false"></div>
             
        <div class="fixed bottom-0 left-0 right-0 bg-white rounded-t-3xl h-[85vh] flex flex-col shadow-2xl transform transition-transform duration-300"
             x-show="sheetOpen" x-transition:enter-start="translate-y-full" x-transition:enter-end="translate-y-0"
             x-transition:leave-start="translate-y-0" x-transition:leave-end="translate-y-full">
            
            <div class="w-full flex justify-center pt-3 pb-1" @click="sheetOpen = false"><div class="w-12 h-1.5 bg-slate-200 rounded-full"></div></div>
            
            <div class="px-6 py-3 flex justify-between items-center border-b border-slate-50 pb-4">
                <div>
                    <h2 class="text-xl font-bold text-slate-800 truncate max-w-[200px]" x-text="selectedUser?.name"></h2>
                    <div class="text-xs text-slate-400 font-mono" x-text="'ID: ' + selectedUser?.id"></div>
                </div>
                <button @click="sheetOpen = false" class="w-8 h-8 bg-slate-100 rounded-full flex items-center justify-center text-slate-500 active:bg-slate-200">
                    <i class="fa-solid fa-xmark"></i>
                </button>
            </div>

            <div class="flex-1 overflow-y-auto p-6 space-y-6 bg-[#F8FAFC]">
                <div class="bg-white p-5 rounded-2xl shadow-sm border border-slate-100">
                    <h3 class="text-xs font-bold text-slate-400 uppercase tracking-wide mb-4">Gói Cước</h3>
                    <div class="flex items-center gap-4 mb-5">
                        <div class="w-14 h-14 rounded-2xl flex items-center justify-center text-2xl shadow-sm border border-slate-50"
                             :class="selectedUser?.is_pro && !selectedUser?.is_expired ? 'bg-gradient-to-br from-yellow-400 to-yellow-600 text-white' : 'bg-slate-100 text-slate-400'">
                            <i class="fa-solid fa-crown"></i>
                        </div>
                        <div>
                            <div class="text-lg font-bold text-slate-800" 
                                 x-text="selectedUser?.is_pro ? (selectedUser?.is_expired ? 'ĐÃ HẾT HẠN' : 'GÓI PRO') : 'GÓI FREE'"></div>
                            <div class="text-sm" :class="selectedUser?.is_expired ? 'text-red-500 font-medium' : 'text-slate-500'">
                                <i class="fa-regular fa-calendar mr-1"></i>
                                <span x-text="selectedUser?.expiry_date ? 'Hết hạn: ' + formatDate(selectedUser.expiry_date) : 'Vô thời hạn'"></span>
                            </div>
                        </div>
                    </div>
                    <div class="grid grid-cols-2 gap-3">
                        <button class="py-3 bg-slate-900 text-white rounded-xl text-sm font-bold shadow-lg shadow-slate-200 active:scale-95 transition" @click="extendUser(30)">+30 Ngày</button>
                        <button class="py-3 bg-white border border-slate-200 text-slate-700 rounded-xl text-sm font-bold active:scale-95 transition hover:bg-slate-50" @click="extendUser(7)">+7 Ngày</button>
                        <button class="py-3 bg-orange-50 text-orange-600 border border-orange-100 rounded-xl text-sm font-bold active:scale-95 transition hover:bg-orange-100" @click="extendUser(-7)">-7 Ngày</button>
                        <button class="py-3 bg-red-50 text-red-600 border border-red-100 rounded-xl text-sm font-bold active:scale-95 transition hover:bg-red-100" @click="deactivateUser()"><i class="fa-solid fa-ban mr-1"></i> Hủy Gói</button>
                        <button x-show="!selectedUser?.is_banned" @click="toggleBan(true)"
                            class="w-full py-3 bg-red-50 text-red-600 border border-red-200 rounded-xl text-sm font-bold active:scale-95 transition flex items-center justify-center gap-2 hover:bg-red-100">
                        <i class="fa-solid fa-ban"></i> CHẶN USER
                    </button>
                    
                    <button x-show="selectedUser?.is_banned" @click="toggleBan(false)"
                            class="w-full py-3 bg-green-50 text-green-600 border border-green-200 rounded-xl text-sm font-bold active:scale-95 transition flex items-center justify-center gap-2 hover:bg-green-100 animate-pulse">
                        <i class="fa-solid fa-lock-open"></i> BỎ CHẶN
                    </button>
                    <div x-show="selectedUser?.is_banned" class="text-center text-[10px] text-red-500 mt-1 font-bold">⛔ User này đang bị chặn hoàn toàn khỏi hệ thống</div>
                    </div>
                </div>

                <div class="grid grid-cols-2 gap-3 mt-4">
                    <div class="bg-white p-3 rounded-xl shadow-sm border border-slate-100 flex items-center gap-3">
                        <div class="w-10 h-10 rounded-full flex shrink-0 items-center justify-center text-lg"
                             :class="selectedUser?.config?.vn30 ? 'bg-blue-100 text-blue-600' : 'bg-slate-100 text-slate-400'">
                            <i class="fa-solid fa-chart-line"></i>
                        </div>
                        <div class="min-w-0">
                            <div class="text-[10px] text-slate-400 font-bold uppercase truncate">VN30F1m</div>
                            <div class="text-sm font-bold text-slate-700" x-text="selectedUser?.config?.vn30 ? 'BẬT' : 'TẮT'"></div>
                        </div>
                    </div>

                    <div class="bg-white p-3 rounded-xl shadow-sm border border-slate-100 flex items-center gap-3">
                        <div class="w-10 h-10 rounded-full flex shrink-0 items-center justify-center text-lg"
                             :class="selectedUser?.config?.stock ? 'bg-purple-100 text-purple-600' : 'bg-slate-100 text-slate-400'">
                            <i class="fa-solid fa-bell"></i>
                        </div>
                        <div class="min-w-0">
                            <div class="text-[10px] text-slate-400 font-bold uppercase truncate">Cổ Phiếu</div>
                            <div class="text-sm font-bold text-slate-700" x-text="selectedUser?.config?.stock ? 'BẬT' : 'TẮT'"></div>
                        </div>
                    </div>
                </div>

                <div class="bg-white p-5 rounded-2xl shadow-sm border border-slate-100">
                    <h3 class="text-xs font-bold text-slate-400 uppercase tracking-wide mb-3 flex justify-between">
                        <span>Đang theo dõi</span>
                        <span class="bg-slate-100 text-slate-600 px-1.5 py-0.5 rounded text-[10px]" x-text="selectedUser?.watchlist ? selectedUser.watchlist.length : 0"></span>
                    </h3>
                    <div class="flex flex-wrap gap-2">
                        <template x-for="sym in selectedUser?.watchlist">
                            <span class="px-3 py-1.5 bg-blue-50 text-blue-700 border border-blue-100 rounded-lg text-sm font-bold" x-text="sym"></span>
                        </template>
                        <template x-if="!selectedUser?.watchlist || selectedUser?.watchlist.length === 0">
                            <span class="text-sm text-slate-400 italic w-full text-center py-2">Danh mục trống</span>
                        </template>
                    </div>
                </div>

                <div class="bg-white rounded-2xl shadow-sm border border-slate-100 overflow-hidden">
                    <div class="bg-slate-50 px-4 py-3 border-b border-slate-100 flex justify-between items-center">
                        <h3 class="text-xs font-bold text-slate-500 uppercase tracking-wide">Nhật ký hoạt động (10 lệnh)</h3>
                        <i class="fa-solid fa-list-ul text-slate-300 text-xs"></i>
                    </div>
                    <div class="max-h-48 overflow-y-auto">
                        <table class="w-full text-left text-sm">
                            <tbody class="divide-y divide-slate-50">
                                <template x-for="log in selectedUser?.logs">
                                    <tr class="hover:bg-slate-50 transition">
                                        <td class="px-4 py-2.5 font-mono text-xs text-blue-600 font-medium bg-blue-50/30 w-1/3" x-text="log.command"></td>
                                        <td class="px-4 py-2.5 text-slate-500 text-xs text-right" x-text="formatDate(log.used_at) + ' ' + new Date(log.used_at).toLocaleTimeString('vi-VN', {hour:'2-digit', minute:'2-digit'})"></td>
                                    </tr>
                                </template>
                                <template x-if="!selectedUser?.logs || selectedUser?.logs.length === 0">
                                    <tr><td colspan="2" class="px-4 py-6 text-center text-slate-400 text-xs italic">Chưa có hoạt động nào</td></tr>
                                </template>
                            </tbody>
                        </table>
                    </div>
                </div>

                <div class="bg-white p-4 rounded-2xl shadow-sm border border-slate-100">
                    <div class="flex justify-between items-center mb-2">
                        <h3 class="text-xs font-bold text-slate-400 uppercase tracking-wide">Ghi chú Admin</h3>
                        <button @click="saveNote()" x-show="isNoteDirty" 
                                class="text-[10px] bg-blue-600 text-white px-2 py-1 rounded font-bold animate-pulse">
                            Lưu
                        </button>
                    </div>
                    <textarea x-model="currentNote" @input="isNoteDirty = true"
                              class="w-full bg-yellow-50 border border-yellow-100 rounded-xl p-3 text-sm text-slate-700 focus:ring-2 focus:ring-yellow-400 outline-none resize-none"
                              rows="2" placeholder="Nhập ghi chú..."></textarea>
                </div>

                <div>
                    <h3 class="text-xs font-bold text-slate-400 uppercase tracking-wide mb-3 px-1">Giao dịch gần đây</h3>
                    <div class="space-y-2">
                        <template x-for="order in selectedUser?.orders">
                            <div class="bg-white p-3 rounded-xl border border-slate-100 flex justify-between items-center"
                                 :class="order.status === 'PENDING' ? 'opacity-70' : ''">
                                <div class="flex items-center gap-3">
                                    <div class="w-8 h-8 rounded-full flex items-center justify-center text-xs"
                                         :class="order.status === 'PAID' ? 'bg-green-100 text-green-600' : 'bg-yellow-100 text-yellow-600'">
                                        <i :class="order.status === 'PAID' ? 'fa-solid fa-check' : 'fa-solid fa-hourglass'"></i>
                                    </div>
                                    <div>
                                        <div class="text-sm font-bold" x-text="formatMoney(order.amount)"></div>
                                        <div class="text-[10px] text-slate-400" x-text="formatDate(order.created_at)"></div>
                                    </div>
                                </div>
                                <span class="text-xs font-bold px-2 py-1 rounded"
                                      :class="order.status === 'PAID' ? 'text-green-600 bg-green-50' : 'text-yellow-600 bg-yellow-50'"
                                      x-text="order.status"></span>
                            </div>
                        </template>
                        <template x-if="!selectedUser?.orders || selectedUser?.orders.length === 0">
                            <div class="text-center text-sm text-slate-400 italic py-4">Chưa có giao dịch nào.</div>
                        </template>
                    </div>
                </div>
                
                <div class="h-6"></div> </div>

            <div class="p-4 border-t border-slate-100 bg-white pb-8 grid grid-cols-2 gap-3">
                
                <button @click="sendMessage()" 
                        class="w-full py-2.5 rounded-xl bg-blue-600 text-white text-xs font-bold shadow-md shadow-blue-200 active:scale-95 transition flex items-center justify-center gap-1.5">
                    <i class="fa-brands fa-telegram text-sm"></i> BOT CHAT
                </button>

                <button @click="requestContact(selectedUser)" 
                        class="w-full py-2.5 rounded-xl bg-white border border-slate-200 text-slate-700 text-xs font-bold active:scale-95 transition flex items-center justify-center gap-1.5 hover:bg-slate-50">
                    <i class="fa-regular fa-comments text-sm"></i> ADMIN CHAT
                </button>
                
            </div>
        </div>
    </div>

    <div x-show="toast.visible" x-cloak 
         class="fixed top-4 left-1/2 -translate-x-1/2 bg-slate-800/90 backdrop-blur text-white px-4 py-2.5 rounded-full text-sm font-medium shadow-xl z-[60] flex items-center gap-2 transition-all"
         x-transition:enter="transform -translate-y-10 opacity-0"
         x-transition:enter-end="transform translate-y-0 opacity-100"
         x-transition:leave="transform -translate-y-10 opacity-0">
        <i class="fa-solid fa-circle-check text-green-400"></i>
        <span x-text="toast.message"></span>
    </div>

    <script>
        function mobileApp() {
            return {
                isLoading: false,
                searchQuery: '',
                filterStatus: 'all',
                sheetOpen: false,
                selectedUser: null,
                currentNote: '',
                isNoteDirty: false,
                toast: { visible: false, message: '' },
                users: {{ initial_data | safe }},
                adminId: '{{ admin_id }}',

                tabs: [
                    { id: 'all', label: 'Tất cả' },
                    { id: 'pro', label: 'Pro' },
                    { id: 'churned', label: 'Tiềm năng' },
                    { id: 'expiring', label: 'Sắp hết' },
                    { id: 'free', label: 'Free' }
                ],

                getCount(tabId) {
                    if (tabId === 'all') return this.users.length;
                    if (tabId === 'pro') return this.users.filter(u => u.is_pro && !u.is_expired).length;
                    
                    // [MỚI] Đếm số user đã dùng Trial nhưng giờ không phải Pro
                    if (tabId === 'churned') return this.users.filter(u => u.has_used_trial && (!u.is_pro || u.is_expired)).length;
                    
                    if (tabId === 'expiring') return this.users.filter(u => u.is_pro && !u.is_expired && u.days_left <= 3).length;
                    if (tabId === 'free') return this.users.filter(u => !u.is_pro).length;
                    return 0;
                },
                get filteredUsers() {
                    return this.users.filter(user => {
                        const s = this.searchQuery.toLowerCase();
                        const uName = user.name ? user.name.toLowerCase() : '';
                        const uId = String(user.id);
                        
                        // Search logic
                        if (!(!s || uId.includes(s) || uName.includes(s))) return false;
                        
                        // Tab logic
                        if (this.filterStatus === 'pro') return user.is_pro && !user.is_expired;
                        
                        // [MỚI] Logic lọc Tiềm năng
                        if (this.filterStatus === 'churned') {
                            const isExpired = !user.is_pro || user.is_expired;
                            // Lấy: (Đã dùng trial và hết hạn) HOẶC (Là gói Pro và hết hạn)
                            return isExpired && (user.has_used_trial || user.plan_name === 'pro');
                        }                        
                        if (this.filterStatus === 'expiring') return user.is_pro && !user.is_expired && user.days_left <= 3;
                        if (this.filterStatus === 'free') return !user.is_pro;
                        
                        return true;
                    });
                },

                async sendMessage() {
                    const msg = prompt(`Gửi tin nhắn cho ${this.selectedUser.name}:`);
                    if (!msg) return;
                    
                    this.isLoading = true;
                    try {
                        const res = await fetch('/api/admin/user/message', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({ admin_id: this.adminId, target_id: this.selectedUser.id, text: msg })
                        });
                        const text = await res.text();
                        let result; try { result = JSON.parse(text); } catch { throw new Error("Server trả về HTML lỗi."); }

                        if (result.ok) this.showToast('📩 Đã gửi tin nhắn!');
                        else alert('❌ Lỗi: ' + result.message);
                    } catch (e) { alert('❌ Lỗi mạng: ' + e.message); } 
                    finally { this.isLoading = false; }
                },

                async toggleBan(shouldBan) {
                    const action = shouldBan ? 'ban' : 'unban';
                    const text = shouldBan ? 'CHẶN' : 'BỎ CHẶN';
                    
                    if (!confirm(`⚠️ Xác nhận ${text} user ${this.selectedUser.name}?\n(Hành động này có hiệu lực ngay lập tức)`)) return;
                    
                    this.isLoading = true;
                    try {
                        const res = await fetch('/api/admin/user/ban', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({ 
                                admin_id: this.adminId, 
                                target_id: this.selectedUser.id, 
                                action: action 
                            })
                        });
                        const result = await res.json();
                        if (result.ok) {
                            this.selectedUser.is_banned = shouldBan; // Cập nhật UI ngay
                            this.showToast(`✅ Đã ${text} thành công!`);
                        } else {
                            alert('❌ Lỗi: ' + result.message);
                        }
                    } catch (e) { alert('❌ Lỗi mạng: ' + e.message); } 
                    finally { this.isLoading = false; }
                },

                // Hàm xử lý mở chat thông minh
                async requestContact(user) {
                    this.isLoading = true;
                    try {
                        // 1. Gọi API
                        await fetch('/api/admin/user/contact', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({ 
                                admin_id: this.adminId, 
                                target_id: user.id,
                                target_name: user.name,
                                username: user.username 
                            })
                        });
                        
                        // 2. Đóng Web App (Fix lỗi 'Telegram is not defined')
                        // Kiểm tra xem object Telegram có tồn tại không trước khi gọi
                        if (window.Telegram && window.Telegram.WebApp) {
                            window.Telegram.WebApp.close();
                        } else {
                            console.warn("Không tìm thấy Telegram SDK. Đang chạy trên trình duyệt?");
                            alert("✅ Đã gửi link chat về bot! Bạn hãy kiểm tra tin nhắn.");
                        }
                        
                    } catch (e) {
                        alert('Lỗi kết nối: ' + e.message);
                    } finally {
                        this.isLoading = false;
                    }
                },

                async extendUser(days) {
                    const actionName = days > 0 ? `Cộng thêm ${days} ngày` : `TRỪ ĐI ${Math.abs(days)} ngày`;
                    if (!confirm(`Xác nhận: ${actionName} cho ${this.selectedUser.name}?`)) return;
                    this.callApi('/api/admin/user/extend', { days: days }, '✅ Đã cập nhật!');
                },

                async deactivateUser() {
                    if (!confirm(`⚠️ NGUY HIỂM: NGƯNG KÍCH HOẠT gói Pro của ${this.selectedUser.name}?`)) return;
                    this.callApi('/api/admin/user/deactivate', {}, '🚫 Đã hủy gói thành công!');
                },

                async callApi(url, body, successMsg) {
                    this.isLoading = true;
                    try {
                        const res = await fetch(url, {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({ admin_id: this.adminId, target_id: this.selectedUser.id, ...body })
                        });
                        const text = await res.text();
                        let result; try { result = JSON.parse(text); } catch { throw new Error("Server lỗi HTML."); }

                        if (result.ok) {
                            this.showToast(successMsg);
                            this.sheetOpen = false;
                        } else {
                            alert('❌ Lỗi: ' + result.message);
                        }
                    } catch (e) { alert('❌ Lỗi mạng: ' + e.message); } 
                    finally { this.isLoading = false; }
                },
                async saveNote() {
                    if (!this.selectedUser) return;
                    this.isLoading = true;
                    try {
                        const res = await fetch('/api/admin/user/note', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({ 
                                admin_id: this.adminId, 
                                target_id: this.selectedUser.id, 
                                note: this.currentNote 
                            })
                        });
                        const result = await res.json();
                        if (result.ok) {
                            this.selectedUser.admin_note = this.currentNote;
                            this.isNoteDirty = false;
                            this.showToast('✅ Đã lưu ghi chú!');
                        } else {
                            alert('❌ Lỗi: ' + result.message);
                        }
                    } catch (e) { alert('❌ Lỗi mạng: ' + e.message); } 
                    finally { this.isLoading = false; }
                },
                copyId(id) { navigator.clipboard.writeText(id); this.showToast('Đã sao chép ID: ' + id); },
                showToast(msg) { this.toast.message = msg; this.toast.visible = true; setTimeout(() => this.toast.visible = false, 2500); },
                openSheet(user) { 
                    this.selectedUser = user; 
                    this.currentNote = user.admin_note || ''; 
                    this.isNoteDirty = false;
                    this.sheetOpen = true; 
                },
                getStatusLabel(user) { return !user.is_pro ? 'FREE' : (user.is_expired ? 'EXPIRED' : 'PRO'); },
                getStatusClass(user) { return !user.is_pro ? 'bg-slate-100 text-slate-500' : (user.is_expired ? 'bg-red-100 text-red-600' : 'bg-yellow-100 text-yellow-700'); },
                formatDate(isoStr) { if (!isoStr) return '—'; try { return new Date(isoStr).toLocaleDateString('vi-VN'); } catch { return isoStr; } },
                formatMoney(num) { return new Intl.NumberFormat('vi-VN', { style: 'currency', currency: 'VND' }).format(num); }
            }
        }
    </script>
</body>
</html>
"""


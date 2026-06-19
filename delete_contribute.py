import re

with open('gateway.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Delete api_admin_list_pending
content = re.sub(r'@flask_app\.route\("/api/admin/contributions/pending", methods=\["GET"\]\)[\s\S]*?except Exception as e:[\s\S]*?return jsonify\(\{"ok": False, "message": str\(e\)\}\), 500', '', content)

# Delete api_admin_moderate
content = re.sub(r'@flask_app\.route\("/api/admin/contributions/moderate", methods=\["POST"\]\)[\s\S]*?except Exception as e:[\s\S]*?return jsonify\(\{"ok": False, "message": str\(e\)\}\), 500', '', content)

# Delete api_user_contributions (no decorator)
content = re.sub(r'async def api_user_contributions\(\):[\s\S]*?except Exception as e:[\s\S]*?return jsonify\(\{"ok": False, "message": str\(e\)\}\), 500', '', content)

with open('gateway.py', 'w', encoding='utf-8') as f:
    f.write(content)

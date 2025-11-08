# Stub file cho vnai.scope.profile

class Inspector:
    def fingerprint(self):
        # vnstock chỉ cần gọi để tạo ID máy, nên ta giả vờ trả chuỗi tĩnh
        return "dummy_fingerprint_1234"

# Tạo biến toàn cục mà vnstock mong đợi
inspector = Inspector()

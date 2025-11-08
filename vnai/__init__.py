# Stub module vnai để bypass các lỗi từ vnstock

def setup(*args, **kwargs):
    # vnstock gọi hàm này trong core.__init__
    return None

def optimize_execution(*args, **kwargs):
    # vnstock dùng hàm này như decorator (@optimize_execution)
    # nên ta trả về một decorator "trống" để không làm gì cả
    def decorator(func):
        return func
    return decorator

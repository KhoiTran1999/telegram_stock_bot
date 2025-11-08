# vnai stub module – bypass all vnstock dependencies

def setup(*args, **kwargs):
    return None

def optimize_execution(*args, **kwargs):
    # giả lập decorator không làm gì
    def decorator(func):
        return func
    return decorator

def accept_license_terms(*args, **kwargs):
    # vnstock gọi để xác nhận điều khoản, ta chỉ giả lập
    print("✅ VNStock license terms accepted (stubbed).")
    return True

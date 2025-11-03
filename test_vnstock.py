from vnstock import Trading
# Khởi tạo đối tượng Trading và lấy bảng giá HPG
trading = Trading(source='VCI')
# trading.price_board(['HPG'])
print(trading.price_board(['HPG']))
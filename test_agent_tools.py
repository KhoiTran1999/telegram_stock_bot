import unittest
import asyncio
import json
from unittest.mock import patch, MagicMock

# Import các thành phần cần test
from agent_tools import ToolRegistry, tool_get_fundamentals, tool_get_company_profile

# Mock class cho kết quả của fetch_manual_pe_pb
class MockManualValuation:
    def __init__(self, pe, pb, eps_ttm, bvps):
        self.pe = pe
        self.pb = pb
        self.eps_ttm = eps_ttm
        self.bvps = bvps
        self.computed_at = "2026-06-23T12:00:00Z"

class TestAgentTools(unittest.IsolatedAsyncioTestCase):
    
    async def test_registry_wrapper_catches_error(self):
        """Test decorator ToolRegistry bắt lỗi và trả về chuỗi JSON chứa error."""
        registry = ToolRegistry()
        
        @registry.register(name="test_tool_error")
        async def mock_tool_error():
            raise ValueError("Lỗi thử nghiệm")
            
        result = await registry.tools["test_tool_error"]()
        
        # Kết quả phải là JSON string
        result_dict = json.loads(result)
        self.assertIn("error", result_dict)
        self.assertTrue("Lỗi thử nghiệm" in result_dict["error"])

    async def test_registry_wrapper_keyerror_data(self):
        """Test decorator ToolRegistry định dạng riêng cho KeyError('data')."""
        registry = ToolRegistry()
        
        @registry.register(name="test_tool_keyerror")
        async def mock_tool_keyerror():
            raise KeyError("data")
            
        result = await registry.tools["test_tool_keyerror"]()
        result_dict = json.loads(result)
        
        self.assertIn("error", result_dict)
        self.assertTrue("Lấy dữ liệu từ API vnstock thất bại" in result_dict["error"])

    @patch("agent_tools.fetch_manual_pe_pb")
    async def test_get_fundamentals_with_none_values(self, mock_fetch):
        """Test tool_get_fundamentals không bị crash khi các chỉ số là None."""
        # Giả lập fetch_manual_pe_pb trả về đối tượng có None
        mock_fetch.return_value = MockManualValuation(pe=None, pb=1.5, eps_ttm=None, bvps=15000)
        
        # Gọi tool
        result = await tool_get_fundamentals("HPG")
        
        # Kiểm tra kết quả có chứa N/A cho PE và EPS, và format đúng PB và BVPS
        self.assertIn("- P/E: N/A", result)
        self.assertIn("- P/B: 1.50x", result)
        self.assertIn("- EPS (TTM): N/A", result)
        self.assertIn("- Book Value: 15,000 VND", result)

    @patch("agent_tools.get_profile_from_redis")
    @patch("agent_tools.Company")
    async def test_get_company_profile_api_keyerror(self, mock_company_class, mock_redis):
        """Test tool_get_company_profile không crash khi Vnstock ném KeyError('data')."""
        # Giả lập redis trả về rỗng
        mock_redis.return_value = None
        
        # Giả lập Company(symbol, source='VCI').overview ném KeyError('data')
        mock_company_instance = MagicMock()
        mock_company_instance.overview = MagicMock(side_effect=KeyError("data"))
        mock_company_class.return_value = mock_company_instance
        
        result = await tool_get_company_profile("HPG")
        
        # Dù API ném KeyError, hàm vẫn phải chạy thành công và trả về JSON chứa lỗi hoặc empty nếu AI redis cũng empty
        result_dict = json.loads(result)
        self.assertIn("error", result_dict)
        self.assertTrue("Không tìm thấy thông tin" in result_dict["error"])

if __name__ == '__main__':
    unittest.main()

# Tool tạo phiếu lương PDF

Tool lấy `../x.pdf` làm mẫu, thay nội dung ở hai hàng **Employee Name** và
**Employee ID**, sau đó lưu PDF đánh số tăng dần vào thư mục `docs`.

## Cách dùng nhanh

1. Mở `danh_sach.txt` bằng Notepad và lưu ở dạng UTF-8.
2. Nhập mỗi người trên một dòng. Ví dụ:

   ```text
   Nguyễn Văn An
   Sok Dara | random
   Jane Doe | BIS-2025-3456
   ```

   Chỉ ghi họ tên (hoặc ghi `| random`) thì Employee ID được tạo ngẫu nhiên
   theo dạng `BIS-2025-####`. Có thể ghi ID cụ thể sau dấu `|` nếu cần.

3. Nhấp đúp `run.bat`.

Kết quả:

- Các PDF nằm ở thư mục cha `docs`: `1.pdf`, `2.pdf`, ... Nếu đã có PDF
  đánh số thì tool bắt đầu từ số lớn nhất cộng 1.
- `docs/ketqua.txt` được nối thêm từng dòng theo định dạng:

  ```text
  Họ tên|Campuchia|BELTEI International School, Phnom Penh|đường dẫn PDF|THCS|Giáo viên|Lớp 9|Bảng lương
  ```

Tool không xóa dữ liệu trong file đầu vào. Nếu chạy lại mà giữ nguyên danh
sách, các dòng cũ sẽ được tạo lại thành PDF mới. Hãy thay hoặc xóa các dòng đã
xử lý trước lần chạy tiếp theo.

## Tùy chọn dòng lệnh

Có thể chỉ định một file TXT khác:

```powershell
python gen_docs.py --input "D:\duong-dan\ten.txt"
```

Xem toàn bộ tùy chọn:

```powershell
python gen_docs.py --help
```

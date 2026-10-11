# Giọng AI

Ứng dụng Go chạy trên máy, mở giao diện bằng trình duyệt. Cần Go 1.24 trở lên.

```bash
go run .
```

Mặc định mở `http://127.0.0.1:17860`. Nếu cổng đã được ứng dụng khác dùng, xem địa chỉ được in trong Terminal. Sau khi sửa code hoặc giao diện, thoát ứng dụng rồi chạy lại.

## Thiết lập cảm xúc F5-TTS

1. Trong **Tạo audio**, chọn **F5-TTS**. Nếu chưa cài, ứng dụng hướng dẫn cài VieNeu trước, rồi cài F5-TTS. Chờ mô hình sẵn sàng.
2. Tạo giọng gốc trong **Giọng cá nhân**, hoặc chọn một giọng cá nhân đã có.
3. Bấm **Thiết lập cảm xúc** ở mục **Thẻ biểu cảm**, hoặc bấm **Cảm xúc** cạnh giọng trong thư viện.
4. Chọn giọng gốc và sắc thái cần thiết lập.
5. Chọn một mẫu đã lưu hoặc bấm **Tải file hoặc ghi âm mẫu mới**. Sau khi lưu mẫu mới, ứng dụng đưa bạn trở lại setup.
6. Nghe lại mẫu. Bấm **Tự nhận dạng** hoặc nhập đúng lời nói trong mẫu; sửa từ sai rồi xác nhận lời thoại khớp và mẫu là của cùng người nói.
7. Có thể **Nghe thử sắc thái** trước khi **Lưu cảm xúc**. Lặp lại cho các sắc thái khác.

Các nút **ⓘ** mở popup giải thích việc chuẩn bị mẫu, cùng người nói, lời thoại và giới hạn chất lượng. Có popup **Hướng dẫn thiết lập từ đầu** trong bước đầu tiên.

## Dùng thẻ trong nội dung

Chọn F5-TTS và giọng cá nhân gốc đã thiết lập, rồi chèn thẻ từ menu hoặc viết trực tiếp:

```text
[vui] Cuối cùng chúng ta cũng làm được!
[buon] Tôi vẫn nhớ những ngày ở bên nhau.
[macdinh] Câu chuyện của chúng ta tiếp tục.
```

Các thẻ: `[vui]`, `[buon]`, `[gian]`, `[haohung]`, `[thitham]`, `[kechuyen]`. Thẻ áp dụng cho phần sau nó đến khi gặp thẻ tiếp theo. `[macdinh]` quay về mẫu giọng gốc. Thẻ không được đưa vào lời đọc. Mẫu chưa thiết lập hoặc đã bị xóa sẽ báo lỗi.

F5-TTS lấy sắc thái từ audio tham chiếu; nhãn “vui” tự nó không biến mẫu trung tính thành giọng vui. Các mẫu nên là của cùng người, rõ tiếng, khoảng 5–10 giây, kết thúc trọn câu và có chút yên lặng ở cuối. File tải lên hiện lấy tối đa 12 giây đầu. Lời thoại phải khớp chính đoạn audio được lưu. Chỉ dùng giọng của mình hoặc giọng đã được cho phép sử dụng.

Các liên kết cảm xúc nằm trong metadata của giọng gốc tại thư mục `engine/voices`. Bỏ liên kết không xóa file mẫu. Hiệu quả cảm xúc tùy mẫu, nội dung và mô hình; nên nghe thử trước.

VieNeu có các thẻ thử nghiệm riêng: `[cười]`, `[thở dài]`, `[hắng giọng]`. Những thẻ này không dùng với F5-TTS hoặc Microsoft.

## Kiểm tra code

```bash
go test ./...
python3 -m unittest discover -s engine -p '*_test.py'
```

Các test Python không cần tải mô hình. Kiểm tra chất lượng cảm xúc cần mẫu audio thực tế.

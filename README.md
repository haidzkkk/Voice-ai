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

## Cài F5 trên Windows

Bản CPU được chọn mặc định. Chỉ bật NVIDIA khi cần GPU; thư viện CUDA lớn hơn.
PyTorch và torchaudio được khóa phiên bản/backend trong cả bước cài F5 để tránh tải thêm phiên bản khác.
Thanh tiến độ khi cài thư viện hiển thị phần trăm **số gói tải xong**, không phải phần trăm dung lượng toàn bộ cài đặt. Tổng số gói có thể tăng khi công cụ phát hiện thêm phụ thuộc. Dòng trạng thái hiển thị log mới nhất và thời gian không có log mới.
Khi tải model, thanh tiến độ hiển thị số byte đã tải / tổng và phần trăm theo dung lượng.
Luồng cài có giới hạn 45 phút; khi lỗi, cache được giữ để lần thử sau có thể dùng lại. Không tự chuyển NVIDIA sang CPU rồi tải thêm bộ thư viện thứ hai.

## Chọn thư mục lưu và chuyển sang ổ khác

Mở **Cài đặt**: chọn riêng thư mục model/thư viện, audio xuất và giọng cá nhân bằng nút **Chọn thư mục…**, hoặc nhập đường dẫn tuyệt đối. Bấm **Lưu cài đặt** rồi thoát/mở lại app.

Nếu đã có dữ liệu, bật **Chuyển dữ liệu đã có sang nơi mới** trước khi lưu. App sao chép model/cache, audio và giọng, hiển thị tiến độ theo dung lượng, kiểm tra bản sao rồi mới lưu đường dẫn. Bản cũ được giữ nguyên; có thể dọn thủ công sau khi kiểm tra bản mới chạy tốt. Không tắt app hoặc rút ổ đĩa khi chuyển. File đích cùng tên nhưng khác nội dung sẽ báo lỗi thay vì ghi đè.

Môi trường Python và marker cài đặt không được chuyển vì không thể di chuyển đáng tin cậy. Tại thư mục mới cần cài lại thư viện AI; model đã chuyển sẽ được dùng lại. Cache thư viện được sao chép khi có thể, nhưng một số thư viện vẫn cần tải lại. Nếu muốn giữ nguyên giọng/audio ở vị trí cũ, nhập chính đường dẫn hiện tại cho chúng.

Cài đặt lưu trong thư mục cấu hình người dùng `GiongAI/storage.json`. Biến môi trường `GIONGAI_ENGINE_DIR` và `GIONGAI_OUT` được ưu tiên hơn cài đặt; cần gỡ chúng trước khi dùng chức năng chuyển.

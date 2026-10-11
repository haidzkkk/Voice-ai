package main

import (
	"strings"
	"testing"
)

func TestSplit(t *testing.T) {
	var b strings.Builder
	for i := 0; i < 300; i++ {
		b.WriteString("Đây là câu số một trăm, một câu tiếng Việt dài để kiểm tra chia đoạn & <ký tự>. ")
	}
	b.WriteString(strings.Repeat("中文没有空格", 2000))
	in := strings.TrimSpace(b.String())
	parts := splitText(in, maxChunkBytes)
	joined := strings.Join(parts, "")
	if strings.ReplaceAll(joined, " ", "") != strings.ReplaceAll(in, " ", "") {
		t.Fatal("mất nội dung khi chia đoạn")
	}
	for i, p := range parts {
		if len(xmlEscape(p)) > maxChunkBytes {
			t.Fatalf("đoạn %d quá dài: %d", i, len(xmlEscape(p)))
		}
		if i < 8 && !strings.HasSuffix(p, ".") {
			t.Fatalf("đoạn %d không kết thúc ở cuối câu: ...%q", i, p[len(p)-20:])
		}
	}
	t.Logf("%d đoạn", len(parts))
}

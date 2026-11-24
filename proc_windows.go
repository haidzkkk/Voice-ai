//go:build windows

package main

import (
	"os"
	"os/exec"
	"syscall"
)

// hideWindow không hiện cửa sổ đen khi chạy tiến trình con trên Windows.
func hideWindow(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000} // CREATE_NO_WINDOW
}

// registerURLScheme đăng ký giao thức giongai:// vào registry (HKCU) để trang web
// có thể mở engine bằng một cú bấm ở lần sau (không cần tự tìm & chạy exe).
func registerURLScheme() {
	exe, err := os.Executable()
	if err != nil {
		return
	}
	const base = `HKCU\Software\Classes\giongai`
	run := func(args ...string) {
		cmd := exec.Command("reg", args...)
		hideWindow(cmd)
		cmd.Run()
	}
	run("add", base, "/ve", "/d", "URL:Giong AI", "/f")
	run("add", base, "/v", "URL Protocol", "/d", "", "/f")
	run("add", base+`\shell\open\command`, "/ve", "/d", `"`+exe+`" "%1"`, "/f")
}

//go:build !windows

package main

import "os/exec"

func hideWindow(cmd *exec.Cmd) {}

// registerURLScheme chỉ có tác dụng trên Windows; hệ khác bỏ qua.
func registerURLScheme() {}

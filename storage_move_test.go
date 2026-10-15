package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestStorageCopyKeepsOriginalAndRejectsConflict(t *testing.T) {
	root := t.TempDir()
	src := filepath.Join(root, "old")
	dst := filepath.Join(root, "new")
	os.MkdirAll(src, 0755)
	os.MkdirAll(dst, 0755)
	file := filepath.Join(src, "voice.wav")
	if err := os.WriteFile(file, []byte("recording"), 0600); err != nil {
		t.Fatal(err)
	}
	files, err := planStorageCopy(src, dst, false)
	if err != nil {
		t.Fatal(err)
	}
	for _, f := range files {
		if err = copyStorageFile(f); err != nil {
			t.Fatal(err)
		}
	}
	for _, p := range []string{file, filepath.Join(dst, "voice.wav")} {
		b, err := os.ReadFile(p)
		if err != nil || string(b) != "recording" {
			t.Fatal("copy did not preserve source/destination")
		}
	}
	os.WriteFile(filepath.Join(dst, "voice.wav"), []byte("different"), 0600)
	if _, err = planStorageCopy(src, dst, false); err == nil {
		t.Fatal("conflicting destination accepted")
	}
}
func TestStorageCopySkipsNonPortableRuntime(t *testing.T) {
	root := t.TempDir()
	src := filepath.Join(root, "old")
	dst := filepath.Join(root, "new")
	os.MkdirAll(dst, 0755)
	for _, dir := range []string{"venv-f5", "python", "models", "voices"} {
		os.MkdirAll(filepath.Join(src, dir), 0755)
		os.WriteFile(filepath.Join(src, dir, "file"), []byte("data"), 0600)
	}
	files, err := planStorageCopy(src, dst, true)
	if err != nil {
		t.Fatal(err)
	}
	if len(files) != 1 || filepath.Base(filepath.Dir(files[0].source)) != "models" {
		t.Fatalf("incorrect migration plan: %+v", files)
	}
	nested := filepath.Join(src, "nested")
	os.MkdirAll(nested, 0755)
	if _, err = planStorageCopy(src, nested, true); err == nil {
		t.Fatal("recursive destination accepted")
	}
}

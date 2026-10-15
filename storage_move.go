package main

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
)

var storageMove struct {
	sync.Mutex
	Running  bool   `json:"running"`
	Done     int64  `json:"done"`
	Total    int64  `json:"total"`
	File     string `json:"file"`
	Error    string `json:"error"`
	Complete bool   `json:"complete"`
}

func storageMoving() bool { storageMove.Lock(); defer storageMove.Unlock(); return storageMove.Running }

type storageCopyFile struct {
	source, target string
	size           int64
	mode           os.FileMode
}

func fileDigest(path string) ([32]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return [32]byte{}, err
	}
	defer f.Close()
	h := sha256.New()
	_, err = io.Copy(h, f)
	var sum [32]byte
	copy(sum[:], h.Sum(nil))
	return sum, err
}
func planStorageCopy(source, target string, engine bool) ([]storageCopyFile, error) {
	if filepath.Clean(source) == filepath.Clean(target) {
		return nil, nil
	}
	source, err := filepath.EvalSymlinks(source)
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	target, err = filepath.EvalSymlinks(target)
	if err != nil {
		return nil, err
	}
	rel, _ := filepath.Rel(source, target)
	back, _ := filepath.Rel(target, source)
	if rel == "." || back == "." {
		return nil, nil
	}
	if (rel != ".." && !strings.HasPrefix(rel, ".."+string(os.PathSeparator))) || (back != ".." && !strings.HasPrefix(back, ".."+string(os.PathSeparator))) {
		return nil, fmt.Errorf("thư mục mới phải nằm ngoài thư mục cũ: %s", target)
	}
	var files []storageCopyFile
	err = filepath.WalkDir(source, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		r, _ := filepath.Rel(source, path)
		first := strings.Split(r, string(os.PathSeparator))[0]
		if engine && (first == "venv" || first == "venv-f5" || first == "voices" || first == "python" || first == "installed.json" || first == "installed-f5.json" || first == "engine.log") {
			if d.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		if d.IsDir() {
			return nil
		}
		info, err := os.Stat(path)
		if err != nil {
			return err
		}
		if info.IsDir() && engine && first == "cache" && d.Type()&os.ModeSymlink != 0 {
			// uv cache directory links are disposable; real cache archives still copy.
			return nil
		}
		if !info.Mode().IsRegular() {
			return fmt.Errorf("không thể chuyển file đặc biệt: %s", path)
		}
		dest := filepath.Join(target, r)
		if st, err := os.Lstat(dest); err == nil {
			if !st.Mode().IsRegular() {
				return fmt.Errorf("đích không phải file thường: %s", dest)
			}
			a, e := fileDigest(path)
			if e != nil {
				return e
			}
			b, e := fileDigest(dest)
			if e != nil {
				return e
			}
			if a != b {
				return fmt.Errorf("file đích đã tồn tại với nội dung khác: %s", dest)
			}
		} else if !os.IsNotExist(err) {
			return err
		}
		files = append(files, storageCopyFile{path, dest, info.Size(), info.Mode()})
		return nil
	})
	return files, err
}
func copyStorageFile(file storageCopyFile) error {
	storageMove.Lock()
	storageMove.File = file.source
	storageMove.Unlock()
	if _, err := os.Stat(file.target); err == nil {
		storageMove.Lock()
		storageMove.Done += file.size
		storageMove.Unlock()
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(file.target), 0755); err != nil {
		return err
	}
	// Reject symlinked destination parents, which could redirect writes to old data.
	parent, err := filepath.EvalSymlinks(filepath.Dir(file.target))
	if err != nil {
		return err
	}
	if parent != filepath.Clean(filepath.Dir(file.target)) {
		return fmt.Errorf("thư mục đích chứa liên kết: %s", file.target)
	}
	src, err := os.Open(file.source)
	if err != nil {
		return err
	}
	defer src.Close()
	tmp, err := os.CreateTemp(parent, ".giongai-copy-*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	defer tmp.Close()
	hash := sha256.New()
	buf := make([]byte, 1024*1024)
	for {
		n, e := src.Read(buf)
		if n > 0 {
			if _, err = tmp.Write(buf[:n]); err != nil {
				return err
			}
			hash.Write(buf[:n])
			storageMove.Lock()
			storageMove.Done += int64(n)
			storageMove.Unlock()
		}
		if e == io.EOF {
			break
		}
		if e != nil {
			return e
		}
	}
	if err = tmp.Sync(); err != nil {
		return err
	}
	if err = tmp.Close(); err != nil {
		return err
	}
	sum, err := fileDigest(tmp.Name())
	if err != nil {
		return err
	}
	if string(sum[:]) != string(hash.Sum(nil)) {
		return fmt.Errorf("kiểm tra bản sao thất bại: %s", file.target)
	}
	if err = os.Chmod(tmp.Name(), file.mode.Perm()); err != nil {
		return err
	}
	return os.Rename(tmp.Name(), file.target)
}
func registerStorageMoveAPI(mux *http.ServeMux) {
	mux.HandleFunc("/api/settings/storage-move", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "GET" {
			storageMove.Lock()
			defer storageMove.Unlock()
			writeJSON(w, map[string]any{"running": storageMove.Running, "done": storageMove.Done, "total": storageMove.Total, "file": storageMove.File, "error": storageMove.Error, "complete": storageMove.Complete})
			return
		}
		if r.Method != "POST" {
			w.WriteHeader(405)
			return
		}
		var in storageSettings
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 16384)).Decode(&in); err != nil {
			http.Error(w, "Cài đặt không hợp lệ", 400)
			return
		}
		if os.Getenv("GIONGAI_ENGINE_DIR") != "" || os.Getenv("GIONGAI_OUT") != "" {
			http.Error(w, "Gỡ biến môi trường đường dẫn trước khi chuyển dữ liệu", 400)
			return
		}
		// Explicit paths prevent ambiguous defaults while relocating.
		for _, p := range []*string{&in.Engine, &in.Audio, &in.Voices} {
			v, err := validateStoragePath(*p)
			if err != nil || v == "" {
				http.Error(w, "Hãy chọn đủ ba thư mục tuyệt đối, có quyền ghi", 400)
				return
			}
			*p = v
		}
		instMu.Lock()
		busy := installing
		instMu.Unlock()
		jobsMu.Lock()
		for _, j := range jobs {
			if j.Status == "running" {
				busy = true
			}
		}
		jobsMu.Unlock()
		if busy {
			http.Error(w, "Chờ cài đặt và tạo audio hoàn tất trước khi chuyển", 409)
			return
		}
		storageMove.Lock()
		if storageMove.Running {
			storageMove.Unlock()
			http.Error(w, "Đang chuyển dữ liệu", 409)
			return
		}
		storageMove.Running = true
		storageMove.Done = 0
		storageMove.Total = 0
		storageMove.Error = ""
		storageMove.Complete = false
		storageMove.Unlock()
		go func() {
			var err error
			defer func() {
				storageMove.Lock()
				storageMove.Running = false
				if err != nil {
					storageMove.Error = err.Error()
				} else {
					storageMove.Complete = true
				}
				storageMove.Unlock()
			}()
			stopAllEngines()
			var files []storageCopyFile
			for _, pair := range []struct {
				src, dst string
				engine   bool
			}{{engineDir(), in.Engine, true}, {outDir, in.Audio, false}, {voiceStorageDir(), in.Voices, false}} {
				var part []storageCopyFile
				part, err = planStorageCopy(pair.src, pair.dst, pair.engine)
				if err != nil {
					return
				}
				files = append(files, part...)
			}
			storageMove.Lock()
			for _, f := range files {
				storageMove.Total += f.size
			}
			storageMove.Unlock()
			for _, f := range files {
				if err = copyStorageFile(f); err != nil {
					return
				}
			}
			path := storageConfigPath()
			if err = os.MkdirAll(filepath.Dir(path), 0755); err != nil {
				return
			}
			var data []byte
			data, err = json.MarshalIndent(in, "", "  ")
			if err != nil {
				return
			}
			err = os.WriteFile(path, data, 0600)
		}()
		writeJSON(w, map[string]bool{"started": true})
	})
}

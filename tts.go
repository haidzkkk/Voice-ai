package main

// Client tối giản cho dịch vụ đọc văn bản của Microsoft Edge (Read Aloud),
// chỉ dùng thư viện chuẩn của Go (tự cài đặt WebSocket).

import (
	"bufio"
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"
	"unicode"
	"unicode/utf8"
)

const (
	baseURL            = "speech.platform.bing.com/consumer/speech/synthesize/readaloud"
	trustedClientToken = "6A5AA1D4EAFF4E9FB37E23D68491D6F4"
	chromiumFull       = "143.0.3650.75"
	chromiumMajor      = "143"
	secMSGECVersion    = "1-" + chromiumFull
	winEpoch           = 11644473600
	maxChunkBytes      = 3000 // byte của văn bản (đã escape XML) cho mỗi lần gửi
	userAgent          = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/" + chromiumMajor + ".0.0.0 Safari/537.36 Edg/" + chromiumMajor + ".0.0.0"
)

var (
	skewMu    sync.Mutex
	clockSkew float64 // giây, bù lệch đồng hồ máy so với máy chủ
)

func wssURL() string {
	if u := os.Getenv("GIONGAI_WSS_URL"); u != "" { // chỉ dùng để kiểm thử
		return u
	}
	return "wss://" + baseURL + "/edge/v1?TrustedClientToken=" + trustedClientToken
}

func voicesURL() string {
	if u := os.Getenv("GIONGAI_VOICES_URL"); u != "" {
		return u
	}
	return "https://" + baseURL + "/voices/list?trustedclienttoken=" + trustedClientToken
}

func secMSGEC() string {
	skewMu.Lock()
	s := clockSkew
	skewMu.Unlock()
	t := int64(float64(time.Now().Unix())+s) + winEpoch
	t -= t % 300
	ticks := fmt.Sprintf("%d", t*10000000)
	h := sha256.Sum256([]byte(ticks + trustedClientToken))
	return strings.ToUpper(hex.EncodeToString(h[:]))
}

func adjustSkew(dateHeader string) bool {
	if dateHeader == "" {
		return false
	}
	t, err := http.ParseTime(dateHeader)
	if err != nil {
		return false
	}
	skewMu.Lock()
	clockSkew = float64(t.Unix() - time.Now().Unix())
	skewMu.Unlock()
	return true
}

func uuidHex() string {
	b := make([]byte, 16)
	rand.Read(b)
	return hex.EncodeToString(b)
}

func jsDate() string {
	return time.Now().UTC().Format("Mon Jan 02 2006 15:04:05") + " GMT+0000 (Coordinated Universal Time)"
}

// ---------------------- Danh sách giọng ----------------------

type Voice struct {
	ShortName    string `json:"ShortName"`
	Gender       string `json:"Gender"`
	Locale       string `json:"Locale"`
	FriendlyName string `json:"FriendlyName,omitempty"`
}

var fallbackVoices = []Voice{
	{"vi-VN-HoaiMyNeural", "Female", "vi-VN", ""},
	{"vi-VN-NamMinhNeural", "Male", "vi-VN", ""},
	{"en-US-AvaMultilingualNeural", "Female", "en-US", ""},
	{"en-US-AndrewMultilingualNeural", "Male", "en-US", ""},
	{"en-US-EmmaMultilingualNeural", "Female", "en-US", ""},
	{"en-US-BrianMultilingualNeural", "Male", "en-US", ""},
	{"en-US-JennyNeural", "Female", "en-US", ""},
	{"en-US-GuyNeural", "Male", "en-US", ""},
	{"en-GB-SoniaNeural", "Female", "en-GB", ""},
	{"en-GB-RyanNeural", "Male", "en-GB", ""},
	{"zh-CN-XiaoxiaoNeural", "Female", "zh-CN", ""},
	{"zh-CN-YunxiNeural", "Male", "zh-CN", ""},
	{"ja-JP-NanamiNeural", "Female", "ja-JP", ""},
	{"ja-JP-KeitaNeural", "Male", "ja-JP", ""},
	{"ko-KR-SunHiNeural", "Female", "ko-KR", ""},
	{"ko-KR-InJoonNeural", "Male", "ko-KR", ""},
	{"fr-FR-DeniseNeural", "Female", "fr-FR", ""},
	{"fr-FR-HenriNeural", "Male", "fr-FR", ""},
	{"th-TH-PremwadeeNeural", "Female", "th-TH", ""},
	{"th-TH-NiwatNeural", "Male", "th-TH", ""},
}

func fetchVoices(ctx context.Context) ([]Voice, error) {
	for attempt := 0; attempt < 2; attempt++ {
		u := voicesURL() + "&Sec-MS-GEC=" + secMSGEC() + "&Sec-MS-GEC-Version=" + secMSGECVersion
		req, _ := http.NewRequestWithContext(ctx, "GET", u, nil)
		req.Header.Set("User-Agent", userAgent)
		req.Header.Set("Accept", "*/*")
		req.Header.Set("Accept-Language", "en-US,en;q=0.9")
		req.Header.Set("Sec-CH-UA", `" Not;A Brand";v="99", "Microsoft Edge";v="`+chromiumMajor+`", "Chromium";v="`+chromiumMajor+`"`)
		req.Header.Set("Sec-CH-UA-Mobile", "?0")
		req.Header.Set("Sec-Fetch-Site", "none")
		req.Header.Set("Sec-Fetch-Mode", "cors")
		req.Header.Set("Sec-Fetch-Dest", "empty")
		resp, err := (&http.Client{Timeout: 20 * time.Second}).Do(req)
		if err != nil {
			return nil, err
		}
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		if resp.StatusCode == 403 && attempt == 0 && adjustSkew(resp.Header.Get("Date")) {
			continue
		}
		if resp.StatusCode != 200 {
			return nil, fmt.Errorf("máy chủ trả về mã %d", resp.StatusCode)
		}
		var vs []Voice
		if err := json.Unmarshal(body, &vs); err != nil {
			return nil, err
		}
		if len(vs) == 0 {
			return nil, errors.New("danh sách giọng trống")
		}
		return vs, nil
	}
	return nil, errors.New("không lấy được danh sách giọng")
}

// ---------------------- Chuẩn bị văn bản ----------------------

func cleanText(s string) string {
	return strings.Map(func(r rune) rune {
		if r == '\n' || r == '\t' {
			return ' '
		}
		if unicode.IsControl(r) || r == '�' {
			return ' '
		}
		return r
	}, s)
}

func xmlEscape(s string) string {
	var b strings.Builder
	for _, r := range s {
		switch r {
		case '&':
			b.WriteString("&amp;")
		case '<':
			b.WriteString("&lt;")
		case '>':
			b.WriteString("&gt;")
		case '"':
			b.WriteString("&quot;")
		case '\'':
			b.WriteString("&apos;")
		default:
			b.WriteRune(r)
		}
	}
	return b.String()
}

// splitText chia văn bản thành các đoạn sao cho mỗi đoạn sau khi escape
// không vượt quá limit byte; ưu tiên cắt ở cuối câu, rồi đến khoảng trắng.
func splitText(text string, limit int) []string {
	text = strings.TrimSpace(text)
	var out []string
	for text != "" {
		if len(xmlEscape(text)) <= limit {
			out = append(out, text)
			break
		}
		// tìm vị trí rune lớn nhất mà phần escape vừa giới hạn
		size, cut := 0, 0
		for i, r := range text {
			add := len(xmlEscape(string(r)))
			if size+add > limit {
				break
			}
			size += add
			cut = i + utf8.RuneLen(r)
		}
		if cut == 0 { // không thể xảy ra với limit hợp lý
			_, w := utf8.DecodeRuneInString(text)
			cut = w
		}
		part := text[:cut]
		best := -1
		// Ưu tiên: cuối câu > dấu chấm phẩy/phẩy > khoảng trắng
		for _, group := range [][]string{{". ", "! ", "? ", "\u3002", "\uff01", "\uff1f", "\u2026 "}, {"; ", ", ", "\uff0c", "\u3001"}, {" "}} {
			for _, sep := range group {
				if i := strings.LastIndex(part, sep); i > len(part)/3 && i+len(sep) > best {
					best = i + len(sep)
				}
			}
			if best > 0 {
				break
			}
		}
		if best > 0 {
			cut = best
		}
		if p := strings.TrimSpace(text[:cut]); p != "" {
			out = append(out, p)
		}
		text = strings.TrimSpace(text[cut:])
	}
	return out
}

// ---------------------- WebSocket tối giản ----------------------

type wsConn struct {
	c  net.Conn
	br *bufio.Reader
}

func dialTCP(ctx context.Context, u *url.URL) (net.Conn, error) {
	d := &net.Dialer{Timeout: 20 * time.Second}
	host := u.Host
	if u.Port() == "" {
		if u.Scheme == "wss" {
			host += ":443"
		} else {
			host += ":80"
		}
	}
	// Hỗ trợ proxy HTTP(S) từ biến môi trường (nếu có)
	if u.Scheme == "wss" {
		probe := &http.Request{URL: &url.URL{Scheme: "https", Host: u.Host}}
		if pu, _ := http.ProxyFromEnvironment(probe); pu != nil {
			c, err := d.DialContext(ctx, "tcp", pu.Host)
			if err != nil {
				return nil, err
			}
			fmt.Fprintf(c, "CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n", host, host)
			br := bufio.NewReader(c)
			resp, err := http.ReadResponse(br, &http.Request{Method: "CONNECT"})
			if err != nil {
				c.Close()
				return nil, err
			}
			if resp.StatusCode != 200 {
				c.Close()
				return nil, fmt.Errorf("proxy từ chối kết nối (%d)", resp.StatusCode)
			}
			return c, nil
		}
	}
	return d.DialContext(ctx, "tcp", host)
}

type handshakeError struct {
	status int
	date   string
}

func (e *handshakeError) Error() string {
	return fmt.Sprintf("máy chủ từ chối kết nối (mã %d)", e.status)
}

func wsDial(ctx context.Context, rawURL string, hdr http.Header) (*wsConn, error) {
	u, err := url.Parse(rawURL)
	if err != nil {
		return nil, err
	}
	c, err := dialTCP(ctx, u)
	if err != nil {
		return nil, err
	}
	if u.Scheme == "wss" {
		tc := tls.Client(c, &tls.Config{ServerName: u.Hostname(), NextProtos: []string{"http/1.1"}})
		tc.SetDeadline(time.Now().Add(20 * time.Second))
		if err := tc.HandshakeContext(ctx); err != nil {
			c.Close()
			return nil, err
		}
		c = tc
	}
	keyB := make([]byte, 16)
	rand.Read(keyB)
	key := base64.StdEncoding.EncodeToString(keyB)
	var req bytes.Buffer
	fmt.Fprintf(&req, "GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n", u.RequestURI(), u.Host, key)
	for k, vs := range hdr {
		for _, v := range vs {
			fmt.Fprintf(&req, "%s: %s\r\n", k, v)
		}
	}
	req.WriteString("\r\n")
	c.SetDeadline(time.Now().Add(20 * time.Second))
	if _, err := c.Write(req.Bytes()); err != nil {
		c.Close()
		return nil, err
	}
	br := bufio.NewReader(c)
	resp, err := http.ReadResponse(br, &http.Request{Method: "GET"})
	if err != nil {
		c.Close()
		return nil, err
	}
	if resp.StatusCode != 101 {
		c.Close()
		return nil, &handshakeError{resp.StatusCode, resp.Header.Get("Date")}
	}
	return &wsConn{c: c, br: br}, nil
}

func (w *wsConn) writeFrame(op byte, payload []byte) error {
	var h bytes.Buffer
	h.WriteByte(0x80 | op)
	n := len(payload)
	switch {
	case n < 126:
		h.WriteByte(0x80 | byte(n))
	case n < 65536:
		h.WriteByte(0x80 | 126)
		binary.Write(&h, binary.BigEndian, uint16(n))
	default:
		h.WriteByte(0x80 | 127)
		binary.Write(&h, binary.BigEndian, uint64(n))
	}
	mask := make([]byte, 4)
	rand.Read(mask)
	h.Write(mask)
	m := make([]byte, n)
	for i := range payload {
		m[i] = payload[i] ^ mask[i%4]
	}
	h.Write(m)
	w.c.SetWriteDeadline(time.Now().Add(20 * time.Second))
	_, err := w.c.Write(h.Bytes())
	return err
}

func (w *wsConn) WriteText(s string) error { return w.writeFrame(1, []byte(s)) }

// ReadMessage trả về opcode (1 = text, 2 = binary) và nội dung đầy đủ.
func (w *wsConn) ReadMessage() (byte, []byte, error) {
	var msg []byte
	var msgOp byte
	for {
		w.c.SetReadDeadline(time.Now().Add(30 * time.Second))
		var hd [2]byte
		if _, err := io.ReadFull(w.br, hd[:]); err != nil {
			return 0, nil, err
		}
		fin := hd[0]&0x80 != 0
		op := hd[0] & 0x0f
		masked := hd[1]&0x80 != 0
		n := uint64(hd[1] & 0x7f)
		if n == 126 {
			var b [2]byte
			if _, err := io.ReadFull(w.br, b[:]); err != nil {
				return 0, nil, err
			}
			n = uint64(binary.BigEndian.Uint16(b[:]))
		} else if n == 127 {
			var b [8]byte
			if _, err := io.ReadFull(w.br, b[:]); err != nil {
				return 0, nil, err
			}
			n = binary.BigEndian.Uint64(b[:])
		}
		if n > 64<<20 {
			return 0, nil, errors.New("khung dữ liệu quá lớn")
		}
		var mk [4]byte
		if masked {
			if _, err := io.ReadFull(w.br, mk[:]); err != nil {
				return 0, nil, err
			}
		}
		p := make([]byte, n)
		if _, err := io.ReadFull(w.br, p); err != nil {
			return 0, nil, err
		}
		if masked {
			for i := range p {
				p[i] ^= mk[i%4]
			}
		}
		switch op {
		case 8:
			return 8, p, io.EOF
		case 9:
			w.writeFrame(10, p)
			continue
		case 10:
			continue
		case 0:
			msg = append(msg, p...)
		default:
			msgOp = op
			msg = p
		}
		if fin {
			return msgOp, msg, nil
		}
	}
}

func (w *wsConn) Close() {
	w.writeFrame(8, []byte{0x03, 0xe8})
	w.c.Close()
}

// ---------------------- Tổng hợp giọng nói ----------------------

type TTSOptions struct {
	Voice  string
	Rate   int // %
	Pitch  int // Hz
	Volume int // %
}

func signed(n int, unit string) string {
	if n >= 0 {
		return fmt.Sprintf("+%d%s", n, unit)
	}
	return fmt.Sprintf("%d%s", n, unit)
}

func synthChunk(ctx context.Context, text string, o TTSOptions) ([]byte, error) {
	hdr := http.Header{}
	hdr.Set("Pragma", "no-cache")
	hdr.Set("Cache-Control", "no-cache")
	hdr.Set("Origin", "chrome-extension://jdiccldimpdaibmpdkjnbmckianbfold")
	hdr.Set("User-Agent", userAgent)
	hdr.Set("Accept-Language", "en-US,en;q=0.9")

	var ws *wsConn
	var err error
	for attempt := 0; attempt < 2; attempt++ {
		u := wssURL() + "&ConnectionId=" + uuidHex() + "&Sec-MS-GEC=" + secMSGEC() + "&Sec-MS-GEC-Version=" + secMSGECVersion
		ws, err = wsDial(ctx, u, hdr)
		var he *handshakeError
		if err != nil && errors.As(err, &he) && he.status == 403 && attempt == 0 && adjustSkew(he.date) {
			continue
		}
		break
	}
	if err != nil {
		return nil, err
	}
	defer ws.Close()
	stop := context.AfterFunc(ctx, func() { ws.c.Close() })
	defer stop()

	cfg := "X-Timestamp:" + jsDate() + "\r\nContent-Type:application/json; charset=utf-8\r\nPath:speech.config\r\n\r\n" +
		`{"context":{"synthesis":{"audio":{"metadataoptions":{"sentenceBoundaryEnabled":"false","wordBoundaryEnabled":"false"},"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}` + "\r\n"
	if err := ws.WriteText(cfg); err != nil {
		return nil, err
	}
	ssml := "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='en-US'>" +
		"<voice name='" + xmlEscape(o.Voice) + "'>" +
		"<prosody pitch='" + signed(o.Pitch, "Hz") + "' rate='" + signed(o.Rate, "%") + "' volume='" + signed(o.Volume, "%") + "'>" +
		xmlEscape(text) + "</prosody></voice></speak>"
	msg := "X-RequestId:" + uuidHex() + "\r\nContent-Type:application/ssml+xml\r\nX-Timestamp:" + jsDate() + "Z\r\nPath:ssml\r\n\r\n" + ssml
	if err := ws.WriteText(msg); err != nil {
		return nil, err
	}

	var audio bytes.Buffer
	for {
		op, data, err := ws.ReadMessage()
		if err != nil {
			if ctx.Err() != nil {
				return nil, ctx.Err()
			}
			if op == 8 {
				return nil, errors.New("máy chủ đóng kết nối giữa chừng")
			}
			return nil, err
		}
		switch op {
		case 1:
			if headerPath(string(data)) == "turn.end" {
				if audio.Len() == 0 {
					return nil, errors.New("không nhận được âm thanh (kiểm tra lại tên giọng)")
				}
				return audio.Bytes(), nil
			}
		case 2:
			if len(data) < 2 {
				continue
			}
			hl := int(binary.BigEndian.Uint16(data[:2]))
			if 2+hl > len(data) {
				continue
			}
			if headerPath(string(data[2:2+hl])) == "audio" {
				audio.Write(data[2+hl:])
			}
		}
	}
}

func headerPath(h string) string {
	if i := strings.Index(h, "\r\n\r\n"); i >= 0 {
		h = h[:i]
	}
	for _, line := range strings.Split(h, "\r\n") {
		if k, v, ok := strings.Cut(line, ":"); ok && strings.EqualFold(k, "Path") {
			return strings.TrimSpace(v)
		}
	}
	return ""
}

// Synthesize đọc toàn bộ văn bản, trả về file MP3 (ghép các đoạn).
func Synthesize(ctx context.Context, text string, o TTSOptions, progress func(done, total int)) ([]byte, error) {
	chunks := splitText(cleanText(text), maxChunkBytes)
	if len(chunks) == 0 {
		return nil, errors.New("văn bản trống")
	}
	var out bytes.Buffer
	for i, c := range chunks {
		var b []byte
		var err error
		for try := 0; try < 3; try++ {
			b, err = synthChunk(ctx, c, o)
			if err == nil || ctx.Err() != nil {
				break
			}
			time.Sleep(time.Duration(try+1) * time.Second)
		}
		if err != nil {
			return nil, fmt.Errorf("đoạn %d/%d: %w", i+1, len(chunks), err)
		}
		out.Write(b)
		if progress != nil {
			progress(i+1, len(chunks))
		}
	}
	return out.Bytes(), nil
}
